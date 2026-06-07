---
name: mici
description: >-
  Swiss-army toolkit for the comma.ai comma four (aka mici) device — covers both
  driving its touchscreen UI and low-level hardware debug. Use whenever the task
  touches a comma four. Two areas: (1) UI — capture a screenshot of the display and
  inject touch (taps, swipes, long-presses) to test UI changes, read what's on
  screen, or drive openpilot's settings/onboarding; cues like "screenshot the mici",
  "tap the settings gear", "swipe to the next screen", "what's on screen", "test my
  UI change on the device". (2) MDMA — operate the wired mici debug and monitoring
  adapter to power the SOC on/off, cut/cycle VIN, force or drop QDL mode (the un-brick
  path before flashing AGNOS or to recover a bricked board), open the MSM serial
  console, run commands over serial with no network, or profile boot time; cues
  involving physical power, QDL, serial console, or power-on boot timing. Not for:
  devices other than the comma four, or building AGNOS images.
allowed-tools: Bash(*), Read
---

# mici — comma four toolkit

Tools for the **comma four** (aka mici). Two areas, each with its own reference and
script. Read the reference for the area you need before using its commands.

## UI — screenshot + touch  →  `references/ui.md`

See and drive the touchscreen for testing UI changes. One host CLI,
`scripts/mici`, hides all device plumbing (deploy, transport, venv, pulling
screenshots back):

```bash
$SKILL_DIR/scripts/mici --transport mdma capture [OUT.png]    # wired adapter, no network
$SKILL_DIR/scripts/mici --host comma@HOST capture [OUT.png]   # over SSH; prints PNG path
$SKILL_DIR/scripts/mici --host comma@HOST tap LX LY           # landscape coords (536x240)
$SKILL_DIR/scripts/mici --host comma@HOST swipe LX1 LY1 LX2 LY2
$SKILL_DIR/scripts/mici --host comma@HOST hold LX LY
$SKILL_DIR/scripts/mici --host comma@HOST run 'tap 268 120; wait .6; capture'   # chain in one call
```

Two first-class transports, chosen with `--transport ssh|mdma|auto` (or `MICI_TRANSPORT`):
- **mdma** — the wired adapter, no network. Needs no host.
- **ssh** — pass `--host USER@HOST` (or set `MICI_HOST`); ask the user for it if unknown.
- **auto** (default) — prefers MDMA if the adapter is wired, else SSH.

Coordinates are upright-landscape (the frame you see in a capture). Prefer `mici run`
for multi-step flows — it runs the whole chain in one device invocation. **See
`references/ui.md`** for coordinates, the chain step language, openpilot UI navigation
behavior (home-is-one-big-button, the ~30 s interactive timeout, swipe scroller
routing), and internals.

## MDMA — hardware debug board  →  `references/mdma.md`

Low-level access via the wired **mici debug and monitoring adapter**. Only applies
when a comma four is **physically wired to an MDMA adapter** (else `scripts/mdma.py`
prints `MDMA not found.`). Power the SOC, force QDL for flashing/un-bricking, open the
serial console, run commands over serial, profile boot:

```bash
$SKILL_DIR/scripts/mdma.py reboot          # normal power-cycle
$SKILL_DIR/scripts/mdma.py reboot-qdl      # force QDL (un-brick / pre-flash)
$SKILL_DIR/scripts/mdma.py serial          # interactive MSM UART console
$SKILL_DIR/scripts/mdma.py bash 'uname -a' # run a command over serial, no network
$SKILL_DIR/scripts/mdma.py profile-boot    # timestamped boot trace
```

**See `references/mdma.md`** for the full command table, `bash -` multi-line/stdin
usage, `--missing-ok`, the flashing flow, and how it works (VIN/QDL GPIO, serial
protocol).

> The `scripts/mdma.py bash` command is also the no-network transport the UI CLI
> falls back to — so the two areas share the same serial link to the device.

## Scripts

- `scripts/mici` — host CLI for UI capture/touch/chain (see `references/ui.md`).
- `scripts/mici_ui.py` — on-device driver the CLI deploys and runs (don't call directly).
- `scripts/mdma.py` — self-contained `uv` MDMA driver (see `references/mdma.md`).
