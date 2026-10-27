"""
Operating state that must survive restarts and be shared by the web service and the hourly job:
the automation mode, the stop-automation latch, emergency requests, and job leases.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import uuid
from typing import Optional

from .schema import SCHEMA

MODES = ("observe", "recommend", "approval", "bounded_auto")
MODE_LABEL = {
    "observe": "Observe: collect and display only",
    "recommend": "Recommend: propose changes, never apply them",
    "approval": "Approval required: apply only changes you approve",
    "bounded_auto": "Bounded automatic: apply only allowed actions within policy",
}
DEFAULT_MODE = "recommend"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def get(con, key: str, default=None):
    r = con.execute(f"SELECT value FROM {SCHEMA}.control_state WHERE key = ?", [key]).fetchone()
    if not r or r[0] is None:
        return default
    try:
        return json.loads(r[0])
    except (TypeError, ValueError):
        return r[0]


def put(con, key: str, value, by: str = "system") -> None:
    con.execute(f"INSERT INTO {SCHEMA}.control_state VALUES (?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                [key, json.dumps(value, default=str), now(), by])


def dry_run(environ=None) -> bool:
    """DRY_RUN is true unless explicitly set to false. Live writes need DRY_RUN=false AND a mode that allows them."""
    env = os.environ if environ is None else environ
    return str(env.get("DRY_RUN", "true")).lower() not in ("false", "0", "no")


def mode(con) -> str:
    m = get(con, "mode", DEFAULT_MODE)
    return m if m in MODES else DEFAULT_MODE


def set_mode(con, m: str, by: str) -> None:
    if m not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    put(con, "mode", m, by)


# --------------------------------------------------------------------------- #
# Stop automation (latched until someone resets it)
# --------------------------------------------------------------------------- #
def automation_stopped(con) -> Optional[dict]:
    v = get(con, "automation_stop")
    return v if isinstance(v, dict) and v.get("active") else None


def stop_automation(con, by: str, reason: str) -> dict:
    v = {"active": True, "since": now().isoformat(), "by": by, "reason": reason[:300]}
    put(con, "automation_stop", v, by)
    return v


def reset_automation_stop(con, by: str) -> None:
    put(con, "automation_stop", {"active": False, "reset_at": now().isoformat(), "by": by}, by)


# --------------------------------------------------------------------------- #
# Leases: one job at a time per name, survives crashes because it expires
# --------------------------------------------------------------------------- #
def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def acquire(con, name: str, owner: str, ttl_minutes: int = 50) -> bool:
    t = now()
    con.execute(f"INSERT INTO {SCHEMA}.locks VALUES (?, NULL, NULL, NULL) ON CONFLICT DO NOTHING", [name])
    con.execute(f"UPDATE {SCHEMA}.locks SET owner = ?, acquired_at = ?, expires_at = ? "
                "WHERE name = ? AND (owner IS NULL OR expires_at IS NULL OR expires_at < ?)",
                [owner, t, t + dt.timedelta(minutes=ttl_minutes), name, t])
    r = con.execute(f"SELECT owner FROM {SCHEMA}.locks WHERE name = ?", [name]).fetchone()
    return bool(r and r[0] == owner)


def release(con, name: str, owner: str) -> None:
    con.execute(f"UPDATE {SCHEMA}.locks SET owner = NULL, expires_at = NULL WHERE name = ? AND owner = ?", [name, owner])


def lock_holder(con, name: str) -> Optional[dict]:
    r = con.execute(f"SELECT owner, acquired_at, expires_at FROM {SCHEMA}.locks WHERE name = ?", [name]).fetchone()
    if not r or not r[0]:
        return None
    exp = r[2]
    if isinstance(exp, str):
        exp = dt.datetime.fromisoformat(exp.replace(" ", "T"))
    if exp and exp < now():
        return None
    return {"owner": r[0], "acquired_at": r[1], "expires_at": r[2]}
