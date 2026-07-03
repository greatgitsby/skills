# MDMA — mici debug and monitoring adapter (hardware debug board)

The MDMA is a hardware debug adapter for low-level **comma four** (aka mici) development.
It only works when a comma four is **physically wired to an MDMA adapter** — else the
script prints `MDMA not found.`. It connects to the SOC over USB + a UART and lets you:

- power the SOC on/off
- force the SOC into **QDL mode** for un-brickable flashing
- read/write the SOC's UART (serial console)
- run arbitrary bash/python on the device over serial and capture the output
- profile boot time with per-line timestamps

The driver is `scripts/mdma.py`, a self-contained `uv` script — the `pyusb` dependency
is declared inline, so `uv` fetches it automatically; no venv setup.

## Prerequisites

- An MDMA connected to the host. It exposes a Microchip USB hub + a serial-by-id device
  at `/dev/serial/by-id/usb-Microchip_Tech_USB2_Controller_Hub-if01`; if that path is
  absent the script prints `MDMA not found.` and exits (code 1, or 0 with `--missing-ok`).
- `uv` installed (shebang is `#!/usr/bin/env -S uv run --script`).
- `screen` installed (only for the `serial` command).
- USB control transfers need permissions — on `USBError: Access denied`, run under `sudo`
  or install udev rules.

## Commands

Run from the skill's directory (or pass the full path to `scripts/mdma.py`).

State-changing commands narrate progress as timestamped `[mdma +N.Ns] …` lines on
**stderr** (power cycle issued / boot output detected / login seen / handshake ok / every
retry + fallback with its reason), so a failure localizes straight from the log.
**stdout** carries only device output (`bash` / `flash --verify`). `boot` (and the boot
stage of `flash`) additionally captures the **entire boot console** to a timestamped file
under `/tmp/mdma/`, path printed on stderr — read it post-hoc to debug a bad boot without
re-running.

| Command | What it does |
| --- | --- |
| `flash [--verify CMD] -- <host cmd...>` | **Whole kernel-iteration loop in one command**: force QDL → run the host flash command → `boot` to a confirmed live shell → optionally run `--verify CMD` over serial. Prints per-stage timings (`qdl=… flash=… boot=… total=…`). One arg runs via the shell; multiple args exec directly. |
| `boot` | Power-cycle into a normal boot and **block until a live shell, ready for `bash`**. Verifies the power cut took (re-cycles if serial stays silent — a known MDMA flake) and logs in the instant the getty prompt appears (~26 s typical). |
| `qdl` (alias `reboot-qdl`) | Power-cycle into **QDL mode** for flashing — the un-brick path. Just run it; it's the reliable way in. |
| `reboot` | Raw power-cycle into a normal boot. Returns immediately — does **not** wait or verify (use `boot` for that). |
| `off` | Cut power to the SOC (VIN + aux off). |
| `serial` | Open the MSM UART console with `screen` at 115200 baud. |
| `bash <cmd...>` / `bash -` | Run a bash script on the device **over serial**, print its stdout/stderr, exit with the script's code. One-liner inline, or `-` to read a multi-line script from stdin (heredocs, embedded `python3`, etc. work). Output is gzip-compressed on the device for speed and is byte-exact/binary-safe. Logs in with `comma`/`comma` at a `login:` prompt. `--wait SECONDS` waits for a *booting* device to reach a shell first (default 0 = fail fast); `--timeout SECONDS` bounds waiting for output (default 30). |
| `profile-boot` | Reboot and stream the console with `[seconds.ms]` timestamps until the login/shell prompt. |

```bash
# multi-line script from stdin ('-'): heredocs + embedded python3 work verbatim —
# the body is base64'd over the wire, so no quoting/line-timing hazards. preferred
# for anything non-trivial (no temp files):
scripts/mdma.py bash - <<'EOF'
for svc in boardd pandad; do
  echo "== $svc =="
  pgrep -a "$svc" || echo "(not running)"
done
EOF

# after a kernel flash: `boot` blocks until a live shell is confirmed, so the next
# `bash` won't race the boot — the reliable replacement for `reboot` + `bash --wait`:
scripts/mdma.py boot
scripts/mdma.py bash 'uname -r'

# --wait folds the boot-wait into one command when you didn't reboot via `boot`:
scripts/mdma.py bash --wait 120 'uname -r'
```

### `--missing-ok`

Makes the script exit **0** instead of 1 when no MDMA is connected — use it in automation
that should no-op when the adapter is absent (this is how agnos-builder's flash scripts
invoke it, e.g. `scripts/mdma.py --missing-ok reboot-qdl`). With no subcommand, the script
prints help and exits 0.

## How it works (for debugging)

- **Power / VIN**: toggled by writing a GPIO register over a USB control transfer to the
  Microchip "HFC" hub (`0424:704c`). `VIN_EN` is GPIO bit 92.
- **QDL forcing**: on comma four, powering the *aux* USB ports up *before* VIN forces the
  SOC into QDL on boot. `reboot-qdl` does exactly this ordering; `reboot` powers VIN first.
- **Aux USB power**: toggled via `SET_FEATURE`/`CLEAR_FEATURE` (PORT_POWER) on the
  `0424:7002` and `0424:4002` hubs.
- **`profile-boot`**: opens the serial device raw at 115200 8N1, drains stale bytes,
  reboots, prints each line prefixed with elapsed seconds, stops at a `login:`/`#`/`$`
  prompt (`PROMPT_RE`).
- **`bash`**: drives the console to a shell (logging in `comma`/`comma` if at `login:`),
  then sends **one line** that runs the script and frames its gzipped+base64'd output
  between a **per-call nonce marker** carrying the blob's byte length and the exit code.
  - **Outbound**: the script is base64'd on the host, so only one line crosses the
    line-oriented link — multi-line scripts, heredocs, and `python3` survive with no
    quoting/line-timing hazards.
  - **Inbound**: stdout+stderr is **gzipped then base64'd on the device** before crossing
    back. 115200 baud is only ~7.5 KB/s effective and *is* the bottleneck, so compressing
    device-side is the speed win (~3x on dmesg-like text, up to ~7x). The host strips
    cruft, decodes, gunzips, so captured bytes are **exact and binary-safe**.
  - **Nonce markers + length, not a fixed sentinel**: markers are built per-call from a
    random nonce and sent as `printf` *format + arg* (`printf 'MDMABEG%s…' <nonce>`), so
    the concatenated token only ever appears in real output — never in the echoed command
    line. The begin marker carries the blob's exact byte count; the host slices between
    markers and **verifies the length** before decoding. The old fixed literal
    (`__MDMA_BEG_837__`) appeared verbatim in the echoed command, so a clipped/drained
    echo could slice the *input* base64 into the output blob — the dominant base64-
    corruption failure. Nonce + length framing eliminates it and self-detects truncation.
  - **Exit code**: captured **inside** the command substitution (`echo $? > /tmp/.mdma_rc_<nonce>`);
    reading `${PIPESTATUS[0]}` *after* `out=$(…)` would reflect the assignment's own
    pipeline (always 0), not the script's. `rc` rides back in the end marker and becomes
    `bash`'s process exit code. The tiny rc temp file is removed each call.
  - **Reliability**: prompt detection nudges the console with newlines for up to ~12 s (an
    idle getty / emergency shell can sit silent until poked), instead of firing one newline
    and giving up after 3 s — that short window was the dominant **"no response from serial
    console"** false negative. A transient round-trip failure (garbled/truncated frame,
    length mismatch, decode error) is **retried once** with a fresh login; a genuinely dead
    console fails cleanly. *During early kernel bring-up the device-side console may stop
    servicing the UART entirely — no host retry fixes that; reboot to recover.*
  - **`--wait` (boot-wait)**: a *booting* device can't be detected by a prompt regex alone
    — the UART **replays buffered serial input** as it boots (this tool's own prior command
    frames, stale `root@none:~#` prompts), so a naive match fires on residue and the command
    gets shredded by boot spew. `--wait` instead waits for the console to go **quiet** (boot
    output stopped for `QUIESCE_IDLE` s), then proves a live shell with a **random-token
    `echo` handshake** replayed residue can't fake; loops until it passes or the deadline.
    This is what makes `reboot` + `bash --wait` reliable across the device's variable
    (≈5–45 s) boot.
- **`boot`**: a *verified, event-driven* power-cycle-to-live-shell. Opens the console,
  cycles VIN, then: (1) **verifies the cut took** — a real cycle spews boot output within
  `CYCLE_SPEW_TIMEOUT` (8 s); a silent line means the VIN toggle no-op'd (known MDMA flake)
  and the cycle re-issues (up to 3×); (2) **fast path** — watches boot spew for the getty
  `comma-* login:` prompt (`PROMPT_RE`, idle-reset window `BOOT_SPEW_IDLE` 30 s), logs in
  the moment it appears, proves the shell with the random-token **liveness handshake**.
  ~26 s power-cut→ready typical. (3) Anything off the happy path (emergency shell — never
  prints `login:` — garbled login, residue match) falls back to `wait_until_ready()`: the
  quiesce → login → handshake waiter `bash --wait` also uses, budget `BOOT_READY_TIMEOUT`
  (90 s). Readiness = a confirmed live shell, so the **emergency shell counts as ready**.
  - Runs over the **serial console**, not SSH — works with no network, but the device must
    be booted to a login/shell prompt (not QDL or mid-boot) and needs `gzip`/`base64`/`bash`
    on PATH (AGNOS has all three).
  - **Emergency / maintenance shell**: on a drop to the systemd emergency shell (e.g. a
    failed mount during development) the console lands directly on a `root@…#` shell with
    **no `login:`** and turns on **bracketed-paste mode**, wrapping its prompt and every
    echoed line in `\x1b[?2004h`/`l` escapes. `bash` is resilient: prompt detection matches
    an ANSI-stripped view (`ANSI_RE`), and `_extract` strips those escapes before decoding
    so their base64-legal interior bytes (`2004`, `h`, `l`) can't corrupt the blob.

The `serial` command `execvp`s into `screen` and replaces the process — interactive-only,
don't call it from non-interactive automation (it won't return).

## Typical flashing flow

QDL flashing is the un-brickable recovery/flash path. General sequence (driven by
agnos-builder's `flash_*.sh`):

1. `scripts/mdma.py qdl` — drop the SOC into QDL.
2. Run the QDL flasher (`qdl` / the agnos-builder script) to write images.
3. `scripts/mdma.py boot` — boot the flashed system **and wait until it's at a live
   shell**, so the next step (verify the kernel, run anything) lands on a ready device.
   (Use bare `reboot` only if you don't need to wait.)

Or all three plus verify in one command:
`scripts/mdma.py flash --verify 'uname -r' -- <kernel-flash-command>`.

`boot` is the reliability win: a bare `reboot` returns the instant it toggles VIN, so any
command run next races the ≈5–45 s (longer after a flash) boot and gets shredded by boot
spew. `boot` waits it out, **verifies the power cut happened** (re-cycles on the no-op-VIN
flake), and **confirms a live shell** before returning.

### Required hardware setup for QDL — the aux cable

**QDL will never enumerate unless the SOC's aux USB is physically wired to the host.** On
the MDMA DESK board this is a separate **aux USB-C connector** (the one *not* labeled
`UFP`; `UFP` is the host/control+serial uplink). Loop a USB-C cable:

> **dev board aux USB-C  →  the comma four's USB-C port**

The board bridges that aux port to the host through its **USB3 hub `0424:7002` (Bus 2)**;
the `aux("on")` step in `reboot`/`qdl` powers that hub's ports. Leave this cable plugged —
once set up, the entire flash loop runs hands-free.

**⚠️ `lsusb -d 3801:9008` is NOT a QDL indicator.** On the MDMA dev board the `3801:9008`
(Qualcomm QUSB_BULK) device shows up in `lsusb` even with a healthy, normally-booted device
— a board artifact — so it proves nothing about QDL mode and you can't poll it to verify or
refute QDL entry. **To get to QDL reliably, just run `scripts/mdma.py qdl`** — the aux-first
power cycle is the mechanism; don't build enumeration checks around it.

Diagnosing a missing/wrong aux connection (control-transfer probes against the hubs):
- The QDL path is the **`0424:7002` hub on Bus 2**. A cable in the wrong port enumerates as
  `3801:ddcc panda` on the **`0424:4002` hub (Bus 1)** — the panda USB2 path, never triggers
  QDL.
- 7002 is a **SuperSpeed hub** (bcdUSB 0x0320): its port-power bit is `wPortStatus`
  **bit 9 (0x200)**, *not* the USB2 bit 8. Reading bit 8 falsely shows "unpowered." With the
  aux cable absent the ports read **powered, `connected=False`** — that empty-but-powered
  state is the tell that the cable isn't plugged.

### Hands-free flash loop (aux cable stays plugged)

```bash
scripts/mdma.py flash --verify 'uname -r' -- <kernel-flash-command>
# ── flash loop qdl=1.2s flash=…s boot=26.5s verify=0.1s total=…s
```

Equivalent manual steps: `qdl` → `<kernel-flash-command>` → `boot` (boots the flashed
kernel and blocks until a live shell) → `bash 'uname -r'` (verify).

Gotchas observed:
- **A power cycle doesn't always cut VIN.** The raw toggle sometimes no-ops — the device
  never cycles and stays on its current boot. `boot` detects and retries this automatically
  (a real cycle spews boot output within seconds); after raw `reboot`/`qdl`, confirm by
  checking **uptime reset** (`scripts/mdma.py bash 'cut -d" " -f1 /proc/uptime'` should drop
  to double digits or less). A re-enumeration of the MDMA hubs (fresh `lsusb` device numbers)
  tends to restore a working power toggle.
- After a firehose reset (post-flash) the device often re-appears in **QDL**;
  `scripts/mdma.py boot` (VIN-first) is what boots it out into the flashed system.
- **Don't poll `lsusb -d 3801:9008` to verify QDL** — always present on the dev board (see
  above). Just run `qdl` and proceed; the flasher itself is the real test of QDL.

## Maintaining this reference

`scripts/mdma.py` started as a **copy** of `agnos-builder/scripts/mdma.py` but has since
diverged: the `bash`, `boot`, `qdl`, `flash`, and `off` commands are **skill-only** and
don't exist upstream. When re-syncing after upstream changes, don't blindly overwrite —
merge upstream in and keep the skill-only machinery: the `_Transient` exception; the `Mdma`
methods `open_serial`, `_drain`, `_read_until` (with its `nudge=` arg), `_wait_for_boot`,
`_handshake`, `wait_until_ready` (and `BOOT_READY_TIMEOUT`), `boot` (verified cycle +
fast-path login; `CYCLE_SPEW_TIMEOUT`/`BOOT_SPEW_IDLE`), `qdl`, `_ensure_login` (and
`ANY_PROMPT_TIMEOUT`/`QUIESCE_IDLE`), `exec`, `_exec_once`, `_nonce`, `_extract`; the
module-level `flash_flow` and `bash_script` functions; the `zlib`/`base64`/`subprocess`
imports; and the subparser wiring incl. the `reboot-qdl` → `qdl` alias map. Then re-read
this reference against the new command table / behavior and update as needed.
