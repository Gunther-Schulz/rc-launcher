"""rc-launcher — Coolify-deployed mobile-first launcher for Claude Code
sessions running on a server.

Routes (HTTP Basic Auth on everything except /api/health):
  GET  /                  home / status overview
  GET  /api/health        liveness probe (no auth)
  GET  /api/diag/...      diagnostic snapshots (auth, may be removed)

  GET  /claude            claude-login state page
  POST /claude/login/start
  POST /claude/login/code
  POST /claude/logout
  POST /claude/verify     spawn `claude --print` to confirm credentials work

  GET  /gh                GitHub PAT page
  POST /gh/token
  POST /gh/logout

  GET  /repos             list repos accessible to the stored PAT
  POST /repos/open        parse a pasted GitHub URL → repo detail
  GET  /repos/{o}/{r}     repo detail + branches list

Auth: HTTP Basic Auth in-app (Coolify's native flag doesn't propagate
to compose-deploy Traefik labels). Password from $RCL_PASSWORD, which
Coolify populates from $SERVICE_PASSWORD_ADMIN at install time.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import claude_login, gh_login, github_api

_USERNAME = os.environ.get("RCL_USERNAME", "admin")
_PASSWORD = os.environ.get("RCL_PASSWORD") or ""

_basic = HTTPBasic(realm="rc-launcher")


def require_auth(
    credentials: HTTPBasicCredentials = Depends(_basic),
) -> str:
    if not _PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RCL_PASSWORD not configured on the server",
        )
    ok_user = secrets.compare_digest(credentials.username, _USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, _PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="rc-launcher"'},
        )
    return credentials.username


app = FastAPI(title="rc-launcher")

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_HERE / "templates")


def _nav_ctx(active: str) -> dict:
    """Context for the persistent top nav, included on every page."""
    return {
        "nav_active": active,
        "nav_claude_logged_in": claude_login.logged_in(),
        "nav_gh_logged_in": gh_login.logged_in(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "claude_logged_in": claude_login.logged_in(),
            "gh_logged_in": gh_login.logged_in(),
            **_nav_ctx("home"),
        },
    )


@app.get("/api/health")
def health() -> JSONResponse:
    """Public liveness probe — no auth required, suitable for external
    monitoring (Coolify's own health check, uptime services, etc).
    Reports nothing sensitive."""
    return JSONResponse({"ok": True})


@app.get("/api/diag/claude-home")
def diag_claude_home(_user: str = Depends(require_auth)) -> JSONResponse:
    """Snapshot of /home/node/.claude — diagnostic, may be removed later."""
    base = Path("/home/node/.claude")
    out: dict = {"exists": base.exists(), "entries": []}
    if base.exists():
        for p in sorted(base.iterdir()):
            try:
                stat = p.stat()
                out["entries"].append({
                    "name": p.name,
                    "type": "dir" if p.is_dir() else "file",
                    "size": stat.st_size if p.is_file() else None,
                })
            except OSError as e:
                out["entries"].append({"name": p.name, "error": str(e)})
    out["claude_md_exists"] = (base / "CLAUDE.md").exists()
    out["claude_dotjson_exists"] = Path("/home/node/.claude.json").exists()
    return JSONResponse(out)


@app.get("/api/diag/devenv")
async def diag_devenv(_user: str = Depends(require_auth)) -> JSONResponse:
    """One-shot Round 2 verification: confirm runtime tools resolve as the
    node user (PATH + sudoers secure_path), home volume seeded, npmrc prefix
    redirected. Remove once verified."""
    import asyncio
    home = Path("/home/node")

    async def run(cmd: str) -> dict:
        proc = await asyncio.create_subprocess_shell(
            f"sudo -u node -E HOME=/home/node bash -lc {cmd!r}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return {"rc": proc.returncode, "out": out.decode(errors="replace").strip()}

    return JSONResponse({
        "home_entries": sorted(p.name for p in home.iterdir()) if home.exists() else None,
        "npmrc": (home / ".npmrc").read_text() if (home / ".npmrc").exists() else None,
        "uv_version": await run("uv --version"),
        "pipx_version": await run("pipx --version"),
        "claude_version": await run("claude --version"),
        "npm_prefix": await run("npm config get prefix"),
        "whoami": await run("whoami && echo PATH=$PATH"),
    })


# ── Claude login flow ──────────────────────────────────────────────────────


@app.get("/claude", response_class=HTMLResponse)
def claude_page(
    request: Request,
    _user: str = Depends(require_auth),
    verify_ok: Optional[bool] = None,
    verify_msg: Optional[str] = None,
):
    return templates.TemplateResponse(
        request=request,
        name="claude.html",
        context={
            "logged_in": claude_login.logged_in(),
            "state": claude_login.current_state(),
            "verify_ok": verify_ok,
            "verify_msg": verify_msg,
            **_nav_ctx("claude"),
        },
    )


@app.post("/claude/verify")
async def claude_verify(_user: str = Depends(require_auth)):
    ok, msg = await claude_login.verify()
    qs = urlencode({"verify_ok": "1" if ok else "0", "verify_msg": msg})
    return RedirectResponse(url=f"/claude?{qs}", status_code=303)


@app.post("/claude/login/start")
async def claude_login_start(_user: str = Depends(require_auth)):
    await claude_login.start_login()
    return RedirectResponse(url="/claude", status_code=303)


@app.post("/claude/login/code")
async def claude_login_code(
    _user: str = Depends(require_auth),
    code: str = Form(...),
):
    await claude_login.submit_code(code)
    return RedirectResponse(url="/claude", status_code=303)


@app.post("/claude/logout")
async def claude_logout_route(_user: str = Depends(require_auth)):
    await claude_login.logout()
    return RedirectResponse(url="/claude", status_code=303)


# ── GitHub login flow (PAT-based) ──────────────────────────────────────────


@app.get("/gh", response_class=HTMLResponse)
def gh_page(
    request: Request,
    _user: str = Depends(require_auth),
    last_error: Optional[str] = None,
):
    return templates.TemplateResponse(
        request=request,
        name="gh.html",
        context={
            "logged_in": gh_login.logged_in(),
            "user": gh_login.current_user(),
            "last_error": last_error,
            **_nav_ctx("gh"),
        },
    )


@app.post("/gh/token")
async def gh_set_token(
    _user: str = Depends(require_auth),
    token: str = Form(...),
):
    ok, msg = await gh_login.set_token(token)
    if ok:
        return RedirectResponse(url="/gh", status_code=303)
    qs = urlencode({"last_error": msg})
    return RedirectResponse(url=f"/gh?{qs}", status_code=303)


@app.post("/gh/logout")
async def gh_logout_route(_user: str = Depends(require_auth)):
    gh_login.logout()
    return RedirectResponse(url="/gh", status_code=303)


# ── Repo browsing ──────────────────────────────────────────────────────────


@app.get("/repos", response_class=HTMLResponse)
async def repos_page(
    request: Request,
    _user: str = Depends(require_auth),
    open_error: Optional[str] = None,
):
    token = gh_login.get_token()
    if not token:
        return RedirectResponse(url="/gh", status_code=303)
    error = None
    repos: list[dict] = []
    try:
        repos = await github_api.list_repos(token)
    except github_api.GitHubError as e:
        error = e.message
    except Exception as e:
        error = f"Unexpected error: {e}"
    return templates.TemplateResponse(
        request=request,
        name="repos.html",
        context={
            "repos": repos,
            "error": error,
            "open_error": open_error,
            **_nav_ctx("repos"),
        },
    )


@app.post("/repos/open")
async def repos_open(
    _user: str = Depends(require_auth),
    ref: str = Form(...),
):
    """Parse a pasted GitHub URL/shorthand → redirect to /repos/{owner}/{repo}."""
    parsed = github_api.parse_repo_ref(ref)
    if not parsed:
        msg = (
            f"Could not parse {ref!r} as a GitHub repo. "
            "Expected formats: https://github.com/owner/repo, "
            "git@github.com:owner/repo, or just owner/repo."
        )
        qs = urlencode({"open_error": msg})
        return RedirectResponse(url=f"/repos?{qs}", status_code=303)
    owner, repo = parsed
    return RedirectResponse(url=f"/repos/{owner}/{repo}", status_code=303)


@app.get("/repos/{owner}/{repo}", response_class=HTMLResponse)
async def repo_page(
    request: Request,
    owner: str,
    repo: str,
    _user: str = Depends(require_auth),
):
    token = gh_login.get_token()
    if not token:
        return RedirectResponse(url="/gh", status_code=303)
    error = None
    branches: list[dict] = []
    repo_meta: Optional[dict] = None
    try:
        repo_meta = await github_api.get_repo(token, owner, repo)
        branches = await github_api.list_branches(token, owner, repo)
    except github_api.GitHubError as e:
        error = e.message
    except Exception as e:
        error = f"Unexpected error: {e}"
    return templates.TemplateResponse(
        request=request,
        name="repo.html",
        context={
            "owner": owner,
            "name": repo,
            "repo": repo_meta,
            "branches": branches,
            "error": error,
            **_nav_ctx("repos"),
        },
    )
