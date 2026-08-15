"""Client for the upstream Garage Trip Chores API (chores.garage-trip.cz).

Responsibilities:
  * REST calls (list/create/delete tasks, mark done, schedule, users, stats).
  * A resilient WebSocket consumer that authenticates with the API key and
    streams state-change events, keeping an in-memory cache fresh and forwarding
    each event to a registered handler (our own fan-out layer).

The upstream is the source of truth for tasks, assignments and workload stats.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
import websockets

from .config import settings

log = logging.getLogger("upstream")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

# Server -> client event names from the AsyncAPI spec.
TASK_EVENTS = {
    "task_created",
    "task_updated",
    "task_done",
    "task_assigned",
    "task_acked",
    "task_refused",
    "task_timeout",
}


class UpstreamClient:
    def __init__(self) -> None:
        headers = {}
        if settings.CHORES_API_KEY:
            # REST auth is HTTP Bearer per the upstream OpenAPI security scheme.
            headers["Authorization"] = f"Bearer {settings.CHORES_API_KEY}"
        self._http = httpx.AsyncClient(
            base_url=settings.CHORES_API_BASE, timeout=15.0, headers=headers
        )
        # in-memory caches
        self.tasks: dict[int, dict[str, Any]] = {}
        self.users: list[dict[str, Any]] = []
        self.stats: dict[str, dict[str, Any]] = {}
        self._event_handlers: list[EventHandler] = []
        self._ws_task: Optional[asyncio.Task] = None
        self._closing = False
        self.connected = False

    # -- event subscription --------------------------------------------------

    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    async def _emit(self, event: dict[str, Any]) -> None:
        for h in self._event_handlers:
            try:
                await h(event)
            except Exception:  # noqa: BLE001 - never let one handler kill the stream
                log.exception("event handler failed")

    # -- REST ----------------------------------------------------------------

    async def refresh_tasks(self) -> list[dict[str, Any]]:
        try:
            resp = await self._http.get("/tasks")
            resp.raise_for_status()
            data = resp.json() or []
            self.tasks = {t["id"]: t for t in data}
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh_tasks failed: %s", exc)
            return list(self.tasks.values())

    async def refresh_users(self) -> list[dict[str, Any]]:
        try:
            resp = await self._http.get("/users")
            resp.raise_for_status()
            self.users = resp.json() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh_users failed: %s", exc)
        return self.users

    async def refresh_stats(self) -> dict[str, dict[str, Any]]:
        try:
            resp = await self._http.get("/stats")
            resp.raise_for_status()
            self.stats = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh_stats failed: %s", exc)
        return self.stats

    async def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post("/tasks", json=body)
        resp.raise_for_status()
        task = resp.json()
        self.tasks[task["id"]] = task
        return task

    async def delete_task(self, task_id: int) -> None:
        resp = await self._http.delete(f"/tasks/{task_id}")
        resp.raise_for_status()
        self.tasks.pop(task_id, None)

    async def mark_done(self, task_id: int) -> None:
        resp = await self._http.post(f"/tasks/{task_id}/done")
        resp.raise_for_status()
        # Reflect completion in the cache immediately so a page refresh is
        # correct even if the upstream WebSocket event is delayed or missed.
        task = self.tasks.get(task_id)
        if task and not task.get("completed"):
            task["completed"] = datetime.now(timezone.utc).isoformat()

    async def schedule(self, task_id: int) -> None:
        resp = await self._http.post(f"/tasks/{task_id}/schedule")
        resp.raise_for_status()

    async def task_stats(self, task_id: int) -> dict[str, Any]:
        resp = await self._http.get(f"/tasks/{task_id}/stats")
        resp.raise_for_status()
        return resp.json()

    # -- WebSocket consumer --------------------------------------------------

    def start(self) -> None:
        self._ws_task = asyncio.create_task(self._run_ws())

    async def close(self) -> None:
        self._closing = True
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._http.aclose()

    async def _run_ws(self) -> None:
        if not settings.CHORES_API_KEY:
            log.warning("CHORES_API_KEY not set - skipping upstream WebSocket; live updates disabled")
            return
        backoff = 1
        while not self._closing:
            try:
                # The upstream gates the WebSocket upgrade with the same Bearer
                # auth as REST; it also expects an api_key message once open.
                async with websockets.connect(
                    settings.CHORES_WS_URL,
                    ping_interval=20,
                    additional_headers={"Authorization": f"Bearer {settings.CHORES_API_KEY}"},
                ) as ws:
                    await ws.send(json.dumps({"api_key": settings.CHORES_API_KEY}))
                    self.connected = True
                    backoff = 1
                    log.info("upstream WebSocket connected")
                    # prime caches on (re)connect
                    await asyncio.gather(
                        self.refresh_tasks(), self.refresh_users(), self.refresh_stats()
                    )
                    async for raw in ws:
                        await self._handle_ws_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                log.warning("upstream WebSocket error: %s (reconnecting in %ss)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.connected = False

    async def _handle_ws_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("non-JSON ws message: %r", raw)
            return

        event_type = msg.get("type") or msg.get("event")
        chore = msg.get("chore")
        assignment = msg.get("assignment")

        # Keep the task cache coherent with the event.
        if chore and isinstance(chore, dict) and "id" in chore:
            if event_type == "task_done":
                self.tasks.pop(chore["id"], None)
            else:
                self.tasks[chore["id"]] = chore

        # Stats shift on most transitions; refresh lazily on terminal events.
        if event_type in {"task_done", "task_assigned", "task_timeout"}:
            await self.refresh_stats()

        await self._emit(
            {"type": event_type, "chore": chore, "assignment": assignment}
        )


upstream = UpstreamClient()
