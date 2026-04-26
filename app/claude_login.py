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
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._logging import make_logger

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CLAUDE_BIN = shutil.which("claude") or "/usr/local/share/npm-global/bin/claude"

_log = make_logger("claude_login")

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
# Abandoned login flows (URL fetched but no code submitted) hold a pty
# and a claude process forever. Watchdog kills any awaiting_code state
# older than this and resets to idle.
ABANDON_TIMEOUT = 10 * 60  # 10 minutes
# `claude --print` smoke test for verify endpoint.
VERIFY_TIMEOUT = 20

PTY_COLS = 1000
PTY_ROWS = 50


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


def _check_watchdog() -> None:
    """Kill any awaiting_code flow older than ABANDON_TIMEOUT."""
    global _state
    if (
        _state.status == "awaiting_code"
        and _state.started_at
        and time.time() - _state.started_at > ABANDON_TIMEOUT
    ):
        age = time.time() - _state.started_at
        _log(f"watchdog: awaiting_code is {age:.0f}s old, abandoning")
        _cleanup()
        _state = LoginState(
            status="failed",
            error=(
                f"Login flow abandoned ({age / 60:.0f} min idle). "
                "Click 'Log in to Claude' to start over."
            ),
            finished_at=time.time(),
        )


def current_state() -> LoginState:
    _check_watchdog()
    return _state


def logged_in() -> bool:
    return CREDENTIALS_FILE.exists()


async def verify() -> tuple[bool, str]:
    """Make a real Claude API call with the stored credentials.

    Spawns `claude --print` with a tiny prompt; success = response received,
    failure = no response within VERIFY_TIMEOUT or claude exited with error.

    Returns (ok, message).
    """
    if not logged_in():
        return False, "Not logged in (no credentials file)."

    _log("verify: spawning claude --print to confirm credentials work")
    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "--print",
            "Reply with the single word OK.",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TERM": "dumb", "HOME": str(Path.home())},
        )
    except FileNotFoundError as exc:
        return False, f"could not spawn claude: {exc}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=VERIFY_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _log(f"verify: timeout after {VERIFY_TIMEOUT}s")
        return False, f"claude --print did not respond within {VERIFY_TIMEOUT}s"

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    _log(
        f"verify: exit={proc.returncode} stdout_len={len(stdout)} stderr_len={len(stderr)}"
    )
    if proc.returncode == 0 and stdout:
        return True, f"claude responded: {stdout[:200]}"
    return False, (
        f"claude exited code={proc.returncode}. "
        f"stdout: {stdout[:300] or '(empty)'}\n"
        f"stderr: {stderr[:300] or '(empty)'}"
    )


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
    _log(f"start: spawning {CLAUDE_BIN} auth login (pty {PTY_COLS}x{PTY_ROWS})")
    try:
        process = subprocess.Popen(
            # `claude auth login` is the documented current command (claude
            # 2.1.119). The bare `claude login` alias completes OAuth but
            # doesn't populate oauthAccount.organizationUuid in .claude.json,
            # which `claude remote-control` needs to determine eligibility —
            # surfaced as "Unable to determine your organization for Remote
            # Control eligibility" the first time we tried to spawn RC.
            [CLAUDE_BIN, "auth", "login"],
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
        # claude.com almost certainly enables bracketed-paste mode in its
        # input prompt (most modern TUIs do). Real terminals wrap pasted
        # text in `\e[200~ ... \e[201~` so the app knows it's a paste vs
        # individual keystrokes. Without those markers a bulk write of
        # >80 chars gets misinterpreted (verified: 38-char bogus rejected
        # in 100ms, 86-char bogus times out at 30s with only the echoed
        # `*`s).
        BRACKETED_PASTE_START = b"\x1b[200~"
        BRACKETED_PASTE_END = b"\x1b[201~"
        os.write(
            _state.master_fd,
            BRACKETED_PASTE_START + code.encode() + BRACKETED_PASTE_END + b"\r",
        )
        _log(f"submit: code written as bracketed paste ({len(code)} bytes + paste markers + \\r)")
    except OSError as exc:
        _state.status = "failed"
        _state.error = f"failed to write code: {exc}"
        _log(f"submit: write failed: {exc}")
        _cleanup()
        return _state

    # Patterns claude prints when it rejects a code. Verified by direct
    # test (curl POST with a bogus code): claude responded within 100ms
    # with "OAuth error: Invalid code. Please make sure the full code
    # was copied" and stayed alive re-prompting. Without this detection
    # we'd wait the full timeout for nothing.
    REJECTION_PATTERNS = ("oauth error", "invalid code")

    os.set_blocking(_state.master_fd, False)
    deadline = time.time() + CODE_WAIT_TIMEOUT
    last_log_size = pre_submit_buf_size
    rejected_pattern: Optional[str] = None
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
            # Check rejection patterns ONLY in bytes received after we
            # wrote the code, to avoid matching pre-existing text.
            new_text_lower = (
                _strip_ansi(_state.stdout_buf[pre_submit_buf_size:])
                .decode("utf-8", errors="replace")
                .lower()
            )
            for pat in REJECTION_PATTERNS:
                if pat in new_text_lower:
                    rejected_pattern = pat
                    break
            if rejected_pattern:
                _log(f"submit: detected rejection pattern {rejected_pattern!r}, failing fast")
                break
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
        if rejected_pattern is not None:
            _state.error = (
                f"Claude rejected the code (matched: {rejected_pattern!r}). "
                f"Likely an expired or mistyped code — restart for a fresh URL "
                f"and try again.\n\nCaptured output:\n{tail}"
            )
            _log(f"submit: FAILED — rejected ({rejected_pattern!r}) in {elapsed:.1f}s")
        elif exit_code is not None:
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
