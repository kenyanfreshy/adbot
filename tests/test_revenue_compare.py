"""App comparison for the Revenue tab (revenue/compare.py): per-app daily downloads, paid units and USD proceeds,
with apps outside the registry grouped by store SKU and other currencies counted but never added."""
import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    import duckdb
except ImportError:
    sys.path.insert(0, HERE)
    import duckdb_cli_shim as duckdb  # type: ignore

from revenue import compare
from revenue.schema import SCHEMA as S, ensure_schema

TODAY = dt.date(2026, 10, 27)
COLS = ("line_key", "source_platform", "app_id", "source_app_ref", "currency", "period_start", "period_end", "data_status",
        "transaction_type", "units", "proceeds_amount", "proceeds_currency", "sku", "parent_ref", "imported_at",
        "reporting_timezone", "source_report_id")


class Compare(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        ensure_schema(self.con)
        self.n = 0

    def add(self, day, ttype, units, proceeds=None, ccy="USD", app_id=None, sku=None, parent=None, status="estimated"):
        self.n += 1
        d = TODAY - dt.timedelta(days=day)
        self.con.execute(f"INSERT INTO {S}.sales_reports ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                         [f"k{self.n}", "apple_app_store", app_id, "6774855397", ccy, d, d, status, ttype, units,
                          proceeds, ccy if proceeds is not None else None, sku, parent, dt.datetime(2026, 10, 27),
                          "America/Los_Angeles", "r1"])

    def test_registered_app_metrics_by_day(self):
        self.add(3, "download", 5, 0, app_id="rnr_pro", sku="rnr")
        self.add(3, "subscription", 2, 13.98, app_id="rnr_pro", parent="rnr")
        self.add(3, "iap", 1, 84.99, app_id="rnr_pro", parent="rnr")
        self.add(3, "restore", 4, 0, app_id="rnr_pro", parent="rnr")          # restores are not paid units
        self.add(3, "redownload", 9, 0, app_id="rnr_pro", sku="rnr")          # nor are re-downloads new downloads
        out = compare.compare(self.con, today=TODAY)
        day, downloads, paid, proceeds = out["series"]["rnr_pro"][0]
        self.assertEqual((day, downloads, paid), ("2026-10-24", 5, 3))
        self.assertAlmostEqual(proceeds, 98.97, places=2)
        self.assertTrue(out["estimated"])
        self.assertEqual(out["extra_apps"], [])

    def test_unmatched_app_grouped_by_sku_and_named(self):
        self.add(2, "download", 1, sku="vitalsync-health-tracker-00")
        self.add(2, "subscription", 1, 4.24, sku="vitalsync.healthtracker.premium_monthly", parent="vitalsync-health-tracker-00")
        self.add(5, "download", 3, sku="some-other-app")
        out = compare.compare(self.con, today=TODAY)
        self.assertEqual(out["series"]["vitalsync"], [["2026-10-25", 1, 1, 4.24]])   # app row + its IAP row, one app
        self.assertIn({"app_id": "vitalsync", "name": "VitalSync Health Tracker", "sku": "vitalsync-health-tracker-00"}, out["extra_apps"])
        self.assertIn("sku:some-other-app", out["series"])

    def test_other_currencies_counted_not_added(self):
        self.add(1, "subscription", 1, 4.24, app_id="ccrn_adult")
        self.add(1, "subscription", 1, 3.99, ccy="EUR", app_id="ccrn_adult")
        out = compare.compare(self.con, today=TODAY)
        self.assertAlmostEqual(out["series"]["ccrn_adult"][0][3], 4.24, places=2)
        self.assertEqual(out["other_currency_rows"], 1)

    def test_missing_metric_is_unavailable_not_zero(self):
        self.add(1, "download", 2, app_id="nursebound_pro")                    # no proceeds reported at all
        row = compare.compare(self.con, today=TODAY)["series"]["nursebound_pro"][0]
        self.assertEqual(row[1], 2)
        self.assertIsNone(row[2])                                               # no paid rows: unavailable
        self.assertIsNone(row[3])                                               # no proceeds: unavailable, not $0

    def test_window(self):
        self.add(compare.WINDOW_DAYS + 1, "download", 7, app_id="rnr_pro")     # older than the window: left out
        self.add(compare.WINDOW_DAYS, "download", 1, app_id="rnr_pro")
        out = compare.compare(self.con, today=TODAY)
        self.assertEqual([r[1] for r in out["series"]["rnr_pro"]], [1])
        self.assertEqual(compare.compare(self._empty(), today=TODAY)["series"], {})

    def _empty(self):
        c = duckdb.connect(":memory:")
        ensure_schema(c)
        return c


if __name__ == "__main__":
    unittest.main()
