"""rc-launcher — Phase 2.

Adds the `claude login` OAuth wrap: tap-able URL + paste-code form.
Delegates the pty dance to `claude_login.py`.

Auth: HTTP Basic Auth enforced in-app (see require_auth). Password from
$RCL_PASSWORD env var, which Coolify populates from SERVICE_PASSWORD_ADMIN.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import claude_login, gh_login

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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "phase": 3,
            "claude_logged_in": claude_login.logged_in(),
            "gh_logged_in": gh_login.logged_in(),
        },
    )


@app.get("/api/health")
def health(_user: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse({"ok": True, "phase": 3})


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
        },
    )


@app.post("/claude/verify")
async def claude_verify(_user: str = Depends(require_auth)):
    ok, msg = await claude_login.verify()
    # Pass result via query string so the redirected GET can render it.
    from urllib.parse import urlencode
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


# ── GitHub login flow ──────────────────────────────────────────────────────


@app.get("/gh", response_class=HTMLResponse)
def gh_page(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request=request,
        name="gh.html",
        context={
            "logged_in": gh_login.logged_in(),
            "state": gh_login.current_state(),
        },
    )


@app.post("/gh/login/start")
async def gh_login_start(_user: str = Depends(require_auth)):
    await gh_login.start_login()
    return RedirectResponse(url="/gh", status_code=303)


@app.post("/gh/logout")
async def gh_logout_route(_user: str = Depends(require_auth)):
    await gh_login.logout()
    return RedirectResponse(url="/gh", status_code=303)
