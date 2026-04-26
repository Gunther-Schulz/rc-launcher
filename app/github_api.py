"""Thin httpx wrapper around the parts of the GitHub REST API we need."""
from __future__ import annotations

import sys
import time
from typing import Optional

import httpx

GITHUB_API = "https://api.github.com"
USER_AGENT = "rc-launcher/0.1 (+https://github.com/Gunther-Schulz/rc-launcher)"


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
    print(f"[github_api {ts}] {msg}", flush=True, file=sys.stdout)


class GitHubError(Exception):
    """Raised when GitHub returns a non-success status. Carries the
    HTTP status code and a human-readable message for the UI to render."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}")


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }


def _interpret_error(r: httpx.Response) -> GitHubError:
    """Turn a non-2xx response into a friendly error suitable for the UI."""
    try:
        body = r.json()
        gh_msg = body.get("message", r.text)
    except (ValueError, AttributeError):
        gh_msg = r.text[:200]

    if r.status_code == 401:
        return GitHubError(401, "GitHub rejected the token (401). Reconnect at /gh — token may have been revoked or never had `repo` scope.")
    if r.status_code == 403:
        # 403 can be rate-limit OR permission. Inspect headers.
        if r.headers.get("x-ratelimit-remaining") == "0":
            reset = r.headers.get("x-ratelimit-reset")
            try:
                reset_in = max(0, int(reset) - int(time.time()))
                wait = f"in {reset_in // 60} min" if reset_in >= 60 else f"in {reset_in} s"
            except (TypeError, ValueError):
                wait = "shortly"
            return GitHubError(403, f"GitHub rate limit hit. Retry {wait}.")
        return GitHubError(403, f"GitHub returned 403: {gh_msg}")
    if r.status_code == 404:
        return GitHubError(404, f"GitHub returned 404: {gh_msg}")
    return GitHubError(r.status_code, f"GitHub returned {r.status_code}: {gh_msg}")


async def list_repos(token: str, per_page: int = 100) -> list[dict]:
    """List repos accessible to the token. Sorted by most recently pushed.

    Currently fetches just the first page — most personal accounts have
    <100 repos. If the user reports truncation we'll add pagination via
    Link-header walking.
    """
    url = f"{GITHUB_API}/user/repos"
    params = {
        "sort": "pushed",
        "direction": "desc",
        "per_page": per_page,
        "affiliation": "owner,collaborator,organization_member",
    }
    _log(f"list_repos: GET {url} (per_page={per_page})")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, headers=_headers(token))
    if r.status_code != 200:
        raise _interpret_error(r)
    data = r.json()
    if not isinstance(data, list):
        raise GitHubError(500, f"Expected list response from /user/repos, got {type(data).__name__}")
    _log(f"list_repos: returned {len(data)} repo(s)")
    # Trim to fields the UI needs — keeps templates skinny + reduces memory
    return [
        {
            "full_name": r["full_name"],
            "owner": r["owner"]["login"],
            "name": r["name"],
            "private": r.get("private", False),
            "description": r.get("description") or "",
            "language": r.get("language"),
            "default_branch": r.get("default_branch", "main"),
            "pushed_at": r.get("pushed_at"),
            "html_url": r.get("html_url"),
            "fork": r.get("fork", False),
            "archived": r.get("archived", False),
        }
        for r in data
    ]


async def get_repo(token: str, owner: str, repo: str) -> dict:
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    _log(f"get_repo: GET {url}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_headers(token))
    if r.status_code != 200:
        raise _interpret_error(r)
    return r.json()


async def list_branches(
    token: str, owner: str, repo: str, per_page: int = 100
) -> list[dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/branches"
    _log(f"list_branches: GET {url}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={"per_page": per_page}, headers=_headers(token))
    if r.status_code != 200:
        raise _interpret_error(r)
    data = r.json()
    if not isinstance(data, list):
        raise GitHubError(500, f"Expected list from /branches, got {type(data).__name__}")
    return [
        {
            "name": b["name"],
            "protected": b.get("protected", False),
            "commit_sha": b.get("commit", {}).get("sha", "")[:7],
        }
        for b in data
    ]
