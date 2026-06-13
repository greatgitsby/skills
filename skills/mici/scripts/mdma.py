#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pyusb"]
# ///
import argparse
import base64
import errno
import fcntl
import os
import re
import select
import sys
import termios
import time
import zlib

import usb.core

SERIAL_DEV = "/dev/serial/by-id/usb-Microchip_Tech_USB2_Controller_Hub-if01"
PROMPT_RE = re.compile(rb"(?:login:|[#\$])$")
USB_RT_PORT = 0x23
USB_REQ_CLEAR_FEATURE = 1
USB_REQ_SET_FEATURE = 3
USB_PORT_POWER = 8


class Pins:
  HFC_VID = 0x0424
  HFC_PID = 0x704C
  USB7002_VID = 0x0424
  USB7002_PID = 0x7002
  USB4002_VID = 0x0424
  USB4002_PID = 0x4002
  PIO96_OEN = 0xBF800908
  PIO96_OUT = 0xBF800928
  VIN_EN = 1 << (92 - 64)


class Mdma:
  """
    MDMA: the mici debug and monitoring adapter

    an MDMA is your best friend for low level mici (aka comma four) development.
    - power the SOC on and off
    - force the SOC into QDL mode for un-brickability
    - read and write to the SOC's UART
    - and more!
  """

  def hub(self, vid, pid):
    hub = usb.core.find(idVendor=vid, idProduct=pid)
    if hub is None:
      raise SystemExit(f"could not find hub {vid:04x}:{pid:04x}")
    return hub

  def available(self):
    return os.path.exists(SERIAL_DEV)

  def reg(self, addr, value=None, size=4):
    dev = usb.core.find(idVendor=Pins.HFC_VID, idProduct=Pins.HFC_PID)
    if value is None:
      return int.from_bytes(bytes(dev.ctrl_transfer(0xC0, 0x04, addr & 0xFFFF, addr >> 16, size)), "little")
    dev.ctrl_transfer(0x40, 0x03, addr & 0xFFFF, addr >> 16, value.to_bytes(size, "little"))

  def gpio(self, bit, on):
    if on:
      self.reg(Pins.PIO96_OEN, self.reg(Pins.PIO96_OEN) & ~bit)
    else:
      self.reg(Pins.PIO96_OUT, self.reg(Pins.PIO96_OUT) & ~bit)
      self.reg(Pins.PIO96_OEN, self.reg(Pins.PIO96_OEN) | bit)

  def aux(self, action):
    request = USB_REQ_SET_FEATURE if action == "on" else USB_REQ_CLEAR_FEATURE
    for vid, pid in [(Pins.USB7002_VID, Pins.USB7002_PID),  (Pins.USB4002_VID, Pins.USB4002_PID)]:
      try:
        self.hub(vid, pid).ctrl_transfer(USB_RT_PORT, request, USB_PORT_POWER, 1, None, timeout=1000)
      except usb.core.USBError: # try one more time
        self.hub(vid, pid).ctrl_transfer(USB_RT_PORT, request, USB_PORT_POWER, 1, None, timeout=1000)

  def power_off(self):
    self.aux("off")
    self.gpio(Pins.VIN_EN, False)

  def reboot(self, qdl):
    self.aux("off")
    self.gpio(Pins.VIN_EN, False)
    time.sleep(0.1)
    if qdl:
      # on comma 3X and comma four, aux powering
      # up first forces QDL mode on boot
      self.aux("on")
    else:
      self.gpio(Pins.VIN_EN, True)
    boot_time = time.monotonic()
    time.sleep(0.1)
    self.gpio(Pins.VIN_EN, True)
    self.aux("on")

    # give time to enumerate
    if qdl:
      time.sleep(1)

    return boot_time

  def serial(self):
    os.execvp("screen", ["screen", SERIAL_DEV, "115200"])

  def open_serial(self):
    # open the serial device raw at 115200 8N1
    try:
      fd = os.open(SERIAL_DEV, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
      if e.errno == errno.EBUSY:
        raise SystemExit(f"{SERIAL_DEV} is busy; close the serial console first")
      raise

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd

  def _drain(self, fd):
    while os.read(fd, 4096):
      time.sleep(0.05)

  # ANSI CSI / bracketed-paste escapes (e.g. \x1b[?2004h around a prompt). The
  # systemd *emergency shell* wraps its prompt in these, so the raw bytes end in
  # `...#\x1b[?2004h` — a naked `[#\$]\s*$` prompt regex never matches and login
  # detection wrongly reports "no response". We strip these before prompt-matching.
  ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")

  def _read_until(self, fd, pattern, timeout):
    """Read from fd until `pattern` (compiled regex) matches the accumulated
    buffer or `timeout` elapses. Returns (raw_buf, match_or_None).

    Matching is done against an ANSI/bracketed-paste-stripped view of the
    buffer so prompt regexes survive escape sequences (the emergency shell's
    `\x1b[?2004h` paste markers), but the *raw* buffer is returned so callers
    like `exec` can decode the verbatim device bytes."""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if not select.select([fd], [], [], 0.25)[0]:
        continue
      data = os.read(fd, 4096)
      if not data:
        continue
      buf += data.replace(b"\r\n", b"\n")
      m = pattern.search(self.ANSI_RE.sub(b"", buf))
      if m:
        return buf, m
    return buf, None

  # serial console states we may land in: a login: prompt, a Password: prompt,
  # or a live shell prompt (#/$). USERNAME/PASSWORD are the device defaults.
  USERNAME = "comma"
  PASSWORD = "comma"
  LOGIN_RE = re.compile(rb"login:\s*$")
  PASSWORD_RE = re.compile(rb"[Pp]assword:\s*$")
  SHELL_RE = re.compile(rb"[#\$]\s*$")

  def _ensure_login(self, fd, timeout=20.0):
    """Get the serial console to a live shell prompt, logging in with the
    device's default comma/comma credentials if it's sitting at login:."""
    self._drain(fd)
    os.write(fd, b"\n")
    # figure out where we are
    buf, m = self._read_until(fd, re.compile(rb"login:\s*$|[Pp]assword:\s*$|[#\$]\s*$"), 3.0)
    if m is None:
      raise SystemExit("no response from serial console (is the device booted?)")

    # strip escapes before re-testing which prompt we landed on (the emergency
    # shell wraps its prompt in bracketed-paste markers — see ANSI_RE).
    view = self.ANSI_RE.sub(b"", buf)
    if self.SHELL_RE.search(view):
      return  # already at a shell

    if self.PASSWORD_RE.search(view):
      # stale password prompt — bail out to a fresh login by sending a newline
      os.write(fd, b"\n")
      self._read_until(fd, self.LOGIN_RE, 5.0)

    # at this point we expect a login: prompt
    os.write(fd, (self.USERNAME + "\n").encode())
    _, m = self._read_until(fd, self.PASSWORD_RE, 5.0)
    if m is None:
      raise SystemExit("never reached a Password: prompt after sending username")
    os.write(fd, (self.PASSWORD + "\n").encode())
    _, m = self._read_until(fd, self.SHELL_RE, timeout)
    if m is None:
      raise SystemExit("login failed (wrong credentials or no shell prompt)")

  def exec(self, script, timeout=30.0):
    """Run a bash script on the device over the serial console and return its
    output + exit code. The script may be an arbitrary multi-line program
    (heredocs, embedded python3, quotes, etc.).

    Wire protocol: the script is base64-encoded on the host so only a single line
    ever crosses the line-oriented serial console (no quoting/heredoc hazards).
    On the device it's decoded and run under bash, and its combined stdout+stderr
    is piped through `gzip | base64` before crossing back — gzip typically shrinks
    text/log output 5-10x, and 115200 baud (~7.5 KB/s effective) is the real
    bottleneck, so compressing on the device is ~3x faster on large output. The
    host decodes + gunzips, so the captured bytes are exact (no whitespace
    munging). Begin/end sentinels frame the base64 blob; the script's own exit
    code is recovered via bash ${PIPESTATUS[0]}.

    Logs in with comma/comma if the console is at a login prompt. Requires
    gzip + base64 on the device PATH (AGNOS has both)."""
    fd = self.open_serial()
    self._ensure_login(fd)
    self._drain(fd)

    # unique markers framing the base64'd, gzipped output blob.
    beg = "__MDMA_BEG_837__"
    end = "__MDMA_END_837__"

    # device side: decode the script, run it under bash capturing stdout+stderr,
    # compress + base64 the output, then emit the end marker with the SCRIPT's
    # exit code (PIPESTATUS[0]), not gzip's/base64's. base64 -w0 = no line wraps.
    b64 = base64.b64encode(script.encode()).decode()
    # capture the script's exit code into a var on the SAME line as the pipeline
    # (a bare `echo` afterward would reset PIPESTATUS before we read it).
    line = (
      f"echo {beg}; "
      f"{{ printf %s {b64} | base64 -d | bash; }} 2>&1 | gzip -c | base64 -w0; rc=${{PIPESTATUS[0]}}; "
      f"echo; echo {end}:$rc:\n"
    )
    os.write(fd, line.encode())

    end_re = re.compile(rb"" + end.encode() + rb":(-?\d+):")
    buf, m = self._read_until(fd, end_re, timeout)
    os.close(fd)
    if m is None:
      raise SystemExit(f"timed out after {timeout}s waiting for command output")
    return self._extract(buf, beg.encode(), end_re, m)

  def _extract(self, buf, beg, end_re, end_match):
    # NOTE: end_match came from _read_until, whose offsets index an
    # ANSI-stripped view of the buffer, not the raw bytes we slice here — so we
    # re-locate the end marker in the raw buffer and use the LAST occurrence.
    end_raw = None
    for end_raw in end_re.finditer(buf):
      pass

    # the base64 blob lives between the begin marker's line and the end marker.
    # the begin marker appears twice on the wire (the echoed command line + its
    # own output); take everything after the LAST begin-marker newline so the
    # echoed command line is excluded.
    start = buf.rfind(beg)
    nl = buf.find(b"\n", start)
    start = nl + 1 if nl != -1 else len(buf)
    blob = buf[start:end_raw.start() if end_raw else len(buf)]

    # strip ANSI/bracketed-paste escapes FIRST — the emergency shell injects
    # them (e.g. \x1b[?2004l) and their interior chars ("2004", "h", "l") are
    # base64-legal, so they'd otherwise leak into and corrupt the blob. Then
    # drop remaining serial cruft (CRs, stray whitespace/newlines) and
    # decode + gunzip back to the exact device-side bytes.
    blob = self.ANSI_RE.sub(b"", blob)
    blob = re.sub(rb"[^A-Za-z0-9+/=]", b"", blob)
    code = int((end_raw or end_match).group(1))
    if blob:
      try:
        out = zlib.decompress(base64.b64decode(blob), wbits=16 + zlib.MAX_WBITS)
      except Exception as e:
        raise SystemExit(f"failed to decode device output ({e}); is gzip/base64 present on the device?")
      sys.stdout.buffer.write(out)
      sys.stdout.flush()
    return code

  def profile_boot(self):
    # device off for clean serial
    self.power_off()

    fd = self.open_serial()
    while (data := os.read(fd, 4096)):
      time.sleep(0.1)

    # boot!
    start = self.reboot(qdl=False)

    # show serial console with timestamps until boot is done
    pending = b""
    while True:
      if not select.select([fd], [], [], 0.25)[0]:
        continue

      data = os.read(fd, 4096)
      if not data:
        continue
      pending += data.replace(b"\r\n", b"\n")
      while b"\n" in pending:
        line, pending = pending.split(b"\n", 1)
        line = line.rstrip()
        print(f"[{time.monotonic() - start:8.3f}] {line.decode(errors='replace')}", flush=True)
        if PROMPT_RE.search(line):
          return
      if PROMPT_RE.search(pending.strip()):
        print(f"[{time.monotonic() - start:8.3f}] {pending.strip().decode(errors='replace')}", flush=True)
        return


def bash_script(args):
  # script body comes from stdin ("-") or inline argv (joined as one line).
  # both are base64-encoded and run under bash on the device, so multi-line
  # scripts, heredocs, and embedded python3 all work verbatim.
  if args.argv == ["-"] or (not args.argv and not sys.stdin.isatty()):
    script = sys.stdin.read()
  elif args.argv:
    script = " ".join(args.argv)
  else:
    raise SystemExit("bash: provide a command inline, or pipe a script via '-'")
  return Mdma().exec(script, timeout=args.timeout)


if __name__ == "__main__":
  cmds = {
    "reboot":       (lambda a: Mdma().reboot(qdl=False), "reboot comma four into normal boot"),
    "reboot-qdl":   (lambda a: Mdma().reboot(qdl=True), "reboot comma four into QDL mode for flashing"),
    "serial":       (lambda a: Mdma().serial(), "open the MSM UART console with screen"),
    "profile-boot": (lambda a: Mdma().profile_boot(), "reboot comma four and profile boot time"),
    "bash":         (bash_script, "run a bash script on the device over serial and print its output"),
  }

  parser = argparse.ArgumentParser()
  parser.add_argument("--missing-ok", action="store_true", help="continue successfully when no MDMA is connected")
  subparsers = parser.add_subparsers(dest="command", required=True)
  for cmd, (_, hlp) in cmds.items():
    sp = subparsers.add_parser(cmd, help=hlp)
    if cmd == "bash":
      sp.add_argument("argv", nargs="*", help="the bash command to run inline; or '-' to read the script from stdin")
      sp.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for output (default 30)")
  if len(sys.argv) == 1:
    parser.print_help()
    raise SystemExit(0)
  args = parser.parse_args()

  if not Mdma().available():
    print("MDMA not found.")
    raise SystemExit(0 if args.missing_ok else 1)

  rc = cmds[args.command][0](args)
  if isinstance(rc, int):
    raise SystemExit(rc)
