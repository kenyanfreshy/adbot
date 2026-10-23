"""Stage B tests: schema, registry, money. Synthetic data only; no network, no credentials.

Run:  python -m unittest discover -s tests -v
Uses the real `duckdb` package when installed (Render, the trading PC), otherwise
tests/duckdb_cli_shim.py with the DuckDB CLI (set DUCKDB_CLI=/path/to/duckdb).
"""
import datetime as dt
import json
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import duckdb  # noqa: F401
except ImportError:  # sandbox without PyPI: same engine through the CLI
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import duckdb_cli_shim as duckdb  # type: ignore

import revenue
from revenue import money, registry, schema
from revenue.money import CurrencyMismatch, ExchangeRate, Money, MoneyError

S = schema.SCHEMA
TS = "2026-10-20 12:00:00"


def fresh():
    con = duckdb.connect(":memory:")
    revenue.ensure(con)
    return con


def one(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def fin_values(**over):
    """A minimal valid sales_reports row (synthetic)."""
    row = {"line_key": "k1", "source_platform": "apple_app_store", "app_id": "rnr_pro", "source_app_ref": "1234567890",
           "store_product_id": "rnr.pro.monthly", "currency": "USD", "period_start": "2026-10-19", "period_end": "2026-10-19",
           "reporting_timezone": "America/Los_Angeles", "source_report_id": "synthetic:test", "imported_at": TS,
           "data_status": "estimated", "territory": "US", "transaction_type": "subscription", "product_type": None,
           "units": 1, "customer_price": "9.99", "gross_amount": "9.99", "proceeds_amount": "8.49"}
    row.update(over)
    return row


def insert(con, table, row, on_conflict=""):
    cols = ", ".join(row)
    con.execute(f"INSERT INTO {S}.{table} ({cols}) VALUES ({', '.join('?' * len(row))}) {on_conflict}", list(row.values()))


class SchemaTests(unittest.TestCase):
    def test_all_tables_created_and_migrations_idempotent(self):
        con = fresh()
        names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ?", [S]).fetchall()}
        for t in schema.TABLES + ["schema_migrations"]:
            self.assertIn(t, names)
        self.assertEqual(schema.ensure_schema(con), [])          # nothing left to apply
        self.assertEqual(revenue.ensure(con)["migrations"], [])
        self.assertEqual(one(con, f"SELECT max(version) FROM {S}.schema_migrations"), schema.LATEST)

    def test_money_columns_are_decimal_not_double(self):
        con = fresh()
        bad = con.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                          "WHERE table_schema = ? AND (data_type LIKE 'DOUBLE%' OR data_type LIKE 'FLOAT%' OR data_type = 'REAL')", [S]).fetchall()
        self.assertEqual(bad, [])
        t = one(con, "SELECT data_type FROM information_schema.columns WHERE table_schema = ? AND table_name = 'sales_reports' "
                     "AND column_name = 'proceeds_amount'", [S])
        self.assertEqual(t, "DECIMAL(18,6)")

    def test_duplicate_report_line_is_stored_once(self):
        con = fresh()
        insert(con, "sales_reports", fin_values(), "ON CONFLICT DO NOTHING")
        insert(con, "sales_reports", fin_values(source_report_id="synthetic:other-file"), "ON CONFLICT DO NOTHING")
        self.assertEqual(one(con, f"SELECT count(*) FROM {S}.sales_reports"), 1)
        self.assertEqual(Decimal(str(one(con, f"SELECT sum(proceeds_amount) FROM {S}.sales_reports"))), Decimal("8.49"))

    def test_renewal_counted_once(self):
        con = fresh()
        ev = {"event_key": "apple_app_store:renewal:abc:2026-10-19", "source_platform": "apple_app_store", "app_id": "rnr_pro",
              "source_app_ref": "1234567890", "store_product_id": "rnr.pro.monthly", "currency": "USD", "period_start": "2026-10-19",
              "period_end": "2026-11-18", "reporting_timezone": "UTC", "source_report_id": "synthetic:n1", "imported_at": TS,
              "data_status": "estimated", "event_type": "renewal", "subscriber_ref_sha": registry.sha256_ref("orig-1"), "amount": "9.99"}
        insert(con, "subscription_events", ev, "ON CONFLICT DO NOTHING")
        insert(con, "subscription_events", {**ev, "source_report_id": "synthetic:n2"}, "ON CONFLICT DO NOTHING")
        self.assertEqual(one(con, f"SELECT count(*) FROM {S}.subscription_events WHERE event_type = 'renewal'"), 1)

    def test_financial_rows_must_carry_provenance(self):
        con = fresh()
        for field in ("currency", "source_report_id", "reporting_timezone", "data_status", "source_app_ref", "period_start", "imported_at"):
            with self.subTest(missing=field):
                with self.assertRaises(Exception):
                    insert(con, "sales_reports", fin_values(line_key=f"miss-{field}", **{field: None}))
        with self.assertRaises(Exception):
            insert(con, "sales_reports", fin_values(line_key="bad-status", data_status="guess"))
        with self.assertRaises(Exception):
            insert(con, "sales_reports", fin_values(line_key="bad-period", period_start="2026-10-20", period_end="2026-10-19"))
        with self.assertRaises(Exception):
            insert(con, "sales_reports", fin_values(line_key="bad-platform", source_platform="meta_ads"))

    def test_financial_reports_are_finalized_only(self):
        con = fresh()
        base = {k: v for k, v in fin_values().items() if k not in ("line_key", "territory", "transaction_type", "product_type", "units", "customer_price", "gross_amount", "proceeds_amount")}
        with self.assertRaises(Exception):
            insert(con, "financial_reports", {**base, "financial_key": "f1", "fiscal_period": "2026-10", "data_status": "estimated"})
        insert(con, "financial_reports", {**base, "financial_key": "f2", "fiscal_period": "2026-10", "data_status": "finalized", "proceeds_amount": "100.00"})
        self.assertEqual(one(con, f"SELECT count(*) FROM {S}.financial_reports"), 1)

    def test_verified_product_needs_store_confirmation(self):
        con = fresh()
        with self.assertRaises(Exception):  # a reference row can't claim to be verified
            con.execute(f"UPDATE {S}.store_products SET verified = TRUE WHERE product_key = ?",
                        [registry.ref_product_key("rnr_pro", "apple_app_store", "monthly")])
        with self.assertRaises(Exception):  # admin/store rows need the store's product id
            con.execute(f"INSERT INTO {S}.store_products (product_key, app_id, platform, plan_code, display_name, source, created_at, updated_at) "
                        "VALUES ('x', 'rnr_pro', 'google_play', 'monthly', 'x', 'admin', ?, ?)", [TS, TS])

    def test_exchange_rate_needs_source_and_positive_rate(self):
        con = fresh()
        with self.assertRaises(Exception):
            con.execute(f"INSERT INTO {S}.exchange_rates VALUES ('2026-10-01', 'EUR', 'USD', 0, 'ecb', ?)", [TS])
        with self.assertRaises(Exception):
            con.execute(f"INSERT INTO {S}.exchange_rates VALUES ('2026-10-01', 'EUR', 'USD', 1.07, NULL, ?)", [TS])


class RegistryTests(unittest.TestCase):
    def test_seed_contents(self):
        con = fresh()
        apps = dict(con.execute(f"SELECT app_id, name FROM {S}.applications").fetchall())
        self.assertEqual(apps, {"nursebound_pro": "NurseBound Pro", "rnr_pro": "Rapid Nursing Review Pro", "ccrn_adult": "CCRN Adult"})
        prices = {(r[0], r[1]): Decimal(str(r[2])) for r in con.execute(
            f"SELECT p.app_id, p.plan_code, x.amount FROM {S}.store_products p JOIN {S}.product_prices x USING (product_key)").fetchall()}
        self.assertEqual(prices, {
            ("nursebound_pro", "monthly"): Decimal("14.99"), ("nursebound_pro", "annual"): Decimal("99.99"), ("nursebound_pro", "lifetime"): Decimal("149.99"),
            ("rnr_pro", "monthly"): Decimal("9.99"), ("rnr_pro", "three_month"): Decimal("49.99"), ("rnr_pro", "annual"): Decimal("89.99"), ("rnr_pro", "lifetime"): Decimal("119.00"),
            ("ccrn_adult", "monthly"): Decimal("12.99"), ("ccrn_adult", "three_month"): Decimal("29.99"), ("ccrn_adult", "annual"): Decimal("59.99")})

    def test_reference_data_makes_no_assumptions(self):
        con = fresh()
        # every product unverified, including three-month passes; no billing period guessed
        self.assertEqual(con.execute(f"SELECT DISTINCT product_type, verified, source, billing_period FROM {S}.store_products").fetchall(),
                         [("unverified", False, "reference", None)])
        # no Android products or prices assumed from iOS
        self.assertEqual(one(con, f"SELECT count(*) FROM {S}.store_products WHERE platform = 'google_play'"), 0)
        # reference prices only ever come from the 'reference' source, US storefront, USD
        self.assertEqual(con.execute(f"SELECT DISTINCT source, territory, currency FROM {S}.product_prices").fetchall(), [("reference", "US", "USD")])
        # identifiers start empty, publication status unknown
        self.assertEqual(con.execute(f"SELECT DISTINCT apple_app_id, bundle_id, package_name, publication_status FROM {S}.store_listings").fetchall(),
                         [(None, None, None, "unknown")])
        # no revenue rows are created by seeding
        for t in ("sales_reports", "financial_reports", "purchase_events", "subscription_events", "refund_events", "advertising_spend"):
            self.assertEqual(one(con, f"SELECT count(*) FROM {S}.{t}"), 0, t)

    def test_seed_is_idempotent_and_keeps_edits(self):
        con = fresh()
        registry.update_listing(con, "rnr_pro", "google_play", {"package_name": "com.example.rnr"})
        con.execute(f"DELETE FROM {S}.applications WHERE app_id = 'ccrn_adult'")  # force ensure() to reseed
        res = revenue.ensure(con)
        self.assertEqual(res["seeded"]["applications"], 1)
        self.assertEqual(res["seeded"]["store_products"], 0)
        self.assertEqual(one(con, f"SELECT package_name FROM {S}.store_listings WHERE app_id = 'rnr_pro' AND platform = 'google_play'"), "com.example.rnr")
        self.assertEqual(registry.seed(con), {k: 0 for k in ("applications", "store_listings", "store_products", "product_prices", "store_accounts")})

    def test_update_listing_validation(self):
        con = fresh()
        registry.update_listing(con, "nursebound_pro", "apple_app_store", {"apple_app_id": "6470000001", "bundle_id": "com.example.nursebound"})
        bad = [("rnr_pro", "apple_app_store", {"apple_app_id": "12ab"}),
               ("rnr_pro", "apple_app_store", {"bundle_id": "nodots"}),
               ("rnr_pro", "apple_app_store", {"package_name": "com.x.y"}),          # wrong store
               ("rnr_pro", "google_play", {"package_name": "com.bad-dash.app"}),
               ("rnr_pro", "apple_app_store", {"apple_app_id": "6470000001"}),       # already on NurseBound
               ("nope", "google_play", {"package_name": "com.x.y"}),
               ("rnr_pro", "tiktok", {"package_name": "com.x.y"}),
               ("rnr_pro", "google_play", {})]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(registry.RegistryError):
                    registry.update_listing(con, *args)
        registry.update_listing(con, "nursebound_pro", "apple_app_store", {"bundle_id": ""})  # clearing is allowed
        self.assertIsNone(one(con, f"SELECT bundle_id FROM {S}.store_listings WHERE app_id = 'nursebound_pro' AND platform = 'apple_app_store'"))
        self.assertGreaterEqual(one(con, f"SELECT count(*) FROM {S}.audit_logs WHERE action = 'registry.update_listing'"), 2)

    def test_registry_reports_credentials_by_name_only(self):
        con = fresh()
        secret = "S3CR3T-" + "x" * 50
        env = {"META_ACCESS_TOKEN": secret, "META_AD_ACCOUNT_IDS": "act_1"}
        reg = registry.registry(con, environ=env)
        text = json.dumps(reg, default=str)
        self.assertNotIn(secret, text)
        src = {s["platform"]: s for s in reg["sources"]}
        self.assertEqual(set(src), set(schema.PLATFORMS))
        self.assertEqual(src["meta_ads"]["status"], "configured")
        self.assertEqual(src["apple_app_store"]["status"], "not_configured")
        self.assertIn("ASC_PRIVATE_KEY", src["apple_app_store"]["credentials"]["missing"])
        self.assertIsNone(src["google_play"]["last_success"])  # never synced -> no time, not a fake one
        self.assertEqual(len(reg["apps"]), 3)

    def test_sync_jobs_and_last_success(self):
        con = fresh()
        j1 = registry.start_job(con, "apple_app_store", "sales_daily", dt.date(2026, 10, 18), dt.date(2026, 10, 18))
        registry.finish_job(con, j1, "succeeded", 10, 10)
        j2 = registry.start_job(con, "apple_app_store", "sales_daily", dt.date(2026, 10, 19), dt.date(2026, 10, 19))
        registry.finish_job(con, j2, "failed", message="HTTP 401 with token eyJhbGciOiJFUzI1NiIsImtpZCI6IkFCQyJ9.eyJpc3MiOiJ4In0.sig")
        src = {s["platform"]: s for s in registry.registry(con, environ={})["sources"]}["apple_app_store"]
        self.assertEqual(str(src["period_end"])[:10], "2026-10-18")   # last *successful* period
        self.assertEqual(src["last_status"], "failed")
        self.assertNotIn("eyJhbGci", src["last_message"] or "")
        with self.assertRaises(ValueError):
            registry.finish_job(con, j2, "done")

    def test_redact(self):
        d = registry.redact({"access_token": "abc", "nested": {"private_key": "-----BEGIN"}, "note": "ok",
                             "blob": "A" * 60, "list": [{"password": "p"}]})
        self.assertEqual(d, {"access_token": "[redacted]", "nested": {"private_key": "[redacted]"}, "note": "ok",
                             "blob": "[redacted]", "list": [{"password": "[redacted]"}]})
        msg = registry.scrub_text("Meta said: Invalid OAuth access token EAAG" + "b" * 60 + " (code 190); Authorization: Bearer abc.def")
        self.assertEqual(msg, "Meta said: Invalid OAuth access token [redacted] (code 190); Authorization: [redacted]")
        self.assertEqual(registry.scrub_text("pem -----BEGIN PRIVATE KEY-----\nMIGT\n-----END PRIVATE KEY----- end"), "pem [redacted] end")
        self.assertEqual(registry.scrub_text("No Sales data for 2026-10-19 (vendor 85012345)"), "No Sales data for 2026-10-19 (vendor 85012345)")
        path = "HTTP 403 on androidpublisher/applications/com.alexkorir.nursebound/oneTimeProducts"
        self.assertEqual(registry.scrub_text(path), path)                    # API paths stay readable
        self.assertIn("[redacted]", registry.scrub_text("token ya29.a0AfB_byC" + "q" * 60))
        self.assertEqual(registry.sha256_ref("tok"), registry.sha256_ref("tok"))
        self.assertNotIn("tok", registry.sha256_ref("tok"))
        self.assertIsNone(registry.sha256_ref(""))


class MoneyTests(unittest.TestCase):
    def test_decimal_exactness_and_parsing(self):
        self.assertEqual((Money.of("0.10", "USD") + Money.of("0.20", "USD")).amount, Decimal("0.30"))
        self.assertEqual(money.to_decimal("1,234.50"), Decimal("1234.50"))
        self.assertEqual(money.to_decimal("(12.00)"), Decimal("-12.00"))
        self.assertEqual(money.to_decimal("−0.70"), Decimal("-0.70"))
        self.assertIsNone(money.to_decimal(""))
        for bad in (0.1, "abc", "NaN", "Infinity", True):
            with self.subTest(bad=bad):
                with self.assertRaises(MoneyError):
                    money.to_decimal(bad)
        with self.assertRaises(MoneyError):
            Money.of("1", "usd$")
        self.assertEqual(Money.of("1", "usd").currency, "USD")

    def test_currencies_never_mix(self):
        with self.assertRaises(CurrencyMismatch):
            Money.of("1", "USD") + Money.of("1", "EUR")
        with self.assertRaises(CurrencyMismatch):
            money.total([Money.of("1", "USD"), Money.of("1", "GBP")])
        by = money.sum_by_currency([Money.of("1", "USD"), Money.of("2", "EUR"), Money.of("3", "USD")])
        self.assertEqual({k: v.amount for k, v in by.items()}, {"USD": Decimal("4"), "EUR": Decimal("2")})

    def test_unavailable_propagates(self):
        self.assertIsNone(money.total([]))
        self.assertIsNone(money.total([Money.of("5", "USD"), None]))
        self.assertIsNone(money.ratio(Decimal("5"), Decimal("0")))
        self.assertIsNone(money.ratio(None, Decimal("2")))
        self.assertEqual(money.ratio(Decimal("5"), Decimal("2")), Decimal("2.5"))
        self.assertIsNone(money.convert(None, None, "USD"))

    def test_conversion_needs_documented_rate(self):
        eur = Money.of("100.00", "EUR")
        with self.assertRaises(CurrencyMismatch):
            money.convert(eur, None, "USD")
        with self.assertRaises(MoneyError):
            ExchangeRate(dt.date(2026, 10, 1), "EUR", "USD", "1.07", "")
        with self.assertRaises(MoneyError):
            ExchangeRate(dt.date(2026, 10, 1), "EUR", "USD", "-1", "ecb")
        r = ExchangeRate(dt.date(2026, 10, 1), "EUR", "USD", "1.0700", "ecb")
        c = money.convert(eur, r, "USD")
        self.assertEqual(c.money, Money.of("107.0000", "USD"))
        self.assertEqual(c.original, eur)
        self.assertEqual(c.rate.source, "ecb")
        back = money.convert(Money.of("107", "USD"), r, "EUR")   # inverse direction of the same rate
        self.assertEqual(back.money.amount.quantize(Decimal("0.01")), Decimal("100.00"))
        with self.assertRaises(CurrencyMismatch):
            money.convert(Money.of("1", "GBP"), r, "USD")

    def test_rounding(self):
        self.assertEqual(Money.of("2.0000005", "USD").stored(), Decimal("2.000000"))   # half-even
        self.assertEqual(Money.of("2.0000015", "USD").stored(), Decimal("2.000002"))
        self.assertEqual(Money.of("1234.505", "USD").display(), "USD 1,234.51")
        self.assertEqual(Money.of("1500.4", "JPY").display(), "JPY 1,500")


if __name__ == "__main__":
    unittest.main()
