"""
Stage C: App Store Connect connector (read-only).

What it pulls, using a Team API key (ASC_ISSUER_ID, ASC_KEY_ID, ASC_PRIVATE_KEY,
ASC_VENDOR_NUMBER on the adbot-hourly Render job):

  1. Products  GET /v1/apps/{id}/inAppPurchasesV2 and
               GET /v1/apps/{id}/subscriptionGroups?include=subscriptions
     -> verifies each product's real type (auto-renewable, non-renewing,
        non-consumable, consumable) and subscription period. Reference rows
        from the price list stay untouched.
  2. Sales     GET /v1/salesReports  SALES / SUMMARY / DAILY / 1_0
     -> one gzip TSV per day; parsed by header name into revenue.sales_reports
        as 'estimated' data. Up to 365 days back (Apple keeps daily reports for
        one year), then only the days still missing.
  3. Finance   GET /v1/financeReports  FINANCE_DETAIL / region Z1 / monthly
     -> archived as received; parsed into revenue.financial_reports
        ('finalized') when the header row is recognised. Otherwise the file is
        kept and the job says so; nothing is guessed.

Every file is archived in revenue.report_blobs, every run is a
revenue.synchronization_jobs row, every import is idempotent (natural keys,
ON CONFLICT DO NOTHING). Nothing here writes to App Store Connect.

Facts this relies on (official docs, checked 2026-10-22):
  * JWT: ES256, header kid + typ JWT, payload iss/iat/exp/aud='appstoreconnect-v1',
    lifetime <= 20 minutes. Sales and Finance endpoints need a Team key.
  * Summary Sales Report columns (Units, 'Developer Proceeds' per unit,
    Customer Price per unit, MM/DD/YYYY dates, negative values = refunds).
  * Product type identifiers (1/1F/1T/F1 downloads, 3/3F redownloads,
    7/7F/7T/F7 updates, IA1/IA1-M/FI1 IAP, IA3 restore, IA9 non-renewing,
    IAY auto-renewable).
  * Daily reports are available the next day, generally by 8 a.m. PT, and are
    kept for one year.
  * FINANCE_DETAIL is served with region code Z1 (Apple staff, developer forums).
Not stated by Apple, so not assumed: the time zone of a "report day". It is
recorded as such in reporting_timezone.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Callable, Iterable, Optional

from . import registry
from .money import to_decimal
from .schema import SCHEMA

log = logging.getLogger("revenue.apple")

API = "https://api.appstoreconnect.apple.com"
PLATFORM = "apple_app_store"
SALES_TZ = "Apple report day (time zone not stated by Apple)"
FINANCE_TZ = "Apple fiscal month"
ENV = ("ASC_ISSUER_ID", "ASC_KEY_ID", "ASC_PRIVATE_KEY", "ASC_VENDOR_NUMBER")

# Apple 'Product Type Identifier' -> our transaction type
PRODUCT_TYPES = {
    **{k: "download" for k in ("1", "1-B", "F1-B", "1E", "1EP", "1EU", "1F", "1T", "F1")},
    **{k: "redownload" for k in ("3", "3F", "3T", "F3")},
    **{k: "update" for k in ("7", "7F", "7T", "F7")},
    **{k: "iap" for k in ("IA1", "IA1-M", "FI1")},
    "IA3": "restore",
    **{k: "subscription" for k in ("IA9", "IA9-M", "IAY", "IAY-M")},
}
IAP_TYPES = {"CONSUMABLE": "consumable", "NON_CONSUMABLE": "non_consumable", "NON_RENEWING_SUBSCRIPTION": "non_renewing_subscription"}
SUB_PERIODS = {"ONE_WEEK": ("P1W", "other"), "ONE_MONTH": ("P1M", "monthly"), "TWO_MONTHS": ("P2M", "other"),
               "THREE_MONTHS": ("P3M", "three_month"), "SIX_MONTHS": ("P6M", "other"), "ONE_YEAR": ("P1Y", "annual")}


class AppleError(Exception):
    pass


class AppleAuthError(AppleError):
    """401/403: the key, its role or the vendor number is wrong. Retrying won't help."""

    def __init__(self, msg: str, status: int = 401, path: str = ""):
        super().__init__(msg)
        self.status, self.path = status, path


class AppleFormatError(AppleError):
    """A report arrived in a shape we don't recognise. The raw file is kept."""


# --------------------------------------------------------------------------- #
# Credentials + token
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Creds:
    issuer_id: str
    key_id: str
    private_key: str
    vendor_number: str

    def __repr__(self) -> str:  # never print the key
        return f"Creds(issuer_id=…{self.issuer_id[-4:]}, key_id={self.key_id}, vendor=…{self.vendor_number[-3:]})"

    @classmethod
    def from_env(cls, environ=None) -> Optional["Creds"]:
        env = os.environ if environ is None else environ
        vals = {k: str(env.get(k, "") or "").strip() for k in ENV}
        key_file = find_key_file(env)
        if key_file and not vals["ASC_PRIVATE_KEY"]:
            vals["ASC_PRIVATE_KEY"] = "(file)"
        if not all(vals.values()):
            return None
        if key_file:  # a Render Secret File holds a multi-line key safely; prefer it
            with open(key_file, encoding="utf-8", errors="replace") as fh:
                key = normalize_key(fh.read(), source=f"secret file {os.path.basename(key_file)}")
        else:
            key = normalize_key(vals["ASC_PRIVATE_KEY"])
        import re
        if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", vals["ASC_ISSUER_ID"]):
            raise AppleError(f"ASC_ISSUER_ID should look like 57246542-96fe-1a63-e053-0824d011072a (it holds {len(vals['ASC_ISSUER_ID'])} characters)")
        if not re.fullmatch(r"[A-Z0-9]{10}", vals["ASC_KEY_ID"]):
            raise AppleError(f"ASC_KEY_ID should be the 10-character Key ID, e.g. 2X9R4HXF34 (it holds {len(vals['ASC_KEY_ID'])} characters)")
        if key_file and vals["ASC_KEY_ID"] not in os.path.basename(key_file) and re.search(r"AuthKey_[A-Z0-9]{10}", os.path.basename(key_file)):
            raise AppleError(f"ASC_KEY_ID {vals['ASC_KEY_ID']} doesn't match the key file {os.path.basename(key_file)}")
        if not vals["ASC_VENDOR_NUMBER"].isdigit():
            raise AppleError("ASC_VENDOR_NUMBER should be digits only (Payments and Financial Reports, top left)")
        return cls(vals["ASC_ISSUER_ID"], vals["ASC_KEY_ID"], key, vals["ASC_VENDOR_NUMBER"])


SECRET_DIR = "/etc/secrets"  # where Render mounts Secret Files


def find_key_file(env) -> Optional[str]:
    """ASC_PRIVATE_KEY_FILE, else a single *.p8 among Render Secret Files."""
    path = str(env.get("ASC_PRIVATE_KEY_FILE", "") or "").strip()
    if path:
        return path if os.path.isfile(path) else None
    secret_dir = env.get("ASC_SECRET_DIR", SECRET_DIR)
    try:
        p8 = sorted(f for f in os.listdir(secret_dir) if f.lower().endswith(".p8"))
    except OSError:
        return None
    return os.path.join(secret_dir, p8[0]) if len(p8) == 1 else None


def normalize_key(raw: str, source: str = "ASC_PRIVATE_KEY") -> str:
    """Accept the .p8 text however it was pasted: with or without the BEGIN/END lines,
    on one line, with literal \\n escapes or surrounding quotes. Never echoes the key."""
    import binascii
    k = raw.strip().strip('"').strip("'").replace("\\n", "\n").replace("\r", "")
    if "BEGIN" in k and "END" in k:
        body = k.split("-----")[2] if k.count("-----") >= 4 else ""
    else:
        body = k
    body = "".join(body.split())
    try:
        der = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        der = b""
    if len(der) < 60 or der[:1] != b"\x30":
        hint = "it looks like a file name or path" if (".p8" in k or "AuthKey" in k) and len(k) < 80 else \
               "it is too short to be a key" if len(body) < 80 else "it isn't the key text"
        raise AppleError(f"{source} isn't a usable .p8 key ({hint}; it holds {len(k)} characters, a key is about 240). "
                         "Add the AuthKey_….p8 file under Secret Files on adbot-hourly, or paste its whole text into ASC_PRIVATE_KEY")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


def make_token(creds: Creds, now: Optional[float] = None, lifetime: int = 19 * 60) -> str:
    import jwt  # PyJWT[crypto], already a dependency for Apple Search Ads
    if lifetime > 20 * 60:
        raise ValueError("App Store Connect rejects tokens that live longer than 20 minutes")
    iat = int(now if now is not None else time.time())
    return jwt.encode({"iss": creds.issuer_id, "iat": iat, "exp": iat + lifetime, "aud": "appstoreconnect-v1"},
                      creds.private_key, algorithm="ES256", headers={"kid": creds.key_id, "typ": "JWT"})


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, creds: Creds, session=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, max_retries: int = 5):
        import requests
        self.creds, self.http = creds, session or requests.Session()
        self.sleep, self.clock, self.max_retries = sleep, clock, max_retries
        self._tok, self._tok_exp = None, 0.0
        self.calls = 0

    def _token(self) -> str:
        if not self._tok or self.clock() > self._tok_exp - 60:
            self._tok = make_token(self.creds, self.clock())
            self._tok_exp = self.clock() + 19 * 60
        return self._tok

    def _get(self, path: str, params: dict, accept: str):
        url = path if path.startswith("http") else API + path
        last = None
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            try:
                r = self.http.get(url, params=params, timeout=60,
                                  headers={"Authorization": f"Bearer {self._token()}", "Accept": accept})
            except Exception as e:  # network trouble: retry with backoff
                last = registry.scrub_text(f"{type(e).__name__}: {e}")
                self.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code in (401, 403):
                where = url.replace(API, "").split("?")[0]
                raise AppleAuthError(f"HTTP {r.status_code} on {where}: {_apple_detail(r)} [signed with key ID {self.creds.key_id}, "
                                     f"issuer …{self.creds.issuer_id[-4:]}, vendor …{self.creds.vendor_number[-3:]}]",
                                     status=r.status_code, path=where)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {_apple_detail(r)}"
                wait = r.headers.get("Retry-After")
                self.sleep(min(120, float(wait)) if wait and str(wait).isdigit() else min(60, 2 ** attempt))
                continue
            return r
        raise AppleError(f"gave up after {self.max_retries + 1} attempts ({last})")

    def report(self, path: str, params: dict) -> tuple[Optional[bytes], str]:
        """Download a gzip report. Returns (bytes, '') or (None, reason) when Apple has no file."""
        r = self._get(path, params, "application/a-gzip")
        if r.status_code == 200:
            return r.content, ""
        if r.status_code == 404:
            return None, _apple_detail(r)
        raise AppleError(f"HTTP {r.status_code}: {_apple_detail(r)}")

    def pages(self, path: str, params: dict) -> Iterable[dict]:
        """Yield each JSON page, following links.next."""
        url, p = path, dict(params)
        while url:
            r = self._get(url, p, "application/json")
            if r.status_code != 200:
                raise AppleError(f"HTTP {r.status_code}: {_apple_detail(r)}")
            body = r.json()
            yield body
            url, p = (body.get("links") or {}).get("next"), {}


def _apple_detail(r) -> str:
    try:
        errs = r.json().get("errors") or []
        msg = "; ".join(f"{e.get('code', '')} {e.get('detail') or e.get('title') or ''}".strip() for e in errs)
    except Exception:  # noqa: BLE001
        msg = (r.text or "")[:200]
    return registry.scrub_text(msg)[:300]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def gunzip(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def _text(data: bytes) -> str:
    raw = gunzip(data)
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _date(s: str) -> dt.date:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise AppleFormatError(f"unrecognised date {s!r}")


SALES_COLS = {  # our name: header as Apple writes it (matched case-insensitively, by prefix for 'developer proceeds')
    "sku": "sku", "title": "title", "ptype": "product type identifier", "units": "units",
    "proceeds": "developer proceeds", "begin": "begin date", "end": "end date", "ccy": "customer currency",
    "country": "country code", "proceeds_ccy": "currency of proceeds", "apple_id": "apple identifier",
    "price": "customer price", "parent": "parent identifier", "subscription": "subscription", "period": "period",
}
SALES_REQUIRED = ("sku", "ptype", "units", "proceeds", "begin", "end", "ccy", "proceeds_ccy", "apple_id", "price")


def parse_sales_summary(data: bytes) -> tuple[list[str], list[dict]]:
    """Parse a SALES/SUMMARY report. Returns (header, rows as {header: value}). Raises AppleFormatError."""
    lines = [l for l in _text(data).splitlines() if l.strip()]
    if not lines:
        return [], []
    reader = csv.reader(lines, delimiter="\t")
    header = [h.strip() for h in next(reader)]
    low = [h.lower() for h in header]
    idx = {}
    for key, name in SALES_COLS.items():
        hit = next((i for i, h in enumerate(low) if h == name or (name == "developer proceeds" and h.startswith(name))), None)
        if hit is not None:
            idx[key] = hit
    missing = [SALES_COLS[k] for k in SALES_REQUIRED if k not in idx]
    if missing:
        raise AppleFormatError(f"sales report is missing columns: {', '.join(missing)}")
    rows = []
    for vals in reader:
        if len(vals) < len(header):
            vals = vals + [""] * (len(header) - len(vals))
        rows.append({"_all": dict(zip(header, vals)), **{k: vals[i].strip() for k, i in idx.items()}})
    return header, rows


def _signed(units: Decimal, per_unit: Optional[Decimal]) -> Optional[Decimal]:
    """units x per-unit amount; negative whenever Apple marks the line as a refund (units or price < 0)."""
    if per_unit is None:
        return None
    mag = abs(units * per_unit)
    return -mag if (units < 0 or per_unit < 0) else mag


class AppMatcher:
    """Maps Apple report rows to our registry apps using IDs the owner entered (no guessing by title)."""

    def __init__(self, con):
        self.by_apple_id, self.by_bundle, self.by_sku = {}, [], {}
        for app_id, apple_id, bundle in con.execute(
                f"SELECT app_id, apple_app_id, bundle_id FROM {SCHEMA}.store_listings WHERE platform = 'apple_app_store'").fetchall():
            if apple_id:
                self.by_apple_id[str(apple_id)] = app_id
            if bundle:
                self.by_bundle.append((str(bundle), app_id))
        for sku, app_id in con.execute(
                f"SELECT DISTINCT sku, app_id FROM {SCHEMA}.sales_reports WHERE source_platform = 'apple_app_store' "
                "AND transaction_type IN ('download', 'redownload', 'update') AND sku IS NOT NULL AND app_id IS NOT NULL").fetchall():
            self.by_sku[str(sku)] = app_id

    def learn(self, rows: list[dict]) -> None:
        for r in rows:  # an app's own download rows tie its SKU to its Apple ID
            if PRODUCT_TYPES.get(r["ptype"]) in ("download", "redownload", "update") and r["apple_id"] in self.by_apple_id:
                self.by_sku[r["sku"]] = self.by_apple_id[r["apple_id"]]

    def match(self, r: dict) -> Optional[str]:
        if r.get("apple_id") in self.by_apple_id:
            return self.by_apple_id[r["apple_id"]]
        if r.get("parent") and r["parent"] in self.by_sku:
            return self.by_sku[r["parent"]]
        for pid in (r.get("sku"), r.get("title")):
            for bundle, app_id in self.by_bundle:
                if pid and (pid == bundle or pid.startswith(bundle + ".")):
                    return app_id
        return None


def sales_lines(report_id: str, rows: list[dict], header: list[str], matcher: AppMatcher, imported_at: dt.datetime) -> list[dict]:
    matcher.learn(rows)
    out = []
    dims = [h for h in header if h.lower() != "units"]
    for r in rows:
        units = to_decimal(r["units"]) or Decimal(0)
        price, proceeds = to_decimal(r["price"]), to_decimal(r["proceeds"])
        ttype = PRODUCT_TYPES.get(r["ptype"], "other")
        if units < 0 or (price is not None and price < 0):
            ttype = "refund"
        is_app_row = PRODUCT_TYPES.get(r["ptype"]) in ("download", "redownload", "update")
        key_src = "|".join([PLATFORM, "SALES"] + [f"{h}={r['_all'].get(h, '')}" for h in dims])
        begin, end = _date(r["begin"]), _date(r["end"])
        out.append({
            "line_key": hashlib.sha256(key_src.encode("utf-8")).hexdigest(),
            "source_platform": PLATFORM, "app_id": matcher.match(r),
            "source_app_ref": (r.get("parent") or r["apple_id"]) if not is_app_row else r["apple_id"],
            "store_product_id": None if is_app_row else r["sku"],
            "currency": r["ccy"].upper(), "period_start": begin, "period_end": end, "reporting_timezone": SALES_TZ,
            "source_report_id": report_id, "imported_at": imported_at, "data_status": "estimated",
            "territory": (r.get("country") or "").upper() or None, "transaction_type": ttype, "product_type": r["ptype"],
            "units": int(units.to_integral_value(rounding=ROUND_HALF_EVEN)),
            "customer_price": price, "gross_amount": _signed(units, price), "proceeds_amount": _signed(units, proceeds),
            "proceeds_currency": r["proceeds_ccy"].upper(), "sku": r["sku"], "parent_ref": r.get("parent") or None,
            "subscription_kind": r.get("subscription") or None, "sub_period": r.get("period") or None,
        })
    return out


FIN_COLS = {"start": "start date", "end": "end date", "vendor_id": "vendor identifier", "qty": "quantity",
            "share": "partner share", "ext_share": "extended partner share", "share_ccy": "partner share currency",
            "sale_or_return": "sales or return", "apple_id": "apple identifier", "country": "country of sale",
            "price": "customer price", "ccy": "customer currency"}
FIN_REQUIRED = ("start", "end", "qty", "ext_share", "share_ccy", "apple_id")


def parse_finance_detail(data: bytes) -> list[dict]:
    """Find the header row (Apple adds summary rows around the data) and return line dicts."""
    lines = _text(data).splitlines()
    for n, line in enumerate(lines):
        low = [c.strip().lower() for c in line.split("\t")]
        if "extended partner share" in low and "quantity" in low:
            idx = {k: low.index(v) for k, v in FIN_COLS.items() if v in low}
            missing = [FIN_COLS[k] for k in FIN_REQUIRED if k not in idx]
            if missing:
                raise AppleFormatError(f"finance report is missing columns: {', '.join(missing)}")
            out = []
            for raw in lines[n + 1:]:
                vals = raw.split("\t")
                if len(vals) < len(low) or not vals[idx["start"]].strip():
                    if out:
                        break  # data block ended (totals follow)
                    continue
                out.append({k: vals[i].strip() for k, i in idx.items()} | {"_line": raw})
            return out
    raise AppleFormatError("finance report header row not found")


# --------------------------------------------------------------------------- #
# Storage helpers
# --------------------------------------------------------------------------- #
def _count(con, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {SCHEMA}.{table}").fetchone()[0])


def insert_rows(con, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    before = _count(con, table)
    cols = list(rows[0])
    sql = f"INSERT INTO {SCHEMA}.{table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
    params = [[r[c] for c in cols] for r in rows]
    if hasattr(con, "executemany"):
        con.executemany(sql, params)
    else:
        for p in params:
            con.execute(sql, p)
    return _count(con, table) - before


def record_file(con, report_id: str, report_type: str, subtype: Optional[str], freq: Optional[str], version: Optional[str],
                start: dt.date, end: dt.date, tz: str, status: str, data: bytes, rows: int, now: dt.datetime) -> None:
    con.execute(f"INSERT INTO {SCHEMA}.report_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT DO NOTHING",
                [report_id, PLATFORM, report_type, subtype, freq, version, start, end, tz, status,
                 hashlib.sha256(data).hexdigest(), rows, now])
    if data:
        con.execute(f"INSERT INTO {SCHEMA}.report_blobs VALUES (?, 'gzip', ?, ?) ON CONFLICT DO NOTHING",
                    [report_id, base64.b64encode(gzip.compress(gunzip(data))).decode(), now])


def _have(con, report_id: str) -> bool:
    return bool(con.execute(f"SELECT 1 FROM {SCHEMA}.report_files WHERE report_id = ?", [report_id]).fetchall())


# --------------------------------------------------------------------------- #
# Sync steps
# --------------------------------------------------------------------------- #
def pacific_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:  # noqa: BLE001
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date()


def sync_sales(con, client: Client, creds: Creds, today: dt.date, backfill_days: int, max_days: Optional[int] = None) -> dict:
    """Fetch each missing daily SALES report from yesterday back `backfill_days` (max 365)."""
    backfill_days = max(1, min(365, backfill_days))
    stats = {"days_checked": 0, "files": 0, "empty_days": 0, "not_ready": 0, "lines": 0, "inserted": 0, "unmatched": 0}
    matcher = AppMatcher(con)
    days = [today - dt.timedelta(days=i) for i in range(1, backfill_days + 1)]
    todo = [d for d in days if not _have(con, f"apple:SALES:SUMMARY:DAILY:{d.isoformat()}:1_0")]
    for d in todo[: max_days or len(todo)]:
        stats["days_checked"] += 1
        rid = f"apple:SALES:SUMMARY:DAILY:{d.isoformat()}:1_0"
        data, why = client.report("/v1/salesReports", {
            "filter[frequency]": "DAILY", "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY",
            "filter[version]": "1_0", "filter[vendorNumber]": creds.vendor_number, "filter[reportDate]": d.isoformat()})
        now = registry.now()
        if data is None:
            if (today - d).days <= 2:      # may simply not be published yet: try again next run
                stats["not_ready"] += 1
            else:                          # Apple answers 404 for days with no sales; remember the day as empty
                record_file(con, rid, "SALES", "SUMMARY", "DAILY", "1_0", d, d, SALES_TZ, "estimated", b"", 0, now)
                stats["empty_days"] += 1
            continue
        header, rows = parse_sales_summary(data)
        lines = sales_lines(rid, rows, header, matcher, now)
        stats["lines"] += len(lines)
        stats["unmatched"] += sum(1 for l in lines if l["app_id"] is None)
        stats["inserted"] += insert_rows(con, "sales_reports", lines)
        record_file(con, rid, "SALES", "SUMMARY", "DAILY", "1_0", d, d, SALES_TZ, "estimated", data, len(lines), now)
        stats["files"] += 1
    return stats


def fiscal_months(today: dt.date, n: int) -> list[str]:
    out, y, m = [], today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out


def sync_finance(con, client: Client, creds: Creds, today: dt.date, months: int = 12) -> dict:
    stats = {"months_checked": 0, "files": 0, "not_ready": 0, "lines": 0, "inserted": 0, "unparsed": 0}
    matcher = AppMatcher(con)
    for i, ym in enumerate(fiscal_months(today, months)):
        rid = f"apple:FINANCE_DETAIL:Z1:MONTHLY:{ym}"
        if _have(con, rid):
            continue
        stats["months_checked"] += 1
        data, why = client.report("/v1/financeReports", {"filter[regionCode]": "Z1", "filter[reportType]": "FINANCE_DETAIL",
                                                         "filter[reportDate]": ym, "filter[vendorNumber]": creds.vendor_number})
        y, m = map(int, ym.split("-"))
        start = dt.date(y, m, 1)
        end = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        now = registry.now()
        if data is None:
            if i < 2:   # the last two fiscal months may not be published yet
                stats["not_ready"] += 1
            else:
                record_file(con, rid, "FINANCE_DETAIL", "Z1", "MONTHLY", None, start, end, FINANCE_TZ, "finalized", b"", 0, now)
            continue
        try:
            rows = parse_finance_detail(data)
        except AppleFormatError as e:
            log.warning("finance report %s kept but not parsed: %s", ym, e)
            record_file(con, rid, "FINANCE_DETAIL", "Z1", "MONTHLY", None, start, end, FINANCE_TZ, "finalized", data, 0, now)
            stats["unparsed"] += 1
            stats["files"] += 1
            continue
        lines = []
        for r in rows:
            q = to_decimal(r["qty"]) or Decimal(0)
            lines.append({
                "financial_key": hashlib.sha256(f"{rid}|{r['_line']}".encode("utf-8")).hexdigest(),
                "source_platform": PLATFORM, "app_id": matcher.match({"apple_id": r["apple_id"], "sku": r.get("vendor_id")}),
                "source_app_ref": r["apple_id"], "store_product_id": r.get("vendor_id") or None,
                "currency": r["share_ccy"].upper(), "period_start": _date(r["start"]), "period_end": _date(r["end"]),
                "reporting_timezone": FINANCE_TZ, "source_report_id": rid, "imported_at": now, "data_status": "finalized",
                "fiscal_period": ym, "region": "Z1", "territory": (r.get("country") or "").upper() or None,
                "units": int(q.to_integral_value(rounding=ROUND_HALF_EVEN)),
                "gross_amount": None,  # customer price is in the customer's currency; never mixed into partner-share currency
                "proceeds_amount": to_decimal(r["ext_share"]),
            })
        stats["lines"] += len(lines)
        stats["inserted"] += insert_rows(con, "financial_reports", lines)
        record_file(con, rid, "FINANCE_DETAIL", "Z1", "MONTHLY", None, start, end, FINANCE_TZ, "finalized", data, len(lines), now)
        stats["files"] += 1
    return stats


def _plan_from_name(text: str) -> str:
    t = (text or "").lower()
    if "lifetime" in t:
        return "lifetime"
    if any(w in t for w in ("3month", "3_month", "3-month", "three", "quarter", "3mo")):
        return "three_month"
    if any(w in t for w in ("annual", "year")):
        return "annual"
    if "month" in t:
        return "monthly"
    return "other"


def sync_products(con, client: Client) -> dict:
    """Verify product types/periods for every app whose Apple ID is set. Read-only."""
    stats = {"apps": 0, "products": 0, "errors": []}
    apps = con.execute(f"SELECT app_id, apple_app_id FROM {SCHEMA}.store_listings "
                       "WHERE platform = 'apple_app_store' AND apple_app_id IS NOT NULL").fetchall()
    now = registry.now()

    def upsert(app_id, product_id, name, ptype, period, plan, state):
        key = f"{PLATFORM}:{product_id}"
        con.execute(
            f"INSERT INTO {SCHEMA}.store_products (product_key, app_id, platform, store_product_id, plan_code, display_name, product_type, "
            "billing_period, availability, source, verified, verified_at, created_at, updated_at) "
            "VALUES (?, ?, 'apple_app_store', ?, ?, ?, ?, ?, ?, 'store_api', TRUE, ?, ?, ?) "
            "ON CONFLICT (product_key) DO UPDATE SET plan_code = excluded.plan_code, display_name = excluded.display_name, "
            "product_type = excluded.product_type, billing_period = excluded.billing_period, availability = excluded.availability, "
            "verified = TRUE, verified_at = excluded.verified_at, updated_at = excluded.updated_at",
            [key, app_id, product_id, plan, name or product_id, ptype, period, state or "unknown", now, now, now])
        stats["products"] += 1

    for app_id, apple_id in apps:
        stats["apps"] += 1
        try:
            for page in client.pages(f"/v1/apps/{apple_id}/inAppPurchasesV2", {"limit": 200}):
                for item in page.get("data") or []:
                    a = item.get("attributes") or {}
                    ptype = IAP_TYPES.get(a.get("inAppPurchaseType"))
                    if not a.get("productId") or not ptype:
                        continue  # unknown type: leave it unverified rather than guess
                    upsert(app_id, a["productId"], a.get("name"), ptype, None,
                           _plan_from_name(f"{a.get('productId')} {a.get('name')}") if ptype != "consumable" else "other", a.get("state"))
            for page in client.pages(f"/v1/apps/{apple_id}/subscriptionGroups", {"include": "subscriptions", "limit": 200}):
                for item in page.get("included") or []:
                    if item.get("type") != "subscriptions":
                        continue
                    a = item.get("attributes") or {}
                    period = SUB_PERIODS.get(a.get("subscriptionPeriod"))
                    if not a.get("productId") or not period:
                        continue
                    upsert(app_id, a["productId"], a.get("name"), "auto_renewable_subscription", period[0], period[1], a.get("state"))
        except AppleAuthError as e:
            if e.status != 403:
                raise
            # the key's role can't read product setup (Finance can't): sales and finance still work
            stats["errors"].append("product types not verified: this key's role can't read in-app purchase setup "
                                   "(needs App Manager or Admin access); sales and finance are unaffected")
            stats["forbidden"] = True
            break
        except AppleError as e:
            stats["errors"].append(f"{app_id}: {e}")
    return stats


# --------------------------------------------------------------------------- #
# Entry point used by the hourly job
# --------------------------------------------------------------------------- #
def _recent_success(con, job_type: str, hours: int) -> bool:
    since = registry.now() - dt.timedelta(hours=hours)
    return bool(con.execute(f"SELECT 1 FROM {SCHEMA}.synchronization_jobs WHERE source = ? AND job_type = ? "
                            "AND status IN ('succeeded', 'partial') AND finished_at > ?", [PLATFORM, job_type, since]).fetchall())


def _set_account(con, status: str, error: Optional[str] = None) -> None:
    con.execute(f"UPDATE {SCHEMA}.store_accounts SET status = ?, last_verified_at = CASE WHEN ? = 'verified' THEN ? ELSE last_verified_at END, "
                "last_error = ?, updated_at = ? WHERE account_id = 'apple_app_store_reporting'",
                [status, status, registry.now(), error, registry.now()])


def run(con, environ=None, client: Optional[Client] = None, today: Optional[dt.date] = None) -> dict:
    """One sync pass. Silent no-op when the four ASC_* settings aren't present."""
    env = os.environ if environ is None else environ
    try:
        creds = Creds.from_env(env)
    except AppleError as e:
        _set_account(con, "error", str(e))
        return {"skipped": str(e)}
    if creds is None:
        return {"skipped": "App Store Connect settings not present"}
    client = client or Client(creds)
    today = today or pacific_today()
    backfill = int(env.get("ASC_BACKFILL_DAYS", "365") or 365)
    daily = not _recent_success(con, "daily", 20)   # products + finance once a day; sales every run until the day is in
    job = registry.start_job(con, PLATFORM, "daily" if daily else "sales", today - dt.timedelta(days=backfill), today - dt.timedelta(days=1))
    result, problems = {}, []
    try:
        if daily:
            result["products"] = sync_products(con, client)
            problems += result["products"].pop("errors")
        result["sales"] = sync_sales(con, client, creds, today, backfill)
        if daily:
            result["finance"] = sync_finance(con, client, creds, today)
            if result["finance"]["unparsed"]:
                problems.append(f"{result['finance']['unparsed']} finance file(s) kept but not parsed (unrecognised layout)")
        if result["sales"]["unmatched"]:
            problems.append(f"{result['sales']['unmatched']} sales line(s) not matched to an app: check the Apple ID / bundle ID on the Revenue tab")
    except AppleAuthError as e:
        _set_account(con, "error", str(e))
        registry.finish_job(con, job, "failed", message=f"App Store Connect refused the key: {e}")
        registry.audit(con, "scheduler", "apple.sync", PLATFORM, {"status": "failed", "error": str(e)}, dry_run=False)
        return {"failed": str(e)}
    except Exception as e:  # noqa: BLE001 - record and carry on; the ad bot must keep running
        msg = registry.scrub_text(f"{type(e).__name__}: {e}")
        registry.finish_job(con, job, "failed", message=msg)
        log.exception("Apple sync failed")
        return {"failed": msg}
    _set_account(con, "verified")
    seen = sum(v.get("lines", 0) for v in result.values() if isinstance(v, dict))
    ins = sum(v.get("inserted", 0) for v in result.values() if isinstance(v, dict))
    summary = json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "errors"} for k, v in result.items()})
    registry.finish_job(con, job, "partial" if problems else "succeeded", seen, ins,
                        message=("; ".join(problems) + " | " if problems else "") + summary)
    registry.audit(con, "scheduler", "apple.sync", PLATFORM, {"calls": client.calls, **result}, dry_run=False)
    return result
