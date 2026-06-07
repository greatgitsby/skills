---
name: mdma
description: For comma.ai device development (comma four / mici / comma 3X, agnos-builder, openpilot), operate the MDMA — the mici debug and monitoring adapter, a wired hardware debug board — to do low-level things SSH and op.sh cannot. Use whenever the user wants to power the SOC on/off or cut/cycle VIN power, force or drop the device into QDL mode (the un-brick path, e.g. "get it into QDL first" before flashing AGNOS or to recover a bricked board), open the MSM UART / serial console, or reboot-and-profile boot time — even if "MDMA" isn't named; the cue is physical power, QDL, serial, or power-on boot timing on a comma device. Not for: software-only access over SSH/op.sh, building AGNOS images, profiling openpilot/cereal inside a running system, or power/serial on non-comma hardware.
---

# MDMA — mici debug and monitoring adapter

The MDMA is a hardware debug adapter for low-level **comma four** (aka mici) and **comma 3X** development. It connects to the SOC over USB and a UART, and lets you:

- power the SOC on and off
- force the SOC into **QDL mode** for un-brickable flashing
- read/write the SOC's UART (serial console)
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

Run from this skill's directory (or pass the full path to `scripts/mdma.py`):

| Command | What it does |
| --- | --- |
| `reboot` | Power-cycle the SOC into a **normal** boot. |
| `reboot-qdl` | Power-cycle the SOC into **QDL mode** for flashing — the un-brick path. |
| `serial` | Open the MSM UART console with `screen` at 115200 baud. |
| `profile-boot` | Reboot into normal boot and stream the serial console with `[seconds.ms]` timestamps until the login/shell prompt appears. |

```bash
# normal reboot
scripts/mdma.py reboot

# force QDL mode (e.g. before flashing)
scripts/mdma.py reboot-qdl

# open the serial console (exit screen with Ctrl-A then \)
scripts/mdma.py serial

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
- **QDL forcing**: on comma 3X / comma four, powering the *aux* USB ports up *before* VIN forces the SOC into QDL on boot. `reboot-qdl` does exactly this ordering; `reboot` powers VIN first instead.
- **Aux USB power** is toggled via `SET_FEATURE`/`CLEAR_FEATURE` (PORT_POWER) on the `0424:7002` and `0424:4002` hubs.
- **`profile-boot`** opens the serial device raw at 115200 8N1, drains stale bytes, reboots, then prints each line prefixed with elapsed seconds, stopping when it sees a `login:`, `#`, or `$` prompt (`PROMPT_RE`).

The `serial` command `execvp`s into `screen` and replaces the process, so it's interactive-only — don't call it from non-interactive automation; it won't return.

## Typical flashing flow

QDL flashing is the un-brickable recovery/flash path. The general sequence (driven by agnos-builder's `flash_*.sh`):

1. `scripts/mdma.py reboot-qdl` — drop the SOC into QDL.
2. Run the QDL flasher (e.g. `qdl` / the agnos-builder flash script) to write images.
3. `scripts/mdma.py reboot` — boot the freshly flashed system.

## Maintaining this skill

`scripts/mdma.py` is a **copy** of `agnos-builder/scripts/mdma.py`. To re-sync after upstream changes:

```bash
cp /path/to/agnos-builder/scripts/mdma.py scripts/mdma.py
```

Then re-read this SKILL.md against the new command table / behavior and update as needed.
