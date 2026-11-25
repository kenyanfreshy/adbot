"""
Spend Watch and Real Return.

Spend is OBSERVED spend from the imports (platforms report late), in the budget currency; rows in other currencies
are counted and reported, never converted or added. Real Return is BLENDED: all store proceeds for an app (organic
included, since there's no install attribution feed) divided by that app's ad spend over the same days. It answers
"is the app earning more than its ads cost?", not "what did each ad earn?".
"""
from __future__ import annotations

import datetime as dt


def _engine_view(ctx) -> dict:
    """The decision engine's own aggregation (read-only), so every number matches the phone app."""
    if "_engine" not in ctx.out:
        import dlt_pipeline as bot
        from control import engine as ceng
        from control import ingest
        cmap = ingest.mapping(ctx.con)
        ctx.out["_engine"] = {"agg": ceng.aggregate(ctx.ad_rows(), ctx.rules, cmap, ctx.today, bot.APPS,
                                                    lambda n: bot.app_for(n, ctx.rules)),
                              "cmap": cmap}
    return ctx.out["_engine"]


def _resolve(ctx, r) -> str | None:
    import dlt_pipeline as bot
    from control import ingest
    app = ingest.resolve_app(r, _engine_view(ctx)["cmap"], lambda n: bot.app_for(n, ctx.rules))
    return app if app in bot.APPS else None


def spend_watch(ctx) -> dict:
    import dlt_pipeline as bot
    from control import budget
    rules, cur = ctx.rules, ctx.rules.budget_currency
    agg = _engine_view(ctx)["agg"]
    pace = budget.pacing(rules, agg["mtd_app"], agg["mtd_channel"], ctx.today, bot.APPS, agg["unattributed_mtd"])
    yday = (ctx.today - dt.timedelta(days=1)).isoformat()
    by_channel: dict = {}
    other_ccy = 0
    for r in ctx.ad_rows():
        if r["date"] != yday or not r.get("spend"):
            continue
        if r.get("currency") and r["currency"] != cur:
            other_ccy += 1
            continue
        by_channel[r["channel"]] = round(by_channel.get(r["channel"], 0.0) + r["spend"], 2)
    mtd_total = round(sum(agg["mtd_app"].values()) + agg["unattributed_mtd"], 2)
    tot = pace.get("total")
    return {"currency": cur, "yesterday": yday, "yesterday_by_channel": by_channel,
            "yesterday_total": round(sum(by_channel.values()), 2),
            "mtd_by_app": {a: round(v, 2) for a, v in agg["mtd_app"].items()},
            "mtd_by_channel": {c: round(v, 2) for c, v in agg["mtd_channel"].items()},
            "mtd_unattributed": round(agg["unattributed_mtd"], 2), "mtd_total": mtd_total,
            "monthly_budget": tot["monthly"] if tot else None, "usable": tot["usable"] if tot else None,
            "daily_allowance": tot["daily_allowance"] if tot else None,
            "under_limit": (mtd_total <= tot["usable"]) if tot else None,
            "days_left": pace["days_left"], "other_currency_rows": other_ccy}


def real_return(ctx) -> dict:
    import dlt_pipeline as bot
    from revenue.schema import SCHEMA as REV
    cur = ctx.rules.budget_currency
    end = ctx.today - dt.timedelta(days=1)                      # complete days only
    windows = {"7d": end - dt.timedelta(days=6), "28d": end - dt.timedelta(days=27)}
    names = dict(ctx.con.execute(f"SELECT app_id, bot_app_name FROM {REV}.applications WHERE bot_app_name IS NOT NULL").fetchall())
    store_rows = ctx.con.execute(
        f"SELECT app_id, CAST(period_start AS VARCHAR) AS d, source_platform, "
        f"  sum(proceeds_amount) FILTER (WHERE proceeds_currency = ? AND transaction_type <> 'refund') AS proceeds, "
        f"  sum(proceeds_amount) FILTER (WHERE proceeds_currency = ? AND transaction_type = 'refund') AS refunds, "
        f"  sum(units) FILTER (WHERE transaction_type = 'download') AS downloads, "
        f"  sum(units) FILTER (WHERE transaction_type IN ('subscription', 'iap')) AS paid "
        f"FROM {REV}.sales_reports WHERE app_id IS NOT NULL AND period_start BETWEEN ? AND ? GROUP BY ALL",
        [cur, cur, windows["28d"], end]).fetchall()
    last_store_day: dict = {}
    for _app, d, store, *_ in store_rows:
        last_store_day[store] = max(last_store_day.get(store, ""), str(d)[:10])

    out = {"currency": cur, "end": end.isoformat(), "basis": "blended: all store proceeds (organic included) ÷ ad spend",
           "store_data_through": last_store_day, "apps": {}}
    for w, start in windows.items():
        s, e = start.isoformat(), end.isoformat()
        spend: dict = {}
        for r in ctx.ad_rows():
            if s <= r["date"] <= e and r.get("spend") and (not r.get("currency") or r["currency"] == cur):
                app = _resolve(ctx, r)
                if app:
                    spend[app] = spend.get(app, 0.0) + r["spend"]
        store: dict = {}
        for app_id, d, _store, proceeds, refunds, downloads, paid in store_rows:
            if not (s <= str(d)[:10] <= e):
                continue
            app = names.get(app_id)
            if app not in bot.APPS:
                continue
            g = store.setdefault(app, {"proceeds": 0.0, "downloads": 0, "paid": 0})
            g["proceeds"] += float(proceeds or 0) + float(refunds or 0)     # refunds are negative lines
            g["downloads"] += int(downloads or 0)
            g["paid"] += int(paid or 0)
        for app in bot.APPS:
            sp, st = round(spend.get(app, 0.0), 2), store.get(app)
            proceeds = round(st["proceeds"], 2) if st else None
            out["apps"].setdefault(app, {})[w] = {
                "spend": sp, "proceeds": proceeds, "downloads": st["downloads"] if st else None, "paid": st["paid"] if st else None,
                "return": round(proceeds / sp, 2) if (proceeds is not None and sp > 0) else None,
                "cost_per_paid": round(sp / st["paid"], 2) if (st and st["paid"] and sp > 0) else None}
    return out
