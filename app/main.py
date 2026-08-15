"""FastAPI application entrypoint for the Garage Trip Chores app."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import db, service
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
    try:
        yield
    finally:
        poller.cancel()
        await upstream.close()


app = FastAPI(title="Garage Trip Chores", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(page_routes.router)
app.include_router(api_routes.router)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
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
