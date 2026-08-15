"""JSON API backing the pages."""
from __future__ import annotations

import random
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException, Response

from .. import db, service
from ..catalog import (
    CHORE_TEMPLATES,
    FUNNY_ACK_MESSAGES,
    SKILLS,
    TEMPLATES_BY_KEY,
    size_for,
    template_time,
)
from ..config import settings
from ..models import ChoreCreateIn, ManualWorkIn, ProfileIn
from ..suggestions import build_person_pool
from ..upstream import upstream

router = APIRouter(prefix="/api")

COOKIE = "uid"


def _resolve_discord_id(handle: str) -> str:
    """Map a Discord handle to its upstream discord_id when the person has
    already registered through the Discord bot; otherwise use a provisional id
    that reconciles once they appear upstream."""
    for u in upstream.users:
        if (u.get("handle") or "").lower() == handle.lower():
            return u["discord_id"]
    return f"handle:{handle.lower()}"


def _current_uid(uid: Optional[str]) -> str:
    if not uid:
        raise HTTPException(status_code=401, detail="Not registered - please join first.")
    return uid


# --- identity / profile -----------------------------------------------------

@router.post("/register")
async def register(body: ProfileIn, response: Response):
    discord_id = _resolve_discord_id(body.discord_handle)
    profile = db.upsert_profile(
        discord_id=discord_id,
        name=body.name,
        discord_handle=body.discord_handle,
        skills=body.skills,
        max_capacity_min=body.max_capacity_min,
    )
    response.set_cookie(COOKIE, discord_id, max_age=60 * 60 * 24 * 14, samesite="lax")
    await service.broadcast_local({"type": "profile_updated", "discord_id": discord_id})
    return {"profile": profile, "discord_id": discord_id, "matched_upstream": not discord_id.startswith("handle:")}


@router.get("/me")
async def me(uid: Optional[str] = Cookie(default=None)):
    if not uid:
        return {"profile": None}
    profile = db.get_profile(uid)
    return {"profile": profile, "discord_id": uid}


@router.put("/me")
async def update_me(body: ProfileIn, response: Response, uid: Optional[str] = Cookie(default=None)):
    _current_uid(uid)
    # allow the handle to be corrected; keep the same identity key
    profile = db.upsert_profile(
        discord_id=uid,  # type: ignore[arg-type]
        name=body.name,
        discord_handle=body.discord_handle,
        skills=body.skills,
        max_capacity_min=body.max_capacity_min,
    )
    await service.broadcast_local({"type": "profile_updated", "discord_id": uid})
    return {"profile": profile}


@router.get("/me/manual-work")
async def list_manual(uid: Optional[str] = Cookie(default=None)):
    uid = _current_uid(uid)
    return {"entries": db.manual_work_for(uid)}


@router.post("/me/manual-work")
async def add_manual(body: ManualWorkIn, uid: Optional[str] = Cookie(default=None)):
    uid = _current_uid(uid)
    entry = db.add_manual_work(uid, body.description, body.minutes)
    await service.broadcast_local({"type": "workload_updated", "discord_id": uid})
    return {"entry": entry}


# --- reference data ---------------------------------------------------------

@router.get("/templates")
async def templates():
    return {"templates": CHORE_TEMPLATES}


@router.get("/skills")
async def skills():
    return {"skills": SKILLS}


@router.get("/people")
async def people():
    pool = build_person_pool(upstream.users, upstream.stats)
    pool.sort(key=lambda p: (p["normalized_total"], p["workload_min"]))
    return {"people": pool, "children_count": settings.CHILDREN_COUNT}


# --- chores -----------------------------------------------------------------

@router.get("/chores")
async def chores():
    views = service.list_chore_views()
    return {
        "chores": views,
        "suggestions": {c["id"]: service.suggestions_for(c["id"])["top"] for c in views},
        "upstream_connected": upstream.connected,
    }


@router.get("/chores/{task_id}")
async def get_chore(task_id: int):
    task = upstream.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Unknown chore")
    return {"chore": service.build_chore_view(task), "suggestions": service.suggestions_for(task_id)}


@router.get("/chores/{task_id}/suggestions")
async def chore_suggestions(task_id: int):
    return service.suggestions_for(task_id)


@router.post("/chores")
async def create_chore(body: ChoreCreateIn):
    template = TEMPLATES_BY_KEY.get(body.template_key) if body.template_key else None

    # If created from a template, derive time (with head-count scaling) and caps.
    est = body.estimated_time_min
    caps = body.necessary_capabilities
    if template:
        headcount = body.headcount or 0
        est = template_time(template, headcount)
        caps = caps or template["necessary_capabilities"]

    upstream_body = {
        "name": body.name,
        "necessary_workers": body.necessary_workers,
        "estimated_time_min": est,
        "assignment_timeout_min": body.assignment_timeout_min,
        "necessary_capabilities": caps or None,
    }
    if body.deadline:
        upstream_body["deadline"] = body.deadline

    try:
        task = await upstream.create_task(upstream_body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Upstream create failed: {exc}")

    db.set_chore_meta(task["id"], urgent=body.urgent, size=size_for(est), template_key=body.template_key)
    view = service.build_chore_view(task)
    await service.broadcast_local(
        {"type": "task_created", "chore": view, "suggestions": service.suggestions_for(task["id"])["top"]}
    )
    return {"chore": view}


@router.post("/chores/{task_id}/claim")
async def claim_chore(task_id: int, uid: Optional[str] = Cookie(default=None)):
    uid = _current_uid(uid)
    if task_id not in upstream.tasks:
        raise HTTPException(status_code=404, detail="Unknown chore")
    db.add_claim(task_id, uid)
    view = service.build_chore_view(upstream.tasks[task_id])
    profile = db.get_profile(uid)
    name = (profile or {}).get("name", "friend")
    ack = random.choice(FUNNY_ACK_MESSAGES).format(name=name)
    await service.broadcast_local({"type": "task_claimed", "chore": view, "by": uid})
    return {"ack": ack, "chore": view}


@router.post("/chores/{task_id}/unclaim")
async def unclaim_chore(task_id: int, uid: Optional[str] = Cookie(default=None)):
    uid = _current_uid(uid)
    db.remove_claim(task_id, uid)
    view = service.build_chore_view(upstream.tasks.get(task_id, {"id": task_id, "necessary_workers": 1}))
    await service.broadcast_local({"type": "task_claimed", "chore": view, "by": uid})
    return {"chore": view}


@router.post("/chores/{task_id}/done")
async def done_chore(task_id: int):
    try:
        await upstream.mark_done(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Upstream done failed: {exc}")
    await service.broadcast_local({"type": "task_done", "chore": {"id": task_id}})
    return {"ok": True}


@router.delete("/chores/{task_id}")
async def delete_chore(task_id: int):
    try:
        await upstream.delete_task(task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Upstream delete failed: {exc}")
    await service.broadcast_local({"type": "task_done", "chore": {"id": task_id}})
    return {"ok": True}
