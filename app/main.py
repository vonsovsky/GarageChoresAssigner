"""FastAPI application entrypoint for the Garage Trip Chores app."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db, service
from .auth import router as auth_router
from .config import settings
from .routes import api as api_routes
from .routes import pages as page_routes
from .upstream import upstream
from .ws import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _poll_loop() -> None:
    """Fallback refresh so tasks/stats/users stay coherent even if the upstream
    WebSocket is quiet or an event is missed (e.g. changes made via Discord)."""
    while True:
        await asyncio.sleep(30)
        await asyncio.gather(
            upstream.refresh_tasks(), upstream.refresh_users(), upstream.refresh_stats()
        )


async def _heartbeat_loop() -> None:
    """Ping connected clients so idle WebSockets stay alive and dead ones are
    pruned — keeps the always-on TV dashboard receiving live updates."""
    while True:
        await asyncio.sleep(25)
        await manager.broadcast({"type": "ping"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    upstream.on_event(service.on_upstream_event)
    if not settings.has_upstream_key:
        log.warning("CHORES_API_KEY is empty — set it in .env for live upstream sync.")
    upstream.start()
    # prime caches immediately (WS also primes on connect)
    await asyncio.gather(upstream.refresh_tasks(), upstream.refresh_users(), upstream.refresh_stats())
    poller = asyncio.create_task(_poll_loop())
    heartbeat = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        poller.cancel()
        heartbeat.cancel()
        await upstream.close()


app = FastAPI(title="Garage Trip Chores", lifespan=lifespan)

# Paths reachable without a session (so people can actually log in).
_PUBLIC_EXACT = {"/", "/healthz", "/unauthorized", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/auth/", "/static/")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


def _session_authed(session) -> bool:
    return bool(session.get("user") or session.get("tablet"))


# Auth gate. Defined before SessionMiddleware is added so that middleware ends
# up OUTER (runs first) and `request.session` is populated here.
@app.middleware("http")
async def require_auth(request: Request, call_next):
    if settings.AUTH_REQUIRED and not _is_public(request.url.path):
        if not _session_authed(request.session):
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "Login required"}, status_code=401)
            return RedirectResponse("/", status_code=302)
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth_router)
app.include_router(page_routes.router)
app.include_router(api_routes.router)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if settings.AUTH_REQUIRED and not _session_authed(ws.session):
        await ws.close(code=1008)  # policy violation
        return
    await manager.connect(ws)
    try:
        await ws.send_json(service.snapshot())
        while True:
            # We don't require anything from clients; just keep the socket open.
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:  # noqa: BLE001
        await manager.disconnect(ws)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "upstream_connected": upstream.connected}
