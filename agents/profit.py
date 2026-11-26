"""
Profit Tracker (phase 5): what an install is actually worth, and which keywords / ad sets earn more than they cost.

  value per download   per app, store proceeds (net of refunds, budget currency only) ÷ store downloads over the
                       last 90 complete days. Organic and paid installs are mixed: it is an average, not attribution.
  estimated return     per keyword / ad set over 28 days: installs × that app's value per download ÷ spend.
  sync gaps            how far behind each store's proceeds are, and stores that report units but no money.

Read-only. The Rules tab's "value per install" is used by the decision engine; this agent only SUGGESTS a value
there (you type it in yourself). It never changes rules, bids or budgets.
"""
from __future__ import annotations

import datetime as dt

MIN_DOWNLOADS = 20          # below this the value per download is shown as "early"
LAG_WARN_DAYS = 4           # store proceeds older than this are reported as a gap
EARNING, LOSING = 1.0, 0.5  # estimated return thresholds
MIN_SPEND = 10.0            # an ad must have spent this much to be called losing


def value_per_download(store_rows: list[tuple], names: dict, apps: list[str], cur: str) -> dict:
    """store_rows: (app_id, proceeds_currency, transaction_type, units, proceeds). Pure, for tests."""
    acc = {a: {"proceeds": 0.0, "downloads": 0, "other_currency": 0} for a in apps}
    for app_id, ccy, ttype, units, proceeds in store_rows:
        app = names.get(app_id)
        if app not in acc:
            continue
        if ttype == "download":
            acc[app]["downloads"] += int(units or 0)
        if proceeds is not None:
            if ccy and ccy != cur:
                acc[app]["other_currency"] += 1
            else:
                acc[app]["proceeds"] += float(proceeds)          # refunds are negative lines
    out = {}
    for app, a in acc.items():
        v = round(a["proceeds"] / a["downloads"], 2) if a["downloads"] else None
        out[app] = {"proceeds": round(a["proceeds"], 2), "downloads": a["downloads"], "value_per_download": v,
                    "confidence": None if v is None else ("ok" if a["downloads"] >= MIN_DOWNLOADS else "early"),
                    "other_currency_lines": a["other_currency"]}
    return out


def score_ads(rows: list[dict], vpd: dict, resolve, cur: str) -> list[dict]:
    """Group ad rows per entity and estimate what its installs earned. Pure, for tests."""
    ents: dict = {}
    for r in rows:
        if r.get("currency") and r["currency"] != cur:
            continue
        k = (r["channel"], r.get("entity_id"))
        e = ents.setdefault(k, {"channel": r["channel"], "name": r.get("keyword") or r.get("entity_id"), "campaign": r.get("campaign"),
                                "app": resolve(r), "spend": 0.0, "installs": 0.0, "taps": 0.0})
        e["spend"] += r.get("spend") or 0.0
        e["installs"] += r.get("installs") or 0.0
        e["taps"] += r.get("taps") or 0.0
    out = []
    for e in ents.values():
        v = (vpd.get(e["app"]) or {}).get("value_per_download")
        e["spend"], e["installs"], e["taps"] = round(e["spend"], 2), round(e["installs"], 1), int(e["taps"])
        e["est_value"] = round(e["installs"] * v, 2) if v is not None else None
        e["est_return"] = round(e["est_value"] / e["spend"], 2) if (e["est_value"] is not None and e["spend"] > 0) else None
        out.append(e)
    return out


def profit_tracker(ctx) -> dict:
    import dlt_pipeline as bot
    from revenue.schema import SCHEMA as REV
    from .money import _resolve
    con, cur = ctx.con, ctx.rules.budget_currency
    end = ctx.today - dt.timedelta(days=1)
    start90, start28 = end - dt.timedelta(days=89), end - dt.timedelta(days=27)
    names = dict(con.execute(f"SELECT app_id, bot_app_name FROM {REV}.applications WHERE bot_app_name IS NOT NULL").fetchall())
    store_rows = con.execute(
        f"SELECT app_id, proceeds_currency, transaction_type, units, proceeds_amount FROM {REV}.sales_reports "
        f"WHERE app_id IS NOT NULL AND period_start BETWEEN ? AND ?", [start90, end]).fetchall()
    vpd = value_per_download(store_rows, names, list(bot.APPS), cur)

    gaps = []
    for platform, last_money, last_any, money_lines in con.execute(
            f"SELECT source_platform, max(period_start) FILTER (WHERE proceeds_amount IS NOT NULL), max(period_start), "
            f"count(*) FILTER (WHERE proceeds_amount IS NOT NULL) FROM {REV}.sales_reports GROUP BY 1").fetchall():
        store = {"apple_app_store": "App Store", "google_play": "Google Play"}.get(platform, platform)
        if not money_lines:
            gaps.append({"store": store, "text": f"{store} reports units but no proceeds yet (the money report isn't imported or parsed)"})
            continue
        lag = (ctx.today - (last_money if isinstance(last_money, dt.date) else dt.date.fromisoformat(str(last_money)[:10]))).days
        if lag > LAG_WARN_DAYS:
            gaps.append({"store": store, "text": f"{store} proceeds stop at {str(last_money)[:10]} ({lag} days ago)", "days": lag})

    s, e = start28.isoformat(), end.isoformat()
    ads = score_ads([r for r in ctx.ad_rows() if s <= r["date"] <= e], vpd, lambda r: _resolve(ctx, r), cur)
    scored = [a for a in ads if a["est_return"] is not None]
    earning = sorted([a for a in scored if a["est_return"] >= EARNING], key=lambda a: -a["est_return"])[:5]
    losing = sorted([a for a in scored if a["est_return"] < LOSING and a["spend"] >= MIN_SPEND], key=lambda a: -a["spend"])[:5]

    set_vpi = (ctx.rules.value_per_install or {})
    suggest = {app: v["value_per_download"] for app, v in vpd.items()
               if v["value_per_download"] and v["confidence"] == "ok" and not float(set_vpi.get(app) or 0)}
    totals = {"spend": round(sum(a["spend"] for a in ads), 2),
              "est_value": round(sum(a["est_value"] or 0 for a in ads), 2)}
    totals["est_return"] = round(totals["est_value"] / totals["spend"], 2) if totals["spend"] else None
    return {"currency": cur, "window": {"value": [start90.isoformat(), e], "ads": [s, e]},
            "basis": "estimate: installs × the app's average proceeds per download (organic included)",
            "apps": vpd, "earning": earning, "losing": losing, "totals_28d": totals, "sync_gaps": gaps,
            "suggest_value_per_install": suggest,
            "note": None if any(v["value_per_download"] for v in vpd.values()) else "No store downloads with proceeds in the last 90 days"}
