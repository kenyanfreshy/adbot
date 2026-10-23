"""Stage C tests: App Store Connect connector against a fake Apple (synthetic reports, throwaway key).

No network, no real credentials. Run:  python -m unittest discover -s tests -v
"""
import datetime as dt
import gzip
import json
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    import duckdb
except ImportError:
    sys.path.insert(0, HERE)
    import duckdb_cli_shim as duckdb  # type: ignore

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import revenue
from revenue import apple, registry
from revenue.schema import SCHEMA as S

KEY = ec.generate_private_key(ec.SECP256R1())
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
PUB = KEY.public_key()
ENV = {"ASC_ISSUER_ID": "57246542-96fe-1a63-e053-0824d011072a", "ASC_KEY_ID": "2X9R4HXF34",
       "ASC_PRIVATE_KEY": PEM.replace("\n", "\\n"), "ASC_VENDOR_NUMBER": "85000001", "ASC_BACKFILL_DAYS": "5"}
TODAY = dt.date(2026, 10, 22)

HEADER = ["Provider", "Provider Country", "SKU", "Developer", "Title", "Version", "Product Type Identifier", "Units",
          "Developer Proceeds", "Begin Date", "End Date", "Customer Currency", "Country Code", "Currency of Proceeds",
          "Apple Identifier", "Customer Price", "Promo Code", "Parent Identifier", "Subscription", "Period", "Category",
          "CMB", "Device", "Supported Platforms", "Proceeds Reason", "Preserved Pricing", "Client", "Order Type"]


def sales_file(day: dt.date, rows: list[dict], encoding="utf-8") -> bytes:
    d = day.strftime("%m/%d/%Y")
    lines = ["\t".join(HEADER)]
    for r in rows:
        base = {"Provider": "APPLE", "Provider Country": "US", "Developer": "Alex Korir", "Version": "1.0", "Begin Date": d, "End Date": d,
                "Category": "Education", "Device": "iPhone", "Supported Platforms": "iOS"}
        base.update(r)
        lines.append("\t".join(str(base.get(h, "")) for h in HEADER))
    return gzip.compress(("\n".join(lines) + "\n").encode(encoding))


DAY1 = TODAY - dt.timedelta(days=3)
DAY1_ROWS = [
    # app download row ties SKU 'RNR001' to Apple ID 6470001234
    {"SKU": "RNR001", "Title": "Rapid Nursing Review", "Product Type Identifier": "1F", "Units": "12", "Developer Proceeds": "0",
     "Customer Currency": "USD", "Country Code": "US", "Currency of Proceeds": "USD", "Apple Identifier": "6470001234", "Customer Price": "0"},
    # new monthly subscription, matched through Parent Identifier (app SKU)
    {"SKU": "rnr.pro.monthly", "Title": "rnr.pro.monthly", "Product Type Identifier": "IAY", "Units": "2", "Developer Proceeds": "6.99",
     "Customer Currency": "USD", "Country Code": "US", "Currency of Proceeds": "USD", "Apple Identifier": "6470009999", "Customer Price": "9.99",
     "Parent Identifier": "RNR001", "Subscription": "New", "Period": "1 Month"},
    # renewal in euros, proceeds paid in euros
    {"SKU": "rnr.pro.monthly", "Title": "rnr.pro.monthly", "Product Type Identifier": "IAY", "Units": "1", "Developer Proceeds": "7.00",
     "Customer Currency": "EUR", "Country Code": "DE", "Currency of Proceeds": "EUR", "Apple Identifier": "6470009999", "Customer Price": "10.99",
     "Parent Identifier": "RNR001", "Subscription": "Renewal", "Period": "1 Month"},
    # refund: negative units AND negative price must still be a negative amount
    {"SKU": "rnr.pro.lifetime", "Title": "rnr.pro.lifetime", "Product Type Identifier": "IA1", "Units": "-1", "Developer Proceeds": "-83.30",
     "Customer Currency": "USD", "Country Code": "US", "Currency of Proceeds": "USD", "Apple Identifier": "6470008888", "Customer Price": "-119.00",
     "Parent Identifier": "RNR001"},
    # CCRN: no IDs entered yet -> stored, but unmatched (never guessed from the title)
    {"SKU": "CCRN01", "Title": "CCRN Adult", "Product Type Identifier": "1", "Units": "3", "Developer Proceeds": "0",
     "Customer Currency": "USD", "Country Code": "US", "Currency of Proceeds": "USD", "Apple Identifier": "6470005555", "Customer Price": "0"},
]


class Resp:
    def __init__(self, status, content=b"", body=None, headers=None):
        self.status_code, self.content, self._body, self.headers = status, content, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


NOT_FOUND = {"errors": [{"code": "NOT_FOUND", "detail": "There were no sales for the date specified."}]}


class FakeApple:
    """Routes requests like App Store Connect would, and records every call."""

    def __init__(self, sales=None, finance=None, iaps=None, subs=None, script=None):
        self.sales, self.finance = sales or {}, finance or {}
        self.iaps, self.subs = iaps or {}, subs or {}
        self.script = list(script or [])  # responses to return first, whatever the request
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {}), headers))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        p = params or {}
        if url.endswith("/v1/salesReports"):
            data = self.sales.get(p["filter[reportDate]"])
            return Resp(200, data) if data else Resp(404, body=NOT_FOUND)
        if url.endswith("/v1/financeReports"):
            data = self.finance.get(p["filter[reportDate]"])
            return Resp(200, data) if data else Resp(404, body=NOT_FOUND)
        if "/inAppPurchasesV2" in url:
            return Resp(200, body={"data": self.iaps.get(url.split("/")[-2], []), "links": {}})
        if "/subscriptionGroups" in url:
            return Resp(200, body={"data": [], "included": self.subs.get(url.split("/")[-2], []), "links": {}})
        return Resp(404, body=NOT_FOUND)


def setup_con():
    con = duckdb.connect(":memory:")
    revenue.ensure(con)
    registry.update_listing(con, "rnr_pro", "apple_app_store", {"apple_app_id": "6470001234", "bundle_id": "com.example.rnr"})
    return con


def client(fake):
    return apple.Client(apple.Creds.from_env(ENV), session=fake, sleep=lambda s: None)


def q(con, sql, params=None):
    return con.execute(sql, params or []).fetchall()


class TokenTests(unittest.TestCase):
    def test_token_matches_apple_spec(self):
        creds = apple.Creds.from_env(ENV)
        tok = apple.make_token(creds, now=1_800_000_000)
        hdr = jwt.get_unverified_header(tok)
        self.assertEqual((hdr["alg"], hdr["kid"], hdr["typ"]), ("ES256", "2X9R4HXF34", "JWT"))
        body = jwt.decode(tok, PUB, algorithms=["ES256"], audience="appstoreconnect-v1", options={"verify_exp": False, "verify_iat": False})
        self.assertEqual(body["iss"], ENV["ASC_ISSUER_ID"])
        self.assertLessEqual(body["exp"] - body["iat"], 20 * 60)
        with self.assertRaises(ValueError):
            apple.make_token(creds, lifetime=21 * 60)

    def test_credentials(self):
        self.assertIsNone(apple.Creds.from_env({}))
        self.assertIsNone(apple.Creds.from_env({**ENV, "ASC_VENDOR_NUMBER": " "}))
        for bad, hint in (("not-a-key", "too short"), ("AuthKey_2X9R4HXF34.p8", "file name"), ("x" * 120, "isn't the key")):
            with self.subTest(bad=bad):
                with self.assertRaises(apple.AppleError) as cm:
                    apple.Creds.from_env({**ENV, "ASC_PRIVATE_KEY": bad})
                self.assertIn(hint, str(cm.exception))
                self.assertNotIn(bad if len(bad) > 30 else "§", str(cm.exception))
        # the same key pasted without the BEGIN/END lines, on one line, or with Windows line endings still works
        body = "".join(PEM.split("-----")[2].split())
        for pasted in (body, PEM.replace("\n", "\r\n"), PEM.replace("\n", " "), '"' + PEM + '"'):
            with self.subTest(form=pasted[:12]):
                c = apple.Creds.from_env({**ENV, "ASC_PRIVATE_KEY": pasted})
                jwt.decode(apple.make_token(c), PUB, algorithms=["ES256"], audience="appstoreconnect-v1")
        creds = apple.Creds.from_env(ENV)
        self.assertIn("BEGIN PRIVATE KEY", creds.private_key)
        self.assertNotIn("PRIVATE", repr(creds))


class ParseTests(unittest.TestCase):
    def test_parse_and_missing_columns(self):
        header, rows = apple.parse_sales_summary(sales_file(DAY1, DAY1_ROWS))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1]["proceeds"], "6.99")
        utf16 = sales_file(DAY1, DAY1_ROWS[:1], encoding="utf-16")
        self.assertEqual(len(apple.parse_sales_summary(utf16)[1]), 1)
        bad = gzip.compress(b"SKU\tUnits\nX\t1\n")
        with self.assertRaises(apple.AppleFormatError):
            apple.parse_sales_summary(bad)
        self.assertEqual(apple.parse_sales_summary(gzip.compress(b"")), ([], []))

    def test_product_types_follow_apple_list(self):
        for code, kind in {"1F": "download", "3F": "redownload", "7T": "update", "IA1": "iap", "IA3": "restore",
                           "IA9": "subscription", "IAY": "subscription", "IAY-M": "subscription"}.items():
            self.assertEqual(apple.PRODUCT_TYPES[code], kind)


class SyncTests(unittest.TestCase):
    def run_sync(self, con, fake, today=TODAY, env=ENV):
        return apple.run(con, environ=env, client=client(fake), today=today)

    def test_end_to_end_sales(self):
        con = setup_con()
        fake = FakeApple(sales={DAY1.isoformat(): sales_file(DAY1, DAY1_ROWS)})
        res = self.run_sync(con, fake)
        self.assertEqual(res["sales"]["files"], 1)
        self.assertEqual(res["sales"]["not_ready"], 2)      # yesterday and the day before: retry later
        self.assertEqual(res["sales"]["empty_days"], 2)     # older 404s are remembered as empty
        rows = q(con, f"SELECT transaction_type, app_id, units, currency, gross_amount, proceeds_amount, proceeds_currency, subscription_kind, data_status "
                      f"FROM {S}.sales_reports ORDER BY transaction_type, currency, units")
        got = [(t, a, int(u), c, None if g is None else Decimal(str(g)), None if p is None else Decimal(str(p)), pc, sk, ds) for t, a, u, c, g, p, pc, sk, ds in rows]
        self.assertIn(("subscription", "rnr_pro", 2, "USD", Decimal("19.98"), Decimal("13.98"), "USD", "New", "estimated"), got)
        self.assertIn(("subscription", "rnr_pro", 1, "EUR", Decimal("10.99"), Decimal("7.00"), "EUR", "Renewal", "estimated"), got)
        self.assertIn(("refund", "rnr_pro", -1, "USD", Decimal("-119.00"), Decimal("-83.30"), "USD", None, "estimated"), got)
        self.assertIn(("download", None, 3, "USD", Decimal("0"), Decimal("0"), "USD", None, "estimated"), got)   # CCRN: unmatched, not guessed
        tz = q(con, f"SELECT DISTINCT reporting_timezone FROM {S}.sales_reports")[0][0]
        self.assertIn("not stated", tz)
        job = q(con, f"SELECT status, message FROM {S}.synchronization_jobs")[0]
        self.assertEqual(job[0], "partial")                  # because one line couldn't be matched to an app
        self.assertIn("not matched", job[1])
        self.assertEqual(q(con, f"SELECT status FROM {S}.store_accounts WHERE platform = 'apple_app_store'")[0][0], "verified")
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.report_blobs")[0][0], 1)
        # every call carried a bearer token and the vendor number; no call wrote anything
        self.assertTrue(all(h["Authorization"].startswith("Bearer ") for _, _, h in fake.calls))

    def test_rerun_is_idempotent_and_only_asks_for_missing_days(self):
        con = setup_con()
        files = {DAY1.isoformat(): sales_file(DAY1, DAY1_ROWS)}
        self.run_sync(con, FakeApple(sales=files))
        n = q(con, f"SELECT count(*) FROM {S}.sales_reports")[0][0]
        fake2 = FakeApple(sales=files)
        res = self.run_sync(con, fake2)
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.sales_reports")[0][0], n)
        self.assertNotIn("products", res)                                   # once a day only
        asked = sorted(p["filter[reportDate]"] for u, p, _ in fake2.calls if u.endswith("/v1/salesReports"))
        self.assertEqual(asked, [(TODAY - dt.timedelta(days=2)).isoformat(), (TODAY - dt.timedelta(days=1)).isoformat()])
        # the same line arriving in a re-issued file is not double counted
        rid = "x"
        header, rows = apple.parse_sales_summary(files[DAY1.isoformat()])
        lines = apple.sales_lines(rid, rows, header, apple.AppMatcher(con), registry.now())
        self.assertEqual(apple.insert_rows(con, "sales_reports", lines), 0)

    def test_bad_key_is_reported_not_raised(self):
        con = setup_con()
        body = {"errors": [{"code": "NOT_AUTHORIZED", "detail": "Authentication credentials are missing or invalid. token eyJhbGciOiJFUzI1NiJ9.eyJ4IjoxfQ.sig"}]}
        res = self.run_sync(con, FakeApple(script=[Resp(401, body=body)]))
        self.assertIn("failed", res)
        acc = q(con, f"SELECT status, last_error FROM {S}.store_accounts WHERE platform = 'apple_app_store'")[0]
        self.assertEqual(acc[0], "error")
        self.assertNotIn("eyJhbGci", acc[1])
        job = q(con, f"SELECT status, message FROM {S}.synchronization_jobs")[0]
        self.assertEqual(job[0], "failed")
        self.assertIn("refused the key", job[1])

    def test_rate_limit_and_outage(self):
        con = setup_con()
        files = {DAY1.isoformat(): sales_file(DAY1, DAY1_ROWS)}
        fake = FakeApple(sales=files, script=[Resp(429, headers={"Retry-After": "1"}), Resp(503), ConnectionError("reset")])
        res = self.run_sync(con, fake)
        self.assertIn("sales", res)                                         # recovered after retries
        con2 = setup_con()
        res2 = self.run_sync(con2, FakeApple(script=[Resp(500)] * 12))
        self.assertIn("failed", res2)
        self.assertEqual(q(con2, f"SELECT status FROM {S}.synchronization_jobs")[0][0], "failed")

    def test_not_configured_is_a_quiet_skip(self):
        con = setup_con()
        self.assertIn("skipped", apple.run(con, environ={}))
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.synchronization_jobs")[0][0], 0)

    def test_products_are_verified_from_the_store(self):
        con = setup_con()
        iaps = {"6470001234": [
            {"attributes": {"productId": "rnr.pro.lifetime", "name": "Lifetime", "inAppPurchaseType": "NON_CONSUMABLE", "state": "READY_TO_SUBMIT"}},
            {"attributes": {"productId": "rnr.pro.3month", "name": "Three-Month Pass", "inAppPurchaseType": "NON_RENEWING_SUBSCRIPTION"}},
            {"attributes": {"productId": "rnr.mystery", "inAppPurchaseType": "SOMETHING_NEW"}}]}
        subs = {"6470001234": [
            {"type": "subscriptions", "attributes": {"productId": "rnr.pro.monthly", "name": "Monthly", "subscriptionPeriod": "ONE_MONTH"}},
            {"type": "subscriptions", "attributes": {"productId": "rnr.pro.annual", "name": "Annual", "subscriptionPeriod": "ONE_YEAR"}},
            {"type": "subscriptionGroupLocalizations", "attributes": {"name": "ignored"}}]}
        self.run_sync(con, FakeApple(iaps=iaps, subs=subs))
        got = {r[0]: r[1:] for r in q(con, f"SELECT store_product_id, product_type, plan_code, billing_period, verified FROM {S}.store_products WHERE source = 'store_api'")}
        self.assertEqual(got, {
            "rnr.pro.lifetime": ("non_consumable", "lifetime", None, True),
            "rnr.pro.3month": ("non_renewing_subscription", "three_month", None, True),   # the 3-month pass is NOT auto-renewing here
            "rnr.pro.monthly": ("auto_renewable_subscription", "monthly", "P1M", True),
            "rnr.pro.annual": ("auto_renewable_subscription", "annual", "P1Y", True)})
        # unknown type left out, reference rows untouched
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.store_products WHERE source = 'reference' AND verified")[0][0], 0)

    def test_finance_detail(self):
        con = setup_con()
        month = apple.fiscal_months(TODAY, 3)[2]
        fin = "\n".join([
            "Start Date\tEnd Date\tUPC\tISRC/ISBN\tVendor Identifier\tQuantity\tPartner Share\tExtended Partner Share\tPartner Share Currency\t"
            "Sales or Return\tApple Identifier\tArtist/Show/Developer/Author\tTitle\tLabel/Studio/Network/Developer/Publisher\tGrid\t"
            "Product Type Identifier\tISAN/Other Identifier\tCountry Of Sale\tPre-order Flag\tPromo Code\tCustomer Price\tCustomer Currency",
            f"07/27/2026\t08/30/2026\t\t\trnr.pro.monthly\t5\t6.99\t34.95\tUSD\tS\t6470001234\tAlex Korir\tMonthly\t\t\tIAY\t\tUS\t\t\t9.99\tUSD",
            "", "Total_Rows\t1", "Total_Amount\t34.95"])
        res = self.run_sync(con, FakeApple(finance={month: gzip.compress(fin.encode())}))
        self.assertEqual(res["finance"]["inserted"], 1)
        r = q(con, f"SELECT app_id, units, proceeds_amount, currency, data_status, gross_amount FROM {S}.financial_reports")[0]
        self.assertEqual((r[0], int(r[1]), Decimal(str(r[2])), r[3], r[4], r[5]), ("rnr_pro", 5, Decimal("34.95"), "USD", "finalized", None))
        # a layout we don't recognise is archived, not guessed
        con2 = setup_con()
        res2 = self.run_sync(con2, FakeApple(finance={month: gzip.compress(b"Some\tOther\tLayout\n1\t2\t3\n")}))
        self.assertEqual(res2["finance"]["unparsed"], 1)
        self.assertEqual(q(con2, f"SELECT count(*) FROM {S}.financial_reports")[0][0], 0)
        self.assertEqual(q(con2, f"SELECT count(*) FROM {S}.report_blobs")[0][0], 1)
        self.assertEqual(q(con2, f"SELECT status FROM {S}.synchronization_jobs")[0][0], "partial")


if __name__ == "__main__":
    unittest.main()
