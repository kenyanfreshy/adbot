"""Stage D tests: Google Play connector against a fake Google (synthetic data, throwaway key). No network."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
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
from cryptography.hazmat.primitives.asymmetric import rsa

import revenue
from revenue import google_play as gp, registry
from revenue.schema import SCHEMA as S

RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA = {"type": "service_account", "project_id": "adbot", "private_key_id": "abc123",
      "private_key": RSA.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
      "client_email": "adbot-play-reports@adbot.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token"}
PKG = "com.alexkorir.rapidnursingreview"
BUCKET = "pubsite_prod_7714024473854983317"
TODAY = dt.date(2026, 10, 23)


def secret_dir(content=json.dumps(SA), name="play-service-account.json"):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, name), "w") as fh:
        fh.write(content)
    return d


def env(**over):
    e = {"GOOGLE_SECRET_DIR": secret_dir(), "GOOGLE_PLAY_REPORTS_BUCKET": BUCKET}
    e.update(over)
    return e


def zipped(name, text, encoding="utf-8"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text.encode(encoding))
    return buf.getvalue()


SUBS = [{"productId": "rnr_pro", "listings": [{"languageCode": "en-US", "title": "RNR Pro"}], "basePlans": [
    {"basePlanId": "monthly", "state": "ACTIVE", "autoRenewingBasePlanType": {"billingPeriodDuration": "P1M"},
     "regionalConfigs": [{"regionCode": "US", "price": {"currencyCode": "USD", "units": "9", "nanos": 990000000}},
                         {"regionCode": "KE", "price": {"currencyCode": "KES", "units": "1100"}}]},
    {"basePlanId": "three-month-pass", "state": "ACTIVE", "prepaidBasePlanType": {"billingPeriodDuration": "P3M"},
     "regionalConfigs": [{"regionCode": "US", "price": {"currencyCode": "USD", "units": "49", "nanos": 990000000}}]},
    {"basePlanId": "weird", "state": "ACTIVE"}]}]
OTPS = [{"productId": "rnr_lifetime", "listings": [{"title": "Lifetime access"}], "purchaseOptions": [
    {"purchaseOptionId": "buy", "state": "ACTIVE", "buyOption": {},
     "regionalPricingAndAvailabilityConfigs": [{"regionCode": "US", "price": {"currencyCode": "USD", "units": "119"}, "availability": "AVAILABLE"},
                                               {"regionCode": "GB", "price": {"currencyCode": "GBP", "units": "99"}, "availability": "NO_LONGER_AVAILABLE"}]}]}]

SALES_CSV = "\n".join([
    "Order Number,Order Charged Date,Order Charged Timestamp,Financial Status,Device Model,Product Title,Product ID,Product Type,SKU ID,"
    "Currency of Sale,Item Price,Taxes Collected,Charged Amount,City of Buyer,State of Buyer,Postal Code of Buyer,Country of Buyer,Base Plan ID",
    f"GPA.1111-2222-3333-44444,2026-10-05,1759622400,Charged,Pixel 8,RNR Pro,{PKG},subscription,rnr_pro,USD,9.99,0.80,10.79,,,,US,monthly",
    f"GPA.1111-2222-3333-44444..0,2026-10-20,1760918400,Charged,Pixel 8,RNR Pro,{PKG},subscription,rnr_pro,USD,9.99,0.80,10.79,,,,US,monthly",
    f"GPA.5555-6666-7777-88888,2026-10-07,1759795200,Refund,Pixel 7,Lifetime,{PKG},inapp,rnr_lifetime,USD,119.00,0.00,119.00,,,,US,",
    "GPA.9999-0000-1111-22222,2026-10-08,1759881600,Charged,Pixel 7,Other app,com.someone.else,inapp,x,EUR,5.00,0.00,5.00,,,,DE,"])
EARN_CSV = "\n".join([
    "Description,Transaction Date,Transaction Time,Tax Type,Transaction Type,Refund Type,Product Title,Product id,Product Type,Sku Id,"
    "Hardware,Buyer Country,Buyer State,Buyer Postal Code,Buyer Currency,Amount (Buyer Currency),Currency Conversion Rate,Merchant Currency,Amount (Merchant Currency)",
    f"GPA.1111-2222-3333-44444,2026-09-05,1:00:00 AM PDT,,Charge,,RNR Pro,{PKG},subscription,rnr_pro,,US,,,USD,9.99,1.000000,USD,9.99",
    f"GPA.1111-2222-3333-44444,2026-09-05,1:00:00 AM PDT,,Google fee,,RNR Pro,{PKG},subscription,rnr_pro,,US,,,USD,-1.50,1.000000,USD,-1.50"])


class Resp:
    def __init__(self, status, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeGoogle:
    def __init__(self, files=None, token_status=200, deny=(), bucket_exists=True):
        self.files = files or {}
        self.token_status, self.deny, self.bucket_exists = token_status, set(deny), bucket_exists
        self.calls, self.assertions = [], []

    def post(self, url, data=None, timeout=None):
        self.assertions.append(data["assertion"])
        if self.token_status != 200:
            return Resp(self.token_status, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        return Resp(200, {"access_token": "ya29.fake-access-token-" + "x" * 40, "expires_in": 3599})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for d in self.deny:
            if d in url:
                return Resp(403, {"error": {"code": 403, "message": "The caller does not have permission"}})
        if url.endswith("/applications/com.alexkorir.ccrn/oneTimeProducts") or url.endswith("/applications/com.alexkorir.ccrn/subscriptions"):
            return Resp(204)                                   # Google's answer for an empty list
        if url.endswith(f"/applications/{PKG}/subscriptions"):
            return Resp(200, {"subscriptions": SUBS})
        if url.endswith(f"/applications/{PKG}/oneTimeProducts"):
            if not (params or {}).get("pageToken"):
                return Resp(200, {"oneTimeProducts": OTPS[:0], "nextPageToken": "p2"})   # exercise paging
            return Resp(200, {"oneTimeProducts": OTPS})
        if f"/b/{BUCKET}/o/" in url and (params or {}).get("alt") == "media":
            name = url.split("/o/")[1].replace("%2F", "/")
            return Resp(200, content=self.files[name][1])
        if f"/b/{BUCKET}/o" in url:
            if not self.bucket_exists:
                return Resp(404, {"error": {"message": "Not Found"}})
            p = (params or {}).get("prefix", "")
            return Resp(200, {"items": [{"name": n, "generation": g} for n, (g, _) in self.files.items() if n.startswith(p)]})
        return Resp(404, {"error": {"message": "Not Found"}})


def setup_con():
    con = duckdb.connect(":memory:")
    revenue.ensure(con)
    registry.update_listing(con, "rnr_pro", "google_play", {"package_name": PKG})
    return con


def q(con, sql, p=None):
    return con.execute(sql, p or []).fetchall()


def run(con, fake, e=None, today=TODAY, force=True, **kw):
    e = e or env()
    return gp.run(con, environ=e, client=gp.Client(gp.load_service_account(e), session=fake, sleep=lambda s: None), today=today, force=force, **kw)


class CredTests(unittest.TestCase):
    def test_key_loading(self):
        self.assertIsNone(gp.load_service_account({"GOOGLE_SECRET_DIR": tempfile.mkdtemp()}))
        self.assertEqual(gp.load_service_account(env())["client_email"], SA["client_email"])
        self.assertEqual(gp.load_service_account({"GOOGLE_SECRET_DIR": tempfile.mkdtemp(), "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON": json.dumps(SA)})["type"], "service_account")
        with self.assertRaises(gp.PlayError) as cm:
            gp.load_service_account({"GOOGLE_SECRET_DIR": secret_dir("not json")})
        self.assertIn("isn't valid JSON", str(cm.exception))
        with self.assertRaises(gp.PlayError):
            gp.load_service_account({"GOOGLE_SECRET_DIR": secret_dir(json.dumps({"type": "authorized_user"}))})

    def test_bucket_forms(self):
        for raw in (BUCKET, f"gs://{BUCKET}/stats/", f"https://storage.cloud.google.com/{BUCKET}/stats/crashes/x.csv?authuser=1"):
            self.assertEqual(gp.bucket_name({"GOOGLE_PLAY_REPORTS_BUCKET": raw}), BUCKET)
        self.assertIsNone(gp.bucket_name({}))
        with self.assertRaises(gp.PlayError):
            gp.bucket_name({"GOOGLE_PLAY_REPORTS_BUCKET": "my-bucket"})

    def test_token_assertion(self):
        fake = FakeGoogle()
        run(setup_con(), fake)
        claims = jwt.decode(fake.assertions[0], RSA.public_key(), algorithms=["RS256"], audience=SA["token_uri"], options={"verify_iat": False})
        self.assertEqual(claims["iss"], SA["client_email"])
        self.assertIn("androidpublisher", claims["scope"])
        self.assertIn("devstorage.read_only", claims["scope"])
        self.assertLessEqual(claims["exp"] - claims["iat"], 3600)

    def test_money(self):
        self.assertEqual(gp.money({"currencyCode": "usd", "units": "9", "nanos": 990000000}), ("USD", Decimal("9.99")))
        self.assertEqual(gp.money({"currencyCode": "USD", "units": "-1", "nanos": -750000000}), ("USD", Decimal("-1.75")))
        self.assertIsNone(gp.money(None))


class ProductTests(unittest.TestCase):
    def test_products_types_and_prices(self):
        con = setup_con()
        res = run(con, FakeGoogle())
        self.assertNotIn("failed", res)
        got = {r[0]: r[1:] for r in q(con, f"SELECT product_key, product_type, plan_code, billing_period, verified FROM {S}.store_products WHERE platform = 'google_play'")}
        self.assertEqual(got, {
            "google_play:rnr_pro:monthly": ("auto_renewable_subscription", "monthly", "P1M", True),
            "google_play:rnr_pro:three-month-pass": ("non_renewing_subscription", "three_month", "P3M", True),   # prepaid = not auto-renewing
            "google_play:rnr_lifetime:buy": ("one_time_product", "lifetime", None, True)})
        prices = {(r[0], r[1], r[2]): Decimal(str(r[3])) for r in q(con, f"SELECT product_key, territory, currency, amount FROM {S}.product_prices WHERE source = 'store_api'")}
        self.assertEqual(prices[("google_play:rnr_pro:monthly", "US", "USD")], Decimal("9.99"))
        self.assertEqual(prices[("google_play:rnr_pro:monthly", "KE", "KES")], Decimal("1100"))
        self.assertNotIn(("google_play:rnr_lifetime:buy", "GB", "GBP"), prices)      # no longer available: not listed as a price
        self.assertEqual(q(con, f"SELECT status FROM {S}.store_accounts WHERE platform = 'google_play'")[0][0], "verified")

    def test_price_history(self):
        con = setup_con()
        run(con, FakeGoogle())
        n = q(con, f"SELECT count(*) FROM {S}.product_prices WHERE source = 'store_api'")[0][0]
        run(con, FakeGoogle(), today=TODAY + dt.timedelta(days=1))                 # unchanged: nothing new
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.product_prices WHERE source = 'store_api'")[0][0], n)
        SUBS[0]["basePlans"][0]["regionalConfigs"][0]["price"] = {"currencyCode": "USD", "units": "10", "nanos": 990000000}
        try:
            run(con, FakeGoogle(), today=TODAY + dt.timedelta(days=5))
        finally:
            SUBS[0]["basePlans"][0]["regionalConfigs"][0]["price"] = {"currencyCode": "USD", "units": "9", "nanos": 990000000}
        hist = q(con, f"SELECT amount, effective_from, effective_to FROM {S}.product_prices WHERE product_key = 'google_play:rnr_pro:monthly' "
                      "AND territory = 'US' ORDER BY effective_from")
        self.assertEqual([(Decimal(str(a)), str(f)[:10], None if t is None else str(t)[:10]) for a, f, t in hist],
                         [(Decimal("9.99"), "2026-10-23", "2026-10-27"), (Decimal("10.99"), "2026-10-28", None)])

    def test_permission_problems(self):
        con = setup_con()
        res = run(con, FakeGoogle(deny=["/applications/"]))
        job = q(con, f"SELECT status, message FROM {S}.synchronization_jobs WHERE job_type = 'products'")[0]
        self.assertEqual(job[0], "partial")
        self.assertIn("Android Developer API", job[1])
        con2 = setup_con()
        res2 = run(con2, FakeGoogle(token_status=400))
        self.assertIn("failed", res2)
        acc = q(con2, f"SELECT status, last_error FROM {S}.store_accounts WHERE platform = 'google_play'")[0]
        self.assertEqual(acc[0], "error")
        self.assertIn("Invalid JWT Signature", acc[1])
        self.assertNotIn("BEGIN PRIVATE KEY", acc[1])

    def test_empty_lists_are_not_errors(self):
        con = setup_con()
        registry.update_listing(con, "ccrn_adult", "google_play", {"package_name": "com.alexkorir.ccrn"})
        res = run(con, FakeGoogle())
        self.assertEqual(res["products"]["apps"], 2)
        self.assertEqual(q(con, f"SELECT status FROM {S}.synchronization_jobs WHERE job_type = 'products'")[0][0], "succeeded")   # empty lists are fine
        msg = q(con, f"SELECT message FROM {S}.synchronization_jobs WHERE job_type = 'products'")[0][0]
        self.assertNotIn("204", msg)

    def test_daily_guard_and_skip(self):
        con = setup_con()
        self.assertIn("skipped", gp.run(con, environ={"GOOGLE_SECRET_DIR": tempfile.mkdtemp()}))
        run(con, FakeGoogle())                               # clean run (bucket set, no files) -> done for the day
        self.assertIn("skipped", run(con, FakeGoogle(), force=False))
        con2 = setup_con()
        run(con2, FakeGoogle(deny=["/applications/"]))       # product warning -> products retried after 50 minutes
        con2.execute(f"UPDATE {S}.synchronization_jobs SET finished_at = finished_at - INTERVAL 55 MINUTE")
        res = run(con2, FakeGoogle(deny=["/applications/"]), force=False)
        self.assertIn("status", res["products"])
        self.assertIn("skipped", res["reports"])             # the clean report pass still counts for the day


class ReportTests(unittest.TestCase):
    def files(self, earnings=EARN_CSV):
        return {"sales/salesreport_202610.zip": ("1001", zipped("salesreport_202610.csv", SALES_CSV)),
                "earnings/earnings_202609.zip": ("2001", zipped("PlayApps_202609.csv", earnings))}

    def test_reports_import(self):
        con = setup_con()
        res = run(con, FakeGoogle(files=self.files()))
        self.assertEqual(res["reports"]["files"], 2)
        sales = q(con, f"SELECT transaction_type, app_id, units, currency, gross_amount, subscription_kind, data_status, proceeds_amount "
                       f"FROM {S}.sales_reports ORDER BY period_start")
        norm = [(t, a, int(u), c, None if g is None else Decimal(str(g)), k, ds, p) for t, a, u, c, g, k, ds, p in sales]
        self.assertEqual(norm, [
            ("subscription", "rnr_pro", 1, "USD", Decimal("9.99"), "New", "estimated", None),
            ("refund", "rnr_pro", -1, "USD", Decimal("-119.00"), None, "estimated", None),
            ("iap", None, 1, "EUR", Decimal("5.00"), None, "estimated", None),            # someone else's package: unmatched
            ("subscription", "rnr_pro", 1, "USD", Decimal("9.99"), "Renewal", "estimated", None)])
        fin = q(con, f"SELECT transaction_type, proceeds_amount, currency, data_status, fiscal_period FROM {S}.financial_reports ORDER BY proceeds_amount")
        self.assertEqual([(t, Decimal(str(p)), c, s, f) for t, p, c, s, f in fin],
                         [("Google fee", Decimal("-1.50"), "USD", "finalized", "2026-09"), ("Charge", Decimal("9.99"), "USD", "finalized", "2026-09")])
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.report_blobs")[0][0], 2)
        self.assertEqual(q(con, f"SELECT status, outcome FROM {S}.synchronization_jobs WHERE job_type = 'reports'")[0], ("partial", "ok_imported"))   # one unmatched line

    def test_new_generation_adds_only_new_orders(self):
        con = setup_con()
        run(con, FakeGoogle(files=self.files()))
        n = q(con, f"SELECT count(*) FROM {S}.sales_reports")[0][0]
        f2 = self.files()
        f2["sales/salesreport_202610.zip"] = ("1002", zipped("salesreport_202610.csv", SALES_CSV + "\n" +
            f"GPA.3333-0000-0000-00000,2026-10-21,1761004800,Charged,Pixel 9,RNR Pro,{PKG},subscription,rnr_pro,USD,9.99,0.80,10.79,,,,US,monthly"))
        fake = FakeGoogle(files=f2)
        res = run(con, fake)
        self.assertEqual(res["reports"]["inserted"], 1)
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.sales_reports")[0][0], n + 1)
        downloaded = [u for u, p in fake.calls if p.get("alt") == "media"]
        self.assertEqual(len(downloaded), 1)                      # the unchanged earnings file isn't downloaded again

    def test_unknown_layout_and_utf16(self):
        con = setup_con()
        res = run(con, FakeGoogle(files={"earnings/earnings_202609.zip": ("1", zipped("e.csv", "Foo,Bar\n1,2"))}))
        self.assertEqual(res["reports"]["unparsed"], 1)
        self.assertEqual(q(con, f"SELECT count(*) FROM {S}.financial_reports")[0][0], 0)
        con2 = setup_con()
        res2 = run(con2, FakeGoogle(files={"earnings/earnings_202609.zip": ("1", zipped("e.csv", EARN_CSV, "utf-16"))}))
        self.assertEqual(res2["reports"]["inserted"], 2)

    def test_bucket_problems_dont_block_products(self):
        con = setup_con()
        res = run(con, FakeGoogle(deny=["/b/"]))
        self.assertEqual(res["products"]["products"], 3)
        self.assertEqual(q(con, f"SELECT outcome FROM {S}.synchronization_jobs WHERE job_type = 'reports'")[0][0], "access_denied")
        con2 = setup_con()
        run(con2, FakeGoogle(bucket_exists=False))
        self.assertEqual(q(con2, f"SELECT outcome FROM {S}.synchronization_jobs WHERE job_type = 'reports'")[0][0], "not_found")
        con3 = setup_con()
        run(con3, FakeGoogle(), e=env(GOOGLE_PLAY_REPORTS_BUCKET=""))
        self.assertEqual(q(con3, f"SELECT outcome, message FROM {S}.synchronization_jobs WHERE job_type = 'reports'")[0][0], "not_configured")


def denied_403():
    """What Cloud Storage answers when the identity lacks storage.objects.list on the bucket."""
    return FakeGoogle(deny=["/b/"])


class ReportStatusTests(unittest.TestCase):
    """Report sync is tracked apart from product sync (revenue/google_play.py)."""

    def jobs(self, con, kind):
        return q(con, f"SELECT status, outcome, message FROM {S}.synchronization_jobs WHERE job_type = ? ORDER BY started_at", [kind])

    def test_products_succeed_while_reports_get_403(self):
        con = setup_con()
        res = run(con, denied_403())
        self.assertEqual(res["products"]["status"], "succeeded")
        self.assertEqual(res["products"]["products"], 3)
        self.assertEqual(res["reports"]["outcome"], "access_denied")
        self.assertEqual(self.jobs(con, "products")[0][0], "succeeded")
        status, outcome, msg = self.jobs(con, "reports")[0]
        self.assertEqual((status, outcome), ("failed", "access_denied"))
        self.assertIn(SA["client_email"], msg)
        self.assertIn(BUCKET, msg)
        self.assertNotIn("awaiting", msg.lower())
        src = next(x for x in registry.registry(con, environ={})["sources"] if x["platform"] == "google_play")
        self.assertEqual(src["reports"]["state"], "access_denied")
        self.assertIsNone(src["last_success"])                  # products worked, but no report pass ever did
        self.assertIsNotNone(src["products_last_success"])

    def test_failed_attempts_keep_last_success(self):
        con = setup_con()
        run(con, FakeGoogle())                                   # bucket readable, empty
        ok = gp.report_state(con)["last_success"]
        self.assertIsNotNone(ok)
        run(con, denied_403())
        st = gp.report_state(con)
        self.assertEqual(st["state"], "access_denied")
        self.assertEqual(str(st["last_success"]), str(ok))
        self.assertEqual(st["failures_in_a_row"], 1)

    def test_403_is_not_retried_every_hour(self):
        con = setup_con()
        run(con, denied_403())
        con.execute(f"UPDATE {S}.synchronization_jobs SET finished_at = finished_at - INTERVAL 2 HOUR, started_at = started_at - INTERVAL 2 HOUR")
        fake = denied_403()
        res = run(con, fake, force=False)
        self.assertIn("skipped", res)                            # products done for the day, 403 waits for its daily retry
        self.assertFalse(any("/b/" in u for u, _ in fake.calls))
        con.execute(f"UPDATE {S}.synchronization_jobs SET finished_at = finished_at - INTERVAL 23 HOUR, started_at = started_at - INTERVAL 23 HOUR")
        res = run(con, denied_403(), force=False)
        self.assertEqual(res["reports"]["outcome"], "access_denied")

    def test_other_errors_back_off(self):
        now = dt.datetime(2026, 10, 25, 12, 0)
        self.assertEqual(gp._next_report_try("error", now, 1), now + dt.timedelta(minutes=45))
        self.assertEqual(gp._next_report_try("error", now, 3), now + dt.timedelta(hours=4) - dt.timedelta(minutes=15))
        self.assertEqual(gp._next_report_try("error", now, 99), now + dt.timedelta(hours=24) - dt.timedelta(minutes=15))
        self.assertEqual(gp._next_report_try("access_denied", now, 5), now + dt.timedelta(hours=24) - dt.timedelta(minutes=15))

    def test_explicit_retry_bypasses_only_the_report_guard(self):
        con = setup_con()
        run(con, denied_403())
        products_before = len(self.jobs(con, "products"))
        fake = FakeGoogle()                                      # access fixed
        res = run(con, fake, force=False, retry_reports=True, reports_only=True)
        self.assertEqual(res["reports"]["outcome"], "ok_empty")
        self.assertEqual(len(self.jobs(con, "products")), products_before)    # products not re-synced
        self.assertFalse(any("/applications/" in u for u, _ in fake.calls))
        # the phone app's "Retry reports" request: the next hourly run retries reports, products keep their guard
        con2 = setup_con()
        run(con2, denied_403())
        gp.request_report_retry(con2)
        self.assertIsNotNone(gp.report_state(con2)["retry_requested_at"])
        fake2 = FakeGoogle()
        res2 = run(con2, fake2, force=False)
        self.assertEqual(res2["reports"]["outcome"], "ok_empty")
        self.assertIn("skipped", res2["products"])
        self.assertFalse(any("/applications/" in u for u, _ in fake2.calls))
        self.assertIsNone(gp.report_state(con2)["retry_requested_at"])

    def test_empty_bucket_is_not_access_denied(self):
        con = setup_con()
        run(con, FakeGoogle())
        self.assertEqual(gp.report_state(con)["state"], "ok_empty")
        con2 = setup_con()
        run(con2, denied_403())
        self.assertEqual(gp.report_state(con2)["state"], "access_denied")
        con3 = setup_con()
        self.assertEqual(gp.report_state(con3)["state"], "never_checked")

    def test_repeated_imports_do_not_duplicate(self):
        con = setup_con()
        files = ReportTests().files()
        run(con, FakeGoogle(files=files))
        counts = [q(con, f"SELECT count(*) FROM {S}.{t}")[0][0] for t in ("sales_reports", "financial_reports", "report_files")]
        res = run(con, FakeGoogle(files=files), force=False, retry_reports=True, reports_only=True)
        self.assertEqual(res["reports"]["inserted"], 0)
        self.assertEqual(res["reports"]["outcome"], "ok_imported")
        self.assertEqual([q(con, f"SELECT count(*) FROM {S}.{t}")[0][0] for t in ("sales_reports", "financial_reports", "report_files")], counts)


class DiagnoseTests(unittest.TestCase):
    def check_clean(self, d):
        text = json.dumps(d)
        for secret in ("PRIVATE KEY", "ya29.", SA["private_key"][40:80], "assertion"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(d), {"service_account", "bucket", "result", "detail"})

    def test_denied(self):
        fake = denied_403()
        d = gp.diagnose_reports(env(), session=fake)
        self.check_clean(d)
        self.assertEqual((d["service_account"], d["bucket"], d["result"]), (SA["client_email"], BUCKET, "HTTP 403"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1].get("maxResults"), 1)   # at most one object requested
        self.assertNotIn("alt", fake.calls[0][1])                  # nothing downloaded

    def test_success_empty_and_not_configured(self):
        d = gp.diagnose_reports(env(), session=FakeGoogle())
        self.check_clean(d)
        self.assertEqual(d["result"], "success, empty")
        d = gp.diagnose_reports(env(), session=FakeGoogle(files=ReportTests().files()))
        self.assertEqual(d["result"], "success")
        self.assertNotIn("salesreport", json.dumps(d))             # object names are not printed
        d = gp.diagnose_reports(env(GOOGLE_PLAY_REPORTS_BUCKET=""), session=FakeGoogle())
        self.assertEqual(d["result"], "not configured")
        d = gp.diagnose_reports(env(), session=FakeGoogle(token_status=400))
        self.check_clean(d)
        self.assertEqual(d["result"], "HTTP 400")
        self.assertIn("token request", d["detail"])
        self.assertEqual(gp.diagnose_reports(env(), session=FakeGoogle(bucket_exists=False))["result"], "HTTP 404")

    def test_command_prints_only_the_four_fields(self):
        import contextlib
        real = gp.diagnose_reports
        gp.diagnose_reports = lambda: real(env(), session=denied_403())
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = gp._main(["diagnose-reports"])
        finally:
            gp.diagnose_reports = real
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual([l.split()[0] for l in out.strip().splitlines()], ["service_account", "bucket", "result", "detail"])
        self.assertIn(SA["client_email"], out)
        self.assertNotIn("PRIVATE KEY", out)
        self.assertNotIn("ya29.", out)


if __name__ == "__main__":
    unittest.main()
