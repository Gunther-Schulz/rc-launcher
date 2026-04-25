"""Wrap the interactive `gh auth login --web` CLI flow behind HTML.

The native flow:
  1. user runs `gh auth login --web -h github.com -p https`
  2. gh prints a one-time device code (e.g. `ABCD-1234`)
  3. gh prints a github.com URL and waits
  4. user opens URL in a browser, types/pastes the code, authorizes
  5. gh polls GitHub in the background and writes ~/.config/gh/hosts.yml
     once authorization is detected
  6. gh exits 0

Our wrap captures (2) and (3) from gh's stdout and renders them as a
big copyable code + tap-able link. The gh process keeps running in the
background; the /gh page auto-refreshes and we poll the process on each
request. When gh exits 0 we transition to success.

Single-user deploy: module-level singleton state, same pattern as
claude_login.py. Restart of the FastAPI process drops state; user
restarts the login.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

GH_BIN = shutil.which("gh") or "/usr/bin/gh"
HOSTS_FILE = Path.home() / ".config" / "gh" / "hosts.yml"

# gh prints the code on a line like: `! First copy your one-time code: XXXX-XXXX`
GH_CODE_RE = re.compile(r"one[- ]time code:\s*([A-Z0-9-]{4,16})", re.IGNORECASE)
# gh prints the URL like: `Open this URL in your browser: https://github.com/login/device`
GH_URL_RE = re.compile(r"https://github\.com/login/device\b")

_CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
_OSC_RE = re.compile(rb"\x1b\][^\x07]*\x07")

URL_WAIT_TIMEOUT = 30
ABANDON_TIMEOUT = 15 * 60  # gh polls github for ~15 min before giving up
PTY_COLS = 200
PTY_ROWS = 50


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
    print(f"[gh_login {ts}] {msg}", flush=True, file=sys.stdout)


def _safe_preview(b: bytes, n: int = 240) -> str:
    s = _strip_ansi(b).decode("utf-8", errors="replace")
    s = "".join(ch if (ch.isprintable() or ch in "\n\r\t ") else "·" for ch in s)
    s = s.replace("\n", "⏎").replace("\r", "")
    if len(s) > n:
        return s[:n] + f" …({len(s) - n} more chars)"
    return s


def _strip_ansi(data: bytes) -> bytes:
    return _OSC_RE.sub(b"", _CSI_RE.sub(b"", data))


@dataclass
class GhState:
    status: str = "idle"  # idle | awaiting_authorization | success | failed
    device_code: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    master_fd: Optional[int] = field(default=None, repr=False)
    stdout_buf: bytes = field(default=b"", repr=False)


_state = GhState()


def _check_watchdog() -> None:
    """If a login flow has been awaiting for too long, mark it failed."""
    global _state
    if (
        _state.status == "awaiting_authorization"
        and _state.started_at
        and time.time() - _state.started_at > ABANDON_TIMEOUT
    ):
        age = time.time() - _state.started_at
        _log(f"watchdog: awaiting_authorization is {age:.0f}s old, abandoning")
        _cleanup()
        _state = GhState(
            status="failed",
            error=(
                f"GitHub login flow abandoned ({age / 60:.0f} min idle). "
                "Click 'Log in to GitHub' to start over."
            ),
            finished_at=time.time(),
        )


def _poll_process_state() -> None:
    """If a background gh process has exited, update state accordingly."""
    global _state
    if _state.status != "awaiting_authorization" or _state.process is None:
        return
    exit_code = _state.process.poll()
    if exit_code is None:
        return
    # Drain any final stdout
    if _state.master_fd is not None:
        try:
            os.set_blocking(_state.master_fd, False)
            while True:
                try:
                    chunk = os.read(_state.master_fd, 4096)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    break
                _state.stdout_buf += chunk
        except OSError:
            pass

    if exit_code == 0 and logged_in():
        _state.status = "success"
        _state.error = None
        _state.finished_at = time.time()
        _log(f"poll: gh exited 0 and hosts.yml present — SUCCESS")
    else:
        tail = (_strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace"))[-1500:]
        _state.status = "failed"
        _state.error = (
            f"gh auth login exited (code={exit_code}) without writing hosts.yml.\n\n"
            f"Captured output:\n{tail}"
        )
        _state.finished_at = time.time()
        _log(f"poll: gh exited {exit_code}, no hosts.yml — FAILED")
    _cleanup_handles_only()


def _cleanup_handles_only() -> None:
    """Close pty fd but leave _state.process reference for poll()."""
    global _state
    if _state.master_fd is not None:
        try:
            os.close(_state.master_fd)
        except OSError:
            pass
        _state.master_fd = None


def _cleanup() -> None:
    """Kill any lingering gh process and close the pty."""
    global _state
    if _state.process is not None:
        try:
            os.killpg(os.getpgid(_state.process.pid), signal.SIGTERM)
            _log(f"cleanup: SIGTERMed pgid for pid={_state.process.pid}")
        except (ProcessLookupError, PermissionError) as e:
            _log(f"cleanup: SIGTERM skipped ({type(e).__name__})")
        try:
            _state.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_state.process.pid), signal.SIGKILL)
                _log(f"cleanup: SIGKILLed pid={_state.process.pid}")
            except (ProcessLookupError, PermissionError):
                pass
    _cleanup_handles_only()
    _state.process = None


def current_state() -> GhState:
    _check_watchdog()
    _poll_process_state()
    return _state


def logged_in() -> bool:
    """True if gh has a non-empty hosts.yml with at least one host entry."""
    if not HOSTS_FILE.exists():
        return False
    try:
        # hosts.yml must contain "github.com:" or similar; empty file = not logged in
        content = HOSTS_FILE.read_text()
        return "github.com" in content
    except OSError:
        return False


async def _read_until_code_and_url(master_fd: int, deadline: float) -> tuple[Optional[str], Optional[str]]:
    """Read stdout until we have BOTH the device code and the URL, or timeout.

    gh asks 'Authenticate Git with your GitHub credentials? (Y/n)' before
    printing the device code; auto-Enter every 2s accepts that default
    (and any other Y/n we haven't preempted with flags).
    """
    os.set_blocking(master_fd, False)
    last_enter = 0.0
    last_log_size = 0
    ENTER_EVERY = 2.0
    code: Optional[str] = None
    url: Optional[str] = None
    while time.time() < deadline:
        try:
            chunk = os.read(master_fd, 4096)
        except BlockingIOError:
            chunk = None
        except OSError as e:
            _log(f"start: read OSError ({e}); aborting")
            return code, url
        if chunk == b"":
            _log("start: pty closed (EOF) — gh exited before code/URL")
            return code, url
        if chunk:
            _state.stdout_buf += chunk
            if len(_state.stdout_buf) - last_log_size >= 50:
                new_bytes = _state.stdout_buf[last_log_size:]
                _log(f"start: +{len(new_bytes)}B (total {len(_state.stdout_buf)}B): {_safe_preview(new_bytes)}")
                last_log_size = len(_state.stdout_buf)
            text = _strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace")
            if code is None:
                m = GH_CODE_RE.search(text)
                if m:
                    code = m.group(1)
                    _log(f"start: device code extracted: {code}")
            if url is None:
                m = GH_URL_RE.search(text)
                if m:
                    url = m.group(0)
                    _log(f"start: URL extracted: {url}")
            if code and url:
                return code, url
        now = time.time()
        if now - last_enter > ENTER_EVERY:
            try:
                os.write(master_fd, b"\r")
                _log("start: auto-Enter sent")
            except OSError as e:
                _log(f"start: auto-Enter failed ({e})")
            last_enter = now
        await asyncio.sleep(0.1)
    _log(f"start: deadline reached after {URL_WAIT_TIMEOUT}s, code={code!r}, url={url!r}")
    return code, url


async def start_login() -> GhState:
    """Spawn `gh auth login --web` and capture device code + URL.

    The gh process is left RUNNING in the background — it polls GitHub
    until the user authorizes (or 15 min passes). current_state() polls
    its exit on each request.
    """
    global _state

    if _state.process is not None:
        _log("start: prior process exists; cleaning up before fresh spawn")
    _cleanup()

    _state = GhState(status="awaiting_authorization", started_at=time.time())

    master, slave = pty.openpty()
    fcntl.ioctl(
        master,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0),
    )
    env = dict(os.environ)
    # TERM=dumb left gh's prompt unable to consume our \r — gh's TUI
    # library (Bubble Tea / survey) uses real terminal escape codes.
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    # Pre-answer the host + protocol prompts so gh goes straight to web flow.
    cmd = [GH_BIN, "auth", "login", "--web", "--hostname", "github.com", "-p", "https"]
    _log(f"start: spawning {' '.join(cmd)} (pty {PTY_COLS}x{PTY_ROWS})")
    try:
        process = subprocess.Popen(
            cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError) as exc:
        os.close(master)
        os.close(slave)
        _state.status = "failed"
        _state.error = f"failed to spawn gh: {exc}"
        _state.finished_at = time.time()
        _log(f"start: spawn failed: {exc}")
        return _state
    os.close(slave)
    _log(f"start: spawned pid={process.pid}")

    _state.process = process
    _state.master_fd = master

    deadline = time.time() + URL_WAIT_TIMEOUT
    code, url = await _read_until_code_and_url(master, deadline)
    if code is None or url is None:
        tail = (_strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace"))[-1500:]
        exit_code = _state.process.poll() if _state.process else None
        _state.status = "failed"
        _state.error = (
            f"gh did not print device code and/or URL within {URL_WAIT_TIMEOUT}s "
            f"(gh exit={exit_code!r}, code={code!r}, url={url!r}).\n\n"
            f"Captured output:\n{tail}"
        )
        _state.finished_at = time.time()
        _log(f"start: failed (code={code!r}, url={url!r})")
        _cleanup()
        return _state

    _state.device_code = code
    _state.url = url
    _log(f"start: success ({code} + URL captured), gh polling github in bg, elapsed={time.time() - _state.started_at:.1f}s")
    return _state


async def logout() -> GhState:
    """Run `gh auth logout` and reset state."""
    global _state
    _log("logout: cleaning up state and running gh auth logout")
    _cleanup()
    if HOSTS_FILE.exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                GH_BIN, "auth", "logout", "--hostname", "github.com",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                _log(f"logout: gh exited {proc.returncode}, output: {stdout_b.decode(errors='replace').strip()[:200]}")
            except asyncio.TimeoutError:
                proc.kill()
                _log("logout: gh logout timed out")
        except (FileNotFoundError, OSError) as exc:
            _log(f"logout: gh logout spawn failed: {exc}")
        # Whether gh logout worked or not, also wipe the file as a fallback.
        if HOSTS_FILE.exists():
            try:
                HOSTS_FILE.unlink()
                _log("logout: hosts.yml removed as fallback")
            except OSError:
                pass
    _state = GhState(status="idle")
    return _state
