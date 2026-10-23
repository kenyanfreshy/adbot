"""
Stage D: Google Play connector (read-only).

Credentials (adbot-hourly on Render):
  * the service account key as a Secret File  (any single *.json in /etc/secrets,
    or GOOGLE_PLAY_SERVICE_ACCOUNT_FILE, or the JSON text in GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
  * GOOGLE_PLAY_REPORTS_BUCKET  e.g. pubsite_prod_7714024473854983317 (optional until reports exist)

What it pulls, once a day:
  1. Products + prices  (Android Publisher API v3, monetization)
       GET /androidpublisher/v3/applications/{package}/subscriptions
       GET /androidpublisher/v3/applications/{package}/oneTimeProducts
     -> every base plan / purchase option with its real type (auto-renewing,
        prepaid = non-renewing, one-time) and its billing period, plus the
        regional price list, recorded as store_api prices with effective dates.
  2. Money reports      (the Play Console reports bucket, Cloud Storage JSON API)
       sales/salesreport_YYYYMM.zip   -> revenue.sales_reports   ('estimated')
       earnings/earnings_YYYYMM.zip   -> revenue.financial_reports ('finalized')
     Every file is archived as received; a file is re-read only when Google
     publishes a new generation of it. Lines are parsed by header name and
     only when the columns we need are present; otherwise the file is kept and
     the job says so. Nothing is guessed, nothing is written to Google.

Facts relied on (official docs, checked 2026-10-23):
  * list URLs, pageSize/pageToken, response fields subscriptions[] / oneTimeProducts[] / nextPageToken
  * base plan types autoRenewingBasePlanType / prepaidBasePlanType / installmentsBasePlanType
    with billingPeriodDuration; regionalConfigs[].price and
    purchaseOptions[].regionalPricingAndAvailabilityConfigs[].price as Money
    (currencyCode, units, nanos; units and nanos carry the same sign)
  * bucket layout sales/salesreport_YYYYMM.zip, earnings/earnings_YYYYMM.zip;
    service account needs "View financial data" and the devstorage.read_only scope
  * report time zones: Play Help says sales order dates are UTC and earnings dates
    are Pacific Time; another Play Help page says financial data is UTC. Both are
    recorded as the source states them, not converted.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.parse
import zipfile
from decimal import Decimal
from typing import Callable, Iterable, Optional

from . import registry
from .money import to_decimal
from .schema import SCHEMA

log = logging.getLogger("revenue.google_play")

PLATFORM = "google_play"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GCS = "https://storage.googleapis.com/storage/v1"
SCOPES = "https://www.googleapis.com/auth/androidpublisher https://www.googleapis.com/auth/devstorage.read_only"
SECRET_DIR = "/etc/secrets"
SALES_TZ = "UTC (Google Play sales order dates)"
EARNINGS_TZ = "Google Play earnings month (Pacific Time per Play Help)"
PERIOD_PLAN = {"P1M": "monthly", "P3M": "three_month", "P1Y": "annual", "P12M": "annual"}


class PlayError(Exception):
    pass


class PlayAuthError(PlayError):
    def __init__(self, msg: str, status: int = 401, where: str = ""):
        super().__init__(msg)
        self.status, self.where = status, where


class PlayFormatError(PlayError):
    pass


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def load_service_account(environ=None) -> Optional[dict]:
    env = os.environ if environ is None else environ
    text, where = "", ""
    path = str(env.get("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", "") or "").strip()
    if not path:
        d = env.get("GOOGLE_SECRET_DIR", SECRET_DIR)
        try:
            files = sorted(f for f in os.listdir(d) if f.lower().endswith(".json"))
        except OSError:
            files = []
        preferred = [f for f in files if "play" in f.lower() or "service" in f.lower()]
        pick = preferred if len(preferred) == 1 else files if len(files) == 1 else []
        path = os.path.join(d, pick[0]) if pick else ""
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text, where = fh.read(), f"secret file {os.path.basename(path)}"
    elif str(env.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "") or "").strip():
        text, where = str(env["GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"]), "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"
    if not text.strip():
        return None
    try:
        sa = json.loads(text)
    except ValueError:
        raise PlayError(f"{where} isn't valid JSON ({len(text)} characters). Paste the whole downloaded key file")
    missing = [k for k in ("type", "client_email", "private_key", "token_uri") if not sa.get(k)]
    if missing or sa.get("type") != "service_account":
        raise PlayError(f"{where} isn't a service account key (missing {', '.join(missing) or 'type=service_account'})")
    return sa


def bucket_name(environ=None) -> Optional[str]:
    env = os.environ if environ is None else environ
    raw = str(env.get("GOOGLE_PLAY_REPORTS_BUCKET", "") or "").strip()
    if not raw:
        return None
    m = re.search(r"(pubsite_prod[A-Za-z0-9_]*)", raw)   # accept gs:// URIs and download links too
    if not m:
        raise PlayError("GOOGLE_PLAY_REPORTS_BUCKET should look like pubsite_prod_1234… (copied from a report link)")
    return m.group(1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, sa: dict, session=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, max_retries: int = 5):
        import requests
        self.sa, self.http = sa, session or requests.Session()
        self.sleep, self.clock, self.max_retries = sleep, clock, max_retries
        self._tok, self._exp = None, 0.0
        self.calls = 0

    def _token(self) -> str:
        if self._tok and self.clock() < self._exp - 120:
            return self._tok
        import jwt
        now = int(self.clock())
        assertion = jwt.encode({"iss": self.sa["client_email"], "scope": SCOPES, "aud": self.sa["token_uri"],
                                "iat": now, "exp": now + 3600}, self.sa["private_key"], algorithm="RS256",
                               headers={"kid": self.sa.get("private_key_id")} if self.sa.get("private_key_id") else None)
        self.calls += 1
        r = self.http.post(self.sa["token_uri"], timeout=30,
                           data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        if r.status_code != 200:
            raise PlayAuthError(f"Google refused the service account key (HTTP {r.status_code}: {_detail(r)}) "
                                f"[{self.sa['client_email']}]", r.status_code, "token")
        body = r.json()
        self._tok, self._exp = body["access_token"], self.clock() + float(body.get("expires_in", 3600))
        return self._tok

    def get(self, url: str, params: Optional[dict] = None, raw: bool = False):
        last = None
        where = url.replace(API, "androidpublisher").replace(GCS, "storage").split("?")[0]
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            try:
                r = self.http.get(url, params=params or {}, timeout=60, headers={"Authorization": f"Bearer {self._token()}"})
            except PlayAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                last = registry.scrub_text(f"{type(e).__name__}: {e}")
                self.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code in (401, 403):
                raise PlayAuthError(f"HTTP {r.status_code} on {where}: {_detail(r)}", r.status_code, where)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {_detail(r)}"
                self.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise PlayError(f"HTTP {r.status_code} on {where}: {_detail(r)}")
            return r.content if raw else r.json()
        raise PlayError(f"gave up after {self.max_retries + 1} attempts on {where} ({last})")

    def pages(self, url: str, key: str, params: Optional[dict] = None) -> Iterable[dict]:
        p = dict(params or {})
        while True:
            body = self.get(url, p)
            if body is None:
                return
            yield from body.get(key) or []
            tok = body.get("nextPageToken")
            if not tok:
                return
            p["pageToken"] = tok


def _detail(r) -> str:
    try:
        b = r.json()
        e = b.get("error")
        msg = (e.get("message") if isinstance(e, dict) else None) or b.get("error_description") or (e if isinstance(e, str) else "")
    except Exception:  # noqa: BLE001
        msg = (r.text or "")[:200]
    return registry.scrub_text(str(msg))[:300]


# --------------------------------------------------------------------------- #
# Products + prices
# --------------------------------------------------------------------------- #
def money(m: Optional[dict]) -> Optional[tuple[str, Decimal]]:
    if not m or not m.get("currencyCode"):
        return None
    units = Decimal(str(m.get("units", "0") or "0"))
    nanos = Decimal(int(m.get("nanos", 0) or 0)) / Decimal(1_000_000_000)
    return m["currencyCode"].upper(), units + nanos


def _title(listings) -> str:
    for l in listings or []:
        if l.get("title"):
            return l["title"]
    return ""


def _plan(period: Optional[str], text: str) -> str:
    if period in PERIOD_PLAN:
        return PERIOD_PLAN[period]
    t = (text or "").lower()
    if "lifetime" in t:
        return "lifetime"
    if any(w in t for w in ("3month", "3_month", "3-month", "three", "quarter", "3mo")):
        return "three_month"
    if "annual" in t or "year" in t:
        return "annual"
    if "month" in t:
        return "monthly"
    return "other"


def _upsert_product(con, app_id, key, product_id, sub_id, plan, name, ptype, period, state, now):
    con.execute(
        f"INSERT INTO {SCHEMA}.store_products (product_key, app_id, platform, store_product_id, plan_code, display_name, product_type, "
        "billing_period, base_plan_id, availability, source, verified, verified_at, created_at, updated_at) "
        "VALUES (?, ?, 'google_play', ?, ?, ?, ?, ?, ?, ?, 'store_api', TRUE, ?, ?, ?) "
        "ON CONFLICT (product_key) DO UPDATE SET plan_code = excluded.plan_code, display_name = excluded.display_name, "
        "product_type = excluded.product_type, billing_period = excluded.billing_period, availability = excluded.availability, "
        "verified = TRUE, verified_at = excluded.verified_at, updated_at = excluded.updated_at",
        [key, app_id, product_id, plan, name or product_id, ptype, period, sub_id, state or "unknown", now, now, now])


def record_prices(con, product_key: str, prices: list[tuple[str, str, Decimal]], today: dt.date, now: dt.datetime) -> int:
    """Keep a dated price history: unchanged prices are left alone, changed ones are closed and re-opened."""
    current = {(r[0], r[1]): (to_decimal(r[2]), r[3]) for r in con.execute(
        f"SELECT territory, currency, amount, effective_from FROM {SCHEMA}.product_prices "
        "WHERE product_key = ? AND source = 'store_api' AND effective_to IS NULL", [product_key]).fetchall()}
    changed = 0
    for territory, ccy, amount in prices:
        if not re.fullmatch(r"[A-Z]{2}", territory or ""):
            continue
        old = current.get((territory, ccy))
        if old and old[0] == amount:
            continue
        if old and str(old[1])[:10] == today.isoformat():
            con.execute(f"UPDATE {SCHEMA}.product_prices SET amount = ?, imported_at = ? WHERE product_key = ? AND territory = ? "
                        "AND currency = ? AND source = 'store_api' AND effective_from = ?", [str(amount), now, product_key, territory, ccy, today])
        else:
            if old:
                con.execute(f"UPDATE {SCHEMA}.product_prices SET effective_to = ? WHERE product_key = ? AND territory = ? AND currency = ? "
                            "AND source = 'store_api' AND effective_to IS NULL", [today - dt.timedelta(days=1), product_key, territory, ccy])
            con.execute(f"INSERT INTO {SCHEMA}.product_prices VALUES (?, ?, ?, ?, 'store_api', ?, NULL, 'google_play monetization API', ?) "
                        "ON CONFLICT DO NOTHING", [product_key, territory, ccy, str(amount), today, now])
        changed += 1
    return changed


def sync_products(con, client: Client, today: dt.date) -> dict:
    stats = {"apps": 0, "products": 0, "prices_changed": 0, "errors": []}
    now = registry.now()
    apps = con.execute(f"SELECT app_id, package_name FROM {SCHEMA}.store_listings WHERE platform = 'google_play' AND package_name IS NOT NULL").fetchall()
    if not apps:
        stats["errors"].append("no Play package names entered on the Revenue tab")
    for app_id, pkg in apps:
        stats["apps"] += 1
        base = f"{API}/applications/{urllib.parse.quote(pkg, safe='')}"
        try:
            for sub in client.pages(f"{base}/subscriptions", "subscriptions", {"pageSize": 100}):
                name = _title(sub.get("listings"))
                for bp in sub.get("basePlans") or []:
                    kind = ("auto_renewable_subscription", bp["autoRenewingBasePlanType"]) if bp.get("autoRenewingBasePlanType") else \
                           ("non_renewing_subscription", bp["prepaidBasePlanType"]) if bp.get("prepaidBasePlanType") else \
                           ("auto_renewable_subscription", bp["installmentsBasePlanType"]) if bp.get("installmentsBasePlanType") else (None, None)
                    if not kind[0] or not bp.get("basePlanId"):
                        continue  # unknown base plan shape: leave unverified
                    period = kind[1].get("billingPeriodDuration")
                    key = f"google_play:{sub['productId']}:{bp['basePlanId']}"
                    _upsert_product(con, app_id, key, sub["productId"], bp["basePlanId"],
                                    _plan(period, f"{sub['productId']} {bp['basePlanId']} {name}"),
                                    f"{name or sub['productId']} ({bp['basePlanId']})", kind[0], period, bp.get("state"), now)
                    stats["products"] += 1
                    prices = [(rc.get("regionCode"), *m) for rc in bp.get("regionalConfigs") or [] if (m := money(rc.get("price")))]
                    stats["prices_changed"] += record_prices(con, key, prices, today, now)
            for otp in client.pages(f"{base}/oneTimeProducts", "oneTimeProducts", {"pageSize": 100}):
                name = _title(otp.get("listings"))
                for po in otp.get("purchaseOptions") or []:
                    if not po.get("purchaseOptionId") or "rentOption" in po:
                        continue
                    key = f"google_play:{otp['productId']}:{po['purchaseOptionId']}"
                    _upsert_product(con, app_id, key, otp["productId"], po["purchaseOptionId"],
                                    _plan(None, f"{otp['productId']} {po['purchaseOptionId']} {name}"),
                                    name or otp["productId"], "one_time_product", None, po.get("state"), now)
                    stats["products"] += 1
                    prices = [(rc.get("regionCode"), *m) for rc in po.get("regionalPricingAndAvailabilityConfigs") or []
                              if rc.get("availability") in (None, "AVAILABLE", "AVAILABLE_IF_RELEASED") and (m := money(rc.get("price")))]
                    stats["prices_changed"] += record_prices(con, key, prices, today, now)
        except PlayAuthError as e:
            if e.status == 401 or e.where == "token":
                raise
            stats["errors"].append(f"{pkg}: Google refused product access ({e}). Check the Play Console invite and that the "
                                   "Google Play Android Developer API is enabled")
            stats["forbidden"] = True
        except PlayError as e:
            stats["errors"].append(f"{pkg}: {e}")
    return stats


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def unzip_csv(data: bytes) -> str:
    raw = data
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise PlayFormatError("zip has no CSV inside")
            raw = z.read(names[0])
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            if enc == "utf-8-sig" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _rows(text: str) -> tuple[dict, list[list[str]]]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {}, []
    return {h.strip().lower(): i for i, h in enumerate(rows[0])}, rows[1:]


def _col(idx: dict, *names: str) -> Optional[int]:
    for n in names:
        if n in idx:
            return idx[n]
    return None


def _gdate(s: str) -> dt.date:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise PlayFormatError(f"unrecognised date {s!r}")


class PackageMatcher:
    def __init__(self, con):
        self.by_pkg = {p: a for a, p in con.execute(
            f"SELECT app_id, package_name FROM {SCHEMA}.store_listings WHERE platform = 'google_play' AND package_name IS NOT NULL").fetchall()}

    def __call__(self, pkg: Optional[str]) -> Optional[str]:
        return self.by_pkg.get((pkg or "").strip())


def parse_sales(text: str, report_id: str, matcher: PackageMatcher, now: dt.datetime) -> list[dict]:
    idx, rows = _rows(text)
    c = {k: _col(idx, *v) for k, v in {
        "order": ("order number",), "date": ("order charged date",), "status": ("financial status",),
        "pkg": ("product id",), "ptype": ("product type",), "sku": ("sku id",), "ccy": ("currency of sale",),
        "price": ("item price",), "charged": ("charged amount",), "country": ("country of buyer",), "plan": ("base plan id",)}.items()}
    need = ("order", "date", "status", "pkg", "ccy", "charged")
    missing = [k for k in need if c[k] is None]
    if missing:
        raise PlayFormatError(f"estimated sales file is missing columns: {', '.join(missing)}")
    out = []
    for r in rows:
        g = lambda k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else ""  # noqa: E731
        status = g("status")
        refund = "refund" in status.lower()
        order = g("order")
        ptype = g("ptype").lower()
        is_sub = "subscription" in ptype
        amt = to_decimal(g("charged"))
        price = to_decimal(g("price")) if c["price"] is not None else None
        d = _gdate(g("date"))
        out.append({
            "line_key": hashlib.sha256(f"google_play|order|{order}|{status}".encode()).hexdigest(),
            "source_platform": PLATFORM, "app_id": matcher(g("pkg")), "source_app_ref": g("pkg"),
            "store_product_id": g("sku") or None, "currency": g("ccy").upper(), "period_start": d, "period_end": d,
            "reporting_timezone": SALES_TZ, "source_report_id": report_id, "imported_at": now, "data_status": "estimated",
            "territory": g("country").upper() or None, "transaction_type": "refund" if refund else ("subscription" if is_sub else "iap"),
            "product_type": g("ptype") or None, "units": -1 if refund else 1, "customer_price": price,
            "gross_amount": None if price is None else (-abs(price) if refund else price),
            "proceeds_amount": None,   # Google's sales file has no proceeds; they come from the earnings file
            "proceeds_currency": None, "sku": g("sku") or None, "parent_ref": None,
            "subscription_kind": (("Renewal" if ".." in order else "New") if is_sub and not refund else None),
            "sub_period": g("plan") or None,
        })
    return out


def parse_earnings(text: str, report_id: str, month: str, matcher: PackageMatcher, now: dt.datetime) -> list[dict]:
    idx, rows = _rows(text)
    c = {k: _col(idx, *v) for k, v in {
        "desc": ("description",), "date": ("transaction date",), "type": ("transaction type",), "pkg": ("product id",),
        "sku": ("sku id",), "country": ("buyer country",), "mccy": ("merchant currency",),
        "mamt": ("amount (merchant currency)",)}.items()}
    need = ("date", "type", "pkg", "mccy", "mamt")
    missing = [k for k in need if c[k] is None]
    if missing:
        raise PlayFormatError(f"earnings file is missing columns: {', '.join(missing)}")
    y, m = map(int, month.split("-"))
    out = []
    for n, r in enumerate(rows):
        g = lambda k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else ""  # noqa: E731
        d = _gdate(g("date"))
        out.append({
            "financial_key": hashlib.sha256(f"{report_id}|{n}|{','.join(r)}".encode()).hexdigest(),
            "source_platform": PLATFORM, "app_id": matcher(g("pkg")), "source_app_ref": g("pkg"),
            "store_product_id": g("sku") or None, "currency": g("mccy").upper(), "period_start": d, "period_end": d,
            "reporting_timezone": EARNINGS_TZ, "source_report_id": report_id, "imported_at": now, "data_status": "finalized",
            "fiscal_period": month, "region": None, "territory": g("country").upper() or None, "units": None,
            "gross_amount": None, "proceeds_amount": to_decimal(g("mamt")), "transaction_type": g("type") or None,
        })
    return out


def _insert(con, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    before = int(con.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0])
    cols = list(rows[0])
    sql = f"INSERT INTO {SCHEMA}.{table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
    params = [[r[k] for k in cols] for r in rows]
    if hasattr(con, "executemany"):
        con.executemany(sql, params)
    else:
        for p in params:
            con.execute(sql, p)
    return int(con.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0]) - before


def _record(con, report_id, rtype, month, tz, status, data, rows, now):
    y, m = map(int, month.split("-"))
    start, end = dt.date(y, m, 1), dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    con.execute(f"INSERT INTO {SCHEMA}.report_files VALUES (?, 'google_play', ?, NULL, 'MONTHLY', NULL, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT DO NOTHING", [report_id, rtype, start, end, tz, status, hashlib.sha256(data).hexdigest(), rows, now])
    con.execute(f"INSERT INTO {SCHEMA}.report_blobs VALUES (?, 'base64', ?, ?) ON CONFLICT DO NOTHING",
                [report_id, base64.b64encode(data).decode(), now])


def sync_reports(con, client: Client, bucket: str) -> dict:
    stats = {"files": 0, "lines": 0, "inserted": 0, "unparsed": 0, "unmatched": 0}
    matcher, now = PackageMatcher(con), registry.now()
    if client.get(f"{GCS}/b/{bucket}/o", {"maxResults": 1}) is None:
        raise PlayError(f"reports bucket {bucket} not found; copy it again from a Play Console report link")
    for prefix, rtype in (("sales/salesreport_", "ESTIMATED_SALES"), ("earnings/earnings_", "EARNINGS")):
        for obj in client.pages(f"{GCS}/b/{bucket}/o", "items", {"prefix": prefix}):
            name = obj.get("name", "")
            m = re.search(r"_(\d{4})(\d{2})\.zip$", name)
            if not m:
                continue
            month = f"{m.group(1)}-{m.group(2)}"
            rid = f"google:{name}:{obj.get('generation', '')}"
            if con.execute(f"SELECT 1 FROM {SCHEMA}.report_files WHERE report_id = ?", [rid]).fetchall():
                continue
            data = client.get(f"{GCS}/b/{bucket}/o/{urllib.parse.quote(name, safe='')}", {"alt": "media"}, raw=True)
            if data is None:
                continue
            stats["files"] += 1
            try:
                text = unzip_csv(data)
                lines = parse_sales(text, rid, matcher, now) if rtype == "ESTIMATED_SALES" else parse_earnings(text, rid, month, matcher, now)
            except PlayFormatError as e:
                log.warning("Play report %s kept but not parsed: %s", name, e)
                _record(con, rid, rtype, month, SALES_TZ if rtype == "ESTIMATED_SALES" else EARNINGS_TZ,
                        "estimated" if rtype == "ESTIMATED_SALES" else "finalized", data, 0, now)
                stats["unparsed"] += 1
                continue
            stats["lines"] += len(lines)
            stats["unmatched"] += sum(1 for l in lines if l["app_id"] is None)
            stats["inserted"] += _insert(con, "sales_reports" if rtype == "ESTIMATED_SALES" else "financial_reports", lines)
            _record(con, rid, rtype, month, SALES_TZ if rtype == "ESTIMATED_SALES" else EARNINGS_TZ,
                    "estimated" if rtype == "ESTIMATED_SALES" else "finalized", data, len(lines), now)
    return stats


# --------------------------------------------------------------------------- #
# Entry point used by the hourly job
# --------------------------------------------------------------------------- #
def _set_account(con, status: str, error: Optional[str] = None) -> None:
    con.execute(f"UPDATE {SCHEMA}.store_accounts SET status = ?, last_verified_at = CASE WHEN ? = 'verified' THEN ? ELSE last_verified_at END, "
                "last_error = ?, updated_at = ? WHERE account_id = 'google_play_reporting'",
                [status, status, registry.now(), error, registry.now()])


def run(con, environ=None, client: Optional[Client] = None, today: Optional[dt.date] = None, force: bool = False) -> dict:
    env = os.environ if environ is None else environ
    try:
        sa = load_service_account(env)
        bucket = bucket_name(env)
    except PlayError as e:
        _set_account(con, "error", str(e))
        return {"skipped": str(e)}
    if sa is None:
        return {"skipped": "Google Play service account key not present"}
    since = registry.now() - dt.timedelta(hours=20)
    if not force and con.execute(f"SELECT 1 FROM {SCHEMA}.synchronization_jobs WHERE source = 'google_play' "
                                 "AND status IN ('succeeded', 'partial') AND finished_at > ?", [since]).fetchall():
        return {"skipped": "already synced in the last 20 hours"}
    client = client or Client(sa)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    job = registry.start_job(con, PLATFORM, "daily")
    result, problems = {}, []
    try:
        result["products"] = sync_products(con, client, today)
        problems += result["products"].pop("errors")
        if bucket:
            try:
                result["reports"] = sync_reports(con, client, bucket)
                if result["reports"]["unparsed"]:
                    problems.append(f"{result['reports']['unparsed']} report file(s) kept but not parsed (unrecognised layout)")
                if result["reports"]["unmatched"]:
                    problems.append(f"{result['reports']['unmatched']} line(s) not matched to an app: check the Play package names")
            except PlayAuthError as e:
                if e.where == "token":
                    raise
                problems.append(f"reports bucket not readable yet ({e}). The Play Console invite needs 'View financial data'; "
                                "new invites can take up to a day")
                result["reports"] = {"forbidden": True}
            except PlayError as e:
                problems.append(str(e))
                result["reports"] = {"forbidden": True}
        else:
            problems.append("GOOGLE_PLAY_REPORTS_BUCKET not set; only products and prices were read")
    except PlayAuthError as e:
        _set_account(con, "error", str(e))
        registry.finish_job(con, job, "failed", message=str(e))
        registry.audit(con, "scheduler", "google_play.sync", PLATFORM, {"status": "failed", "error": str(e)}, dry_run=False)
        return {"failed": str(e)}
    except Exception as e:  # noqa: BLE001
        msg = registry.scrub_text(f"{type(e).__name__}: {e}")
        registry.finish_job(con, job, "failed", message=msg)
        log.exception("Google Play sync failed")
        return {"failed": msg}
    worked = (not result["products"].get("forbidden") and result["products"]["apps"] > 0) or \
             (isinstance(result.get("reports"), dict) and not result["reports"].get("forbidden") and "files" in result["reports"])
    _set_account(con, "verified" if worked else "error", None if worked else "; ".join(problems)[:500])
    seen = sum(v.get("lines", 0) for v in result.values() if isinstance(v, dict))
    ins = sum(v.get("inserted", 0) for v in result.values() if isinstance(v, dict))
    registry.finish_job(con, job, "partial" if problems else "succeeded", seen, ins,
                        message=("; ".join(problems) + " | " if problems else "") + json.dumps(result))
    registry.audit(con, "scheduler", "google_play.sync", PLATFORM, {"calls": client.calls, **result}, dry_run=False)
    return result
