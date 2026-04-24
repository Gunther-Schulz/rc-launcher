"""rc-launcher — Phase 1 skeleton.

Single FastAPI app served by uvicorn. Renders an index page and a
/api/health endpoint.

Auth: HTTP Basic Auth is enforced by FastAPI middleware. Credentials
come from env vars RCL_USERNAME (default "admin") and RCL_PASSWORD
(required). Coolify populates RCL_PASSWORD from $SERVICE_PASSWORD_ADMIN
auto-magic so the password is auto-generated at first install.

(Note: Coolify v4-beta's native `is_http_basic_auth_enabled` flag does
not propagate into compose-deploy Traefik labels, so we implement the
same RFC-7617 Basic Auth directly in-app. Identical browser UX.)
"""
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

_USERNAME = os.environ.get("RCL_USERNAME", "admin")
_PASSWORD = os.environ.get("RCL_PASSWORD") or ""

_basic = HTTPBasic(realm="rc-launcher")


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    # Reject at boot-time misconfiguration too — never accept empty password.
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
        context={"phase": 1},
    )


@app.get("/api/health")
def health(_user: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse({"ok": True, "phase": 1})
