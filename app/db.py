"""SQLite persistence for the data the upstream chores API does not hold:
local profiles, per-chore metadata (urgent flag, size), manual out-of-scope
work, and local claim tracking.

The upstream API is the source of truth for tasks/assignments/stats; this
layer only augments it. Scale is tiny (~15 users), so a single shared
connection guarded by a lock is plenty.
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
    max_capacity_min INTEGER NOT NULL DEFAULT 240, -- daily workload cap in minutes
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

CREATE TABLE IF NOT EXISTS claims (
    task_id     INTEGER NOT NULL,
    discord_id  TEXT NOT NULL,
    created     TEXT NOT NULL,
    PRIMARY KEY (task_id, discord_id)
);
"""


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.commit()


# --- profiles ---------------------------------------------------------------

def upsert_profile(
    discord_id: str,
    name: str,
    discord_handle: str,
    skills: list[str],
    max_capacity_min: int,
) -> dict[str, Any]:
    with _lock:
        conn = get_conn()
        now = _now()
        conn.execute(
            """
            INSERT INTO profiles (discord_id, name, discord_handle, skills, max_capacity_min, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                name=excluded.name,
                discord_handle=excluded.discord_handle,
                skills=excluded.skills,
                max_capacity_min=excluded.max_capacity_min,
                updated=excluded.updated
            """,
            (discord_id, name, discord_handle, json.dumps(skills), max_capacity_min, now, now),
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


# --- claims (local) ---------------------------------------------------------

def add_claim(task_id: int, discord_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO claims (task_id, discord_id, created) VALUES (?, ?, ?)",
            (task_id, discord_id, _now()),
        )
        conn.commit()


def remove_claim(task_id: int, discord_id: str) -> None:
    with _lock:
        conn = get_conn()
        conn.execute(
            "DELETE FROM claims WHERE task_id = ? AND discord_id = ?", (task_id, discord_id)
        )
        conn.commit()


def claims_for_task(task_id: int) -> list[str]:
    with _lock:
        rows = get_conn().execute(
            "SELECT discord_id FROM claims WHERE task_id = ?", (task_id,)
        ).fetchall()
    return [r["discord_id"] for r in rows]


def all_claims() -> dict[int, list[str]]:
    with _lock:
        rows = get_conn().execute("SELECT task_id, discord_id FROM claims").fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["task_id"], []).append(r["discord_id"])
    return out
