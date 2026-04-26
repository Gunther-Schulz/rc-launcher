"""Thin httpx wrapper around the parts of the GitHub REST API we need."""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from ._logging import make_logger

GITHUB_API = "https://api.github.com"
USER_AGENT = "rc-launcher/0.1 (+https://github.com/Gunther-Schulz/rc-launcher)"

_log = make_logger("github_api")


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


_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _parse_link_next(link_header: str) -> Optional[str]:
    """Return the `next` URL from an RFC-5988 Link header, or None."""
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


# Max repos we'll ever fetch — sanity cap to prevent runaway pagination
# for accounts with thousands of accessible repos.
MAX_REPOS = 500


async def list_repos(token: str, per_page: int = 100) -> list[dict]:
    """List repos accessible to the token. Sorted by most recently pushed.

    Walks Link `rel="next"` pages until the API stops or we hit MAX_REPOS.
    """
    url: Optional[str] = f"{GITHUB_API}/user/repos"
    params: Optional[dict] = {
        "sort": "pushed",
        "direction": "desc",
        "per_page": per_page,
        "affiliation": "owner,collaborator,organization_member",
    }
    out: list[dict] = []
    pages = 0
    async with httpx.AsyncClient(timeout=15) as client:
        while url and len(out) < MAX_REPOS:
            pages += 1
            _log(f"list_repos: GET page {pages}: {url}")
            r = await client.get(url, params=params, headers=_headers(token))
            params = None  # subsequent URLs already include their query
            if r.status_code != 200:
                raise _interpret_error(r)
            data = r.json()
            if not isinstance(data, list):
                raise GitHubError(
                    500,
                    f"Expected list from /user/repos, got {type(data).__name__}",
                )
            out.extend(
                {
                    "full_name": item["full_name"],
                    "owner": item["owner"]["login"],
                    "name": item["name"],
                    "private": item.get("private", False),
                    "description": item.get("description") or "",
                    "language": item.get("language"),
                    "default_branch": item.get("default_branch", "main"),
                    "pushed_at": item.get("pushed_at"),
                    "html_url": item.get("html_url"),
                    "fork": item.get("fork", False),
                    "archived": item.get("archived", False),
                }
                for item in data
            )
            url = _parse_link_next(r.headers.get("Link", ""))
    _log(f"list_repos: returned {len(out)} repo(s) across {pages} page(s)")
    return out


# Patterns to parse a GitHub repo URL or shorthand into (owner, repo).
# Strips an optional .git suffix and handles paths like /tree/branch.
_GITHUB_URL_PATTERNS = [
    # https://github.com/owner/repo[.git][/tree/branch...]
    re.compile(r"^https?://github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?(?:/.*)?/?$"),
    # git@github.com:owner/repo[.git]
    re.compile(r"^git@github\.com:(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?/?$"),
    # owner/repo shorthand (no scheme, no slashes after the second segment)
    re.compile(r"^(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)/?$"),
]


def parse_repo_ref(ref: str) -> Optional[tuple[str, str]]:
    """Extract (owner, repo) from a user-typed GitHub URL or shorthand.

    Returns None if no pattern matches.
    """
    ref = ref.strip()
    for pat in _GITHUB_URL_PATTERNS:
        m = pat.match(ref)
        if m:
            owner = m.group("owner")
            repo = m.group("repo")
            # Strip a trailing .git that the lazy regex sometimes leaves on
            # the repo group when the URL has no path component after it.
            if repo.endswith(".git"):
                repo = repo[: -len(".git")]
            return owner, repo
    return None


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
