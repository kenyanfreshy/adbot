"""
Store ID check (read-only): confirms that the Apple ID, bundle ID and Play
package entered on the Revenue tab belong to real apps in your store accounts.

  App Store   GET /v1/apps/{apple_id}                   -> the app's real bundle ID
              GET /v1/apps/{apple_id}/appStoreVersions  -> its App Store state (READY_FOR_SALE, ...)
  Google Play GET /androidpublisher/v3/applications/{package}/subscriptions?pageSize=1
              -> answers only for a package in the Play Console the service account
                 was invited to; 404 means Google has no app by that name.

Results, stored on revenue.store_listings:
  check_result  verified | mismatch | not_found | no_access | no_keys | error
  check_detail  one plain sentence for the phone app
  publication_status / status_source='store_api' / status_checked_at

Where it runs: the store keys live on the adbot-hourly job, not on the web
service. "Check store IDs" in the app runs the check right away when the web
service has the keys too; otherwise it records a request (check_requested_at)
and the next hourly run (at :07) does it. The hourly run also re-checks every
listing once a day. Nothing here writes to App Store Connect or Google Play.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import urllib.parse
from typing import Optional

from . import registry
from .schema import SCHEMA

log = logging.getLogger("revenue.store_check")

RECHECK_HOURS = 24
LIVE_STATES = ("READY_FOR_SALE", "READY_FOR_DISTRIBUTION")


# --------------------------------------------------------------------------- #
# Which listings to check
# --------------------------------------------------------------------------- #
def targets(con, platform: str, only_due: bool = False) -> list[dict]:
    """Listings of one store that have their identifiers entered."""
    have = "apple_app_id IS NOT NULL" if platform == "apple_app_store" else "package_name IS NOT NULL"
    sql = (f"SELECT l.app_id, l.platform, l.apple_app_id, l.bundle_id, l.package_name, l.check_requested_at, a.name "
           f"FROM {SCHEMA}.store_listings l JOIN {SCHEMA}.applications a USING (app_id) "
           f"WHERE l.platform = ? AND {have}")
    params: list = [platform]
    if only_due:
        sql += " AND (l.check_requested_at IS NOT NULL OR l.status_checked_at IS NULL OR l.status_checked_at < ?)"
        params.append(registry.now() - dt.timedelta(hours=RECHECK_HOURS))
    cur = con.execute(sql + " ORDER BY a.name", params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def request(con, actor: str = "admin") -> int:
    """Mark every listing with identifiers as waiting for a check. Returns how many."""
    ts = registry.now()
    con.execute(f"UPDATE {SCHEMA}.store_listings SET check_requested_at = ? "
                "WHERE apple_app_id IS NOT NULL OR package_name IS NOT NULL", [ts])
    n = con.execute(f"SELECT count(*) FROM {SCHEMA}.store_listings WHERE check_requested_at = ?", [ts]).fetchone()[0]
    registry.audit(con, actor, "store_check.request", None, {"listings": int(n)}, dry_run=False)
    return int(n)


# --------------------------------------------------------------------------- #
# The checks themselves (HTTP only; no database access, so the web app can run
# them without holding the database lock)
# --------------------------------------------------------------------------- #
def _apple_state(client, apple_id: str) -> Optional[str]:
    r = client._get(f"/v1/apps/{apple_id}/appStoreVersions",
                    {"fields[appStoreVersions]": "platform,versionString,appStoreState,appVersionState,createdDate", "limit": 20},
                    "application/json")
    if r.status_code != 200:
        return None
    versions = [v.get("attributes") or {} for v in (r.json().get("data") or [])]
    versions = [v for v in versions if v.get("platform") in (None, "IOS")] or versions
    states = [v.get("appStoreState") or v.get("appVersionState") for v in versions]
    live = next((s for s in states if s in LIVE_STATES), None)
    if live:
        return live
    versions.sort(key=lambda v: v.get("createdDate") or "", reverse=True)
    return (versions[0].get("appStoreState") or versions[0].get("appVersionState")) if versions else "NO_VERSION"


def check_apple(client, row: dict) -> dict:
    """Look the Apple ID up in App Store Connect and compare the bundle ID."""
    from .apple import AppleAuthError, AppleError, _apple_detail
    apple_id, entered = row["apple_app_id"], (row.get("bundle_id") or "").strip()
    try:
        r = client._get(f"/v1/apps/{apple_id}", {"fields[apps]": "bundleId,name,sku"}, "application/json")
        if r.status_code == 404:
            return {"result": "not_found", "status": "not_found",
                    "detail": f"App Store Connect has no app with Apple ID {apple_id} in this account"}
        if r.status_code != 200:
            return {"result": "error", "detail": f"App Store Connect answered HTTP {r.status_code}: {_apple_detail(r)}"}
        a = (r.json().get("data") or {}).get("attributes") or {}
        found, name = a.get("bundleId") or "", a.get("name") or ""
        state = _apple_state(client, apple_id)
    except AppleAuthError as e:
        if e.status == 403:
            return {"result": "no_access", "detail": "the App Store Connect key can't read app details "
                                                       "(its role needs access to Apps; Sales-only keys can't)"}
        return {"result": "error", "detail": f"App Store Connect refused the key: {e}"}
    except AppleError as e:
        return {"result": "error", "detail": str(e)}
    label = f"“{name}”" if name else f"Apple ID {apple_id}"
    if not entered:
        return {"result": "verified", "status": state, "fill_bundle": found,
                "detail": f"{label} found; its bundle ID {found} was filled in"}
    if found != entered:
        how = " (only the letter case differs)" if found.lower() == entered.lower() else ""
        return {"result": "mismatch", "status": state, "found": found,
                "detail": f"{label} has bundle ID {found}, not {entered}{how}"}
    return {"result": "verified", "status": state, "detail": f"{label} matches bundle ID {found}"}


def check_play(client, row: dict) -> dict:
    """Ask the Android Publisher API about the package; only your own apps answer."""
    from .google_play import API, PlayAuthError, PlayError
    pkg = row["package_name"]
    try:
        body = client.get(f"{API}/applications/{urllib.parse.quote(pkg, safe='')}/subscriptions", {"pageSize": 1})
    except PlayAuthError as e:
        if e.where == "token" or e.status == 401:
            return {"result": "error", "detail": f"Google refused the service account key: {e}"}
        return {"result": "no_access", "detail": f"Google won't show {pkg} to the service account: it isn't in your Play Console, "
                                                  "or the account's invite doesn't cover this app yet (invites can take a day)"}
    except PlayError as e:
        return {"result": "error", "detail": str(e)}
    if body is None:
        return {"result": "not_found", "status": "not_found", "detail": f"Google Play has no app with package {pkg}"}
    return {"result": "verified", "status": "in_play_console", "detail": f"{pkg} is in your Play Console"}


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #
def save(con, row: dict, res: dict, actor: str) -> None:
    ts = registry.now()
    sets = ["check_result = ?", "check_detail = ?", "check_requested_at = NULL", "updated_at = ?"]
    params: list = [res["result"], registry.scrub_text(res.get("detail") or "")[:500], ts]
    if res.get("status"):
        sets += ["publication_status = ?", "status_source = 'store_api'", "status_checked_at = ?"]
        params += [res["status"], ts]
    elif res["result"] in ("no_access", "error"):  # 'no_keys' leaves it due, so it runs as soon as keys exist
        sets += ["status_checked_at = ?"]
        params += [ts]
    if res.get("fill_bundle"):
        sets.append("bundle_id = ?")
        params.append(res["fill_bundle"])
    con.execute(f"UPDATE {SCHEMA}.store_listings SET {', '.join(sets)} WHERE app_id = ? AND platform = ?",
                [*params, row["app_id"], row["platform"]])
    registry.audit(con, actor, "store_check.result", f"{row['app_id']}/{row['platform']}",
                   {k: v for k, v in res.items() if k != "detail"} | {"detail": res.get("detail")}, dry_run=False)


# --------------------------------------------------------------------------- #
# Clients from the environment
# --------------------------------------------------------------------------- #
def clients(environ=None, apple_client=None, play_client=None, quick: bool = False) -> dict:
    """{'apple_app_store': client | 'no_keys' | error text, 'google_play': ...}"""
    env = os.environ if environ is None else environ
    retries = 1 if quick else 5
    out: dict = {}
    if apple_client is not None:
        out["apple_app_store"] = apple_client
    else:
        from .apple import AppleError, Client, Creds
        try:
            creds = Creds.from_env(env)
            out["apple_app_store"] = Client(creds, max_retries=retries) if creds else "no_keys"
        except AppleError as e:
            out["apple_app_store"] = f"App Store Connect key settings are wrong: {e}"
    if play_client is not None:
        out["google_play"] = play_client
    else:
        from .google_play import Client, PlayError, load_service_account
        try:
            sa = load_service_account(env)
            out["google_play"] = Client(sa, max_retries=retries) if sa else "no_keys"
        except PlayError as e:
            out["google_play"] = f"Google Play service account key is wrong: {e}"
    return out


NO_KEYS = {
    "apple_app_store": "no App Store Connect key on adbot-hourly yet (ASC_ISSUER_ID, ASC_KEY_ID, ASC_PRIVATE_KEY, ASC_VENDOR_NUMBER)",
    "google_play": "no Google Play service account key on adbot-hourly yet (Secret File play-service-account.json)",
}


def check_rows(client, rows: list[dict]) -> list[tuple[dict, dict]]:
    """Run the checks for rows of one store with a ready client, or explain why not."""
    out = []
    for row in rows:
        if client == "no_keys":
            res = {"result": "no_keys", "detail": NO_KEYS[row["platform"]]}
        elif isinstance(client, str):
            res = {"result": "error", "detail": client}
        else:
            fn = check_apple if row["platform"] == "apple_app_store" else check_play
            try:
                res = fn(client, row)
            except Exception as e:  # noqa: BLE001 - one app's trouble must not stop the others
                res = {"result": "error", "detail": registry.scrub_text(f"{type(e).__name__}: {e}")}
        out.append((row, res))
    return out


# --------------------------------------------------------------------------- #
# Entry point used by the hourly job
# --------------------------------------------------------------------------- #
def run(con, environ=None, apple_client=None, play_client=None, only_due: bool = True) -> dict:
    """Check requested, never-checked and day-old listings. Never raises."""
    summary: dict = {}
    try:
        cl = clients(environ, apple_client, play_client)
        for platform in ("apple_app_store", "google_play"):
            rows = targets(con, platform, only_due)
            if cl[platform] == "no_keys":
                rows = [r for r in rows if r.get("check_requested_at")]  # answer the button; don't log "no keys" every hour
            for row, res in check_rows(cl[platform], rows):
                save(con, row, res, "scheduler")
                summary.setdefault(platform, {}).setdefault(res["result"], 0)
                summary[platform][res["result"]] += 1
    except Exception as e:  # noqa: BLE001 - the ad bot must keep running
        log.exception("store ID check failed")
        summary["failed"] = registry.scrub_text(f"{type(e).__name__}: {e}")
    return summary


def every_hours() -> int:
    """How often the adbot-hourly job runs: render.yaml schedule "7 */N * * *" (UTC). Set WORKER_EVERY_HOURS to match."""
    try:
        n = int(os.getenv("WORKER_EVERY_HOURS", "4"))
    except ValueError:
        n = 4
    return n if n in (1, 2, 3, 4, 6, 8, 12, 24) else 4


def next_hourly_run(now: Optional[dt.datetime] = None) -> dt.datetime:
    """Next scheduled run of the adbot-hourly job: minute 7 of every N-th UTC hour (cron "7 */N * * *")."""
    now = now or registry.now()
    n = every_hours()
    t = now.replace(minute=7, second=0, microsecond=0)
    while t <= now or t.hour % n:
        t += dt.timedelta(hours=1)
    return t
