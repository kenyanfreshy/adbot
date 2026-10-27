"""
One status model for every data source (ad platforms and app stores).

A source is in exactly one of these states:

  not_configured           no credentials / settings on the job that imports it
  unsupported              no integration exists for it in AdBot (e.g. TikTok Ads today)
  connected_not_imported   the platform accepted the credentials, but no import has finished yet
  imported_zero            the last import finished and the platform reported no activity
  imported_activity        the last import finished with impressions, clicks, installs or spend
  partial                  the last import finished for some accounts or reports but not all
  stale                    the newest successful data is older than the freshness limit
  failed                   the last attempt failed and nothing newer succeeded

Four timestamps are always kept apart: last connection check, last import attempt,
last successful import, and the latest reporting date the data covers.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

STATES = ("not_configured", "unsupported", "connected_not_imported", "imported_zero",
          "imported_activity", "partial", "stale", "failed")

LABEL = {
    "not_configured": "Not configured",
    "unsupported": "Not supported yet",
    "connected_not_imported": "Connected, nothing imported yet",
    "imported_zero": "Imported, no activity",
    "imported_activity": "Imported",
    "partial": "Partial",
    "stale": "Stale",
    "failed": "Failed",
}

# states whose data the decision engine may use
USABLE = ("imported_activity", "imported_zero")
# states that need someone's attention, with a severity
ATTENTION = {"failed": "critical", "stale": "warning", "partial": "warning"}


def _ts(v) -> Optional[dt.datetime]:
    if v is None or isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    return dt.datetime.fromisoformat(str(v).replace("Z", "").replace(" ", "T"))


def _date(v) -> Optional[dt.date]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def classify(*, configured: bool, supported: bool = True, last_connection_check=None, last_attempt=None,
             last_success=None, latest_date_covered=None, last_status: Optional[str] = None,
             last_activity_rows: Optional[int] = None, message: Optional[str] = None,
             now: Optional[dt.datetime] = None, stale_after_hours: float = 36, max_data_lag_days: int = 3) -> dict:
    """Return {'state', 'label', 'detail', and the four timestamps} for one source."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    lc, la, ls, ld = _ts(last_connection_check), _ts(last_attempt), _ts(last_success), _date(latest_date_covered)
    out = {"last_connection_check": lc, "last_attempt": la, "last_success": ls, "latest_date_covered": ld}

    def res(state, detail=None):
        return {"state": state, "label": LABEL[state], "detail": detail, **out}

    if not supported:
        return res("unsupported", "AdBot has no integration for this platform yet")
    if not configured:
        return res("not_configured", "Credentials are not set on the job that imports this source")
    failed_last = last_status == "failed" and (ls is None or (la is not None and la > ls))
    if failed_last:
        return res("failed", message or "The last import failed")
    if ls is None:
        if lc is not None:
            return res("connected_not_imported", "The platform accepted the credentials; no import has finished yet")
        return res("connected_not_imported", "Credentials are set; no import has run yet")
    if last_status == "partial":
        return res("partial", message or "Some accounts or reports were not imported")
    if now - ls > dt.timedelta(hours=stale_after_hours):
        return res("stale", f"Last successful import {int((now - ls).total_seconds() // 3600)} h ago")
    if ld is not None and (now.date() - ld).days > max_data_lag_days and (last_activity_rows or 0) > 0:
        return res("stale", f"Newest reporting date is {ld.isoformat()}")
    if not last_activity_rows:
        return res("imported_zero", "The platform reported no impressions, clicks, installs or spend for the window")
    return res("imported_activity")


def from_source_row(row: dict, now=None, stale_after_hours: float = 36) -> dict:
    """Classify a control.source_state row."""
    return {"source": row["source"], "accounts": row.get("accounts"), "currency": row.get("currency"),
            "timezone": row.get("timezone"), "rows": row.get("last_rows"),
            **classify(configured=bool(row.get("configured")), supported=bool(row.get("supported", True)),
                       last_connection_check=row.get("last_connection_check"), last_attempt=row.get("last_attempt"),
                       last_success=row.get("last_success"), latest_date_covered=row.get("latest_date_covered"),
                       last_status=row.get("last_status"), last_activity_rows=row.get("last_activity_rows"),
                       message=row.get("message"), now=now, stale_after_hours=stale_after_hours)}


def from_revenue_source(src: dict, now=None, stale_after_hours: float = 48) -> dict:
    """Map a revenue.registry source (App Store Connect, Google Play, ad accounts) onto the same model."""
    configured = bool((src.get("credentials") or {}).get("present")) or src.get("status") in ("verified", "error")
    rows = src.get("rows") or {}
    activity = sum(int(v or 0) for k, v in rows.items() if k in ("sales_reports", "financial_reports", "advertising_spend"))
    rep = src.get("reports")
    if rep is not None:   # Google Play: products and reports are separate; reports decide the data state
        st = rep.get("state")
        base = classify(configured=configured, last_attempt=rep.get("last_attempt"), last_success=rep.get("last_success"),
                        last_status="failed" if st in ("access_denied", "not_found", "error") else "succeeded",
                        last_activity_rows=activity, message=(rep.get("message") or "").split(" | ")[0] or None,
                        last_connection_check=src.get("products_last_success"), now=now, stale_after_hours=stale_after_hours)
        if st == "not_configured":
            base.update(state="partial", label=LABEL["partial"], detail="Products sync; the reports bucket is not configured")
        elif st in ("access_denied", "not_found", "error") and src.get("products_last_success"):
            base.update(state="partial", label=LABEL["partial"],
                        detail="Products sync; reports: " + ((rep.get("message") or rep.get("text") or st).split(" | ")[0]))
        elif st == "never_checked":
            base.update(state="connected_not_imported", label=LABEL["connected_not_imported"], detail="Reports not checked yet")
        return {"source": src.get("name"), "platform": src.get("platform"), **base}
    last_status = "failed" if (src.get("status") == "error" or src.get("last_status") == "failed") else src.get("last_status")
    base = classify(configured=configured, last_attempt=src.get("last_attempt") or src.get("last_success"),
                    last_success=src.get("last_success"), latest_date_covered=src.get("period_end"),
                    last_status=last_status, last_activity_rows=activity,
                    message=src.get("last_error") or (src.get("last_message") or "").split(" | ")[0] or None,
                    now=now, stale_after_hours=stale_after_hours)
    if last_status == "failed" and src.get("last_success") and src.get("status") == "error":
        base.update(state="failed", label=LABEL["failed"])
    return {"source": src.get("name"), "platform": src.get("platform"), **base}
