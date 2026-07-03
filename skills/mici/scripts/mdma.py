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
import subprocess
import sys
import termios
import time
import zlib

import usb.core

SERIAL_DEV = "/dev/serial/by-id/usb-Microchip_Tech_USB2_Controller_Hub-if01"
# boot is done when the serial getty prints its login prompt. Matching bare
# `#`/`$` line endings ends profiling early on boot spew (kernel logs, shell
# residue), so require the full `comma-<hostname> login:` getty prompt.
PROMPT_RE = re.compile(rb"comma-\S+ login:")
USB_RT_PORT = 0x23
USB_REQ_CLEAR_FEATURE = 1
USB_REQ_SET_FEATURE = 3
USB_PORT_POWER = 8


_T0 = time.monotonic()


def _log(msg):
  """Timestamped progress to stderr — the diagnostic surface for agents driving
  this tool. Every state transition, retry, and fallback is narrated so a
  failure localizes from the log alone. stdout stays reserved for device
  output (`bash`)."""
  print(f"[mdma +{time.monotonic() - _T0:5.1f}s] {msg}", file=sys.stderr, flush=True)


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
  """MDMA: the mici debug and monitoring adapter — low-level mici (comma four)
  dev. Power the SOC on/off, force QDL mode for un-brickability, read/write the
  SOC's UART, and more."""

  # Boot-console capture: while set (see _cap_open), every byte read from the
  # serial fd through self._read is also written to this file as timestamped
  # `[   N.NNN] line` lines (profile-boot style), so the full boot log can be
  # read after the fact instead of re-running with a console attached.
  BOOT_LOG_DIR = "/tmp/mdma"
  _cap = None

  def _cap_open(self, tag):
    os.makedirs(self.BOOT_LOG_DIR, exist_ok=True)
    path = os.path.join(self.BOOT_LOG_DIR, time.strftime(f"{tag}-%Y%m%d-%H%M%S.log"))
    self._cap = open(path, "wb")
    self._cap_t0 = time.monotonic()
    self._cap_pending = b""
    return path

  def _cap_write(self, data):
    self._cap_pending += data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    while b"\n" in self._cap_pending:
      line, self._cap_pending = self._cap_pending.split(b"\n", 1)
      self._cap.write(f"[{time.monotonic() - self._cap_t0:8.3f}] ".encode() + line + b"\n")
    self._cap.flush()

  def _cap_close(self):
    if self._cap:
      if self._cap_pending:
        self._cap_write(b"\n")  # flush a trailing partial line
      self._cap.close()
      self._cap = None

  def _read(self, fd, n=4096):
    """os.read that tees into the boot-console capture file when one is open.
    All console readers go through here so a capture opened in boot() sees the
    complete boot output regardless of which path consumed it."""
    data = os.read(fd, n)
    if data and self._cap:
      self._cap_write(data)
    return data

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

  # A real power cycle spews boot output on the UART within a few seconds of
  # VIN coming up. If the serial line stays silent this long after a cycle, the
  # VIN cut no-op'd (a known MDMA flake) and the cycle must be re-issued.
  CYCLE_SPEW_TIMEOUT = 8.0
  # Fast-path boot watch: how many seconds of serial *idle* (no bytes at all)
  # mean the getty prompt isn't coming and we should fall back to the quiesce
  # waiter. Boot spew gaps are seconds, so 30 s idle is decisively dead.
  BOOT_SPEW_IDLE = 30.0

  def boot(self):
    """Power-cycle into a normal boot and block until the device is at a
    confirmed live shell, ready for `bash`. One resilient, parameterless step
    for the flash→reboot→wait loop.

    - **Verified power cut**: a real cycle spews boot output within seconds; a
      silent line means the VIN cut no-op'd (known MDMA flake), so the cycle is
      re-issued instead of waiting 90 s on a device that never rebooted.
    - **Event-driven ready**: watch the boot spew for the getty `comma-* login:`
      prompt and log in the *moment* it appears — no fixed quiesce windows on
      the happy path. The login + random-token handshake still prove the shell
      is live (replayed residue can't fake it).
    - Off the happy path (emergency shell, garbled login, residue match) falls
      back to the quiesce waiter wait_until_ready.
    Full boot console is captured to a timestamped log under BOOT_LOG_DIR (path
    on stderr) for post-hoc reading. Raises if the device doesn't come up."""
    log_path = self._cap_open("boot")
    _log(f"capturing full boot console to {log_path}")
    try:
      return self._boot()
    finally:
      self._cap_close()

  def _boot(self):
    for attempt in range(3):
      fd = self.open_serial()
      try:
        self._drain(fd)
        self.reboot(qdl=False)
        _log(f"power cycle issued (VIN-first, normal boot; attempt {attempt + 1}/3)")
        # verify the cut took: real boots print UART spew almost immediately
        if not select.select([fd], [], [], self.CYCLE_SPEW_TIMEOUT)[0]:
          _log(f"no boot output within {self.CYCLE_SPEW_TIMEOUT:.0f}s — VIN cut likely no-op'd (known MDMA flake); re-cycling")
          continue
        _log("boot output detected — power cut confirmed, watching for getty login prompt")
        # fast path: log in the instant the getty prompt appears
        _, m = self._read_until(fd, PROMPT_RE, self.BOOT_SPEW_IDLE, idle_reset=True)
        if m is not None:
          _log("getty login prompt seen — logging in (comma/comma)")
          try:
            self._login(fd, timeout=15.0)
            if self._handshake(fd, timeout=8.0):
              _log("liveness handshake ok — device ready for bash")
              return True
            _log("logged in but liveness handshake failed (residue prompt?) — falling back to quiesce waiter")
          except SystemExit as e:
            _log(f"fast-path login failed ({e}) — falling back to quiesce waiter")
        else:
          _log(f"no getty prompt within {self.BOOT_SPEW_IDLE:.0f}s of serial idle (emergency shell or unusual boot?) — falling back to quiesce waiter")
      finally:
        os.close(fd)
      # off the happy path (no getty prompt / failed handshake — e.g. the
      # emergency shell, which never prints login:) — quiesce waiter takes over.
      ok = self.wait_until_ready()
      _log("quiesce waiter confirmed a live shell — device ready for bash")
      return ok
    raise SystemExit("power cycle had no effect after 3 attempts (no boot output on serial) — check MDMA hubs (re-plug / re-enumerate often restores the power toggle)")

  def qdl(self):
    """Force the SOC into QDL mode via the aux-first power cycle. This is the
    reliable path — just run it. NOTE: `lsusb -d 3801:9008` is NOT a QDL
    indicator: on the MDMA dev board that device is always present with a
    healthy device (a board artifact), so enumeration can't verify (or refute)
    QDL entry. Needs the aux USB-C cable looped from the dev board's aux port
    to the comma four's USB-C port."""
    self.reboot(qdl=True)
    _log("aux-first power cycle issued — SOC forced into QDL (no enumeration check possible: 3801:9008 is always present on the dev board)")

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
    # select-gated so it never blocks if the device happens to be mid-output
    # (the fd is in blocking mode, so a bare os.read could stall).
    while select.select([fd], [], [], 0.05)[0]:
      if not self._read(fd):
        break

  # ANSI CSI / bracketed-paste escapes (e.g. \x1b[?2004h around a prompt). The
  # systemd *emergency shell* wraps its prompt in these, so the raw bytes end in
  # `...#\x1b[?2004h` — a naked `[#\$]\s*$` prompt regex never matches and login
  # detection wrongly reports "no response". We strip these before prompt-matching.
  ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")

  def _read_until(self, fd, pattern, timeout, nudge=None, idle_reset=False):
    """Read from fd until `pattern` (compiled regex) matches the accumulated
    buffer or `timeout` elapses. Returns (raw_buf, match_or_None).

    Matching is done against an ANSI/bracketed-paste-stripped view of the
    buffer so prompt regexes survive escape sequences (the emergency shell's
    `\x1b[?2004h` paste markers), but the *raw* buffer is returned so callers
    like `exec` can decode the verbatim device bytes.

    If `nudge` (bytes) is given, it's re-sent to the device every ~1.5 s while
    waiting. The serial getty / emergency shell sometimes sits idle and emits
    nothing until poked, so a single up-front newline can be silently dropped;
    re-nudging wakes it without depending on perfect timing.

    If `idle_reset` is set, the `timeout` measures *idle* time: the deadline is
    pushed forward whenever the device emits bytes. This lets a caller wait
    through a long-but-progressing boot (the console keeps spewing) while still
    failing promptly on a genuinely dead line (no bytes for `timeout` s)."""
    buf = b""
    deadline = time.monotonic() + timeout
    next_nudge = time.monotonic() + 1.5
    while time.monotonic() < deadline:
      if nudge is not None and time.monotonic() >= next_nudge:
        os.write(fd, nudge)
        next_nudge = time.monotonic() + 1.5
      if not select.select([fd], [], [], 0.25)[0]:
        continue
      data = self._read(fd)
      if not data:
        continue
      if idle_reset:
        deadline = time.monotonic() + timeout
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
  # When waiting for a booting device (--wait), how many seconds of serial
  # silence count as "boot output has stopped" before we probe for a live shell.
  QUIESCE_IDLE = 3.0

  def _wait_for_boot(self, fd, wait):
    """Wait up to `wait` s for a *booting* device to reach a usable shell.

    A booting comma four can't be detected by a prompt regex alone: the UART
    replays buffered serial input (this tool's own prior command frames, stale
    `root@none:~#` prompts) as it boots, so a naive match fires on residue, not a
    live shell, and the command we send gets shredded by boot spew. Instead:
    (1) wait for the console to go *quiet* (QUIESCE_IDLE s with no bytes), then
    (2) drive to a shell — a power-cycled device lands at a getty `login:`, so we
    must actually log in (a bare handshake can't pass at login:, and its
    `echo HS-...` probe gets typed as a username, wedging getty in
    Password:/Login-incorrect cycles) — then (3) prove the shell live with a
    random-token echo handshake replayed residue can't fake. Loops until the
    handshake passes or `wait` elapses."""
    hard = time.monotonic() + wait
    while time.monotonic() < hard:
      # wait for boot output to settle: drain until QUIESCE_IDLE s of silence.
      quiet_deadline = time.monotonic() + self.QUIESCE_IDLE
      while time.monotonic() < quiet_deadline and time.monotonic() < hard:
        if select.select([fd], [], [], 0.25)[0] and self._read(fd):
          quiet_deadline = time.monotonic() + self.QUIESCE_IDLE  # reset on output
      # console is quiet (or we're out of time) — get to a shell (logging in at
      # a login: prompt if that's where we are), then prove it's really live.
      try:
        self._login(fd, timeout=min(20.0, max(1.0, hard - time.monotonic())))
      except SystemExit:
        # no usable prompt yet (boot not done, or a garbled login attempt) —
        # nudge and loop back through quiescence.
        os.write(fd, b"\n")
        continue
      if self._handshake(fd, timeout=min(5.0, max(0.5, hard - time.monotonic()))):
        return True
      # prompt was residue, not a live shell — nudge and loop.
      os.write(fd, b"\n")
    return False

  # Budget for the `boot` waiter to reach a confirmed live shell. A comma four
  # boots in ≈5–45 s (observed ~60 s incl. login + handshake settle), so 90 s
  # leaves headroom for a slow first boot after a flash while still failing
  # promptly on a stuck boot. Returns as soon as the shell is live; the budget
  # only bounds a hung boot.
  BOOT_READY_TIMEOUT = 90.0

  def wait_until_ready(self, timeout=BOOT_READY_TIMEOUT, _retries=3):
    """Block until the serial console is at a *confirmed live shell* — ready to
    accept `bash` commands — logging in with comma/comma if it lands at a
    `login:` prompt. Returns True once a fresh-token echo handshake proves the
    shell is real (not replayed boot residue or a bare login: prompt); raises
    SystemExit if `timeout` s elapse without one.

    The readiness primitive behind `boot`, built from the same
    `_ensure_login`/`_handshake` machinery `bash --wait` uses. A just-booted
    console is transiently flaky, so a failed handshake is retried a few times
    (re-driving login) before giving up."""
    deadline = time.monotonic() + timeout
    last_err = "device never reached a live shell"
    for _ in range(max(1, _retries)):
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        break
      fd = self.open_serial()
      try:
        # _ensure_login drives the console through quiesce + (if needed) login
        # to a shell prompt; the trailing handshake then *proves* it's live.
        self._ensure_login(fd, wait=remaining)
        if self._handshake(fd, timeout=min(8.0, max(1.0, deadline - time.monotonic()))):
          return True
        last_err = "reached a shell but it failed the liveness handshake"
      except SystemExit as e:
        last_err = str(e)
        _log(f"readiness attempt failed ({last_err}) — retrying")
      finally:
        os.close(fd)
    raise SystemExit(f"device not ready after {timeout:.0f}s ({last_err})")

  def _handshake(self, fd, timeout=5.0):
    """Prove a live, ready shell by echoing a fresh random token and requiring
    it back. Distinguishes a real shell from replayed boot/serial residue and
    from a `login:` prompt (which won't echo the token)."""
    tok = self._nonce()
    self._drain(fd)
    os.write(fd, f"echo HS-{tok}\n".encode())
    # the command line itself echoes "echo HS-<tok>"; the *result* line is a
    # bare "HS-<tok>". Require the result (not preceded by "echo ").
    want = re.compile(rb"(?<!echo )HS-" + tok.encode())
    _, m = self._read_until(fd, want, timeout)
    return m is not None

  def _ensure_login(self, fd, timeout=20.0, wait=0.0):
    """Get the serial console to a live shell prompt, logging in with the
    device's default comma/comma credentials if it's sitting at login:.

    `wait` (seconds) lets the caller wait for the device to *finish booting*
    before running: see `_wait_for_boot`. With the default wait=0, behaviour is
    the original fixed ~12 s nudge window (fail fast if no prompt)."""
    if wait > 0:
      if self._wait_for_boot(fd, wait):
        return  # handshake already confirmed a live shell
      raise SystemExit(f"device did not reach a live shell within {wait:.0f}s of boot")
    self._login(fd, timeout)

  def _login(self, fd, timeout=20.0):
    """Drive the console from whatever prompt it's at (login:, Password:, or an
    existing shell) to a shell prompt, logging in with comma/comma as needed."""
    any_prompt = re.compile(rb"login:\s*$|[Pp]assword:\s*$|[#\$]\s*$")
    # Don't pre-drain: the prompt may already be sitting in the buffer, and an
    # idle getty/emergency shell can take several seconds (or a few nudges) to
    # echo. Nudge with newlines for up to ANY_PROMPT_TIMEOUT s rather than
    # firing one \n and giving up after 3 s — that 3 s window was the dominant
    # "no response from serial console" false negative.
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

  def exec(self, script, timeout=30.0, wait=0.0, _tries=2):
    """Run a bash script on the device over the serial console and return its
    output + exit code. The script may be arbitrary multi-line (heredocs,
    embedded python3, quotes, etc.).

    Wire protocol: the script is base64'd on the host so only a single line
    crosses the line-oriented console (no quoting/heredoc hazards). On the device
    it's decoded, run under bash, and its stdout+stderr piped through
    `gzip | base64` before crossing back — 115200 baud (~7.5 KB/s effective) is
    the bottleneck, so device-side compression is ~3x faster on large output. The
    device frames the blob with a per-call nonce marker + exact byte count; the
    host slices, verifies the length, gunzips (bytes are exact). Exit code rides
    back in the end marker. See _exec_once / _extract for details.

    Transient serial failures (idle console, dropped/garbled frame) are retried
    once with a fresh login. `wait` (seconds) waits for a booting device to reach
    a shell before running (0 = fail fast). Logs in comma/comma at a login
    prompt. Needs gzip + base64 on the device PATH (AGNOS has both)."""
    # When waiting for a boot, allow more retries: the console is often flaky in
    # the first seconds after a shell appears, so a single resend isn't enough.
    tries = max(_tries, 3) if wait > 0 else _tries
    last_err = None
    for attempt in range(tries):
      fd = self.open_serial()
      try:
        # First attempt waits out the boot; retries re-confirm a live shell with
        # a short handshake (the just-booted console can be transiently flaky)
        # rather than blindly resending into it.
        self._ensure_login(fd, wait=wait if attempt == 0 else (8.0 if wait > 0 else 0.0))
        self._drain(fd)
        return self._exec_once(fd, script, timeout)
      except _Transient as e:
        last_err = e
        if attempt + 1 < tries:
          _log(f"serial round-trip failed ({e}) — retrying with a fresh login ({attempt + 2}/{tries})")
      finally:
        os.close(fd)
    raise SystemExit(str(last_err))

  # A per-call nonce makes the framing immune to the echoed command line: the
  # host sends the marker as printf format + arg (`printf 'MDMABEG%s' <nonce>`),
  # so the *concatenated* token only appears in real output, never in the echo.
  # The old fixed `__MDMA_BEG_837__` literal appeared verbatim in the echo, and a
  # drained/clipped echo would make rfind() land inside it and pull the input
  # script's base64 into the blob — the dominant base64-corruption failure.
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
    self._drain(fd)

    # boot!
    start = self.reboot(qdl=False)

    # discard the adapter's replay of stale console bytes right after power-on
    while time.monotonic() - start < 0.5:
      if not select.select([fd], [], [], 0.15)[0]:
        break
      os.read(fd, 4096)

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


def flash_flow(args):
  """Whole kernel-iteration loop in one command: QDL -> run the host flash
  command -> boot to a confirmed live shell -> optionally run a verify command
  on the device. Prints per-stage timings."""
  if args.argv and args.argv[0] == "--":
    args.argv = args.argv[1:]
  if not args.argv:
    raise SystemExit("flash: provide the host flash command, e.g.: mdma.py flash --verify 'uname -r' -- <kernel-flash-command>")
  m = Mdma()
  stages = []

  def stage(name, fn):
    _log(f"stage {name}: starting")
    t0 = time.monotonic()
    fn()
    stages.append((name, time.monotonic() - t0))
    _log(f"stage {name}: done in {stages[-1][1]:.1f}s")

  stage("qdl", m.qdl)
  def run_flasher():
    # single arg -> shell string; multiple args -> exec list
    cmd = args.argv[0] if len(args.argv) == 1 else args.argv
    _log(f"running host flash command: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    rc = subprocess.call(cmd, shell=len(args.argv) == 1)
    if rc != 0:
      raise SystemExit(f"flash command failed (exit {rc}) — device is still in QDL; fix and rerun, or `mdma.py boot` to boot it back out")
  stage("flash", run_flasher)
  stage("boot", m.boot)
  rc = 0
  if args.verify:
    def run_verify():
      nonlocal rc
      rc = m.exec(args.verify, timeout=args.timeout)
      if rc != 0:
        _log(f"verify command exited {rc}")
    stage("verify", run_verify)
  total = sum(t for _, t in stages)
  print("── flash loop " + " ".join(f"{n}={t:.1f}s" for n, t in stages) + f" total={total:.1f}s", file=sys.stderr)
  return rc


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
  return Mdma().exec(script, timeout=args.timeout, wait=args.wait)


if __name__ == "__main__":
  cmds = {
    "boot":         (lambda a: Mdma().boot(), "power-cycle (verified) and wait until the device is at a live shell, ready for bash"),
    "qdl":          (lambda a: Mdma().qdl(), "force QDL mode (aux-first power cycle) for flashing"),
    "flash":        (flash_flow, "full iteration: QDL -> run host flash command -> boot to live shell [-> --verify CMD]"),
    "bash":         (bash_script, "run a bash script on the device over serial and print its output"),
    "reboot":       (lambda a: (Mdma().reboot(qdl=False), _log("power cycle issued (normal boot) — returned immediately, boot NOT verified/awaited (use `boot` for that)"))[0],
                     "raw power-cycle into normal boot (returns immediately, unverified)"),
    "off":          (lambda a: (Mdma().power_off(), _log("SOC power cut (VIN + aux off)"))[0],
                     "cut power to the SOC (VIN + aux off)"),
    "serial":       (lambda a: Mdma().serial(), "open the MSM UART console with screen"),
    "profile-boot": (lambda a: Mdma().profile_boot(), "reboot comma four and profile boot time"),
  }
  aliases = {"reboot-qdl": "qdl"}  # back-compat (agnos-builder scripts)

  parser = argparse.ArgumentParser()
  parser.add_argument("--missing-ok", action="store_true", help="continue successfully when no MDMA is connected")
  subparsers = parser.add_subparsers(dest="command", required=True)
  for cmd, (_, hlp) in cmds.items():
    names = [cmd] + [a for a, tgt in aliases.items() if tgt == cmd]
    sp = subparsers.add_parser(cmd, aliases=names[1:], help=hlp)
    if cmd == "bash":
      sp.add_argument("argv", nargs="*", help="the bash command to run inline; or '-' to read the script from stdin")
      sp.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for output (default 30)")
      sp.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                      help="wait up to SECONDS for a booting device to reach a shell prompt before running (default 0 = fail fast)")
    if cmd == "flash":
      sp.add_argument("argv", nargs=argparse.REMAINDER,
                      help="host flash command to run while in QDL (prefix with -- ); one arg = run via shell")
      sp.add_argument("--verify", metavar="CMD", help="bash command to run on the device (over serial) after boot")
      sp.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for --verify output (default 30)")
  if len(sys.argv) == 1:
    parser.print_help()
    raise SystemExit(0)
  args = parser.parse_args()
  args.command = aliases.get(args.command, args.command)

  if not Mdma().available():
    print("MDMA not found.")
    raise SystemExit(0 if args.missing_ok else 1)

  rc = cmds[args.command][0](args)
  # bool guard: boot/qdl return True on success, which is an int subclass and
  # would otherwise become exit code 1.
  if isinstance(rc, int) and not isinstance(rc, bool):
    raise SystemExit(rc)
