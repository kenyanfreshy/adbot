"""
Import bookkeeping for the ad platforms.

The hourly job loads performance rows with dlt (merge on channel + entity + date, so a revised
report replaces the earlier numbers instead of adding to them). Around that load this module:
  * runs each source x account as its own job, so one broken account doesn't hide the others,
  * records an import run per job: rows, rows with activity, window, dates covered, currency,
    unmapped rows, and an explicit failure message,
  * keeps control.source_state up to date (the shared status model's inputs),
  * records every campaign seen in control.campaign_map, mapped or not, so unmapped campaigns
    can be reviewed instead of silently skipped.
A successful import with zero rows is recorded as exactly that, never as "synced".
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Callable, Iterable, Iterator, Optional

from .channels import CHANNELS
from .schema import SCHEMA

IGNORED = "__ignored__"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _scrub(text: str) -> str:
    try:
        from revenue.registry import scrub_text
        return scrub_text(text)
    except Exception:  # noqa: BLE001
        return text


def has_activity(r: dict) -> bool:
    return any(float(r.get(f) or 0) > 0 for f in ("impressions", "taps", "installs", "spend"))


class Job:
    """One source x account. fn() yields performance rows; connection() runs the cheapest authenticated call."""

    def __init__(self, source: str, account_id: str, fn: Callable[[], Iterable[dict]], timezone: Optional[str] = None):
        self.source, self.account_id, self.fn, self.timezone = source, account_id, fn, timezone


def collect(jobs: list[Job], start: dt.date, end: dt.date, records: list[dict], campaigns: dict,
            clock: Callable[[], dt.datetime] = now) -> Iterator[dict]:
    """Yield rows from every job, annotated with import_id and account_id, and fill `records`.

    `campaigns` collects (channel, campaign_id) -> {name, account_id, rule_app} for every campaign seen.
    A job that raises is recorded as failed with its (scrubbed) message; the others carry on."""
    for job in jobs:
        rec = {"import_id": uuid.uuid4().hex, "source": job.source, "account_id": job.account_id, "started_at": clock(),
               "finished_at": None, "status": "running", "rows": 0, "activity_rows": 0, "window_start": start,
               "window_end": end, "min_date": None, "max_date": None, "currencies": set(), "timezone": job.timezone,
               "unmapped_rows": 0, "message": None}
        records.append(rec)
        try:
            for row in job.fn():
                if row.get("_campaign_only"):          # a campaign listed but not reported (e.g. unmapped Apple campaign)
                    campaigns[(job.source, str(row["campaign_id"]))] = {"name": row.get("campaign"), "account_id": job.account_id,
                                                                       "rule_app": row.get("app")}
                    continue
                d = str(row["date"])[:10]
                row["date"] = d
                row["import_id"] = rec["import_id"]
                row["account_id"] = row.get("account_id") or job.account_id
                rec["rows"] += 1
                if has_activity(row):
                    rec["activity_rows"] += 1
                if row.get("currency"):
                    rec["currencies"].add(str(row["currency"]).upper())
                if not row.get("app") or row.get("app") == "Unassigned":
                    rec["unmapped_rows"] += 1
                rec["min_date"] = d if rec["min_date"] is None or d < rec["min_date"] else rec["min_date"]
                rec["max_date"] = d if rec["max_date"] is None or d > rec["max_date"] else rec["max_date"]
                if row.get("campaign_id") is not None:
                    campaigns[(job.source, str(row["campaign_id"]))] = {"name": row.get("campaign"), "account_id": row["account_id"],
                                                                       "rule_app": row.get("app") if row.get("app") != "Unassigned" else None}
                yield row
            rec["status"] = "succeeded"
        except Exception as e:  # noqa: BLE001 - recorded, never hidden
            rec["status"] = "failed"
            rec["message"] = _scrub(f"{type(e).__name__}: {e}")[:500]
        rec["finished_at"] = clock()


def quality(rec: dict) -> str:
    if rec["status"] == "failed":
        return "error"
    if rec["rows"] == 0:
        return "empty"
    if len(rec["currencies"]) > 1:
        return "mixed_currency"
    if rec["unmapped_rows"]:
        return "unmapped_rows"
    return "ok"


def record(con, records: list[dict], campaigns: dict, configured: dict, load_error: Optional[str] = None,
           clock: Callable[[], dt.datetime] = now) -> None:
    """Persist import runs, source states and the campaign list. `configured` maps every known source to
    whether its credentials are present. A failed dlt load (load_error) fails every run of this sync."""
    t = clock()
    for rec in records:
        if load_error and rec["status"] == "succeeded":
            rec["status"], rec["message"] = "failed", _scrub(f"rows fetched but not saved: {load_error}")[:500]
        con.execute(f"INSERT INTO {SCHEMA}.import_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [rec["import_id"], rec["source"], rec["account_id"], rec["started_at"], rec["finished_at"], rec["status"],
                     rec["rows"], rec["activity_rows"], rec["window_start"], rec["window_end"], rec["min_date"], rec["max_date"],
                     ",".join(sorted(rec["currencies"])) or None, rec["timezone"], quality(rec), rec["unmapped_rows"], rec["message"]])
    for source, info in CHANNELS.items():
        mine = [r for r in records if r["source"] == source]
        is_conf = bool(configured.get(source))
        prev = con.execute(f"SELECT last_success, latest_date_covered, last_connection_check, last_rows, last_activity_rows "
                           f"FROM {SCHEMA}.source_state WHERE source = ?", [source]).fetchone()
        last_success, latest, conn_check, last_rows, last_act = prev if prev else (None, None, None, None, None)
        status, message, accounts, currencies, tz, last_import = None, None, None, None, None, None
        if mine:
            ok = [r for r in mine if r["status"] == "succeeded"]
            bad = [r for r in mine if r["status"] == "failed"]
            status = "succeeded" if not bad else ("partial" if ok else "failed")
            message = "; ".join(f"{r['account_id'] or source}: {r['message']}" for r in bad)[:500] or None
            accounts = ",".join(sorted({str(r["account_id"]) for r in mine if r["account_id"]})) or None
            currencies = ",".join(sorted(set().union(*[r["currencies"] for r in ok]))) if ok else None
            tz = ",".join(sorted({r["timezone"] for r in mine if r["timezone"]})) or None
            last_import = mine[-1]["import_id"]
            if ok:
                last_success = max(r["finished_at"] for r in ok)
                conn_check = last_success
                dates = [r["max_date"] for r in ok if r["max_date"]]
                if dates:
                    latest = max(dates)
                last_rows = sum(r["rows"] for r in ok)
                last_act = sum(r["activity_rows"] for r in ok)
        elif is_conf or not info["supported"]:
            status = None if is_conf else "not_configured"
        else:
            status = "not_configured"
        con.execute(
            f"INSERT INTO {SCHEMA}.source_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (source) DO UPDATE SET "
            "configured = excluded.configured, supported = excluded.supported, "
            "accounts = COALESCE(excluded.accounts, source_state.accounts), currency = COALESCE(excluded.currency, source_state.currency), "
            "timezone = COALESCE(excluded.timezone, source_state.timezone), "
            "last_connection_check = COALESCE(excluded.last_connection_check, source_state.last_connection_check), "
            "last_attempt = COALESCE(excluded.last_attempt, source_state.last_attempt), last_success = excluded.last_success, "
            "latest_date_covered = excluded.latest_date_covered, last_rows = excluded.last_rows, "
            "last_activity_rows = excluded.last_activity_rows, last_status = COALESCE(excluded.last_status, source_state.last_status), "
            "last_import_id = COALESCE(excluded.last_import_id, source_state.last_import_id), "
            "message = CASE WHEN excluded.last_status IS NULL THEN source_state.message ELSE excluded.message END, "
            "updated_at = excluded.updated_at",
            [source, is_conf, info["supported"], accounts, currencies, tz, conn_check,
             max((r["started_at"] for r in mine), default=None), last_success, latest, last_rows, last_act,
             status, last_import, message, t])
    for (channel, cid), c in campaigns.items():
        con.execute(f"INSERT INTO {SCHEMA}.campaign_map VALUES (?, ?, ?, ?, ?, NULL, FALSE, ?, ?, 'system') "
                    "ON CONFLICT (channel, campaign_id) DO UPDATE SET campaign_name = excluded.campaign_name, "
                    "account_id = COALESCE(excluded.account_id, campaign_map.account_id), rule_app = excluded.rule_app, "
                    "last_seen = excluded.last_seen", [channel, cid, c.get("account_id"), c.get("name"), c.get("rule_app"), t, t])


# --------------------------------------------------------------------------- #
# Campaign mapping
# --------------------------------------------------------------------------- #
def mapping(con) -> dict:
    """(channel, campaign_id) -> app, IGNORED, or None (unmapped). Manual choices win over name rules."""
    out = {}
    for ch, cid, rule_app, manual_app, ignored in con.execute(
            f"SELECT channel, campaign_id, rule_app, manual_app, ignored FROM {SCHEMA}.campaign_map").fetchall():
        out[(ch, str(cid))] = IGNORED if ignored else (manual_app or rule_app)
    return out


def resolve_app(row: dict, cmap: dict, rule_app_fn: Callable[[str], Optional[str]]):
    """The app a performance row belongs to, using the current mapping (so re-mapping applies to history)."""
    key = (row.get("channel"), str(row.get("campaign_id")))
    if key in cmap and cmap[key] is not None:
        return cmap[key]
    return rule_app_fn(f"{row.get('campaign') or ''} {row.get('keyword') or ''}" if row.get("channel") == "Meta Ads"
                       else (row.get("campaign") or ""))


def unmapped(con) -> list[dict]:
    cur = con.execute(f"SELECT channel, campaign_id, account_id, campaign_name, first_seen, last_seen FROM {SCHEMA}.campaign_map "
                      "WHERE NOT ignored AND manual_app IS NULL AND rule_app IS NULL ORDER BY last_seen DESC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def set_mapping(con, channel: str, campaign_id: str, app: Optional[str], apps: list[str], ignored: bool = False,
                by: str = "admin") -> dict:
    if channel not in CHANNELS:
        raise ValueError("unknown channel")
    if app is not None and app not in apps:
        raise ValueError(f"app must be one of: {', '.join(apps)}")
    if not con.execute(f"SELECT 1 FROM {SCHEMA}.campaign_map WHERE channel = ? AND campaign_id = ?", [channel, campaign_id]).fetchall():
        raise ValueError("that campaign hasn't been seen in an import yet")
    con.execute(f"UPDATE {SCHEMA}.campaign_map SET manual_app = ?, ignored = ?, updated_by = ? WHERE channel = ? AND campaign_id = ?",
                [app, bool(ignored), by, channel, campaign_id])
    return {"channel": channel, "campaign_id": campaign_id, "app": app, "ignored": bool(ignored)}
