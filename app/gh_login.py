"""GitHub authentication via Personal Access Token (PAT).

Why PAT not OAuth/Device Flow: see Phase 3 design notes. tl;dr — for a
publishable Coolify template used by individual deployers, PAT has the
lowest setup friction (no OAuth App to register, no client_id to
manage, no shared infra for the project maintainer to maintain).

Flow:
  1. user generates PAT in GitHub UI (classic, scope `repo` + `read:user`)
  2. pastes it into a textarea on /gh
  3. backend POST /gh/token validates with `GET api.github.com/user`
  4. on 200 we cache (token, user metadata) in /var/lib/rcl/data
  5. logout deletes both files

Token storage: /var/lib/rcl/data/github-token (in the persistent `data`
volume so it survives redeploys). User metadata cached separately as
/var/lib/rcl/data/github-user.json so the UI can show "Logged in as X"
without re-fetching every page load.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from ._logging import make_logger

DATA_DIR = Path(os.environ.get("RCL_DATA_DIR", "/var/lib/rcl/data"))
TOKEN_FILE = DATA_DIR / "github-token"
USER_FILE = DATA_DIR / "github-user.json"

GITHUB_API = "https://api.github.com"
USER_AGENT = "rc-launcher/0.1 (+https://github.com/Gunther-Schulz/rc-launcher)"

_log = make_logger("gh_login")


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_token() -> Optional[str]:
    """Return the stored PAT or None."""
    try:
        return TOKEN_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as e:
        _log(f"get_token: read error {e}")
        return None


def logged_in() -> bool:
    return TOKEN_FILE.exists() and bool(get_token())


def current_user() -> Optional[dict]:
    """Return cached GitHub user metadata (login, name, avatar_url) or None."""
    try:
        return json.loads(USER_FILE.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


async def set_token(token: str) -> tuple[bool, str]:
    """Validate `token` against GitHub, persist it on success.

    Returns (ok, message).
    """
    token = token.strip()
    if not token:
        return False, "Token is empty."
    # Reject obvious paste mistakes (whitespace inside, very short, etc).
    if any(c.isspace() for c in token):
        return False, "Token contains whitespace — re-copy without line breaks."
    if len(token) < 20:
        return False, f"Token looks too short ({len(token)} chars) — typical PAT is 40+."

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    _log("set_token: validating with GET /user")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{GITHUB_API}/user", headers=headers)
    except httpx.HTTPError as e:
        _log(f"set_token: network error {e}")
        return False, f"Network error contacting GitHub: {e}"

    if r.status_code == 401:
        return False, "GitHub rejected the token (401). Check that you copied the full token and that it has not been revoked."
    if r.status_code == 403:
        scope_help = ""
        try:
            scopes = r.headers.get("x-oauth-scopes", "")
            if scopes:
                scope_help = f" Token scopes: {scopes!r}."
        except Exception:
            pass
        return False, f"GitHub returned 403.{scope_help} Token may lack the `repo` scope."
    if r.status_code != 200:
        return False, f"Unexpected response from GitHub ({r.status_code}): {r.text[:200]}"

    user = r.json()
    login = user.get("login")
    if not login:
        return False, f"GitHub returned 200 but no `login` field: {r.text[:200]}"

    # Validate scopes — we need `repo` to clone private repos.
    scopes_header = r.headers.get("x-oauth-scopes", "") or ""
    scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
    needed = {"repo"}
    missing = needed - set(scopes)
    # Fine-grained PATs do not return x-oauth-scopes; treat absence as "trust caller."
    if scopes and missing:
        return False, (
            f"Token is missing required scopes: {', '.join(sorted(missing))}. "
            f"Current scopes: {scopes_header}. Recreate with `repo` scope."
        )

    _ensure_data_dir()
    TOKEN_FILE.write_text(token)
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    USER_FILE.write_text(json.dumps({
        "login": login,
        "name": user.get("name") or login,
        "avatar_url": user.get("avatar_url"),
        "html_url": user.get("html_url"),
        "scopes": scopes,
        "saved_at": time.time(),
    }))
    _log(f"set_token: saved token for {login!r}, scopes={scopes}")
    return True, f"Logged in as {login}."


def logout() -> None:
    """Delete stored token + user cache."""
    for p in (TOKEN_FILE, USER_FILE):
        try:
            p.unlink()
            _log(f"logout: removed {p}")
        except FileNotFoundError:
            pass
        except OSError as e:
            _log(f"logout: failed to remove {p}: {e}")
