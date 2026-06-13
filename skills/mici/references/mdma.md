# MDMA — mici debug and monitoring adapter (hardware debug board)

The MDMA is a hardware debug adapter for low-level **comma four** (aka mici) development. It only works when a comma four is **physically wired to an MDMA adapter** — if no MDMA is connected, nothing here applies (the script prints `MDMA not found.`). It connects to the SOC over USB and a UART, and lets you:

- power the SOC on and off
- force the SOC into **QDL mode** for un-brickable flashing
- read/write the SOC's UART (serial console)
- run arbitrary bash/python scripts on the device over serial and capture the output
- profile boot time with per-line timestamps

The driver is `scripts/mdma.py` (bundled in this skill). It is a self-contained `uv` script — the `pyusb` dependency is declared inline, so `uv` fetches it automatically; no venv setup needed.

## Prerequisites

- An MDMA physically connected to the host (it exposes a Microchip USB hub + a serial-by-id device at `/dev/serial/by-id/usb-Microchip_Tech_USB2_Controller_Hub-if01`).
- `uv` installed (the script shebang is `#!/usr/bin/env -S uv run --script`).
- `screen` installed (only for the `serial` command).
- USB control transfers require permissions — if you hit `USBError: Access denied`, run under `sudo` or install the appropriate udev rules.

Check whether an MDMA is present before doing anything:

```bash
ls /dev/serial/by-id/usb-Microchip_Tech_USB2_Controller_Hub-if01
```

If that path doesn't exist, the script prints `MDMA not found.` and exits (code 1, or code 0 with `--missing-ok`).

## Commands

Run from the skill's directory (or pass the full path to `scripts/mdma.py`):

| Command | What it does |
| --- | --- |
| `reboot` | Power-cycle the SOC into a **normal** boot. |
| `reboot-qdl` | Power-cycle the SOC into **QDL mode** for flashing — the un-brick path. |
| `serial` | Open the MSM UART console with `screen` at 115200 baud. |
| `bash <cmd...>` / `bash -` | Run a bash script on the device **over serial** and print its stdout/stderr; exits with the script's exit code. Pass a one-liner inline, or `-` to read a multi-line script from stdin (heredocs, embedded `python3`, etc. all work). Output is gzip-compressed on the device for speed and is byte-exact/binary-safe. Logs in with the default `comma`/`comma` credentials if the console is at a `login:` prompt. |
| `profile-boot` | Reboot into normal boot and stream the serial console with `[seconds.ms]` timestamps until the login/shell prompt appears. |

```bash
# normal reboot
scripts/mdma.py reboot

# force QDL mode (e.g. before flashing)
scripts/mdma.py reboot-qdl

# open the serial console (exit screen with Ctrl-A then \)
scripts/mdma.py serial

# run a one-liner on the device over serial and capture the output.
# quote the whole command so the host shell doesn't expand it first;
# pipes, redirects, and ; all run on the device:
scripts/mdma.py bash 'uname -a; df -h /data'
scripts/mdma.py bash 'dmesg | tail -n 20'

# run a multi-line script from stdin (use '-'). heredocs and embedded
# python3 work verbatim — the body is base64'd over the wire, so no quoting
# or line-by-line hazards. this is the preferred way to run anything
# non-trivial (no temp files needed):
scripts/mdma.py bash - <<'EOF'
for svc in boardd pandad; do
  echo "== $svc =="
  pgrep -a "$svc" || echo "(not running)"
done
python3 <<'PY'
import json, subprocess
print(json.dumps({"uptime": open("/proc/uptime").read().split()[0]}))
PY
EOF

# bump the timeout for slow scripts (default 30s):
scripts/mdma.py bash --timeout 120 'sleep 60; echo woke up'

# reboot and print a timestamped boot trace, returns at the prompt
scripts/mdma.py profile-boot
```

### `--missing-ok`

`--missing-ok` makes the script exit **0** (success) instead of 1 when no MDMA is connected. Use it in automation that should be a no-op when the adapter is absent. This is how the agnos-builder flash scripts invoke it:

```bash
scripts/mdma.py --missing-ok reboot-qdl   # ... flash ...
scripts/mdma.py --missing-ok reboot
```

With no subcommand, the script prints help and exits 0.

## How it works (for debugging)

- **Power / VIN** is toggled by writing a GPIO register over a USB control transfer to the Microchip "HFC" hub (`0424:704c`). `VIN_EN` is GPIO bit 92.
- **QDL forcing**: on comma four, powering the *aux* USB ports up *before* VIN forces the SOC into QDL on boot. `reboot-qdl` does exactly this ordering; `reboot` powers VIN first instead.
- **Aux USB power** is toggled via `SET_FEATURE`/`CLEAR_FEATURE` (PORT_POWER) on the `0424:7002` and `0424:4002` hubs.
- **`profile-boot`** opens the serial device raw at 115200 8N1, drains stale bytes, reboots, then prints each line prefixed with elapsed seconds, stopping when it sees a `login:`, `#`, or `$` prompt (`PROMPT_RE`).
- **`bash`** opens the same raw serial device, drives the console to a shell — logging in with `comma`/`comma` if it lands on a `login:` prompt — then sends **one line** that runs the script and frames its gzipped+base64'd output between a **per-call nonce marker** carrying the blob's byte length and the script's exit code:
  `out=$({ printf %s <b64> | base64 -d | bash; echo $? > /tmp/.mdma_rc_<nonce>; } 2>&1 | gzip -c | base64 -w0); rc=$(cat /tmp/.mdma_rc_<nonce>); rm -f …; printf 'MDMABEG%s:%s:\n' <nonce> "${#out}"; printf '%s\n' "$out"; printf 'MDMAEND%s:%s:\n' <nonce> "$rc"`
  - **Outbound (host→device):** the whole script is base64-encoded on the host, so only a single line crosses the line-oriented serial link — arbitrary multi-line scripts, heredocs, and embedded `python3` survive with no quoting or line-timing hazards.
  - **Inbound (device→host):** the script's combined stdout+stderr is **gzipped then base64'd on the device** before crossing back. 115200 baud is only ~7.5 KB/s effective and *is* the bottleneck, so compressing on the device is the speed win — ~3x on dmesg-like text, up to ~7x on highly compressible output. The host strips non-base64 cruft, base64-decodes, and gunzips, so the captured bytes are **exact and binary-safe** (no whitespace munging; raw bytes 0x00–0xFF round-trip).
  - **Nonce markers + length, not a fixed sentinel:** the markers are built per-call from a random nonce, and the host sends them as `printf` *format + arg* (`printf 'MDMABEG%s…' <nonce>`) so the **concatenated** token (`MDMABEG<nonce>`) only ever appears in real output — never in the command line the console echoes back. The begin marker also carries the blob's exact byte count; the host slices between the markers and **verifies the length matches** before decoding. A fixed literal sentinel (the old `__MDMA_BEG_837__`) appeared verbatim in the echoed command, so a clipped/drained echo could make the host slice the *input* base64 into the output blob — the dominant base64-corruption failure. The nonce + length framing eliminates it and makes any truncated frame self-detecting.
  - **Exit code:** the script's bash exit is captured **inside** the command substitution (`echo $? > /tmp/.mdma_rc_<nonce>`); reading `${PIPESTATUS[0]}` *after* `out=$(…)` would reflect the assignment's own pipeline (always 0), not the script's. `rc` rides back in the end marker and becomes the `bash` subcommand's process exit code. The tiny rc temp file is removed each call.
  - **Reliability:** prompt detection nudges the console with newlines for up to ~12 s (an idle serial getty / emergency shell can sit silent until poked and may take several seconds to echo), instead of firing one newline and giving up after 3 s — that short window was the dominant **"no response from serial console"** false negative. A transient round-trip failure (garbled/truncated frame, length mismatch, decode error) is **retried once** with a fresh login; a genuinely dead console fails cleanly after the prompt window with "no response from serial console". *Note:* during early kernel bring-up the device-side console may stop servicing the UART entirely — that's a device state no host retry can fix; reboot to recover.
  - This runs over the **serial console**, not SSH — works with no network, but the device must be booted to a login/shell prompt (not QDL or mid-boot) and needs `gzip`/`base64`/`bash` on PATH (AGNOS has all three).
  - **Emergency / maintenance shell:** if the device drops to the systemd emergency shell (e.g. a failed `nofail`-less mount during development), the console lands directly on a `root@…#` shell with **no `login:` prompt**. That shell turns on **bracketed-paste mode**, wrapping its prompt and every echoed line in `\x1b[?2004h`/`\x1b[?2004l` escape sequences. `bash` is resilient to this: prompt detection matches against an ANSI-stripped view of the serial buffer (`ANSI_RE`), and `_extract` strips those escapes before base64-decoding so their base64-legal interior bytes (`2004`, `h`, `l`) can't corrupt the output blob. So `bash` works at the emergency shell exactly as it does at a normal login.

The `serial` command `execvp`s into `screen` and replaces the process, so it's interactive-only — don't call it from non-interactive automation; it won't return.

## Typical flashing flow

QDL flashing is the un-brickable recovery/flash path. The general sequence (driven by agnos-builder's `flash_*.sh`):

1. `scripts/mdma.py reboot-qdl` — drop the SOC into QDL.
2. Run the QDL flasher (e.g. `qdl` / the agnos-builder flash script) to write images.
3. `scripts/mdma.py reboot` — boot the freshly flashed system.

### Required hardware setup for QDL — the aux cable

**QDL will never enumerate unless the SOC's aux USB is physically wired to the host.** On the MDMA DESK board this is a separate **aux USB-C connector** (the one *not* labeled `UFP`; `UFP` is the host/control+serial uplink). You must loop a USB-C cable:

> **dev board aux USB-C  →  the comma four's USB-C port**

The board bridges that aux port to the host through its **USB3 hub `0424:7002` (Bus 2)**. The `aux("on")` step in `reboot`/`reboot-qdl` powers that hub's ports; the SOC then presents its aux USB *in device mode only while entering QDL*, so it enumerates as `3801:9008` on **Bus 2** at the QDL moment. Leave this cable plugged — once set up, the entire flash loop runs hands-free from the host with no cable touching.

Verify the path before flashing:

```bash
# aux cable connected + a QDL force in progress → SOC shows here:
lsusb -d 3801:9008   # "Qualcomm ... MSM QUSB_BULK" on Bus 002 == QDL is live
```

Diagnosing a missing/wrong aux connection (control-transfer probes against the hubs):
- The QDL path is the **`0424:7002` hub on Bus 2**. A cable in the wrong port enumerates as `3801:ddcc panda` on the **`0424:4002` hub (Bus 1)** instead — that's the panda USB2 path and never triggers QDL.
- 7002 is a **SuperSpeed hub** (bcdUSB 0x0320): its port-power bit is `wPortStatus` **bit 9 (0x200)**, *not* the USB2 bit 8. Reading bit 8 falsely shows "unpowered." With the aux cable absent the ports read **powered, `connected=False`** — that empty-but-powered state is the tell that the aux cable isn't plugged.

### Hands-free flash loop (aux cable stays plugged)

```bash
scripts/mdma.py reboot-qdl                 # into QDL (verify: lsusb -d 3801:9008 on Bus 2)
( cd /path/to/vamOS && ./vamos flash kernel )   # flash boot_a
scripts/mdma.py reboot                      # VIN-first; boots through the momentary
                                            # QDL flicker into the flashed kernel
```

Gotchas observed:
- **`reboot-qdl` doesn't always cut VIN.** It returns exit 0 but sometimes no-ops the power cut — the device never power-cycles and just stays on its current boot. Confirm the cut by checking that **uptime reset** (`scripts/mdma.py bash 'cut -d" " -f1 /proc/uptime'` should drop to single digits). If uptime kept climbing, re-issue. A re-enumeration of the MDMA hubs (fresh `lsusb` device numbers) tends to restore a working power toggle.
- After a firehose reset (post-flash) the device often re-appears in **QDL**; a normal `scripts/mdma.py reboot` (VIN-first) is what boots it out into the flashed system. With the aux cable plugged you may see a brief `3801:9008` flicker on the reset edge before it proceeds to normal boot — that's expected.
- To make QDL entry robust, drive the aux-before-VIN sequence and then **verify** by polling for `3801:9008` / the 7002 port `connected` bit, retrying if it didn't latch, rather than trusting `reboot-qdl`'s exit code.

## Maintaining this reference

`scripts/mdma.py` started as a **copy** of `agnos-builder/scripts/mdma.py`, but this copy has since diverged: the `bash` command (serial-console command execution with auto-login) is **skill-only** and does not exist upstream. When re-syncing after upstream changes, don't blindly overwrite — merge upstream changes in and keep the `bash` machinery: the `_Transient` exception; the `Mdma` methods `open_serial`, `_drain`, `_read_until` (with its `nudge=` arg), `_ensure_login` (and `ANY_PROMPT_TIMEOUT`), `exec`, `_exec_once`, `_nonce`, `_extract`; the module-level `bash_script` arg resolver; the `zlib`/`base64` imports; and the `bash` subparser wiring.

Then re-read this reference against the new command table / behavior and update as needed.
