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
#
# Stages, each recorded separately so nobody mistakes a request for an effect:
#   requested     the web service received the request            (requested_at)
#   persisted     it was written AND read back from the shared DB  (persisted_at)
#   acknowledged  the hourly worker saw it at the start of a run   (acknowledged_at, acknowledged_by)
# The worker also re-reads the flag before EVERY advertising write, so a stop that lands between two
# writes prevents the second one. Stopping never pauses ads by itself; that is the emergency pause.
# --------------------------------------------------------------------------- #
class StorageError(RuntimeError):
    """The shared database didn't confirm a control write."""


def automation_stopped(con) -> Optional[dict]:
    v = get(con, "automation_stop")
    return v if isinstance(v, dict) and v.get("active") else None


def stop_automation(con, by: str, reason: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    t = now()
    v = {"active": True, "request_id": rid, "requested_at": t.isoformat(), "since": t.isoformat(), "by": by,
         "reason": (reason or "")[:300], "persisted_at": None, "acknowledged_at": None, "acknowledged_by": None}
    put(con, "automation_stop", v, by)
    back = get(con, "automation_stop")
    if not (isinstance(back, dict) and back.get("request_id") == rid and back.get("active")):
        raise StorageError("The stop request was not confirmed by the database; automation may still be running")
    back["persisted_at"] = now().isoformat()
    put(con, "automation_stop", back, by)
    return back


def acknowledge_stop(con, worker: str) -> Optional[dict]:
    """Called by the worker at the start of each run. Returns the (acknowledged) stop, or None."""
    v = automation_stopped(con)
    if v and not v.get("acknowledged_at"):
        v["acknowledged_at"], v["acknowledged_by"] = now().isoformat(), worker
        put(con, "automation_stop", v, worker)
    return v


def stop_stages(v: Optional[dict]) -> list[dict]:
    if not v:
        return []
    return [{"stage": "requested", "done": bool(v.get("requested_at") or v.get("since")), "at": v.get("requested_at") or v.get("since")},
            {"stage": "persisted", "done": bool(v.get("persisted_at")), "at": v.get("persisted_at")},
            {"stage": "worker_acknowledged", "done": bool(v.get("acknowledged_at")), "at": v.get("acknowledged_at"),
             "by": v.get("acknowledged_by")}]


def reset_automation_stop(con, by: str) -> None:
    put(con, "automation_stop", {"active": False, "reset_at": now().isoformat(), "by": by}, by)
    back = get(con, "automation_stop")
    if isinstance(back, dict) and back.get("active"):
        raise StorageError("The resume was not confirmed by the database")


# --------------------------------------------------------------------------- #
# Leases: one job at a time per name, survives crashes because it expires.
# A worker that loses its lease (expired, or taken over) must stop writing: see holds() / renew().
# --------------------------------------------------------------------------- #
class LeaseLost(RuntimeError):
    pass


def owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _t(v):
    if isinstance(v, str):
        return dt.datetime.fromisoformat(v.replace(" ", "T"))
    return v


def acquire(con, name: str, owner: str, ttl_minutes: int = 50) -> bool:
    t = now()
    con.execute(f"INSERT INTO {SCHEMA}.locks VALUES (?, NULL, NULL, NULL) ON CONFLICT DO NOTHING", [name])
    con.execute(f"UPDATE {SCHEMA}.locks SET owner = ?, acquired_at = ?, expires_at = ? "
                "WHERE name = ? AND (owner IS NULL OR expires_at IS NULL OR expires_at < ?)",
                [owner, t, t + dt.timedelta(minutes=ttl_minutes), name, t])
    r = con.execute(f"SELECT owner FROM {SCHEMA}.locks WHERE name = ?", [name]).fetchone()
    return bool(r and r[0] == owner)


def holds(con, name: str, owner: str) -> bool:
    r = con.execute(f"SELECT owner, expires_at FROM {SCHEMA}.locks WHERE name = ?", [name]).fetchone()
    return bool(r and r[0] == owner and r[1] is not None and _t(r[1]) > now())


def renew(con, name: str, owner: str, ttl_minutes: int = 50) -> bool:
    """Extend a lease we still hold. Returns False (and changes nothing) if it expired or someone else has it."""
    t = now()
    con.execute(f"UPDATE {SCHEMA}.locks SET expires_at = ? WHERE name = ? AND owner = ? AND expires_at > ?",
                [t + dt.timedelta(minutes=ttl_minutes), name, owner, t])
    return holds(con, name, owner)


def release(con, name: str, owner: str) -> None:
    con.execute(f"UPDATE {SCHEMA}.locks SET owner = NULL, expires_at = NULL WHERE name = ? AND owner = ?", [name, owner])


def lock_holder(con, name: str) -> Optional[dict]:
    r = con.execute(f"SELECT owner, acquired_at, expires_at FROM {SCHEMA}.locks WHERE name = ?", [name]).fetchone()
    if not r or not r[0]:
        return None
    exp = _t(r[2])
    if exp and exp < now():
        return None
    return {"owner": r[0], "acquired_at": r[1], "expires_at": r[2]}


# --------------------------------------------------------------------------- #
# Worker heartbeat: proves the web service and the hourly job share one database
# --------------------------------------------------------------------------- #
def worker_heartbeat(con, worker: str, storage: str) -> None:
    put(con, "worker_heartbeat", {"at": now().isoformat(), "worker": worker, "storage": storage}, worker)
