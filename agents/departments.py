"""Department Check: every data source in one list, and what needs your attention."""
from __future__ import annotations

import datetime as dt

from control import status as cstatus

SEVERITY = {"failed": "critical", "stale": "warning", "partial": "warning"}


def _store_sources(con) -> list[dict]:
    from revenue import registry as rev_registry
    from revenue.schema import SCHEMA as REV
    reg = rev_registry.registry(con)
    counts: dict = {}
    for t in ("sales_reports", "financial_reports"):
        for p, n in con.execute(f"SELECT source_platform, count(*) FROM {REV}.{t} GROUP BY 1").fetchall():
            counts.setdefault(p, {})[t] = int(n)
    out = []
    for s in reg["sources"]:
        if s["platform"] not in ("apple_app_store", "google_play"):
            continue
        st = cstatus.from_revenue_source({**s, "rows": counts.get(s["platform"], {})})
        out.append({"name": s["name"], "kind": "store", "state": st["state"], "label": st["label"], "detail": st.get("detail")})
    return out


def department_check(ctx) -> dict:
    from control import engine as ceng
    from control import ingest
    con, rules = ctx.con, ctx.rules
    sources, issues = [], []
    for ch, st in ceng.source_statuses(con, rules, now=ctx.now).items():
        if st["state"] == "unsupported":
            continue
        sources.append({"name": ch, "kind": "ads", "state": st["state"], "label": st["label"], "detail": st.get("detail")})
    try:
        sources += _store_sources(con)
    except Exception as e:  # noqa: BLE001 - revenue tables missing: say so instead of failing the whole check
        issues.append({"severity": "warning", "text": f"Store data couldn't be read ({type(e).__name__})"})
    for s in sources:
        sev = SEVERITY.get(s["state"])
        if sev:
            issues.append({"severity": sev, "text": f"{s['name']}: {s['label'].lower()}" + (f" ({s['detail']})" if s.get("detail") else "")})

    unmapped = [u.get("campaign_name") or u.get("campaign_id") for u in ingest.unmapped(con)]
    if unmapped:
        issues.append({"severity": "info", "text": f"{len(unmapped)} campaign(s) not mapped to an app: " + ", ".join(map(str, unmapped[:3]))})

    try:   # Play money reports downloaded but not parsed (kept for a retry)
        kept = con.execute("SELECT count(DISTINCT split_part(report_id, ':', 2)) FROM revenue.report_files WHERE source_platform = 'google_play' "
                           "AND row_count = 0 AND superseded_by IS NULL AND report_type IN ('ESTIMATED_SALES', 'EARNINGS')").fetchone()[0]
        if kept:
            issues.append({"severity": "warning", "text": f"{kept} Google Play money report(s) kept but not parsed yet"})
    except Exception:  # noqa: BLE001
        pass

    trading = None
    try:
        last = con.execute("SELECT max(ts) FROM trading.heartbeat").fetchone()[0]
        if last is not None:
            if isinstance(last, str):
                last = dt.datetime.fromisoformat(last.replace(" ", "T"))
            trading = {"last_heartbeat": last, "hours_ago": round((ctx.now - last).total_seconds() / 3600, 1)}
    except Exception:  # noqa: BLE001 - no trading schema
        pass

    from .llm import usage_today
    order = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order.get(i["severity"], 3))
    return {"sources": sources, "issues": issues, "unmapped": unmapped, "trading": trading,
            "claude_today": usage_today(con, lambda: ctx.now),
            "healthy": sum(1 for s in sources if s["state"] in cstatus.USABLE)}
