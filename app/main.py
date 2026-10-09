"""PeopleGuard AI - FastAPI application entry point.

Run locally:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import pages, routes
from app.retrieval.index import policy_index
from app.storage.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("peopleguard")

VERSION = "1.0.0"
WORKSPACE_COOKIE = "pg_ws"
ACCESS_COOKIE = "pg_access"
OPEN_PATHS = ("/login", "/static/", "/health", "/favicon.ico")


def access_token(settings: config.Settings) -> str:
    return hmac.new(settings.secret_key.encode(), settings.access_code.encode(), hashlib.sha256).hexdigest()


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings or config.get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        summary = policy_index.build(settings.policy_dir)
        if not summary["total_sections"]:
            log.warning("No policy handbook found in %s. Place the handbook there and re-index.", settings.policy_dir)
        else:
            log.info("Indexed %s sections from %s document(s).", summary["total_sections"], len(summary["documents"]))
        log.info(settings.llm_status()["message"])
        yield

    app = FastAPI(title="PeopleGuard AI", version=VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.settings = settings
    app.state.db = Database(settings.database_path)
    app.state.index = policy_index
    app.state.version = VERSION

    @app.middleware("http")
    async def workspace_and_access(request: Request, call_next):
        path = request.url.path
        if settings.access_code and not path.startswith(OPEN_PATHS):
            if not hmac.compare_digest(request.cookies.get(ACCESS_COOKIE, ""), access_token(settings)):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Access code required."}, status_code=401)
                return RedirectResponse(f"/login?next={path}", status_code=303)
        ws = request.cookies.get(WORKSPACE_COOKIE)
        new_ws = None
        if not ws or len(ws) != 32 or not ws.isalnum():
            ws = new_ws = secrets.token_hex(16)
        request.state.workspace = ws
        response = await call_next(request)
        if new_ws:
            response.set_cookie(WORKSPACE_COOKIE, new_ws, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax", secure=settings.secure_cookies)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")
    app.include_router(routes.health_router)
    app.include_router(routes.router)
    app.include_router(pages.router)
    return app


app = create_app()
