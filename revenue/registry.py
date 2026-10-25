"""
Application registry: which apps exist, where they are published, which
products they sell and at what (verified or reference) price.

Seeded from the spec with reference data only:
  * Prices are the iOS prices shown in the product today, stored with
    source='reference'. They are never used as revenue; revenue comes only from
    store reports. No Android prices are seeded: Android prices are not assumed
    to match iOS.
  * Every product's type is 'unverified' (including the three-month passes)
    until the App Store Connect / Google Play connector reads the real product
    and its type in Stage C / D.
  * Store identifiers (Apple App ID, bundle ID, package name) start empty and
    are filled in by the owner on the Revenue screen or by the connectors.

Seeding uses ON CONFLICT DO NOTHING, so it never overwrites anything a person
or a connector has since corrected.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .schema import SCHEMA, STORES

REFERENCE_DATE = dt.date(2026, 10, 22)  # when the reference prices were supplied


@dataclass(frozen=True)
class AppSeed:
    app_id: str
    name: str
    bot_app_name: str
    ios_reference_prices: dict  # plan_code -> "price as text" (USD, US storefront)


APPS: list[AppSeed] = [
    AppSeed("nursebound_pro", "NurseBound Pro", "NurseBound",
            {"monthly": "14.99", "annual": "99.99", "lifetime": "149.99"}),
    AppSeed("rnr_pro", "Rapid Nursing Review Pro", "Rapid Nursing Review",
            {"monthly": "9.99", "three_month": "49.99", "annual": "89.99", "lifetime": "119.00"}),
    AppSeed("ccrn_adult", "CCRN Adult", "CCRN Adult",
            {"monthly": "12.99", "three_month": "29.99", "annual": "59.99"}),
]

PLAN_NAMES = {"monthly": "Monthly", "three_month": "Three-month pass", "annual": "Annual", "lifetime": "Lifetime", "other": "Other"}

# Credentials are referenced by environment-variable NAME only.
# 'reporting' accounts are read-only; ad execution credentials are kept separate (Stage H).
ACCOUNTS = [
    ("apple_app_store_reporting", "apple_app_store", "App Store Connect (sales, finance, analytics)", "reporting",
     ["ASC_ISSUER_ID", "ASC_KEY_ID", "ASC_PRIVATE_KEY", "ASC_VENDOR_NUMBER"]),
    ("google_play_reporting", "google_play", "Google Play Console (reports bucket + Developer API)", "reporting",
     ["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "GOOGLE_PLAY_REPORTS_BUCKET"]),
    ("apple_ads_reporting", "apple_ads", "Apple Ads", "reporting",
     ["ASA_CLIENT_ID", "ASA_TEAM_ID", "ASA_KEY_ID", "ASA_ORG_ID", "ASA_PRIVATE_KEY"]),
    ("google_ads_reporting", "google_ads", "Google Ads", "reporting",
     ["GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_IDS"]),
    ("meta_ads_reporting", "meta_ads", "Meta Ads (Facebook + Instagram)", "reporting",
     ["META_ACCESS_TOKEN", "META_AD_ACCOUNT_IDS"]),
    ("tiktok_ads_reporting", "tiktok_ads", "TikTok Ads", "reporting",
     ["TIKTOK_ACCESS_TOKEN", "TIKTOK_ADVERTISER_IDS"]),
]

_APPLE_ID = re.compile(r"^[0-9]{6,12}$")
_BUNDLE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_SECRETISH = re.compile(r"(token|secret|password|private|key|authorization|credential)", re.I)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def ref_product_key(app_id: str, platform: str, plan: str) -> str:
    return f"ref:{app_id}:{platform}:{plan}"


def sha256_ref(value: Optional[str]) -> Optional[str]:
    """Hash a purchase token / original transaction id before it touches the database."""
    if value is None or value == "":
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Audit + sync job helpers
# --------------------------------------------------------------------------- #
_TOKENISH = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"          # JWTs (Apple/ASA client secrets)
    r"|(?i:bearer)\s+[A-Za-z0-9_\-\.=+/]+"                           # Authorization headers
    r"|-----BEGIN[^-]*-----[\s\S]*?(?:-----END[^-]*-----|$)"           # PEM keys
    r"|(?<![A-Za-z0-9/])[A-Za-z0-9_\-\.=+]{40,}(?![A-Za-z0-9/])")       # long opaque strings (access tokens); URL paths are left readable


def scrub_text(text: str) -> str:
    """Remove token-looking substrings from free text such as API error messages."""
    return _TOKENISH.sub("[redacted]", text)


def redact(obj):
    """Drop anything that looks like a credential before it is logged."""
    if isinstance(obj, dict):
        return {k: ("[redacted]" if _SECRETISH.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def audit(con, actor: str, action: str, target: Optional[str] = None, detail=None, dry_run: bool = True) -> str:
    audit_id = str(uuid.uuid4())
    text = json.dumps(redact(detail), default=str)[:4000] if detail is not None else None
    con.execute(f"INSERT INTO {SCHEMA}.audit_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                [audit_id, now(), actor, action, target, text, bool(dry_run)])
    return audit_id


def start_job(con, source: str, job_type: str, period_start=None, period_end=None, attempt: int = 1) -> str:
    job_id = str(uuid.uuid4())
    con.execute(f"INSERT INTO {SCHEMA}.synchronization_jobs (job_id, source, job_type, period_start, period_end, status, attempt, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?)", [job_id, source, job_type, period_start, period_end, attempt, now()])
    return job_id


def finish_job(con, job_id: str, status: str, rows_seen: int = 0, rows_inserted: int = 0, message: Optional[str] = None,
               outcome: Optional[str] = None) -> None:
    if status not in ("succeeded", "partial", "failed", "skipped"):
        raise ValueError(f"bad job status {status!r}")
    msg = None if message is None else str(redact(message))[:500]
    if outcome is None:
        con.execute(f"UPDATE {SCHEMA}.synchronization_jobs SET status = ?, finished_at = ?, rows_seen = ?, rows_inserted = ?, message = ? "
                    "WHERE job_id = ?", [status, now(), rows_seen, rows_inserted, msg, job_id])
    else:
        con.execute(f"UPDATE {SCHEMA}.synchronization_jobs SET status = ?, finished_at = ?, rows_seen = ?, rows_inserted = ?, message = ?, "
                    "outcome = ? WHERE job_id = ?", [status, now(), rows_seen, rows_inserted, msg, outcome, job_id])


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
def seed(con) -> dict:
    """Insert the reference registry. Idempotent; never overwrites edits."""
    ts = now()
    counts = {"applications": 0, "store_listings": 0, "store_products": 0, "product_prices": 0, "store_accounts": 0}

    def ins(table: str, sql: str, params: list) -> None:
        before = con.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0]
        con.execute(sql, params)
        after = con.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0]
        counts[table] += int(after) - int(before)

    for a in APPS:
        ins("applications", f"INSERT INTO {SCHEMA}.applications VALUES (?, ?, ?, TRUE, ?, ?, ?) ON CONFLICT DO NOTHING",
            [a.app_id, a.name, a.bot_app_name, "seeded from spec; identifiers pending", ts, ts])
        for platform in STORES:
            ins("store_listings",
                f"INSERT INTO {SCHEMA}.store_listings (app_id, platform, updated_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [a.app_id, platform, ts])
        for plan, price in a.ios_reference_prices.items():
            key = ref_product_key(a.app_id, "apple_app_store", plan)
            ins("store_products",
                f"INSERT INTO {SCHEMA}.store_products (product_key, app_id, platform, store_product_id, plan_code, display_name, "
                "product_type, billing_period, availability, source, verified, created_at, updated_at) "
                "VALUES (?, ?, 'apple_app_store', NULL, ?, ?, 'unverified', NULL, 'unknown', 'reference', FALSE, ?, ?) ON CONFLICT DO NOTHING",
                [key, a.app_id, plan, f"{a.name} {PLAN_NAMES[plan]}", ts, ts])
            ins("product_prices",
                f"INSERT INTO {SCHEMA}.product_prices VALUES (?, 'US', 'USD', ?, 'reference', ?, NULL, ?, ?) ON CONFLICT DO NOTHING",
                [key, str(Decimal(price)), REFERENCE_DATE, "owner-supplied iOS price list", ts])
    for account_id, platform, name, purpose, env in ACCOUNTS:
        ins("store_accounts",
            f"INSERT INTO {SCHEMA}.store_accounts (account_id, platform, display_name, purpose, credential_env_vars, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [account_id, platform, name, purpose, ",".join(env), ts])
    if any(counts.values()):
        audit(con, "system", "registry.seed", None, counts, dry_run=False)
    return counts


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def credential_state(env_vars: list[str], environ=None) -> dict:
    """Which credential variables are set. Reports names only, never values."""
    environ = os.environ if environ is None else environ
    missing = [v for v in env_vars if not str(environ.get(v, "")).strip()]
    return {"required": env_vars, "missing": missing, "present": not missing}


def registry(con, environ=None) -> dict:
    apps = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.applications ORDER BY name"))
    listings = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.store_listings"))
    products = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.store_products ORDER BY app_id, platform, plan_code"))
    prices = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.product_prices WHERE effective_to IS NULL"))
    accounts = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.store_accounts ORDER BY account_id"))
    last_ok = {r["source"]: r for r in _dicts(con.execute(
        f"SELECT source, max(finished_at) AS last_success, max(period_end) AS period_end FROM {SCHEMA}.synchronization_jobs "
        "WHERE status IN ('succeeded', 'partial') GROUP BY source"))}
    last_any = {r["source"]: r for r in _dicts(con.execute(
        f"SELECT source, status, message, started_at FROM {SCHEMA}.synchronization_jobs j "
        f"WHERE started_at = (SELECT max(started_at) FROM {SCHEMA}.synchronization_jobs k WHERE k.source = j.source)"))}

    by_product: dict[str, list] = {}
    for p in prices:
        by_product.setdefault(p["product_key"], []).append(p)
    out_apps = []
    for a in apps:
        aid = a["app_id"]
        out_apps.append({
            **a,
            "listings": {l["platform"]: l for l in listings if l["app_id"] == aid},
            "products": [{**p, "prices": by_product.get(p["product_key"], [])} for p in products if p["app_id"] == aid],
        })
    sources = []
    for acc in accounts:
        cred = credential_state([v for v in acc["credential_env_vars"].split(",") if v], environ)
        if acc["status"] in ("verified", "error"):
            # credentials live on the hourly job, not this web service: trust what the job reported
            cred = {**cred, "missing": [], "present": True}
        ok, last = last_ok.get(acc["platform"]), last_any.get(acc["platform"])
        sources.append({
            "account_id": acc["account_id"], "platform": acc["platform"], "name": acc["display_name"], "purpose": acc["purpose"],
            "credentials": cred, "status": acc["status"] if acc["status"] != "not_configured" else ("configured" if cred["present"] else "not_configured"),
            "last_success": ok["last_success"] if ok else None, "period_end": ok["period_end"] if ok else None,
            "last_status": last["status"] if last else None, "last_message": last["message"] if last else None,
            "last_error": acc.get("last_error"),
        })
        if acc["platform"] == "google_play":
            # Products and reports are separate jobs: "last sync" is the last good REPORT pass,
            # so a working product sync never makes a refused bucket look healthy.
            from .google_play import report_state
            rep = report_state(con)
            prod = con.execute(f"SELECT max(finished_at) FROM {SCHEMA}.synchronization_jobs WHERE source = 'google_play' "
                               "AND job_type IN ('products', 'daily') AND status IN ('succeeded', 'partial')").fetchone()[0]
            lp = con.execute(f"SELECT status, message FROM {SCHEMA}.synchronization_jobs WHERE source = 'google_play' "
                             "AND job_type IN ('products', 'daily') ORDER BY started_at DESC LIMIT 1").fetchone()
            sources[-1].update({"reports": rep, "products_last_success": prod, "last_success": rep["last_success"],
                                "last_status": lp[0] if lp else None, "last_message": lp[1] if lp else None})
    return {"apps": out_apps, "sources": sources}


# --------------------------------------------------------------------------- #
# Owner edits (store identifiers only; never prices, never credentials)
# --------------------------------------------------------------------------- #
class RegistryError(ValueError):
    pass


def update_listing(con, app_id: str, platform: str, fields: dict, actor: str = "admin") -> dict:
    if platform not in STORES:
        raise RegistryError("platform must be apple_app_store or google_play")
    if not con.execute(f"SELECT 1 FROM {SCHEMA}.applications WHERE app_id = ?", [app_id]).fetchall():
        raise RegistryError(f"unknown app {app_id!r}")
    allowed = ("apple_app_id", "bundle_id") if platform == "apple_app_store" else ("package_name",)
    clean: dict[str, Optional[str]] = {}
    for k in allowed:
        if k not in fields:
            continue
        v = str(fields[k] or "").strip() or None
        if v is not None:
            if k == "apple_app_id" and not _APPLE_ID.match(v):
                raise RegistryError("Apple App ID is the 6–12 digit number in App Store Connect (App Information → Apple ID)")
            if k == "bundle_id" and not _BUNDLE.match(v):
                raise RegistryError("Bundle ID looks like com.company.app")
            if k == "package_name" and not _PACKAGE.match(v):
                raise RegistryError("Package name looks like com.company.app")
            col = k
            dup = con.execute(f"SELECT app_id FROM {SCHEMA}.store_listings WHERE {col} = ? AND NOT (app_id = ? AND platform = ?)",
                              [v, app_id, platform]).fetchall()
            if dup:
                raise RegistryError(f"{v} is already set on {dup[0][0]}")
        clean[k] = v
    unknown = set(fields) - set(allowed)
    if unknown:
        raise RegistryError(f"can't set {', '.join(sorted(unknown))} on {platform}")
    if not clean:
        raise RegistryError("nothing to update")
    sets = ", ".join(f"{k} = ?" for k in clean) + ", updated_at = ?"
    con.execute(f"UPDATE {SCHEMA}.store_listings SET {sets} WHERE app_id = ? AND platform = ?",
                [*clean.values(), now(), app_id, platform])
    audit(con, actor, "registry.update_listing", f"{app_id}/{platform}", clean, dry_run=False)
    return clean
