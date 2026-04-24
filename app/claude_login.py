"""Wrap the interactive `claude login` CLI flow behind HTML forms.

The native flow:
  1. user runs `claude login`
  2. claude prints an OAuth URL + waits on a "paste code" prompt
  3. user opens URL in a browser, authorizes, receives a callback code
  4. user types the code + Enter; claude verifies and writes credentials

Our wrap keeps (1) and (3) exactly as-is but replaces the terminal parts
with HTML:
  - "Log in to Claude" button -> backend spawns `claude login` under a pty,
    captures stdout, regex-extracts the URL, stores it in module state.
  - status page renders the URL as a tap-able link (mobile friendly) +
    a text input for the callback code.
  - "Submit code" form -> backend writes the code to the pty's stdin,
    waits for the process to exit, checks that the credentials file
    was written, reports success.

Single-user deploy: module-level singleton state. If the FastAPI process
restarts mid-flow, state is dropped; user just restarts the login.
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
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
# Claude also keeps a user-level config outside the .claude dir and a
# backups/ subdir. If login is starting fresh we wipe these so claude
# doesn't hang on a "config-not-found, restore from backup?" prompt.
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_BACKUPS_DIR = Path.home() / ".claude" / "backups"

# Resolve `claude` once at import time so per-request spawns don't depend
# on whatever PATH subprocess inherits. Fallback to the devcontainer
# default install location.
CLAUDE_BIN = shutil.which("claude") or "/usr/local/share/npm-global/bin/claude"
# claude emits an authorize URL either on claude.com or claude.ai; both
# have been seen in the wild depending on account migration status.
OAUTH_URL_RE = re.compile(
    r"https://(?:claude\.com|claude\.ai|console\.anthropic\.com)"
    r"/[a-zA-Z0-9_\-./?=&%+:#]+oauth/authorize[a-zA-Z0-9_\-./?=&%+:#]*"
)
# strip CSI (\x1b[ ... letter) and OSC (\x1b] ... \x07) sequences that
# claude uses for colors, cursor movement, title changes. Everything else
# is left as-is; our URL regex does its own filtering.
_CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
_OSC_RE = re.compile(rb"\x1b\][^\x07]*\x07")

# read timeouts (seconds) — generous so slow hosts aren't a problem
URL_WAIT_TIMEOUT = 60
CODE_WAIT_TIMEOUT = 30

# pty geometry — wide so claude doesn't hard-wrap the OAuth URL mid-char.
# URLs are ~450 chars with state+challenge+scopes, so 1000 gives margin.
PTY_COLS = 1000
PTY_ROWS = 50


@dataclass
class LoginState:
    """Current state of a login flow.  `process` and `master_fd` are
    populated only while the pty is alive; once we finish we close them."""

    status: str = "idle"  # idle | awaiting_code | submitting | success | failed
    url: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # runtime-only
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    master_fd: Optional[int] = field(default=None, repr=False)
    stdout_buf: bytes = field(default=b"", repr=False)


_state = LoginState()


def current_state() -> LoginState:
    return _state


def logged_in() -> bool:
    return CREDENTIALS_FILE.exists()


def _strip_ansi(data: bytes) -> bytes:
    return _OSC_RE.sub(b"", _CSI_RE.sub(b"", data))


def _cleanup() -> None:
    """Kill any lingering claude-login process and close the pty."""
    global _state
    if _state.process is not None:
        try:
            os.killpg(os.getpgid(_state.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            _state.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(_state.process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    if _state.master_fd is not None:
        try:
            os.close(_state.master_fd)
        except OSError:
            pass
    _state.process = None
    _state.master_fd = None


async def _read_until_url(master_fd: int, deadline: float) -> Optional[str]:
    """Read pty stdout until we match an OAuth URL or deadline expires.

    Claude Code's first-run flow (v2.1.119) prompts for theme selection
    before showing the OAuth URL. We accept defaults by pressing Enter
    periodically while waiting for the URL to appear — harmless once
    login output starts (extra Enter just becomes an empty stdin line).
    """
    os.set_blocking(master_fd, False)
    last_enter = 0.0
    ENTER_EVERY = 2.0
    while time.time() < deadline:
        try:
            chunk = os.read(master_fd, 4096)
        except BlockingIOError:
            chunk = None
        except OSError:
            return None
        if chunk == b"":
            # pty closed — process exited
            return None
        if chunk:
            _state.stdout_buf += chunk
            text = _strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace")
            unwrapped = re.sub(r"(?<=[^\s])\r?\n(?=[^\s/])", "", text)
            match = OAUTH_URL_RE.search(unwrapped)
            if match:
                return match.group(0)
        # Periodically press Enter to accept any default on a setup prompt.
        now = time.time()
        if now - last_enter > ENTER_EVERY:
            try:
                os.write(master_fd, b"\r")
            except OSError:
                pass
            last_enter = now
        await asyncio.sleep(0.1)
    return None


def _wait_for_exit(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


async def start_login() -> LoginState:
    """Spawn `claude login` in a pty, wait for the OAuth URL to appear.

    Idempotent when called during an active awaiting_code flow: returns
    the existing state without spawning a second process. Logged-in
    callers should check `logged_in()` first and not call this.
    """
    global _state

    if _state.status == "awaiting_code":
        return _state

    # Clear any terminal state from a prior attempt.
    _cleanup()
    # Purge residual claude state so login starts clean. Otherwise claude
    # hangs on a "config not found — restore from backup?" interactive
    # prompt from leftovers on persistent volumes.
    try:
        CLAUDE_JSON.unlink()
    except FileNotFoundError:
        pass
    if CLAUDE_BACKUPS_DIR.exists():
        shutil.rmtree(CLAUDE_BACKUPS_DIR, ignore_errors=True)

    _state = LoginState(status="awaiting_code", started_at=time.time())

    master, slave = pty.openpty()
    # Widen the pty before claude starts; it reads dimensions at spawn
    # via TIOCGWINSZ and sizes its output accordingly.
    fcntl.ioctl(
        master,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0),
    )
    env = dict(os.environ)
    env["TERM"] = "dumb"  # suppress most ANSI; our stripper is defense-in-depth
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    try:
        process = subprocess.Popen(
            [CLAUDE_BIN, "login"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,  # own process group so we can SIGTERM the tree
            close_fds=True,
        )
    except (FileNotFoundError, OSError) as exc:
        os.close(master)
        os.close(slave)
        _state.status = "failed"
        _state.error = f"failed to spawn claude: {exc}"
        _state.finished_at = time.time()
        return _state
    os.close(slave)

    _state.process = process
    _state.master_fd = master

    deadline = time.time() + URL_WAIT_TIMEOUT
    url = await _read_until_url(master, deadline)
    if url is None:
        # Capture a tail of whatever claude did emit for debugging.
        tail = (
            _strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace")
        )[-1500:]
        exit_code = _state.process.poll() if _state.process else None
        _state.status = "failed"
        _state.error = (
            f"OAuth URL did not appear within {URL_WAIT_TIMEOUT}s "
            f"(claude exit code={exit_code!r}). Captured output:\n{tail}"
        )
        _state.finished_at = time.time()
        _cleanup()
        return _state

    _state.url = url
    return _state


async def submit_code(code: str) -> LoginState:
    """Pipe the OAuth callback code to claude's stdin, wait for exit."""
    global _state

    if _state.status != "awaiting_code":
        _state.error = f"not awaiting code (state={_state.status!r})"
        return _state
    if _state.process is None or _state.master_fd is None:
        _state.status = "failed"
        _state.error = "internal: pty state missing"
        return _state

    code = code.strip()
    if not code:
        _state.error = "code is empty"
        return _state

    _state.status = "submitting"
    try:
        os.write(_state.master_fd, (code + "\n").encode())
    except OSError as exc:
        _state.status = "failed"
        _state.error = f"failed to write code: {exc}"
        _cleanup()
        return _state

    # Drain stdout (non-blocking) while waiting for claude to exit.
    os.set_blocking(_state.master_fd, False)
    deadline = time.time() + CODE_WAIT_TIMEOUT
    while time.time() < deadline:
        if _state.process.poll() is not None:
            break
        try:
            chunk = os.read(_state.master_fd, 4096)
        except BlockingIOError:
            chunk = None
        except OSError:
            break
        if chunk:
            _state.stdout_buf += chunk
        await asyncio.sleep(0.1)

    if _state.process.poll() is None:
        _state.status = "failed"
        _state.error = f"claude did not finish within {CODE_WAIT_TIMEOUT}s"
        _cleanup()
        return _state

    # success is detected by the existence of the credentials file on disk.
    if logged_in():
        _state.status = "success"
        _state.error = None
    else:
        _state.status = "failed"
        _state.error = (
            "claude exited without writing credentials. "
            "Likely an invalid or expired code; try again."
        )
    _state.finished_at = time.time()
    _cleanup()
    return _state


async def logout() -> None:
    """Remove claude's credentials file. Any running login is killed."""
    global _state
    _cleanup()
    _state = LoginState(status="idle")
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
