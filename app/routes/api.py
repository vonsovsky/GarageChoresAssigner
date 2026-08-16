"""JSON API backing the pages."""
from __future__ import annotations

import random
import re
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException, Response

from .. import db, service
from ..catalog import (
    FUNNY_ACK_MESSAGES,
    SKILLS,
    size_for,
    template_time,
)
from ..config import settings
from ..models import (
    AssignIn,
    ChoreCreateIn,
    DepartureIn,
    LoginIn,
    ManualWorkIn,
    ProfileIn,
    TemplateIn,
)
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


@router.post("/login")
async def login(body: LoginIn, response: Response):
    """Sign an existing user back in by their Discord username (restores the
    session cookie for a returning/new-device/cleared-cookie visitor)."""
    profile = db.get_profile_by_handle(body.discord_handle)
    if not profile:
        raise HTTPException(
            status_code=404,
            detail="No profile for that Discord username yet — please join first.",
        )
    discord_id = profile["discord_id"]
    response.set_cookie(COOKIE, discord_id, max_age=60 * 60 * 24 * 14, samesite="lax")
    return {"profile": profile, "discord_id": discord_id}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


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

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "chore"
    key = slug
    n = 2
    while db.template_key_exists(key):
        key = f"{slug}-{n}"
        n += 1
    return key


@router.get("/templates")
async def templates():
    return {"templates": db.all_templates()}


@router.post("/templates")
async def create_template(body: TemplateIn):
    key = _slugify(body.name)
    tpl = db.upsert_template(key, body.model_dump())
    return {"template": tpl}


@router.put("/templates/{key}")
async def update_template(key: str, body: TemplateIn):
    if not db.template_key_exists(key):
        raise HTTPException(status_code=404, detail="Unknown template")
    tpl = db.upsert_template(key, body.model_dump())
    return {"template": tpl}


@router.delete("/templates/{key}")
async def delete_template(key: str):
    if not db.template_key_exists(key):
        raise HTTPException(status_code=404, detail="Unknown template")
    db.delete_template(key)
    return {"ok": True}


@router.get("/skills")
async def skills():
    return {"skills": SKILLS}


@router.get("/people")
async def people():
    pool = build_person_pool(upstream.users, upstream.stats)
    pool.sort(key=lambda p: (p["normalized_total"], p["workload_min"]))
    return {"people": pool, "children_count": settings.CHILDREN_COUNT}


@router.get("/leaderboard")
async def leaderboard():
    return {"rows": service.leaderboard()}


@router.get("/users/{user_id:path}")
async def user_detail(user_id: str):
    return service.user_detail(user_id)


@router.post("/users/{user_id:path}/departure")
async def set_departure(user_id: str, body: DepartureIn):
    """Mark a person as having left the trip early (or back). Departed people
    keep their leaderboard history but are no longer suggested or auto-assigned."""
    db.set_departed(user_id, body.departed)
    await service.broadcast_local({"type": "profile_updated", "discord_id": user_id})
    return {"discord_id": user_id, "departed": body.departed}


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
    template = db.get_template(body.template_key) if body.template_key else None

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


@router.post("/chores/{task_id}/assign")
async def assign_chore(task_id: int, body: AssignIn):
    """Assign an unclaimed chore to someone else. With no discord_id, auto-assign
    the lowest-workload eligible person (the top suggestion)."""
    if task_id not in upstream.tasks:
        raise HTTPException(status_code=404, detail="Unknown chore")
    discord_id = body.discord_id
    if not discord_id:
        top = service.suggestions_for(task_id)["top"]
        if not top:
            raise HTTPException(status_code=409, detail="No eligible person available to auto-assign.")
        discord_id = top[0]
    db.add_claim(task_id, discord_id)
    view = service.build_chore_view(upstream.tasks[task_id])
    name = service.person_name(discord_id)
    await service.broadcast_local({"type": "task_claimed", "chore": view, "by": discord_id})
    return {"chore": view, "assigned": {"discord_id": discord_id, "name": name}, "ack": f"Assigned to {name} ✓"}


@router.post("/chores/{task_id}/unclaim")
async def unclaim_chore(task_id: int, uid: Optional[str] = Cookie(default=None)):
    uid = _current_uid(uid)
    db.remove_claim(task_id, uid)
    view = service.build_chore_view(upstream.tasks.get(task_id, {"id": task_id, "necessary_workers": 1}))
    await service.broadcast_local({"type": "task_claimed", "chore": view, "by": uid})
    return {"chore": view}


@router.post("/chores/{task_id}/unassign")
async def unassign_chore(task_id: int, body: AssignIn):
    """Remove a specific person's assignment from a chore (anyone can manage)."""
    if not body.discord_id:
        raise HTTPException(status_code=422, detail="discord_id is required")
    db.remove_claim(task_id, body.discord_id)
    view = service.build_chore_view(upstream.tasks.get(task_id, {"id": task_id, "necessary_workers": 1}))
    await service.broadcast_local({"type": "task_claimed", "chore": view, "by": body.discord_id})
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
