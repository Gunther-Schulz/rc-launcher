"""Session-management primitives for rc-launcher.

A "session" is a tmux process running `claude remote-control` in a git
worktree under /workspace. Slice 1 (this file initially) only covers the
filesystem pipeline:

  ensure_clone(token, owner, repo)
      → /workspace/<owner>/<repo>/main is a clone of github.com/<owner>/<repo>
        kept reasonably fresh (fetch on every prep).

  ensure_worktree(owner, repo, branch)
      → /workspace/<owner>/<repo>/_wt/<safe_branch>/ is a git worktree of
        the given branch, fast-forwarded to origin/<branch>.

  prep_workspace(token, owner, repo, branch) — orchestrates both.

Authentication: the GitHub PAT is embedded into the clone/fetch URL as
`https://x-access-token:<pat>@github.com/...` for the duration of one git
invocation, then scrubbed back to the bare URL via `git remote set-url`.
Tradeoff vs the URL-matched http.extraheader approach: that one is fussy
about exact URL match and doesn't behave identically across git versions
(macOS git accepts a trailing slash, Debian's didn't apply our header).
Embedded credentials are the canonical lowest-friction form — the token
never lands in .git/config because we scrub immediately after.

Layout rationale:
- "/main" subdir for the bare-ish clone (rather than putting .git directly
  at /workspace/<owner>/<repo>/) so the parent dir cleanly holds both the
  clone and the _wt/ tree without naming collisions.
- Branch dir is encoded by replacing '/' with '__' so feature/foo → feature__foo.
  Reversible enough for inspection; we keep the original branch name in metadata.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ._logging import make_logger

WORKSPACE_ROOT = Path("/workspace")
SESSIONS_DIR = Path(os.environ.get("RCL_DATA_DIR", "/var/lib/rcl/data")) / "sessions"
_log = make_logger("sessions")

# Strips CSI / OSC ANSI escapes so the URL regex can match cleanly.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# claude RC prints exactly: "Continue coding in the Claude app or https://claude.ai/code/session_<ULID>"
_RC_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")


# ── path helpers ──────────────────────────────────────────────────────────


def repo_clone_dir(owner: str, repo: str) -> Path:
    return WORKSPACE_ROOT / owner / repo / "main"


def _safe_branch(branch: str) -> str:
    # git refs allow a lot, but POSIX paths don't. '/' is the only common
    # offender for typical branch names (feature/foo, dependabot/x).
    return branch.replace("/", "__")


def worktree_dir(owner: str, repo: str, branch: str) -> Path:
    return WORKSPACE_ROOT / owner / repo / "_wt" / _safe_branch(branch)


# ── git wrapper ───────────────────────────────────────────────────────────


@dataclass
class GitResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


_TOKEN_IN_URL = re.compile(r"https://x-access-token:[^@\s]+@")


def _scrub(s: str) -> str:
    """Remove embedded creds from any github URL that might appear in
    git's stdout/stderr or in our own command-arg logging."""
    return _TOKEN_IN_URL.sub("https://x-access-token:<redacted>@", s)


async def _run_git(
    cwd: Path,
    *args: str,
    timeout: float = 120.0,
    env: Optional[dict] = None,
) -> GitResult:
    """Run `git <args>` in `cwd`. Caller is responsible for embedding any
    needed auth (e.g., via x-access-token URL) — we do NOT inject auth here.

    GIT_TERMINAL_PROMPT=0 so a missing credential surfaces as a clean
    failure rather than hanging waiting for stdin.
    """
    cmd = ["git", *args]
    log_cmd = " ".join(shlex.quote(_scrub(a)) for a in cmd)
    _log(f"git({cwd}): {log_cmd}")

    run_env = {"GIT_TERMINAL_PROMPT": "0", **(env or {})}
    # Inherit PATH etc from parent for git's own subprocess needs.
    import os as _os
    run_env = {**_os.environ, **run_env}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=run_env,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return GitResult(rc=124, stdout="", stderr=f"git timed out after {timeout}s")
    out = _scrub(out_b.decode(errors="replace"))
    err = _scrub(err_b.decode(errors="replace"))
    if proc.returncode != 0:
        _log(f"git rc={proc.returncode}: {err.strip()[:300]}")
    return GitResult(rc=proc.returncode or 0, stdout=out, stderr=err)


def _auth_url(token: str, owner: str, repo: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def _bare_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


# ── public API ────────────────────────────────────────────────────────────


class PrepError(Exception):
    """Raised when prep_workspace can't set up the clone/worktree."""


async def _with_token_url(
    token: str, owner: str, repo: str, target: Path, op
):
    """Run `op` (a coroutine) while origin URL has the token embedded.
    Restores bare URL afterwards in a finally so the token never persists
    in .git/config even if the operation fails."""
    auth = _auth_url(token, owner, repo)
    bare = _bare_url(owner, repo)
    set_r = await _run_git(target, "remote", "set-url", "origin", auth)
    if not set_r.ok:
        raise PrepError(f"could not set authenticated URL: {set_r.stderr.strip()[:300]}")
    try:
        return await op()
    finally:
        await _run_git(target, "remote", "set-url", "origin", bare)


async def ensure_clone(token: str, owner: str, repo: str) -> Path:
    """Make sure /workspace/<owner>/<repo>/main is a usable clone of the
    GitHub repo. Returns the clone path. Fetches if it already exists."""
    target = repo_clone_dir(owner, repo)

    if (target / ".git").exists():
        _log(f"ensure_clone: existing clone at {target}, fetching")
        async def fetch():
            return await _run_git(target, "fetch", "--all", "--prune", timeout=600.0)
        r = await _with_token_url(token, owner, repo, target, fetch)
        if not r.ok:
            raise PrepError(f"git fetch failed: {r.stderr.strip()[:300]}")
        # Make sure HEAD is detached (idempotent) — covers clones created
        # before the detach-on-clone behavior was added.
        await _run_git(target, "checkout", "--detach", "HEAD")
        return target

    _log(f"ensure_clone: cloning github.com/{owner}/{repo} → {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    auth = _auth_url(token, owner, repo)
    r = await _run_git(
        target.parent,
        "clone", "--no-tags", auth, target.name,
        timeout=600.0,  # large repos
    )
    if not r.ok:
        raise PrepError(f"git clone failed: {r.stderr.strip()[:300]}")
    # Scrub token from remote URL immediately.
    await _run_git(target, "remote", "set-url", "origin", _bare_url(owner, repo))
    # Detach HEAD in the main clone so the default branch is free to be
    # checked out as a worktree under _wt/. Without this, the first
    # `worktree add -B master ...` fails with "branch already in use".
    r = await _run_git(target, "checkout", "--detach", "HEAD")
    if not r.ok:
        raise PrepError(f"git checkout --detach failed: {r.stderr.strip()[:300]}")
    return target


async def ensure_worktree(
    token: str, owner: str, repo: str, branch: str
) -> Path:
    """Make sure /workspace/<owner>/<repo>/_wt/<safe_branch>/ exists as a
    worktree of `branch`, fast-forwarded to origin/<branch>. Returns path."""
    main = repo_clone_dir(owner, repo)
    if not (main / ".git").exists():
        raise PrepError(f"clone not present at {main} — run ensure_clone first")
    wt = worktree_dir(owner, repo, branch)
    remote_ref = f"origin/{branch}"

    if not wt.exists():
        _log(f"ensure_worktree: creating worktree {wt} on {remote_ref}")
        wt.parent.mkdir(parents=True, exist_ok=True)
        # -B creates the local branch (or resets if present), pointing at
        # origin/<branch>. The worktree starts at that commit.
        r = await _run_git(
            main, "worktree", "add", "-B", branch, str(wt), remote_ref,
        )
        if not r.ok:
            raise PrepError(f"git worktree add failed: {r.stderr.strip()[:300]}")
        return wt

    _log(f"ensure_worktree: existing worktree {wt}, ff to {remote_ref}")
    # Fast-forward existing worktree. Don't overwrite uncommitted work.
    async def fetch_branch():
        return await _run_git(wt, "fetch", "origin", branch, timeout=600.0)
    r = await _with_token_url(token, owner, repo, wt, fetch_branch)
    if not r.ok:
        raise PrepError(f"git fetch in worktree failed: {r.stderr.strip()[:300]}")
    r = await _run_git(wt, "merge", "--ff-only", remote_ref)
    if not r.ok:
        # Non-fatal — likely uncommitted changes or diverged. Surface the
        # message but keep the worktree usable.
        _log(f"ensure_worktree: ff-merge skipped: {r.stderr.strip()[:200]}")
    return wt


@dataclass
class PrepResult:
    clone_path: Path
    worktree_path: Path
    head_sha: str
    head_subject: str


async def prep_workspace(
    token: str, owner: str, repo: str, branch: str
) -> PrepResult:
    """Full pipeline: clone (or fetch) + worktree (or ff). Idempotent."""
    clone = await ensure_clone(token, owner, repo)
    wt = await ensure_worktree(token, owner, repo, branch)

    # Capture HEAD info for the UI.
    r = await _run_git(wt, "log", "-1", "--format=%H%n%s")
    if r.ok:
        lines = r.stdout.strip().split("\n", 1)
        sha = lines[0] if lines else ""
        subj = lines[1] if len(lines) > 1 else ""
    else:
        sha, subj = "", ""

    return PrepResult(
        clone_path=clone,
        worktree_path=wt,
        head_sha=sha,
        head_subject=subj,
    )


# ── session lifecycle ─────────────────────────────────────────────────────


@dataclass
class Session:
    """Persisted session metadata. Source of truth lives at
    SESSIONS_DIR/<id>/meta.json. tmux is the source of truth for liveness;
    we sync `state` on each refresh."""
    id: str
    owner: str
    repo: str
    branch: str
    tmux_session: str
    worktree_path: str
    log_path: str
    debug_path: str = ""
    rc_url: Optional[str] = None
    state: str = "starting"   # starting | running | stopped | error
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def display_branch(self) -> str:
        return self.branch


def _session_id(owner: str, repo: str, branch: str) -> str:
    h = hashlib.sha256(f"{owner}/{repo}@{branch}".encode()).hexdigest()
    return h[:10]


def _tmux_name(sid: str) -> str:
    return f"rcl-{sid}"


def _session_dir(sid: str) -> Path:
    return SESSIONS_DIR / sid


def _meta_path(sid: str) -> Path:
    return _session_dir(sid) / "meta.json"


def _log_path(sid: str) -> Path:
    return _session_dir(sid) / "claude.log"


def save_session(s: Session) -> None:
    s.updated_at = time.time()
    d = _session_dir(s.id)
    d.mkdir(parents=True, exist_ok=True)
    _meta_path(s.id).write_text(json.dumps(asdict(s), indent=2))


def load_session(sid: str) -> Optional[Session]:
    try:
        data = json.loads(_meta_path(sid).read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        _log(f"load_session({sid}): {e}")
        return None
    return Session(**data)


def list_session_ids() -> list[str]:
    if not SESSIONS_DIR.exists():
        return []
    return sorted(p.name for p in SESSIONS_DIR.iterdir() if p.is_dir())


# ── tmux + log scanning ───────────────────────────────────────────────────


async def _run(*args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Generic subprocess runner. Used for tmux."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timeout after {timeout}s"
    return (
        proc.returncode or 0,
        out_b.decode(errors="replace"),
        err_b.decode(errors="replace"),
    )


async def tmux_has_session(name: str) -> bool:
    rc, _, _ = await _run("tmux", "has-session", "-t", name)
    return rc == 0


async def tmux_kill_session(name: str) -> None:
    await _run("tmux", "kill-session", "-t", name)


async def tmux_spawn(name: str, cwd: Path, command: str) -> tuple[int, str]:
    """Create a detached tmux session named `name` running `command` from
    cwd. Returns (rc, stderr). stdout is silenced — tmux prints nothing
    interesting to stdout when -d is set."""
    rc, _, err = await _run(
        "tmux", "new-session", "-d", "-s", name, "-c", str(cwd), command,
        timeout=15.0,
    )
    return rc, err


def _scan_for_url(log_path: Path) -> Optional[str]:
    try:
        raw = log_path.read_text(errors="replace")
    except FileNotFoundError:
        return None
    text = _ANSI_RE.sub("", raw)
    m = _RC_URL_RE.search(text)
    return m.group(0) if m else None


# ── public API ────────────────────────────────────────────────────────────


async def start_session(
    token: str, owner: str, repo: str, branch: str
) -> Session:
    """Idempotent. If a session for (owner, repo, branch) already exists
    AND its tmux is alive, return it as-is. Otherwise prep workspace,
    spawn claude remote-control in tmux, return Session(state=starting)."""
    sid = _session_id(owner, repo, branch)
    tmux_name = _tmux_name(sid)
    existing = load_session(sid)
    if existing and await tmux_has_session(tmux_name):
        _log(f"start_session: {sid} already running, returning existing")
        # Try a fresh URL scan in case the previous attempt hadn't found
        # one yet but it's now in the log.
        if not existing.rc_url:
            url = _scan_for_url(Path(existing.log_path))
            if url:
                existing.rc_url = url
                existing.state = "running"
                save_session(existing)
        return existing

    # Prep first — surfaces clone/worktree errors before we touch tmux.
    prep = await prep_workspace(token, owner, repo, branch)

    sess_dir = _session_dir(sid)
    sess_dir.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(sid)
    # Truncate any prior log so we don't match an old URL on restart.
    log_path.write_text("")

    debug_path = sess_dir / "claude-debug.log"
    sess = Session(
        id=sid,
        owner=owner,
        repo=repo,
        branch=branch,
        tmux_session=tmux_name,
        worktree_path=str(prep.worktree_path),
        log_path=str(log_path),
        debug_path=str(debug_path),
    )
    save_session(sess)

    # Spawn an empty session first (default shell). The shell survives
    # `claude` exiting, so we can still inspect the pane via
    # `tmux capture-pane` to diagnose what happened.
    #
    # CRITICAL: `tmux new-session -d` daemonises the tmux server, and
    # the daemon inherits the client's stdin/stdout/stderr file
    # descriptors. If we use subprocess.PIPE for those, communicate()
    # blocks waiting for EOF that never arrives (the daemon keeps the
    # FDs open for the entire session lifetime). DEVNULL gives the
    # daemon nothing to hold onto, so the client exits cleanly.
    # Other tmux subcommands don't fork the daemon and are fine with
    # the regular PIPE-based _run.
    _log(f"start_session({sid}): tmux new-session -d -s {tmux_name} cwd={prep.worktree_path}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-c", str(prep.worktree_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        sess.state = "error"
        sess.error = "tmux new-session timed out after 10s"
        save_session(sess)
        _log(f"start_session({sid}): {sess.error}")
        return sess
    _log(f"start_session({sid}): new-session rc={rc}")
    if rc != 0:
        sess.state = "error"
        sess.error = f"tmux spawn failed (rc={rc})"
        save_session(sess)
        return sess

    # Capture the pane's output to a log file. Catches the URL line
    # that claude prints, plus anything else (errors, prompts).
    pipe_cmd = f"cat > {shlex.quote(str(log_path))}"
    pp_rc, pp_out, pp_err = await _run(
        "tmux", "pipe-pane", "-t", tmux_name, pipe_cmd,
    )
    _log(f"start_session({sid}): pipe-pane rc={pp_rc} out={pp_out.strip()!r} err={pp_err.strip()!r}")

    # CANARY: send a marker first to prove send-keys works at all.
    # If this echo appears in the pane, send-keys is fine and the
    # claude command's failure is downstream. If it doesn't appear,
    # send-keys itself is broken in this environment.
    canary = "echo RCL_CANARY=$RANDOM"
    c1_rc, c1_out, c1_err = await _run(
        "tmux", "send-keys", "-l", "-t", tmux_name, canary,
    )
    _log(f"start_session({sid}): send-keys canary text rc={c1_rc} out={c1_out.strip()!r} err={c1_err.strip()!r}")
    c2_rc, c2_out, c2_err = await _run(
        "tmux", "send-keys", "-t", tmux_name, "Enter",
    )
    _log(f"start_session({sid}): send-keys canary Enter rc={c2_rc} out={c2_out.strip()!r} err={c2_err.strip()!r}")

    # Then the actual claude command.
    claude_cmd = f"claude remote-control --debug-file {shlex.quote(str(debug_path))}"
    sk_rc, sk_out, sk_err = await _run(
        "tmux", "send-keys", "-l", "-t", tmux_name, claude_cmd,
    )
    _log(f"start_session({sid}): send-keys claude text rc={sk_rc} out={sk_out.strip()!r} err={sk_err.strip()!r}")
    if sk_rc != 0:
        sess.state = "error"
        sess.error = f"send-keys (claude text) rc={sk_rc}: {sk_err.strip()[:200]}"
        save_session(sess)
        return sess
    sk_rc, sk_out, sk_err = await _run(
        "tmux", "send-keys", "-t", tmux_name, "Enter",
    )
    _log(f"start_session({sid}): send-keys claude Enter rc={sk_rc} out={sk_out.strip()!r} err={sk_err.strip()!r}")
    if sk_rc != 0:
        sess.state = "error"
        sess.error = f"send-keys (claude Enter) rc={sk_rc}: {sk_err.strip()[:200]}"
        save_session(sess)
        return sess
    _log(f"start_session({sid}): sent claude cmd ({len(claude_cmd)} chars), DONE")

    # Don't block — return so the UI can show "Starting…". Client-side
    # poll on /sessions/{id}/refresh will pick up the URL when claude
    # emits it (typically within 1-2s), or surface a stuck pane.
    return sess


async def capture_pane(name: str) -> str:
    """Snapshot of the current pane (everything visible). Useful for
    diagnosis when the URL never appears in the log."""
    rc, out, _ = await _run(
        "tmux", "capture-pane", "-p", "-t", name, timeout=5.0,
    )
    return out if rc == 0 else ""


async def refresh_session(sid: str) -> Optional[Session]:
    """Re-read meta, reconcile with tmux liveness + log contents. Returns
    the updated Session (also persisted) or None if unknown."""
    sess = load_session(sid)
    if sess is None:
        return None
    alive = await tmux_has_session(sess.tmux_session)

    if not alive:
        if sess.state in ("starting", "running"):
            sess.state = "stopped"
            save_session(sess)
        return sess

    # tmux alive — try to capture URL from the log first, then fall
    # back to the pane snapshot (catches the case where pipe-pane
    # didn't capture in time but the URL is still on screen).
    if not sess.rc_url:
        url = _scan_for_url(Path(sess.log_path))
        if not url:
            pane = await capture_pane(sess.tmux_session)
            stripped = _ANSI_RE.sub("", pane)
            m = _RC_URL_RE.search(stripped)
            if m:
                url = m.group(0)
        if url:
            sess.rc_url = url
            sess.state = "running"
            save_session(sess)
    elif sess.state != "running":
        sess.state = "running"
        save_session(sess)
    return sess


async def stop_session(sid: str) -> bool:
    sess = load_session(sid)
    if sess is None:
        return False
    await tmux_kill_session(sess.tmux_session)
    sess.state = "stopped"
    save_session(sess)
    return True


async def list_sessions() -> list[Session]:
    """All known sessions, refreshed against tmux state."""
    sids = list_session_ids()
    out: list[Session] = []
    for sid in sids:
        s = await refresh_session(sid)
        if s is not None:
            out.append(s)
    return out
