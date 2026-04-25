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
    waits for the credentials file to land (or process to exit).

Single-user deploy: module-level singleton state. If the FastAPI process
restarts mid-flow, state is dropped; user just restarts the login.

Logging: every interesting step prints to stdout with a `[claude_login]`
prefix and a millisecond timestamp. Uvicorn captures stdout, so Coolify's
app log shows the full picture. We deliberately do NOT log the OAuth
code, credentials, or full URL contents — only metadata (lengths, etc).
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

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
# Claude also keeps a user-level config outside the .claude dir and a
# backups/ subdir. If login is starting fresh we wipe these so claude
# doesn't hang on a "config-not-found, restore from backup?" prompt.
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_BACKUPS_DIR = Path.home() / ".claude" / "backups"

CLAUDE_BIN = shutil.which("claude") or "/usr/local/share/npm-global/bin/claude"

OAUTH_URL_RE = re.compile(
    r"https://(?:claude\.com|claude\.ai|console\.anthropic\.com)"
    r"/[a-zA-Z0-9_\-./?=&%+:#]+oauth/authorize[a-zA-Z0-9_\-./?=&%+:#]*"
)
_CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
_OSC_RE = re.compile(rb"\x1b\][^\x07]*\x07")

URL_WAIT_TIMEOUT = 60
# Code submission is normally near-instant once claude reads the code
# from stdin and validates it against Anthropic. We give 30s as a
# generous-but-still-fast cap.
CODE_WAIT_TIMEOUT = 30

PTY_COLS = 1000
PTY_ROWS = 50


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
    print(f"[claude_login {ts}] {msg}", flush=True, file=sys.stdout)


def _safe_preview(b: bytes, n: int = 240) -> str:
    """Strip ANSI + replace non-printable, return a short repr suitable for logs."""
    s = _strip_ansi(b).decode("utf-8", errors="replace")
    s = "".join(ch if (ch.isprintable() or ch in "\n\r\t ") else "·" for ch in s)
    s = s.replace("\n", "⏎").replace("\r", "")
    if len(s) > n:
        return s[:n] + f" …({len(s) - n} more chars)"
    return s


@dataclass
class LoginState:
    status: str = "idle"  # idle | awaiting_code | submitting | success | failed
    url: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
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
    if _state.master_fd is not None:
        try:
            os.close(_state.master_fd)
        except OSError:
            pass
    _state.process = None
    _state.master_fd = None


async def _read_until_url(master_fd: int, deadline: float) -> Optional[str]:
    """Read pty stdout until we match an OAuth URL or deadline expires.

    Auto-Enter every 2s so claude's first-run prompts (theme, etc) accept
    defaults without user action.
    """
    os.set_blocking(master_fd, False)
    last_enter = 0.0
    last_log_size = 0
    ENTER_EVERY = 2.0
    iters_with_data = 0
    while time.time() < deadline:
        try:
            chunk = os.read(master_fd, 4096)
        except BlockingIOError:
            chunk = None
        except OSError as e:
            _log(f"start: read OSError ({e}); aborting")
            return None
        if chunk == b"":
            _log("start: pty closed (EOF) — process exited before URL")
            return None
        if chunk:
            _state.stdout_buf += chunk
            iters_with_data += 1
            # Log new-bytes preview when buffer grows by >= 50 bytes
            if len(_state.stdout_buf) - last_log_size >= 50:
                new_bytes = _state.stdout_buf[last_log_size:]
                _log(f"start: +{len(new_bytes)}B (total {len(_state.stdout_buf)}B): {_safe_preview(new_bytes)}")
                last_log_size = len(_state.stdout_buf)
            text = _strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace")
            unwrapped = re.sub(r"(?<=[^\s])\r?\n(?=[^\s/])", "", text)
            match = OAUTH_URL_RE.search(unwrapped)
            if match:
                url = match.group(0)
                _log(f"start: URL extracted, len={len(url)}, host={url.split('/')[2]}")
                return url
        now = time.time()
        if now - last_enter > ENTER_EVERY:
            try:
                os.write(master_fd, b"\r")
                _log("start: auto-Enter sent")
            except OSError as e:
                _log(f"start: auto-Enter failed ({e})")
            last_enter = now
        await asyncio.sleep(0.1)
    _log(f"start: deadline reached after {URL_WAIT_TIMEOUT}s, no URL found, total {len(_state.stdout_buf)}B captured")
    return None


async def start_login() -> LoginState:
    """Always spawn a fresh `claude login`. Any in-flight process is killed.

    Idempotency was a previous design choice but it broke the "Restart
    (get a fresh URL)" button, which hits this endpoint expecting a new
    URL each time. Cleanup-then-spawn is the right behavior.
    """
    global _state

    if _state.process is not None:
        _log("start: prior process exists; cleaning up before fresh spawn")
    _cleanup()
    # Purge residual claude state so login starts clean.
    purged = []
    try:
        CLAUDE_JSON.unlink()
        purged.append(str(CLAUDE_JSON))
    except FileNotFoundError:
        pass
    if CLAUDE_BACKUPS_DIR.exists():
        shutil.rmtree(CLAUDE_BACKUPS_DIR, ignore_errors=True)
        purged.append(str(CLAUDE_BACKUPS_DIR))
    if purged:
        _log(f"start: purged residual state: {purged}")

    _state = LoginState(status="awaiting_code", started_at=time.time())

    master, slave = pty.openpty()
    fcntl.ioctl(
        master,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0),
    )
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    _log(f"start: spawning {CLAUDE_BIN} login (pty {PTY_COLS}x{PTY_ROWS})")
    try:
        process = subprocess.Popen(
            [CLAUDE_BIN, "login"],
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
        _state.error = f"failed to spawn claude: {exc}"
        _state.finished_at = time.time()
        _log(f"start: spawn failed: {exc}")
        return _state
    os.close(slave)
    _log(f"start: spawned pid={process.pid}")

    _state.process = process
    _state.master_fd = master

    deadline = time.time() + URL_WAIT_TIMEOUT
    url = await _read_until_url(master, deadline)
    if url is None:
        tail = (_strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace"))[-1500:]
        exit_code = _state.process.poll() if _state.process else None
        _state.status = "failed"
        _state.error = (
            f"OAuth URL did not appear within {URL_WAIT_TIMEOUT}s "
            f"(claude exit code={exit_code!r}). Captured output:\n{tail}"
        )
        _state.finished_at = time.time()
        _log(f"start: failed (exit={exit_code!r}), elapsed={time.time() - _state.started_at:.1f}s")
        _cleanup()
        return _state

    _state.url = url
    _log(f"start: success, elapsed={time.time() - _state.started_at:.1f}s")
    return _state


async def submit_code(code: str) -> LoginState:
    """Pipe the OAuth callback code to claude's stdin, wait for completion.

    Done as soon as either (a) credentials file appears or (b) process
    exits or (c) timeout. NO auto-Enter, NO failure-pattern matching:
    just observe and report.
    """
    global _state
    submit_started = time.time()

    if _state.status != "awaiting_code":
        _log(f"submit: rejected — status={_state.status!r}")
        _state.error = f"not awaiting code (state={_state.status!r})"
        return _state
    if _state.process is None or _state.master_fd is None:
        _log("submit: rejected — pty/process state missing")
        _state.status = "failed"
        _state.error = "internal: pty state missing"
        return _state

    code = code.strip()
    if not code:
        _log("submit: rejected — empty code")
        _state.error = "code is empty"
        return _state

    _log(f"submit: writing code (len={len(code)}) to pid={_state.process.pid} stdin")
    _state.status = "submitting"
    pre_submit_buf_size = len(_state.stdout_buf)
    try:
        # claude reads the code prompt in raw/non-canonical mode (it echoes
        # each char as `*`). In that mode the kernel does NOT translate \n
        # to "Enter" — only \r does. Confirmed by logs: sending \n left
        # claude blocked indefinitely while \r as the auto-Enter during
        # start_login successfully advances the prompt.
        os.write(_state.master_fd, (code + "\r").encode())
        _log("submit: code written (with \\r terminator)")
    except OSError as exc:
        _state.status = "failed"
        _state.error = f"failed to write code: {exc}"
        _log(f"submit: write failed: {exc}")
        _cleanup()
        return _state

    os.set_blocking(_state.master_fd, False)
    deadline = time.time() + CODE_WAIT_TIMEOUT
    last_log_size = pre_submit_buf_size
    while time.time() < deadline:
        if logged_in():
            _log("submit: credentials file appeared")
            break
        exit_code = _state.process.poll()
        if exit_code is not None:
            _log(f"submit: process exited (code={exit_code})")
            break
        try:
            chunk = os.read(_state.master_fd, 4096)
        except BlockingIOError:
            chunk = None
        except OSError as e:
            _log(f"submit: read OSError ({e}); aborting wait")
            break
        if chunk:
            _state.stdout_buf += chunk
            if len(_state.stdout_buf) - last_log_size >= 50:
                new_bytes = _state.stdout_buf[last_log_size:]
                _log(f"submit: +{len(new_bytes)}B (total {len(_state.stdout_buf)}B): {_safe_preview(new_bytes)}")
                last_log_size = len(_state.stdout_buf)
        await asyncio.sleep(0.1)

    elapsed = time.time() - submit_started
    if logged_in():
        _state.status = "success"
        _state.error = None
        _log(f"submit: SUCCESS in {elapsed:.1f}s")
    else:
        tail = (_strip_ansi(_state.stdout_buf).decode("utf-8", errors="replace"))[-1500:]
        exit_code = _state.process.poll() if _state.process else None
        _state.status = "failed"
        if exit_code is not None:
            _state.error = (
                f"claude exited (code={exit_code}) without writing credentials.\n\n"
                f"Captured output:\n{tail}"
            )
            _log(f"submit: FAILED — exited code={exit_code} after {elapsed:.1f}s")
        else:
            _state.error = (
                f"claude did not finish within {CODE_WAIT_TIMEOUT}s and "
                f"credentials weren't written.\n\nCaptured output:\n{tail}"
            )
            _log(f"submit: FAILED — timeout after {elapsed:.1f}s, process still alive (pid={_state.process.pid if _state.process else None})")
    _state.finished_at = time.time()
    _cleanup()
    return _state


async def logout() -> None:
    """Remove claude's credentials file. Any running login is killed."""
    global _state
    _log("logout: cleaning up state and removing credentials")
    _cleanup()
    _state = LoginState(status="idle")
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        _log("logout: credentials removed")
    else:
        _log("logout: no credentials file to remove")
