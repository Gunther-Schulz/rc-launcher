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
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._logging import make_logger

WORKSPACE_ROOT = Path("/workspace")
_log = make_logger("sessions")


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
