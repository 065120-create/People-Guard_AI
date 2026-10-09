"""Server-rendered HTML pages. Data is loaded by app/static/js/app.js from the JSON API."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _render(request: Request, name: str, page: str, **extra) -> HTMLResponse:
    settings = request.app.state.settings
    ctx = {"page": page, "llm": settings.llm_status(), "version": request.app.state.version, "access_code_enabled": bool(settings.access_code), **extra}
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return _render(request, "dashboard.html", "dashboard")


@router.get("/cases/new", response_class=HTMLResponse)
def new_case(request: Request):
    return _render(request, "new_case.html", "new_case")


@router.get("/cases", response_class=HTMLResponse)
def history(request: Request):
    return _render(request, "history.html", "history")


@router.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(request: Request, case_id: str):
    return _render(request, "case.html", "case", case_id=case_id)


@router.get("/policies", response_class=HTMLResponse)
def policies(request: Request):
    return _render(request, "policies.html", "policies")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _render(request, "settings.html", "settings")


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = "/", error: str = ""):
    return _render(request, "login.html", "login", next=next if next.startswith("/") else "/", error=error)


@router.post("/login")
def login_submit(request: Request, code: str = Form(""), next: str = Form("/")):
    from app.main import ACCESS_COOKIE, access_token

    settings = request.app.state.settings
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    if not settings.access_code:
        return RedirectResponse(target, status_code=303)
    if not hmac.compare_digest(code.strip(), settings.access_code):
        return RedirectResponse(f"/login?error=1&next={target}", status_code=303)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(ACCESS_COOKIE, access_token(settings), max_age=60 * 60 * 24 * 14, httponly=True, samesite="lax", secure=settings.secure_cookies)
    return resp
