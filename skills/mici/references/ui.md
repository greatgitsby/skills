# UI — see and drive the comma four display (screenshot + touch)

One CLI, `scripts/mici`, run on the host. It captures the mici's screen and injects
touch. It handles everything underneath — deploying the on-device driver, picking the
transport (SSH or MDMA serial), running under the device venv, and pulling
screenshots back to the host. You just call it.

```bash
$SKILL_DIR/scripts/mici capture [OUT.png]           # default OUT: /tmp/mici.png
$SKILL_DIR/scripts/mici tap LX LY [--hold S]
$SKILL_DIR/scripts/mici swipe LX1 LY1 LX2 LY2 [--dur S]
$SKILL_DIR/scripts/mici hold LX LY [--dur S]
$SKILL_DIR/scripts/mici shell 'CMD'                 # arbitrary device command (escape hatch)
```

`capture` prints the **local PNG path as the last line of stdout** — `Read` that file
to look at the screen. Other commands print what they did and exit with the device
command's status. The driver auto-deploys on first use and re-deploys only when it
changes, so there's no setup step.

## Chaining — `mici run` (prefer this for multi-step flows)

Every single command pays transport + venv startup (~5 s). To test a flow, chain the
steps into **one** `mici run` call — the whole script executes in a single device
process (one setup), and all screenshots are pulled back at the end.

```bash
$SKILL_DIR/scripts/mici run 'tap 268 120; wait 0.6; tap 150 120; wait 0.6; capture' \
    --outdir /tmp/run
# or read a longer script from stdin:
$SKILL_DIR/scripts/mici run - --outdir /tmp/run <<'STEPS'
  tap 268 120          # home -> settings
  wait 0.6
  tap 150 120          # settings -> toggles
  wait 0.6
  capture toggles      # screenshot named toggles.png
  tap 370 120 0.1      # tap a toggle (3rd arg = hold seconds)
  wait 0.5
  capture after        # screenshot named after.png
STEPS
```

Steps are separated by `;` or newlines; `#` starts a comment. Coordinates are
landscape, same as the standalone commands:

| step | meaning |
|------|---------|
| `tap LX LY [HOLD]` | tap; optional hold seconds (default 0.08) |
| `swipe LX1 LY1 LX2 LY2 [DUR]` | swipe over DUR seconds (default 0.4) |
| `hold LX LY [DUR]` | long-press (default 0.8) |
| `wait S` | sleep S seconds between steps |
| `capture [NAME]` | screenshot; NAME → `NAME.png` (default = step index) |

Each step prints a `[i] ...` line so a failure is locatable. Captured PNGs land in
`--outdir` (default `/tmp`); their local paths are printed at the end — `Read` them.
Put a `wait` between an action and the capture that checks it so the UI settles, and
keep the whole chain under the ~30 s interactive timeout (see below).

## Coordinates — read this before tapping

Screenshots and all tap/swipe coordinates use the **upright landscape** frame you see
in a capture: **536 wide × 240 tall**, origin top-left, x → right (0..535),
y → down (0..239). A point at the pixel you see in the screenshot is the point you
pass. (Internally the panel is portrait and rotated, but the CLI hides that — you
never deal with native coordinates.)

**Workflow: always capture first, find the target in the image, then act on those
landscape coordinates, then re-capture to confirm.**

```bash
$SKILL_DIR/scripts/mici capture /tmp/s.png   # -> Read /tmp/s.png, locate the button
$SKILL_DIR/scripts/mici tap 120 150          # tap it (landscape coords from the image)
$SKILL_DIR/scripts/mici capture /tmp/s2.png  # -> Read /tmp/s2.png to confirm
```

## Gestures

- **tap** — quick touch; default hold 0.08 s. Use `--hold` for a steadier press.
- **swipe** — drag from (LX1,LY1) to (LX2,LY2) over `--dur` seconds (default 0.4).
  Emits stepped motion so the UI registers it as a scroll, not a teleport. To scroll
  a list **up**, swipe from a lower y to a higher y position on screen (and vice
  versa). Horizontal swipes move between screens/cards.
- **hold** — long-press; default 0.8 s. On the home screen a >0.5 s press toggles
  Experimental Mode (only when longitudinal control is available, i.e. onroad).

## Driving the openpilot UI — what to know

Verified behavior on the mici offroad UI (openpilot 0.11.x, `MiciMainLayout`):

- **The home screen is one big button.** Tapping almost anywhere on home opens
  **Settings** (the gear/network/version are just status, not separate targets). From
  Settings, the big cards (`toggles`, `network`, `device`, `firehose`, `developer`)
  each open a sub-panel when tapped.
- **Interactive timeout resets navigation.** After ~**30 s of no touch offroad**
  (5–10 s onroad), the UI pops back to home and the screen dims. Each touch resets the
  timer. So act within the window — don't leave the device idle mid-flow and expect to
  still be deep in a menu. If a capture unexpectedly shows home, you probably timed
  out; re-drive from home.
- **A horizontal swipe drives whichever scroller is in focus.** On the top level it
  moves between `alerts ← home → onroad` (swipe right→left advances toward onroad;
  left→right goes back toward home/alerts). Inside a panel it scrolls that panel's
  items. If a swipe seems to "escape" to the wrong screen, it bubbled to the main
  scroller — re-capture and reorient rather than assuming the tap failed.

## Transports

Two first-class transports, selected with `--transport ssh|mdma|auto` (or the
`MICI_TRANSPORT` env var). The CLI errors if the chosen transport isn't available.
- **MDMA serial** — needs the MDMA adapter wired (see `references/mdma.md`). Works
  with no network and needs no host.
- **SSH** — needs your key on the device; give the host with `--host USER@HOST`
  (e.g. `--host comma@192.168.1.5`) or the `MICI_HOST` env var.
- **auto** (default) — prefers MDMA when the adapter is wired, else falls back to SSH.

If capture errors with "magic broker did not pass a DRM fd", the display service is
down — check `mici shell 'systemctl status magic.service'`.

## How it works (for debugging)

The host CLI (`scripts/mici`) shells out to the device and runs the on-device driver
(`scripts/mici_ui.py`, deployed to `/data/mici_ui.py`). The driver:

- **Capture**: the `magic` service (`/usr/comma/magic.py`, systemd `magic.service`)
  owns `/dev/dri/card0` as DRM master and hands a dup'd fd to clients on
  `/tmp/drmfd.sock` via `SCM_RIGHTS`. The driver borrows that fd, reads the primary
  plane's committed `FB_ID` via the **atomic property API** (the legacy
  `drmModeGetPlane().fb_id` reads 0 under atomic modesetting), `drmModeGetFB` →
  PRIME-export → `mmap`, interprets the buffer as **ABGR8888** (240×536, pitch 1024),
  and rotates 270° to the upright 536×240 landscape image. No root / `/dev/mem`.
- **Touch**: writes evdev multitouch protocol-B events to `/dev/input/event2`
  (`fts_ts`, world-writable). Native ranges X 0..240, Y 0..536. Landscape→native
  transform `nx = ly, ny = 535 - lx`. The touchscreen reports **180° rotated**
  relative to the framebuffer, so the touch transform and the capture rotation are
  independent. The UI polls touch via raylib at 140 Hz and classifies tap vs scroll by
  velocity, which is why swipes use stepped motion.

## Visualizing touches

Set the **`ShowDebugInfo`** param and restart the UI to turn on its built-in touch
overlay — a **red dot** at the latest touch, a **green→red fade trail** for the swipe
path, an FPS counter, and red widget-bound rectangles:

```bash
$SKILL_DIR/scripts/mici shell 'printf 1 > /data/params/d/ShowDebugInfo'
$SKILL_DIR/scripts/mici shell 'cd /data/openpilot && tools/op.sh start'   # restart openpilot
```

Set it to `0` and restart the UI to turn the overlays off. (The same toggle is "ui
debug mode" under Settings → developer.)

## Maintaining this reference

The fragile parts live in `scripts/mici_ui.py`: the geometry constants
(`NATIVE_W/H`, `land_to_native`, ABGR8888, the 270° rotation) and the `magic` socket
protocol. If a future AGNOS/UI changes the panel resolution, framebuffer format,
rotation, or the broker socket, re-verify by capturing, tapping a known on-screen
target (use the `ShowDebugInfo` touch overlay, see *Visualizing touches*), and
confirming the tap lands where the screenshot shows it. The touch transform and the
capture rotation are independent, so re-check them separately. Editing `mici_ui.py` is
enough — the host CLI auto-redeploys it by content hash.
