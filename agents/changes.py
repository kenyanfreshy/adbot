"""Change Review: what the decision engine has proposed and what needs your OK. Read-only."""
from __future__ import annotations

from control.schema import SCHEMA

WAITING = ("proposed", "approved", "executing", "uncertain")


def change_review(ctx) -> dict:
    cur = ctx.con.execute(
        f"SELECT status, app, channel, entity_name, action, current_value, proposed_value, reason, created_at "
        f"FROM {SCHEMA}.proposals WHERE status IN ('proposed', 'blocked', 'approved', 'executing', 'uncertain') "
        f"ORDER BY created_at DESC")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    needs_ok = [r for r in rows if r["status"] == "proposed"]
    from control import engine as ceng
    from control import state as cstate
    return {"counts": counts, "needs_your_ok": len(needs_ok), "blocked": counts.get("blocked", 0),
            "uncertain": counts.get("uncertain", 0),
            "top": [{k: r[k] for k in ("app", "channel", "entity_name", "action", "current_value", "proposed_value", "reason")}
                    for r in needs_ok[:5]],
            "mode": cstate.mode(ctx.con), "stopped": bool(cstate.automation_stopped(ctx.con)),
            "emergency": bool(ceng.emergency_active(ctx.con))}
