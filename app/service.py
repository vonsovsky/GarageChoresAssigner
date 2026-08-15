"""Service layer: merges upstream tasks with local metadata/claims into the
enriched "chore view" the UI consumes, computes suggestions, and bridges
upstream events to our own WebSocket fan-out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import db
from .catalog import size_for
from .suggestions import build_person_pool, suggest
from .upstream import upstream
from .ws import manager

# A chore whose deadline is within this many minutes is treated as urgent even
# if it wasn't explicitly flagged.
URGENT_DEADLINE_MIN = 60


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_active(task: dict[str, Any]) -> bool:
    return not task.get("completed") and not task.get("cancelled")


def build_chore_view(task: dict[str, Any]) -> dict[str, Any]:
    """Enrich a raw upstream task with local metadata, size, urgency, claims."""
    meta = db.get_chore_meta(task["id"]) or {}
    est = task.get("estimated_time_min", 0)
    size = meta.get("size") or size_for(est)

    urgent = bool(meta.get("urgent"))
    deadline = _parse_dt(task.get("deadline"))
    minutes_to_deadline = None
    if deadline:
        minutes_to_deadline = (deadline - datetime.now(timezone.utc)).total_seconds() / 60
        if minutes_to_deadline <= URGENT_DEADLINE_MIN:
            urgent = True

    claimers = db.claims_for_task(task["id"])
    profiles = {p["discord_id"]: p for p in db.all_profiles()}
    claimer_views = [
        {
            "discord_id": cid,
            "name": (profiles.get(cid) or {}).get("name") or cid,
        }
        for cid in claimers
    ]

    return {
        "id": task["id"],
        "name": task.get("name"),
        "necessary_workers": task.get("necessary_workers", 1),
        "estimated_time_min": est,
        "assignment_timeout_min": task.get("assignment_timeout_min"),
        "necessary_capabilities": task.get("necessary_capabilities") or [],
        "deadline": task.get("deadline"),
        "minutes_to_deadline": round(minutes_to_deadline) if minutes_to_deadline is not None else None,
        "completed": task.get("completed"),
        "cancelled": task.get("cancelled"),
        "created": task.get("created"),
        "size": size,
        "urgent": urgent,
        "template_key": meta.get("template_key"),
        "claimers": claimer_views,
        "claimed_count": len(claimer_views),
        "fully_claimed": len(claimer_views) >= task.get("necessary_workers", 1),
        "active": _is_active(task),
    }


def list_chore_views(active_only: bool = True) -> list[dict[str, Any]]:
    views = [build_chore_view(t) for t in upstream.tasks.values()]
    if active_only:
        views = [v for v in views if v["active"]]
    # urgent first, then soonest deadline, then largest first
    views.sort(
        key=lambda v: (
            not v["urgent"],
            v["minutes_to_deadline"] if v["minutes_to_deadline"] is not None else 10**9,
            -v["estimated_time_min"],
        )
    )
    return views


def suggestions_for(task_id: int) -> dict[str, Any]:
    task = upstream.tasks.get(task_id)
    if not task:
        return {"top": [], "ranked": []}
    pool = build_person_pool(upstream.users, upstream.stats)
    return suggest(task, pool)


def snapshot() -> dict[str, Any]:
    """Full initial payload sent to a client right after it connects."""
    chores = list_chore_views()
    return {
        "type": "snapshot",
        "chores": chores,
        "suggestions": {c["id"]: suggestions_for(c["id"])["top"] for c in chores},
        "upstream_connected": upstream.connected,
    }


async def on_upstream_event(event: dict[str, Any]) -> None:
    """Handler registered with the upstream client; re-broadcast enriched."""
    chore = event.get("chore")
    payload: dict[str, Any] = {"type": event.get("type")}
    if chore and isinstance(chore, dict) and "id" in chore:
        view = build_chore_view(chore)
        payload["chore"] = view
        payload["suggestions"] = suggestions_for(chore["id"])["top"]
    payload["assignment"] = event.get("assignment")
    await manager.broadcast(payload)


async def broadcast_local(message: dict[str, Any]) -> None:
    """Broadcast a locally-originated change (claim, profile edit, etc.)."""
    await manager.broadcast(message)
