"""
App comparison for the Revenue tab (read-only).

One query over revenue.sales_reports returns, per app, day and store (App Store / Google Play), for the last
WINDOW_DAYS days:

  downloads     first-time downloads (transaction_type 'download'): Apple's first-time downloads, Google Play's
                daily user installs
  paid          paid units: subscriptions and in-app purchases (restores and refunds excluded)
  proceeds      proceeds in USD only; rows in other currencies are counted, never converted or added

Apps are the registered apps (revenue.applications) plus any other app in the same store account whose rows
the import couldn't match to one (app_id NULL). Those are grouped by the app's store SKU (an app row's own
SKU, an in-app purchase's parent SKU) and named from EXTRA_APPS when known; they are comparison-only and
are not managed by the ad bot.

The phone draws the chart and the period-over-period change from these rows, so a 90-day view can be
compared with the 90 days before it. A metric with no rows is "unavailable" on the phone, never 0.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .schema import SCHEMA

WINDOW_DAYS = 180          # 90-day view + the 90 days before it
PAID_TYPES = ("subscription", "iap")

# Other apps in the store account, by app SKU: shown in the comparison only.
EXTRA_APPS = {"vitalsync-health-tracker-00": ("vitalsync", "VitalSync Health Tracker")}


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def _i(v) -> Optional[int]:
    return None if v is None else int(v)


def compare(con, today: Optional[dt.date] = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    since = today - dt.timedelta(days=WINDOW_DAYS)
    paid = ", ".join(f"'{t}'" for t in PAID_TYPES)
    cur = con.execute(
        f"SELECT app_id, CASE WHEN app_id IS NULL THEN coalesce(parent_ref, sku) END AS app_sku, period_start AS d, source_platform AS store, "
        f"  sum(units) FILTER (WHERE transaction_type = 'download') AS downloads, "
        f"  sum(units) FILTER (WHERE transaction_type IN ({paid})) AS paid, "
        f"  sum(proceeds_amount) FILTER (WHERE proceeds_currency = 'USD') AS proceeds, "
        f"  count(*) FILTER (WHERE proceeds_amount <> 0 AND coalesce(proceeds_currency, '') <> 'USD') AS other_ccy, "
        f"  bool_or(data_status = 'estimated') AS estimated "
        f"FROM {SCHEMA}.sales_reports WHERE period_start >= ? GROUP BY ALL ORDER BY 3, 4",
        [since])
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    series: dict[str, list] = {}
    extra: dict[str, dict] = {}
    other_ccy, estimated, last = 0, False, None
    stores: set = set()
    for r in rows:
        other_ccy += int(r["other_ccy"] or 0)
        estimated = estimated or bool(r["estimated"])
        d = _iso(r["d"])
        last = max(last, d) if last else d
        key = r["app_id"]
        if key is None:
            sku = r["app_sku"] or "unknown"
            key, name = EXTRA_APPS.get(sku, (f"sku:{sku}", f"Other app ({sku})"))
            extra[key] = {"app_id": key, "name": name, "sku": sku}
        series.setdefault(key, []).append([d, _i(r["downloads"]), _i(r["paid"]), _f(r["proceeds"]), r["store"]])
        stores.add(r["store"])
    return {"window_days": WINDOW_DAYS, "since": since.isoformat(), "today": today.isoformat(), "last_day": last,
            "series": series,                        # app_id -> [[day, downloads, paid, proceeds_usd, store], ...]
            "fields": ["day", "downloads", "paid", "proceeds", "store"],
            "stores": sorted(stores),                # stores with any rows in the window
            "extra_apps": list(extra.values()),      # comparison-only apps (not in the registry)
            "other_currency_rows": other_ccy, "estimated": estimated}
