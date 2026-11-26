"""
Outcome Tracker (phase 7): what happened after each decision you made, measured 7 and 14 days later.

  launch drafts you approved   did the keywords it suggested appear in Apple Ads (you added them)? If so, their
                               cost per install vs the app's cost per install in the days before you approved.
  listing text you approved    store downloads per day after vs before.
  changes you approved         (Ads tab proposals that were approved or applied) the keyword / ad set's spend and
                               installs per day after vs before.

Verdicts are better / worse / flat (within ±15%) / not_used / no_data. They are measurements around a date, not
proof of cause: seasonality and other changes move the same numbers. Each result is written once to
control.outcomes. The summary also goes to the writing agents, so new angles lean towards what you approved.
"""
from __future__ import annotations

import datetime as dt
import json

WINDOWS = (7, 14)
LAG_DAYS = 2            # platforms and stores report late: wait this long past the window
FLAT = 0.15


def _d(v) -> dt.date:
    return v.date() if isinstance(v, dt.datetime) else (v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10]))


def verdict(before: float | None, after: float | None, lower_is_better: bool) -> str:
    """Compare two rates. Pure, for tests."""
    if before is None or after is None:
        return "no_data"
    if before == 0:
        return "flat" if after == 0 else ("worse" if lower_is_better else "better")
    change = (after - before) / before
    if abs(change) <= FLAT:
        return "flat"
    return ("better" if change < 0 else "worse") if lower_is_better else ("better" if change > 0 else "worse")


def _sum(rows, pick) -> dict:
    s = {"spend": 0.0, "installs": 0.0, "taps": 0.0}
    for r in rows:
        if pick(r):
            s["spend"] += r.get("spend") or 0.0
            s["installs"] += r.get("installs") or 0.0
            s["taps"] += r.get("taps") or 0.0
    s = {k: round(v, 2) for k, v in s.items()}
    s["cpi"] = round(s["spend"] / s["installs"], 2) if s["installs"] else None
    return s


def _between(r, a: dt.date, b: dt.date) -> bool:
    return a.isoformat() <= r["date"] <= b.isoformat()


def _measure_draft(ctx, it, day0, days, rows):
    from .money import _resolve
    words = {k["text"].lower() for k in ((it["content"].get("apple_ads") or {}).get("add_keywords") or [])}
    after = (day0, day0 + dt.timedelta(days=days - 1))
    before = (day0 - dt.timedelta(days=days), day0 - dt.timedelta(days=1))
    mine = lambda r: r["channel"] == "Apple Search Ads" and str(r.get("keyword") or "").lower() in words  # noqa: E731
    used = _sum([r for r in rows if _between(r, *after)], mine)
    if not words or not (used["taps"] or used["spend"]):
        return "not_used", None, used, "None of its keywords show up in Apple Ads yet"
    base = _sum([r for r in rows if _between(r, *before)], lambda r: _resolve(ctx, r) == it["app"])
    v = verdict(base["cpi"], used["cpi"], lower_is_better=True)
    return v, base, used, f"New keywords: {used['installs']:g} installs at {used['cpi'] if used['cpi'] is not None else '—'} each vs the app's {base['cpi'] if base['cpi'] is not None else '—'} before"


def _measure_listing(ctx, it, day0, days):
    from revenue.schema import SCHEMA as REV
    out = []
    for a, b in ((day0 - dt.timedelta(days=days), day0 - dt.timedelta(days=1)), (day0, day0 + dt.timedelta(days=days - 1))):
        n = ctx.con.execute(f"SELECT sum(s.units) FROM {REV}.sales_reports s JOIN {REV}.applications a ON a.app_id = s.app_id "
                            f"WHERE a.bot_app_name = ? AND s.transaction_type = 'download' AND s.period_start BETWEEN ? AND ?",
                            [it["app"], a, b]).fetchone()[0]
        out.append(None if n is None else round(float(n) / days, 2))
    v = verdict(out[0], out[1], lower_is_better=False)
    return v, {"downloads_per_day": out[0]}, {"downloads_per_day": out[1]}, f"Store downloads per day: {out[0]} before, {out[1]} after (only if you published the text)"


def _measure_proposal(p, day0, days, rows):
    ch, eid = (str(p["entity_key"]).split("|", 1) + [""])[:2]

    def pick(r):
        if r["channel"] != ch:
            return False
        if eid.startswith("campaign:"):                       # a campaign budget: every row in that campaign
            return str(r.get("campaign_id")) == eid.split(":", 1)[1]
        return str(r.get("entity_id")) == eid
    b = _sum([r for r in rows if _between(r, day0 - dt.timedelta(days=days), day0 - dt.timedelta(days=1))], pick)
    a = _sum([r for r in rows if _between(r, day0, day0 + dt.timedelta(days=days - 1))], pick)
    if str(p["action"]).startswith("pause"):
        v = "better" if b["spend"] > 0 and a["spend"] < b["spend"] * (1 - FLAT) else ("flat" if b["spend"] == 0 else "worse")
        note = f"Spend {b['spend']} before, {a['spend']} after the pause ({days} days each)"
    else:
        v = verdict(b["cpi"], a["cpi"], lower_is_better=True)
        note = f"Cost per install {b['cpi']} before, {a['cpi']} after"
    return v, b, a, note


def outcome_tracker(ctx) -> dict:
    from control import engine as ceng
    from . import studio
    con = ctx.con
    done = {r[0] for r in con.execute("SELECT outcome_id FROM control.outcomes").fetchall()}
    rows = None
    made = 0
    todo = []
    for it in studio.items(con, status=("approved",), limit=500):
        if it["kind"] in ("draft", "listing") and it.get("decided_at"):
            todo.append(("studio", it["item_id"], it.get("app"), "approved", it["decided_at"], it))
    try:
        cur = con.execute("SELECT proposal_id, app, status, decided_at, entity_key, action FROM control.proposals "
                          "WHERE status IN ('approved', 'applied') AND decided_at IS NOT NULL")
        cols = [c[0] for c in cur.description]
        for r in cur.fetchall():
            p = dict(zip(cols, r))
            todo.append(("proposal", p["proposal_id"], p["app"], p["status"], p["decided_at"], p))
    except Exception:  # noqa: BLE001 - no proposals table: nothing to measure there
        pass
    for source, ref, app, decision, decided_at, obj in todo:
        day0 = _d(decided_at) + dt.timedelta(days=1)          # the first full day after the decision
        for days in WINDOWS:
            oid = f"{source}:{ref}:{days}"
            if oid in done or ctx.today < day0 + dt.timedelta(days=days + LAG_DAYS):
                continue
            if rows is None:
                rows = ceng.load_rows(con, ctx.today - dt.timedelta(days=120), ctx.today)
            if source == "proposal":
                v, b, a, note = _measure_proposal(obj, day0, days, rows)
            elif obj["kind"] == "draft":
                v, b, a, note = _measure_draft(ctx, obj, day0, days, rows)
            else:
                v, b, a, note = _measure_listing(ctx, obj, day0, days)
            con.execute("INSERT INTO control.outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [oid, source, ref, app, decision, decided_at, days, ctx.now, v,
                         json.dumps(b) if b is not None else None, json.dumps(a) if a is not None else None, note])
            made += 1
    return {"measured_now": made, **learning(con)}


def learning(con, app: str | None = None) -> dict:
    """What your decisions and their results say so far: approval rates per kind and outcome verdicts."""
    where, params = ("WHERE app = ?", [app]) if app else ("", [])
    kinds = {}
    for kind, st, n in con.execute(f"SELECT kind, status, count(*) FROM control.studio_items {where} "
                                   f"{'AND' if where else 'WHERE'} status IN ('approved', 'rejected') GROUP BY 1, 2", params).fetchall():
        kinds.setdefault(kind, {"approved": 0, "rejected": 0})[st] = int(n)
    verdicts, recent = {}, []
    for v, n in con.execute(f"SELECT verdict, count(*) FROM control.outcomes {where} GROUP BY 1", params).fetchall():
        verdicts[v] = int(n)
    for src, ref, a, days, v, note in con.execute(f"SELECT source, ref_id, app, days, verdict, note FROM control.outcomes {where} "
                                                  "ORDER BY measured_at DESC LIMIT 8", params).fetchall():
        recent.append({"source": src, "ref": ref, "app": a, "days": days, "verdict": v, "note": note})
    return {"decisions": kinds, "verdicts": verdicts, "recent": recent}


def past_decisions(con, app: str, kind: str = "angle", limit: int = 8) -> dict:
    """Titles you approved and rejected, for the writing agents to learn from."""
    out = {"approved": [], "rejected": []}
    for title, st in con.execute("SELECT title, status FROM control.studio_items WHERE app = ? AND kind = ? AND status IN "
                                 f"('approved', 'rejected') ORDER BY decided_at DESC NULLS LAST LIMIT {int(limit) * 2}", [app, kind]).fetchall():
        if len(out[st]) < limit:
            out[st].append(title)
    return out
