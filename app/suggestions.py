"""Ranking of who should take a chore.

Per the agreed design:
  * Required skill and remaining capacity are HARD eligibility filters —
    someone who lacks a required capability, or who is already at/over their
    max capacity, is not suggested at all.
  * Among the eligible, we rank by LOWEST current workload (upstream
    `normalized_total`) so work stays balanced. The top 3 are the "notified"
    people (highlighted / sound on their screens).
"""
from __future__ import annotations

from typing import Any

from . import db
from .config import settings
from .upstream import upstream


def _committed_minutes() -> dict[str, float]:
    """Minutes each person has already committed to in-progress chores they've
    claimed in this app (these never reach the upstream stats), so auto-assign
    accounts for work someone is already on the hook for."""
    committed: dict[str, float] = {}
    for task_id, cids in db.all_claims().items():
        task = upstream.tasks.get(task_id)
        if not task or task.get("completed") or task.get("cancelled"):
            continue
        est = task.get("estimated_time_min", 0)
        for cid in cids:
            committed[cid] = committed.get(cid, 0) + est
    return committed


def build_person_pool(
    users: list[dict[str, Any]],
    stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge upstream users + stats with local profiles into one person list.

    People marked as having left the trip early are excluded so they are never
    suggested or auto-assigned (their leaderboard history is unaffected)."""
    profiles = {p["discord_id"]: p for p in db.all_profiles()}
    manual = db.manual_minutes_by_user()
    departed = db.departed_ids()
    committed = _committed_minutes()
    pool: list[dict[str, Any]] = []

    seen: set[str] = set()
    for u in users:
        did = u.get("discord_id")
        if not did or did in departed:
            continue
        seen.add(did)
        prof = profiles.get(did)
        s = stats.get(did, {})
        # capabilities are the union of upstream roles and locally-set skills
        caps = set(u.get("capabilities") or [])
        if prof:
            caps |= set(prof.get("skills") or [])
        worked_min = float(s.get("total_min", 0)) + manual.get(did, 0) + committed.get(did, 0)
        pool.append(
            {
                "discord_id": did,
                "name": (prof or {}).get("name") or u.get("handle") or did,
                "handle": u.get("handle") or (prof or {}).get("discord_handle") or "",
                "capabilities": sorted(caps),
                "max_capacity_min": (prof or {}).get("max_capacity_min", 240),
                "workload_min": round(worked_min, 1),
                "normalized_total": float(s.get("normalized_total", 0)),
                "present_ticks": int(s.get("present_ticks", 0)),
                "has_profile": prof is not None,
            }
        )

    # Include people who registered in the app but aren't in the upstream user
    # list yet (provisional "handle:" profiles) so they're still suggestible.
    for did, prof in profiles.items():
        if did in seen or did in departed:
            continue
        s = stats.get(did, {})
        worked_min = float(s.get("total_min", 0)) + manual.get(did, 0) + committed.get(did, 0)
        pool.append(
            {
                "discord_id": did,
                "name": prof.get("name") or did,
                "handle": prof.get("discord_handle") or "",
                "capabilities": sorted(set(prof.get("skills") or [])),
                "max_capacity_min": prof.get("max_capacity_min", 240),
                "workload_min": round(worked_min, 1),
                "normalized_total": float(s.get("normalized_total", 0)),
                "present_ticks": int(s.get("present_ticks", 0)),
                "has_profile": True,
            }
        )
    return pool


def _eligible(person: dict[str, Any], required_caps: list[str], task_time: int) -> bool:
    # skill gate
    if required_caps and not set(required_caps).issubset(set(person["capabilities"])):
        return False
    # capacity gate: already at or over the cap -> not eligible
    if person["workload_min"] >= person["max_capacity_min"]:
        return False
    return True


def suggest(
    task: dict[str, Any],
    pool: list[dict[str, Any]],
    top_n: int = 3,
) -> dict[str, Any]:
    """Return ranked suggestions for a task.

    Result: {"top": [...top_n discord_ids...], "ranked": [person, ...]}.
    Each person is annotated with `eligible` and `suggested` flags.
    """
    required_caps = task.get("necessary_capabilities") or []
    task_time = task.get("estimated_time_min", 0)

    annotated = []
    for p in pool:
        p = dict(p)
        p["eligible"] = _eligible(p, required_caps, task_time)
        # remaining capacity is informative for the UI
        p["remaining_min"] = max(0, p["max_capacity_min"] - p["workload_min"])
        annotated.append(p)

    eligible = [p for p in annotated if p["eligible"]]
    # rank: least total workload first (upstream + manual + in-progress claims),
    # then the upstream normalized metric, then name
    eligible.sort(key=lambda p: (p["workload_min"], p["normalized_total"], p["name"].lower()))

    top_ids = [p["discord_id"] for p in eligible[:top_n]]
    for p in annotated:
        p["suggested"] = p["discord_id"] in top_ids

    # ranked list: eligible (sorted) first, then the rest for reference
    rest = [p for p in annotated if not p["eligible"]]
    return {"top": top_ids, "ranked": eligible + rest}
