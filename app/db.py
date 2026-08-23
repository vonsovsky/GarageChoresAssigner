"""SQLite persistence for the data the upstream chores API does not hold:
local profiles, per-chore metadata (urgent flag, size), and manual out-of-scope
work.

The upstream API is the source of truth for tasks/assignments/stats (claims are
its `acked` arrays); this layer only augments it. Scale is tiny (~15 users), so
a single shared connection guarded by a lock is plenty.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .config import settings

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    discord_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    discord_handle  TEXT NOT NULL,
    skills          TEXT NOT NULL DEFAULT '[]',   -- json array of capability strings
    created         TEXT NOT NULL,
    updated         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chore_meta (
    task_id      INTEGER PRIMARY KEY,
    urgent       INTEGER NOT NULL DEFAULT 0,
    size         TEXT NOT NULL DEFAULT 'medium',  -- small|medium|large (derived, overridable)
    template_key TEXT
);

CREATE TABLE IF NOT EXISTS manual_work (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,
    description TEXT NOT NULL,
    minutes     INTEGER NOT NULL,
    created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    key                    TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    necessary_workers      INTEGER NOT NULL DEFAULT 1,
    estimated_time_min     INTEGER NOT NULL DEFAULT 10,
    assignment_timeout_min INTEGER NOT NULL DEFAULT 15,
    necessary_capabilities TEXT NOT NULL DEFAULT '[]',  -- json array
    scales_with_headcount  INTEGER NOT NULL DEFAULT 0,
    per_person_min         INTEGER NOT NULL DEFAULT 0,
    sort_order             INTEGER NOT NULL DEFAULT 0
);
"""


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.commit()
    _seed_templates()


def _seed_templates() -> None:
    """Populate the templates table from the built-in defaults on first run."""
    from .catalog import CHORE_TEMPLATES

    with _lock:
        conn = get_conn()
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] > 0:
            return
        for i, t in enumerate(CHORE_TEMPLATES):
            conn.execute(
                """
                INSERT INTO templates (key, name, necessary_workers, estimated_time_min,
                    assignment_timeout_min, necessary_capabilities, scales_with_headcount,
                    per_person_min, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["key"], t["name"], t["necessary_workers"], t["estimated_time_min"],
                    t["assignment_timeout_min"], json.dumps(t["necessary_capabilities"]),
                    1 if t.get("scales_with_headcount") else 0, t.get("per_person_min", 0), i,
                ),
            )
        conn.commit()


# --- profiles ---------------------------------------------------------------

def upsert_profile(
    discord_id: str,
    name: str,
    discord_handle: str,
    skills: list[str],
) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        now = _now()
        conn.execute(
            """
            INSERT INTO profiles (discord_id, name, discord_handle, skills, created, updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                name=excluded.name,
                discord_handle=excluded.discord_handle,
                skills=excluded.skills,
                updated=excluded.updated
            """,
            (discord_id, name, discord_handle, json.dumps(skills), now, now),
        )
        conn.commit()
    return get_profile(discord_id)  # type: ignore[return-value]


def get_profile(discord_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM profiles WHERE discord_id = ?", (discord_id,)
        ).fetchone()
    return _profile_row(row) if row else None


def get_profile_by_handle(handle: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM profiles WHERE lower(discord_handle) = lower(?)", (handle,)
        ).fetchone()
    return _profile_row(row) if row else None


def all_profiles() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM profiles").fetchall()
    return [_profile_row(r) for r in rows]


def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["skills"] = json.loads(d.get("skills") or "[]")
    return d


# --- chore metadata ---------------------------------------------------------

def set_chore_meta(task_id: int, urgent: bool, size: str, template_key: Optional[str]) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO chore_meta (task_id, urgent, size, template_key)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                urgent=excluded.urgent, size=excluded.size, template_key=excluded.template_key
            """,
            (task_id, 1 if urgent else 0, size, template_key),
        )
        conn.commit()


def get_chore_meta(task_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM chore_meta WHERE task_id = ?", (task_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["urgent"] = bool(d["urgent"])
    return d


def all_chore_meta() -> dict[int, dict[str, Any]]:
    with _lock:
        rows = get_conn().execute("SELECT * FROM chore_meta").fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        d["urgent"] = bool(d["urgent"])
        out[d["task_id"]] = d
    return out


# --- manual (out-of-scope) work ---------------------------------------------

def add_manual_work(discord_id: str, description: str, minutes: int) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO manual_work (discord_id, description, minutes, created) VALUES (?, ?, ?, ?)",
            (discord_id, description, minutes, _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM manual_work WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def manual_work_for(discord_id: str) -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM manual_work WHERE discord_id = ? ORDER BY created DESC", (discord_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def manual_minutes_by_user() -> dict[str, int]:
    with _lock:
        rows = get_conn().execute(
            "SELECT discord_id, COALESCE(SUM(minutes),0) AS m FROM manual_work GROUP BY discord_id"
        ).fetchall()
    return {r["discord_id"]: r["m"] for r in rows}


# --- merge two identities ----------------------------------------------------

def merge_user(from_id: str, to_id: str) -> dict[str, int]:
    """Fold `from_id` into `to_id`: move manual work onto the canonical id and
    delete the duplicate profile. Returns counts moved. (Claims live upstream
    now, keyed by discord_id, so they aren't touched here.)"""
    with _lock:
        conn = get_conn()
        manual = conn.execute(
            "SELECT COUNT(*) FROM manual_work WHERE discord_id = ?", (from_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE manual_work SET discord_id = ? WHERE discord_id = ?", (to_id, from_id)
        )
        conn.execute("DELETE FROM profiles WHERE discord_id = ?", (from_id,))
        conn.commit()
    return {"manual_work": manual}


# --- chore templates --------------------------------------------------------

def _template_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["necessary_capabilities"] = json.loads(d.get("necessary_capabilities") or "[]")
    d["scales_with_headcount"] = bool(d["scales_with_headcount"])
    return d


def all_templates() -> list[dict[str, Any]]:
    with _lock:
        rows = get_conn().execute(
            "SELECT * FROM templates ORDER BY sort_order, name"
        ).fetchall()
    return [_template_row(r) for r in rows]


def get_template(key: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = get_conn().execute("SELECT * FROM templates WHERE key = ?", (key,)).fetchone()
    return _template_row(row) if row else None


def upsert_template(key: str, data: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        # keep new templates after the existing ones
        next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM templates").fetchone()[0]
        conn.execute(
            """
            INSERT INTO templates (key, name, necessary_workers, estimated_time_min,
                assignment_timeout_min, necessary_capabilities, scales_with_headcount,
                per_person_min, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name=excluded.name,
                necessary_workers=excluded.necessary_workers,
                estimated_time_min=excluded.estimated_time_min,
                assignment_timeout_min=excluded.assignment_timeout_min,
                necessary_capabilities=excluded.necessary_capabilities,
                scales_with_headcount=excluded.scales_with_headcount,
                per_person_min=excluded.per_person_min
            """,
            (
                key, data["name"], data["necessary_workers"], data["estimated_time_min"],
                data["assignment_timeout_min"], json.dumps(data["necessary_capabilities"]),
                1 if data["scales_with_headcount"] else 0, data["per_person_min"], next_order,
            ),
        )
        conn.commit()
    return get_template(key)  # type: ignore[return-value]


def delete_template(key: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM templates WHERE key = ?", (key,))
        conn.commit()


def template_key_exists(key: str) -> bool:
    with _lock:
        return get_conn().execute(
            "SELECT 1 FROM templates WHERE key = ?", (key,)
        ).fetchone() is not None
