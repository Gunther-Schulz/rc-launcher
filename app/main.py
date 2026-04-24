"""rc-launcher — Phase 1 skeleton.

Single FastAPI app served by uvicorn. Renders an index page and a
/api/health endpoint so deploy readiness is machine-checkable.

Auth is handled at the edge (Coolify/Traefik HTTP Basic Auth) — no auth
code in this app.
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="rc-launcher")

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_HERE / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"phase": 1},
    )


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "phase": 1})
