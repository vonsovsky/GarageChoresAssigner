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
    directory = _person_directory()  # resolves both local profiles and upstream handles
    claimer_views = [
        {
            "discord_id": cid,
            "name": (directory.get(cid) or {}).get("name") or cid,
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
    claimers = frozenset(db.claims_for_task(task_id))
    fully_claimed = len(claimers) >= task.get("necessary_workers", 1)
    return suggest(task, pool, claimed_ids=claimers, fully_claimed=fully_claimed)


def _person_directory() -> dict[str, dict[str, str]]:
    """Union of upstream users and local profiles → {discord_id: {name, handle}}."""
    directory: dict[str, dict[str, str]] = {}
    for u in upstream.users:
        did = u.get("discord_id")
        if did:
            directory[did] = {"name": u.get("handle") or did, "handle": u.get("handle") or ""}
    for p in db.all_profiles():
        did = p["discord_id"]
        entry = directory.get(did, {})
        entry["name"] = p["name"]
        entry["handle"] = p.get("discord_handle") or entry.get("handle", "")
        directory[did] = entry
    return directory


def person_name(discord_id: str) -> str:
    info = _person_directory().get(discord_id)
    return (info or {}).get("name") or discord_id


def release_active_claims(discord_id: str) -> list[dict[str, Any]]:
    """Drop a person's claims on chores that are still active (not completed or
    cancelled), so those free up for reassignment. Completed chores are left on
    their record. Returns the updated views of the affected chores."""
    affected: list[dict[str, Any]] = []
    for task_id in _claims_by_user().get(discord_id, []):
        task = upstream.tasks.get(task_id)
        if task and _is_active(task):
            db.remove_claim(task_id, discord_id)
            affected.append(build_chore_view(task))
    return affected


def _claims_by_user() -> dict[str, list[int]]:
    per_user: dict[str, list[int]] = {}
    for task_id, cids in db.all_claims().items():
        for cid in cids:
            per_user.setdefault(cid, []).append(task_id)
    return per_user


def leaderboard() -> list[dict[str, Any]]:
    """One row per known person: chores performed (completed & claimed), time
    spent on them, and how many are still in progress."""
    directory = _person_directory()
    per_user = _claims_by_user()
    departed = db.departed_ids()
    # include anyone who has claims even if their profile/user is gone
    for did in per_user:
        directory.setdefault(did, {"name": did, "handle": ""})

    rows: list[dict[str, Any]] = []
    for did, info in directory.items():
        performing = performed = time_spent = 0
        for task_id in per_user.get(did, []):
            task = upstream.tasks.get(task_id)
            if not task or task.get("cancelled"):
                continue
            if task.get("completed"):
                performed += 1
                time_spent += task.get("estimated_time_min", 0)
            else:
                performing += 1
        rows.append(
            {
                "discord_id": did,
                "name": info.get("name") or did,
                "handle": info.get("handle") or "",
                "performed_count": performed,
                "performing_count": performing,
                "time_spent_min": time_spent,
                "departed": did in departed,
            }
        )
    # default order: most time spent first
    rows.sort(key=lambda r: (-r["time_spent_min"], -r["performed_count"], r["name"].lower()))
    return rows


def user_detail(discord_id: str) -> dict[str, Any]:
    """A person's chores: those in progress (on top) and those completed."""
    directory = _person_directory()
    info = directory.get(discord_id, {"name": discord_id, "handle": ""})
    task_ids = _claims_by_user().get(discord_id, [])

    performing: list[dict[str, Any]] = []
    performed: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = upstream.tasks.get(task_id)
        if not task or task.get("cancelled"):
            continue
        view = build_chore_view(task)
        (performed if task.get("completed") else performing).append(view)

    performing.sort(key=lambda v: (not v["urgent"], v.get("created") or ""))
    performed.sort(key=lambda v: v.get("completed") or "", reverse=True)

    return {
        "discord_id": discord_id,
        "name": info.get("name") or discord_id,
        "handle": info.get("handle") or "",
        "performing": performing,
        "performed": performed,
        "time_spent_min": sum(v["estimated_time_min"] for v in performed),
        "departed": discord_id in db.departed_ids(),
    }


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
