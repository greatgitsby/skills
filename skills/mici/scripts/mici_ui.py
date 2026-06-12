#!/usr/bin/env python3
"""mici_ui.py — capture the comma four (mici) display and inject touch events.

Runs ON THE DEVICE (AGNOS). Needs the device Python venv (PIL, libdrm). Talks to
the `magic` display broker over /tmp/drmfd.sock to borrow the DRM master fd, reads
the live scanout framebuffer, and writes evdev multitouch events to the touchscreen.

Geometry (comma four / mici):
  - Framebuffer is NATIVE PORTRAIT 240x536, format ABGR8888, pitch 1024.
  - The screen is VIEWED LANDSCAPE 536x240: native is rotated 90 deg CLOCKWISE
    (PIL rotate(270, expand=True)) to get the upright image.
  - Touchscreen /dev/input/event2 ("fts_ts") reports in the NATIVE portrait frame:
    ABS_MT_POSITION_X 0..240, ABS_MT_POSITION_Y 0..536, multitouch protocol B.

Coordinate transform (this is the thing to get right):
  All command coordinates are LANDSCAPE (lx in 0..535, ly in 0..239), matching what
  you see in a captured (rotated) screenshot. They are converted to native touch
  coordinates before injection:
      nx = ly
      ny = (LAND_W - 1) - lx       # 535 - lx
  The touchscreen reports 180 deg rotated relative to the framebuffer, so the touch
  transform and the capture rotation are independent.

Usage:
  mici_ui.py capture OUT.png            # save upright landscape (536x240) PNG
  mici_ui.py tap LX LY [--hold S]       # tap at landscape (LX,LY); default hold 0.08s
  mici_ui.py swipe LX1 LY1 LX2 LY2 [--dur S]   # swipe in landscape coords (default 0.4s)
  mici_ui.py hold LX LY [--dur S]       # long-press (default 0.8s)
"""
import argparse
import array
import ctypes
import ctypes.util
import glob
import mmap
import os
import socket
import struct
import sys
import time

# ---- geometry ----
NATIVE_W = 240   # framebuffer width  (portrait)
NATIVE_H = 536   # framebuffer height (portrait)
LAND_W = NATIVE_H   # 536
LAND_H = NATIVE_W   # 240

DRM_SOCK = "/tmp/drmfd.sock"
# Touch is on the 894000 i2c bus; this by-path symlink is stable across kernels
# (eventN differs: event2 legacy, event0 mainline).
TOUCH_DEV = next(iter(glob.glob("/dev/input/by-path/*894000.i2c-event")), "/dev/input/event2")


def land_to_native(lx, ly):
    """Landscape (lx,ly) -> native touch (nx,ny). Display is native rotated 90 CW.

    The touchscreen reports 180 deg rotated relative to the framebuffer, so we
    pre-rotate the landscape point 180 (lx->535-lx, ly->239-ly) before the native
    mapping, which simplifies to nx=ly, ny=(LAND_W-1)-lx."""
    nx = ly
    ny = (LAND_W - 1) - lx
    return max(0, min(NATIVE_W - 1, nx)), max(0, min(NATIVE_H - 1, ny))


# ======================================================================
# CAPTURE
# ======================================================================

class _OP(ctypes.Structure):
    _fields_ = [("count_props", ctypes.c_uint32),
                ("props", ctypes.POINTER(ctypes.c_uint32)),
                ("prop_values", ctypes.POINTER(ctypes.c_uint64))]


class _PR(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32),
                ("planes", ctypes.POINTER(ctypes.c_uint32))]


class _PROP(ctypes.Structure):
    _fields_ = [("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("name", ctypes.c_char * 32),
                ("count_values", ctypes.c_uint32),
                ("values", ctypes.POINTER(ctypes.c_uint64)),
                ("count_enums", ctypes.c_uint32), ("enums", ctypes.c_void_p),
                ("count_blobs", ctypes.c_uint32),
                ("blob_ids", ctypes.POINTER(ctypes.c_uint32))]


class _FB(ctypes.Structure):
    _fields_ = [("fb_id", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("pitch", ctypes.c_uint32),
                ("bpp", ctypes.c_uint32), ("depth", ctypes.c_uint32),
                ("handle", ctypes.c_uint32)]


DRM_MODE_OBJECT_PLANE = 0xeeeeeeee


def _drm():
    d = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2", use_errno=True)
    d.drmModeGetPlaneResources.restype = ctypes.POINTER(_PR)
    d.drmModeObjectGetProperties.restype = ctypes.POINTER(_OP)
    d.drmModeGetProperty.restype = ctypes.POINTER(_PROP)
    d.drmModeGetFB.restype = ctypes.POINTER(_FB)
    return d


def _get_master_fd(sock_path=DRM_SOCK):
    """Borrow a dup of the DRM master fd from the `magic` broker via SCM_RIGHTS.
    Returns (socket, fd). Keep the socket open while using the fd."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    _, anc, _, _ = s.recvmsg(1, socket.CMSG_LEN(struct.calcsize("i")))
    for level, typ, data in anc:
        if typ == socket.SCM_RIGHTS:
            a = array.array("i")
            a.frombytes(data[:4])
            return s, a[0]
    s.close()
    raise RuntimeError("magic broker did not pass a DRM fd")


def _active_fb_id(d, fd):
    """Find the FB_ID currently committed to a plane (prefer the primary plane)."""
    d.drmSetClientCap(fd, 1, 1)  # UNIVERSAL_PLANES
    d.drmSetClientCap(fd, 3, 1)  # ATOMIC
    pr = d.drmModeGetPlaneResources(fd).contents
    candidates = []
    for i in range(pr.count):
        pid = pr.planes[i]
        op = d.drmModeObjectGetProperties(fd, pid, DRM_MODE_OBJECT_PLANE)
        if not op:
            continue
        op = op.contents
        info = {}
        for j in range(op.count_props):
            p = d.drmModeGetProperty(fd, op.props[j]).contents
            info[p.name.decode()] = op.prop_values[j]
        if info.get("FB_ID") and info.get("CRTC_ID"):
            candidates.append((int(info["FB_ID"]), int(info.get("type", 99))))
    # plane type 1 == DRM_PLANE_TYPE_PRIMARY
    candidates.sort(key=lambda c: 0 if c[1] == 1 else 1)
    return candidates[0][0] if candidates else 0


def capture(out_path):
    from PIL import Image
    s, fd = _get_master_fd()
    try:
        d = _drm()
        fbid = _active_fb_id(d, fd)
        if not fbid:
            raise RuntimeError("no active framebuffer on any plane")
        fb = d.drmModeGetFB(fd, fbid)
        if not fb:
            raise RuntimeError(f"drmModeGetFB({fbid}) failed errno={ctypes.get_errno()}")
        fb = fb.contents
        w, h, pitch, handle = fb.width, fb.height, fb.pitch, fb.handle

        pfd = ctypes.c_int(0)
        if d.drmPrimeHandleToFD(fd, handle, 0, ctypes.byref(pfd)):
            raise RuntimeError(f"PRIME export failed errno={ctypes.get_errno()}")
        size = pitch * h
        mm = mmap.mmap(pfd.value, size, mmap.MAP_SHARED, mmap.PROT_READ)
        raw = mm.read(size)
        os.close(pfd.value)

        # DRM_FORMAT_ABGR8888 (fourcc 'AB24'): memory order low->high = R,G,B,A
        img = Image.frombuffer("RGBA", (w, h), raw, "raw", "RGBA", pitch, 1).convert("RGB")
        img = img.rotate(270, expand=True)  # native portrait -> upright landscape
        img.save(out_path)
        return img.size
    finally:
        s.close()


# ======================================================================
# TOUCH (evdev multitouch protocol B -> /dev/input/event2)
# ======================================================================

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_TOUCH = 0x14a
ABS_MT_SLOT = 0x2f
ABS_MT_TRACKING_ID = 0x39
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_PRESSURE = 0x3a
ABS_MT_TOUCH_MAJOR = 0x30

# struct input_event on aarch64: time(2x long=16B), __u16 type, __u16 code, __s32 value
_FMT = "llHHi"


def _ev(fd, typ, code, val):
    os.write(fd, struct.pack(_FMT, 0, 0, typ, code, val))


def _syn(fd):
    _ev(fd, EV_SYN, SYN_REPORT, 0)


def _down(fd, nx, ny, tid=1, slot=0):
    _ev(fd, EV_ABS, ABS_MT_SLOT, slot)
    _ev(fd, EV_ABS, ABS_MT_TRACKING_ID, tid)
    _ev(fd, EV_KEY, BTN_TOUCH, 1)
    _ev(fd, EV_ABS, ABS_MT_POSITION_X, nx)
    _ev(fd, EV_ABS, ABS_MT_POSITION_Y, ny)
    _ev(fd, EV_ABS, ABS_MT_TOUCH_MAJOR, 6)
    _ev(fd, EV_ABS, ABS_MT_PRESSURE, 50)
    _syn(fd)


def _move(fd, nx, ny, slot=0):
    _ev(fd, EV_ABS, ABS_MT_SLOT, slot)
    _ev(fd, EV_ABS, ABS_MT_POSITION_X, nx)
    _ev(fd, EV_ABS, ABS_MT_POSITION_Y, ny)
    _syn(fd)


def _up(fd, slot=0):
    _ev(fd, EV_ABS, ABS_MT_SLOT, slot)
    _ev(fd, EV_ABS, ABS_MT_TRACKING_ID, -1)
    _ev(fd, EV_KEY, BTN_TOUCH, 0)
    _syn(fd)


def tap(lx, ly, hold=0.08):
    nx, ny = land_to_native(lx, ly)
    fd = os.open(TOUCH_DEV, os.O_WRONLY)
    try:
        _down(fd, nx, ny)
        time.sleep(hold)
        _up(fd)
    finally:
        os.close(fd)
    return nx, ny


def swipe(lx1, ly1, lx2, ly2, dur=0.4, steps=24):
    n1 = land_to_native(lx1, ly1)
    n2 = land_to_native(lx2, ly2)
    fd = os.open(TOUCH_DEV, os.O_WRONLY)
    try:
        _down(fd, *n1)
        for i in range(1, steps + 1):
            t = i / steps
            nx = int(n1[0] + (n2[0] - n1[0]) * t)
            ny = int(n1[1] + (n2[1] - n1[1]) * t)
            _move(fd, nx, ny)
            time.sleep(dur / steps)
        _up(fd)
    finally:
        os.close(fd)
    return n1, n2


def hold(lx, ly, dur=0.8):
    return tap(lx, ly, hold=dur)


# ======================================================================
# CHAIN — a tiny step language run in one process (no per-step round-trip)
# ======================================================================
#
# Steps are separated by ';' or newlines. '#' starts a comment to end of line.
# Coordinates are landscape, same as the individual commands.
#
#   tap LX LY [HOLD]                 # HOLD seconds optional (default 0.08)
#   swipe LX1 LY1 LX2 LY2 [DUR]      # DUR seconds optional (default 0.4)
#   hold LX LY [DUR]                 # DUR seconds optional (default 0.8)
#   wait S                           # sleep S seconds
#   capture [NAME]                   # screenshot; NAME defaults to step index
#
# capture steps write to /tmp/mici_chain/<NAME>.png on the device; the host CLI
# pulls them all back. Each step prints a line so failures are locatable.

CHAIN_CAP_DIR = "/tmp/mici_chain"


def run_chain(script):
    import re
    import shlex
    steps = []
    for raw in re.split(r"[;\n]", script):
        line = raw.split("#", 1)[0].strip()
        if line:
            steps.append(shlex.split(line))

    os.makedirs(CHAIN_CAP_DIR, exist_ok=True)
    captures = []  # (name, remote_path)
    for i, parts in enumerate(steps):
        op, args = parts[0], parts[1:]
        if op == "tap":
            lx, ly = int(args[0]), int(args[1])
            h = float(args[2]) if len(args) > 2 else 0.08
            tap(lx, ly, h)
            print(f"[{i}] tap {lx} {ly} hold={h}")
        elif op == "swipe":
            a = [int(v) for v in args[:4]]
            dur = float(args[4]) if len(args) > 4 else 0.4
            swipe(*a, dur=dur)
            print(f"[{i}] swipe {a} dur={dur}")
        elif op == "hold":
            lx, ly = int(args[0]), int(args[1])
            dur = float(args[2]) if len(args) > 2 else 0.8
            hold(lx, ly, dur)
            print(f"[{i}] hold {lx} {ly} dur={dur}")
        elif op == "wait":
            time.sleep(float(args[0]))
            print(f"[{i}] wait {args[0]}")
        elif op == "capture":
            name = args[0] if args else str(i)
            if not name.endswith(".png"):
                name += ".png"
            path = os.path.join(CHAIN_CAP_DIR, name)
            size = capture(path)
            captures.append(name)
            print(f"[{i}] capture -> {path} {size[0]}x{size[1]}")
        else:
            raise SystemExit(f"[{i}] unknown step: {op}")
    # final line lists capture filenames for the host to pull
    print("CAPTURES " + " ".join(captures))


# ======================================================================
# CLI
# ======================================================================

def main():
    ap = argparse.ArgumentParser(description="capture + touch for comma four (mici)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture")
    c.add_argument("out")

    t = sub.add_parser("tap")
    t.add_argument("lx", type=int)
    t.add_argument("ly", type=int)
    t.add_argument("--hold", type=float, default=0.08)

    sw = sub.add_parser("swipe")
    sw.add_argument("lx1", type=int)
    sw.add_argument("ly1", type=int)
    sw.add_argument("lx2", type=int)
    sw.add_argument("ly2", type=int)
    sw.add_argument("--dur", type=float, default=0.4)

    h = sub.add_parser("hold")
    h.add_argument("lx", type=int)
    h.add_argument("ly", type=int)
    h.add_argument("--dur", type=float, default=0.8)

    r = sub.add_parser("run", help="execute a chain script (';'/newline steps)")
    r.add_argument("script", help="step script, or '-' to read from stdin")

    a = ap.parse_args()
    if a.cmd == "run":
        script = sys.stdin.read() if a.script == "-" else a.script
        run_chain(script)
        return
    if a.cmd == "capture":
        size = capture(a.out)
        print(f"saved {a.out} {size[0]}x{size[1]}")
    elif a.cmd == "tap":
        nx, ny = tap(a.lx, a.ly, a.hold)
        print(f"tap landscape ({a.lx},{a.ly}) -> native ({nx},{ny})")
    elif a.cmd == "swipe":
        n1, n2 = swipe(a.lx1, a.ly1, a.lx2, a.ly2, a.dur)
        print(f"swipe landscape ({a.lx1},{a.ly1})->({a.lx2},{a.ly2}) "
              f"-> native {n1}->{n2}")
    elif a.cmd == "hold":
        nx, ny = hold(a.lx, a.ly, a.dur)
        print(f"hold {a.dur}s landscape ({a.lx},{a.ly}) -> native ({nx},{ny})")


if __name__ == "__main__":
    main()
