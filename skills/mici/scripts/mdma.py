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


class _Transient(Exception):
  """A serial round-trip failure worth one automatic retry (idle console,
  dropped/garbled/truncated frame). Distinct from hard SystemExit failures
  like a wrong password or a missing adapter."""


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

  def _read_until(self, fd, pattern, timeout, nudge=None):
    """Read from fd until `pattern` (compiled regex) matches the accumulated
    buffer or `timeout` elapses. Returns (raw_buf, match_or_None).

    Matching is done against an ANSI/bracketed-paste-stripped view of the
    buffer so prompt regexes survive escape sequences (the emergency shell's
    `\x1b[?2004h` paste markers), but the *raw* buffer is returned so callers
    like `exec` can decode the verbatim device bytes.

    If `nudge` (bytes) is given, it's re-sent to the device every ~1.5 s while
    waiting. The serial getty / emergency shell sometimes sits idle and emits
    nothing until poked, so a single up-front newline can be silently dropped;
    re-nudging wakes it without depending on perfect timing."""
    buf = b""
    deadline = time.monotonic() + timeout
    next_nudge = time.monotonic() + 1.5
    while time.monotonic() < deadline:
      if nudge is not None and time.monotonic() >= next_nudge:
        os.write(fd, nudge)
        next_nudge = time.monotonic() + 1.5
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
  # How long to nudge for a prompt before declaring the console dead. An idle
  # serial getty / emergency shell can take several seconds to start echoing.
  ANY_PROMPT_TIMEOUT = 12.0

  def _ensure_login(self, fd, timeout=20.0):
    """Get the serial console to a live shell prompt, logging in with the
    device's default comma/comma credentials if it's sitting at login:."""
    # Don't pre-drain: the prompt may already be sitting in the buffer, and an
    # idle getty/emergency shell can take several seconds (or a few nudges) to
    # echo. Nudge with newlines for up to ANY_PROMPT_TIMEOUT s rather than
    # firing one \n and giving up after 3 s — that 3 s window was the dominant
    # "no response from serial console" false negative.
    any_prompt = re.compile(rb"login:\s*$|[Pp]assword:\s*$|[#\$]\s*$")
    buf, m = self._read_until(fd, any_prompt, self.ANY_PROMPT_TIMEOUT, nudge=b"\n")
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

  def exec(self, script, timeout=30.0, _tries=2):
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
    munging). The device frames the blob with a per-call nonce marker and emits
    the blob's exact byte count, so the host slices it precisely and verifies it
    arrived whole; the script's own exit code rides back via ${PIPESTATUS[0]}.

    Transient serial failures (idle console, a dropped/garbled frame) are
    retried once with a fresh login. Logs in with comma/comma if the console is
    at a login prompt. Requires gzip + base64 on the device PATH (AGNOS has
    both)."""
    last_err = None
    for attempt in range(_tries):
      fd = self.open_serial()
      try:
        self._ensure_login(fd)
        self._drain(fd)
        return self._exec_once(fd, script, timeout)
      except _Transient as e:
        last_err = e
      finally:
        os.close(fd)
    raise SystemExit(str(last_err))

  # A per-call nonce makes the framing immune to the command line the device
  # echoes back: the host sends the marker as two shell args joined at runtime
  # (`printf %s%s MDMA_<tag>_ <nonce>`), so the *concatenated* token only ever
  # appears in real output, never in the echoed command. The old fixed
  # `__MDMA_BEG_837__` literal appeared verbatim in the echo, and a drained/
  # clipped echo would make rfind() land inside it and pull the base64 of the
  # input script into the blob — the dominant base64-corruption failure.
  def _exec_once(self, fd, script, timeout):
    nonce = self._nonce()
    beg = f"MDMABEG{nonce}".encode()
    end = f"MDMAEND{nonce}".encode()

    # device side: decode the script, run under bash capturing stdout+stderr,
    # gzip+base64 it into a var, then print the begin marker, the blob's exact
    # length, the blob, and the end marker carrying the SCRIPT's exit code
    # (PIPESTATUS[0]). Markers are assembled from fragments via printf so the
    # full token never appears in the echoed command line. base64 -w0 = no wrap.
    b64 = base64.b64encode(script.encode()).decode()
    # The script's exit code must be captured INSIDE the command substitution:
    # ${PIPESTATUS[0]} read after `out=$(...)` would reflect the assignment's
    # own pipeline (always 0), not the script's. So stash rc into a file inside
    # the subshell and read it back out afterward.
    rcf = f"/tmp/.mdma_rc_{nonce}"
    line = (
      "out=$({ printf %s " + b64 + " | base64 -d | bash; echo $? > "
      + rcf + "; } 2>&1 | gzip -c | base64 -w0); "
      f"rc=$(cat {rcf}); rm -f {rcf}; "
      f"printf 'MDMABEG%s:%s:\\n' {nonce} \"${{#out}}\"; "
      "printf '%s\\n' \"$out\"; "
      f"printf 'MDMAEND%s:%s:\\n' {nonce} \"$rc\"\n"
    )
    os.write(fd, line.encode())

    # end marker carries the exit code: MDMAEND<nonce>:<rc>:
    end_re = re.compile(re.escape(end) + rb":(-?\d+):")
    buf, m = self._read_until(fd, end_re, timeout)
    if m is None:
      raise _Transient(f"timed out after {timeout}s waiting for command output")
    return self._extract(buf, beg, end, end_re)

  def _nonce(self):
    # 8 hex chars; avoids os.urandom-free environments and needs no RNG seed.
    return "%08x" % (id(object()) & 0xFFFFFFFF)

  def _extract(self, buf, beg, end, end_re):
    # the begin marker carries the blob's exact byte length: MDMABEG<nonce>:<n>:
    beg_re = re.compile(re.escape(beg) + rb":(\d+):")
    bm = None
    for bm in beg_re.finditer(buf):
      pass
    em = None
    for em in end_re.finditer(buf):
      pass
    if bm is None or em is None:
      raise _Transient("framing markers missing from device output (garbled frame)")
    code = int(em.group(1))
    want = int(bm.group(1))

    # blob is everything between the begin marker's line and the end marker.
    nl = buf.find(b"\n", bm.end())
    start = nl + 1 if nl != -1 else bm.end()
    raw = buf[start:em.start()]
    # strip ANSI/bracketed-paste escapes FIRST (emergency shell injects e.g.
    # \x1b[?2004l whose interior "2004"/"h"/"l" are base64-legal), then drop
    # serial cruft (CRs, stray newlines/spaces) to leave only the base64 blob.
    blob = self.ANSI_RE.sub(b"", raw)
    blob = re.sub(rb"[^A-Za-z0-9+/=]", b"", blob)

    # length check: if what arrived doesn't match the count the device computed,
    # the frame was truncated/contaminated — retry rather than decode garbage.
    if len(blob) != want:
      raise _Transient(f"blob length mismatch (got {len(blob)}, device sent {want}); frame truncated")

    if blob:
      try:
        out = zlib.decompress(base64.b64decode(blob), wbits=16 + zlib.MAX_WBITS)
      except Exception as e:
        raise _Transient(f"failed to decode device output ({e})")
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
