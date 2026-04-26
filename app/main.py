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

from . import claude_login, gh_login, github_api, sessions

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
    prep_branch: Optional[str] = None,
    prep_ok: Optional[bool] = None,
    prep_msg: Optional[str] = None,
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
            "prep_branch": prep_branch,
            "prep_ok": prep_ok,
            "prep_msg": prep_msg,
            **_nav_ctx("repos"),
        },
    )


# ── Session pipeline (Phase 5) ────────────────────────────────────────────


@app.post("/sessions/start")
async def sessions_start(
    _user: str = Depends(require_auth),
    owner: str = Form(...),
    repo: str = Form(...),
    branch: str = Form(...),
):
    """Full pipeline: clone + worktree + spawn claude remote-control in
    detached tmux. Returns to /sessions/{id} where the page polls for the
    RC URL as it appears in the log."""
    token = gh_login.get_token()
    if not token:
        return RedirectResponse(url="/gh", status_code=303)
    try:
        sess = await sessions.start_session(token, owner, repo, branch)
    except sessions.PrepError as e:
        qs = urlencode({"prep_branch": branch, "prep_ok": "0", "prep_msg": str(e)})
        return RedirectResponse(url=f"/repos/{owner}/{repo}?{qs}", status_code=303)
    except Exception as e:
        qs = urlencode({"prep_branch": branch, "prep_ok": "0",
                        "prep_msg": f"Unexpected: {e}"})
        return RedirectResponse(url=f"/repos/{owner}/{repo}?{qs}", status_code=303)
    return RedirectResponse(url=f"/sessions/{sess.id}", status_code=303)


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_list(
    request: Request,
    _user: str = Depends(require_auth),
):
    items = await sessions.list_sessions()
    # Sort: running first, then starting, then stopped/error; within each
    # group most-recent first.
    order = {"running": 0, "starting": 1, "stopped": 2, "error": 3}
    items.sort(key=lambda s: (order.get(s.state, 9), -s.updated_at))
    return templates.TemplateResponse(
        request=request,
        name="sessions.html",
        context={"sessions": items, **_nav_ctx("sessions")},
    )


@app.get("/sessions/{sid}", response_class=HTMLResponse)
async def session_detail(
    request: Request,
    sid: str,
    _user: str = Depends(require_auth),
):
    sess = await sessions.refresh_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={"sess": sess, **_nav_ctx("sessions")},
    )


@app.get("/sessions/{sid}/refresh")
async def session_refresh_api(
    sid: str,
    _user: str = Depends(require_auth),
):
    """Polled by the detail page to pick up the RC URL once claude emits
    it. Returns the current state + URL as JSON."""
    sess = await sessions.refresh_session(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({
        "state": sess.state,
        "rc_url": sess.rc_url,
        "error": sess.error,
        "updated_at": sess.updated_at,
    })


@app.post("/sessions/{sid}/stop")
async def session_stop(
    sid: str,
    _user: str = Depends(require_auth),
):
    await sessions.stop_session(sid)
    return RedirectResponse(url=f"/sessions/{sid}", status_code=303)
