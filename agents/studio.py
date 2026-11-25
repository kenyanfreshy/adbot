"""
The studio: what the agents write for you to review (angles, ad copy, listing text, launch drafts, creative briefs).

Every item starts as a draft. You approve or reject it on the phone app's Studio tab. Approval only lets the next
agent build on it (copy → launch draft → creative brief); nothing in the studio is ever sent to an ad platform or a
store. Items an agent replaces are marked superseded, never deleted.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from typing import Optional

from control.schema import SCHEMA

KINDS = ("voice", "angle", "copy", "listing", "draft", "brief")
OPEN = ("draft", "approved")

# words that make an ad claim a person must be able to back up (store policies and advertising rules)
CLAIM_WORDS = re.compile(r"(guarantee[sd]?|pass rate|100%|#1|number one|\bbest\b|\bproven\b|\bofficial\b|approved by|"
                         r"endorsed|\bfree\b|\bsecret\b|instantly|\bpass(?:es|ed)?\b|first try)", re.I)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def claim_flags(*texts: str) -> list[str]:
    """Things in ad text a person must check before it runs: numbers and claim words."""
    flags = []
    for t in texts:
        if not t:
            continue
        if re.search(r"\d", t):
            flags.append(f"Contains a number, check it's true: “{t[:80]}”")
        m = CLAIM_WORDS.search(t)
        if m:
            flags.append(f"Makes a claim (“{m.group(0)}”), check you can back it up: “{t[:80]}”")
    return flags


def add(con, kind: str, app: Optional[str], agent: str, title: str, content: dict, parent_id: Optional[str] = None,
        flags: Optional[list] = None, now: Optional[dt.datetime] = None) -> str:
    assert kind in KINDS
    item_id = f"{kind}_{uuid.uuid4().hex[:12]}"
    con.execute(f"INSERT INTO {SCHEMA}.studio_items VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, NULL, NULL)",
                [item_id, kind, app, parent_id, agent, now or _now(), (title or "")[:200],
                 json.dumps(content, default=str), json.dumps(flags or [])])
    return item_id


def items(con, kind: Optional[str] = None, status: Optional[tuple] = None, app: Optional[str] = None,
          parent_id: Optional[str] = None, limit: int = 200) -> list[dict]:
    where, params = [], []
    if kind:
        where.append("kind = ?"); params.append(kind)
    if status:
        where.append(f"status IN ({', '.join('?' * len(status))})"); params += list(status)
    if app:
        where.append("app = ?"); params.append(app)
    if parent_id:
        where.append("parent_id = ?"); params.append(parent_id)
    cur = con.execute(f"SELECT * FROM {SCHEMA}.studio_items {'WHERE ' + ' AND '.join(where) if where else ''} "
                      f"ORDER BY created_at DESC LIMIT {int(limit)}", params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["content"] = json.loads(d["content"] or "{}")
        d["flags"] = json.loads(d["flags"] or "[]")
        out.append(d)
    return out


def get(con, item_id: str) -> Optional[dict]:
    cur = con.execute(f"SELECT * FROM {SCHEMA}.studio_items WHERE item_id = ?", [item_id])
    cols = [d[0] for d in cur.description]
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip(cols, r))
    d["content"], d["flags"] = json.loads(d["content"] or "{}"), json.loads(d["flags"] or "[]")
    return d


def decide(con, item_id: str, approve: bool, by: str = "admin", now: Optional[dt.datetime] = None) -> dict:
    it = get(con, item_id)
    if not it:
        raise ValueError("no such studio item")
    if it["status"] not in ("draft", "approved", "rejected"):
        raise ValueError(f"this item is {it['status']} and can't be changed")
    status = "approved" if approve else "rejected"
    con.execute(f"UPDATE {SCHEMA}.studio_items SET status = ?, decided_by = ?, decided_at = ? WHERE item_id = ?",
                [status, by, now or _now(), item_id])
    return {"item_id": item_id, "status": status}


def supersede(con, kind: str, app: Optional[str], keep: Optional[str] = None) -> int:
    """Mark the older drafts of a kind (per app) superseded when a new batch replaces them. Approved items stay."""
    rows = items(con, kind=kind, status=("draft",), app=app, limit=1000)
    n = 0
    for r in rows:
        if r["item_id"] != keep:
            con.execute(f"UPDATE {SCHEMA}.studio_items SET status = 'superseded' WHERE item_id = ?", [r["item_id"]])
            n += 1
    return n


def due(ctx, key: str, every_days: float) -> bool:
    """True when the agent `key` last did its periodic work more than every_days ago (or never)."""
    from control import state as cstate
    last = cstate.get(ctx.con, f"agents.last.{key}")
    if not last:
        return True
    try:
        t = dt.datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (ctx.now - t) >= dt.timedelta(days=every_days) - dt.timedelta(minutes=30)


def mark_done(ctx, key: str) -> None:
    from control import state as cstate
    cstate.put(ctx.con, f"agents.last.{key}", ctx.now.isoformat(), by="agents")


def forced(ctx) -> bool:
    return str(ctx.environ.get("AGENTS_FORCE") or "").lower() in ("1", "true", "yes")


def waiting(con) -> dict:
    try:
        rows = con.execute(f"SELECT kind, count(*) FROM {SCHEMA}.studio_items WHERE status = 'draft' GROUP BY 1").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {k: int(n) for k, n in rows}
