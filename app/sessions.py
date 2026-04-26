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

Authentication: the GitHub PAT is passed via `git -c http.…extraheader=
Authorization: Bearer <pat>` per command, so the token never lands in
remote.origin.url and isn't written to git config. Future slices reuse
the same wrapper.

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


async def _run_git(
    cwd: Path,
    *args: str,
    token: Optional[str] = None,
    timeout: float = 120.0,
) -> GitResult:
    """Run `git <args>` in `cwd`. If `token` is given, attach Bearer auth
    via -c http.extraheader so private repos work without writing the
    token into git config."""
    cmd: list[str] = ["git"]
    if token:
        # -c extraheader applies to all https://github.com/ requests this
        # invocation makes. Token never logged.
        cmd += [
            "-c",
            f"http.https://github.com/.extraheader=Authorization: Bearer {token}",
        ]
    cmd += list(args)

    log_args = ["<auth>" if "Authorization:" in a else a for a in cmd]
    _log(f"git({cwd}): {' '.join(shlex.quote(a) for a in log_args)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return GitResult(rc=124, stdout="", stderr=f"git timed out after {timeout}s")
    out = out_b.decode(errors="replace")
    err = err_b.decode(errors="replace")
    # Defensive: scrub Bearer in case git ever echoes it back in errors.
    err = re.sub(r"Bearer [A-Za-z0-9_\-]+", "Bearer <redacted>", err)
    out = re.sub(r"Bearer [A-Za-z0-9_\-]+", "Bearer <redacted>", out)
    if proc.returncode != 0:
        _log(f"git rc={proc.returncode}: {err.strip()[:300]}")
    return GitResult(rc=proc.returncode or 0, stdout=out, stderr=err)


# ── public API ────────────────────────────────────────────────────────────


class PrepError(Exception):
    """Raised when prep_workspace can't set up the clone/worktree."""


async def ensure_clone(token: str, owner: str, repo: str) -> Path:
    """Make sure /workspace/<owner>/<repo>/main is a usable clone of the
    GitHub repo. Returns the clone path. Fetches if it already exists."""
    target = repo_clone_dir(owner, repo)
    url = f"https://github.com/{owner}/{repo}.git"

    if (target / ".git").exists():
        _log(f"ensure_clone: existing clone at {target}, fetching")
        r = await _run_git(target, "fetch", "--all", "--prune", token=token)
        if not r.ok:
            raise PrepError(f"git fetch failed: {r.stderr.strip()[:300]}")
        return target

    _log(f"ensure_clone: cloning {url} → {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    r = await _run_git(
        target.parent,
        "clone",
        "--no-tags",
        url,
        target.name,
        token=token,
        timeout=600.0,  # large repos
    )
    if not r.ok:
        raise PrepError(f"git clone failed: {r.stderr.strip()[:300]}")
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
    r = await _run_git(wt, "fetch", "origin", branch, token=token)
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
