"""Stage B web tests: the /api/revenue endpoints behind the phone app's sign-in.

Calls the ASGI app directly (no httpx needed). Synthetic data, no network.
Run:  python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["APP_PASSWORD"] = "test-pass"
os.environ.pop("SESSION_SECRET", None)

try:
    import duckdb
except ImportError:
    sys.path.insert(0, HERE)
    import duckdb_cli_shim as duckdb  # type: ignore

import dlt_pipeline as bot  # noqa: E402

_CON = duckdb.connect(":memory:")
bot.ensure_tables(_CON)          # the same start-up path Render uses (creates the revenue schema too)
bot.connect = lambda: _CON

import web  # noqa: E402


def call(method, path, body=None, cookie=None):
    """Minimal ASGI client. Returns (status, json_or_text, set_cookie)."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"test"), (b"content-type", b"application/json"), (b"x-forwarded-proto", b"https")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 5000), "server": ("test", 443), "root_path": ""}
    out = {"status": None, "headers": [], "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], msg["headers"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(web.app(scope, receive, send))
    set_cookie = next((v.decode() for k, v in out["headers"] if k == b"set-cookie"), None)
    try:
        data = json.loads(out["body"] or b"null")
    except ValueError:
        data = out["body"].decode("utf-8", "replace")
    return out["status"], data, set_cookie


class RevenueWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        status, _, sc = call("POST", "/api/login", {"password": "test-pass"})
        assert status == 200, status
        cls.cookie = sc.split(";")[0]

    def test_requires_sign_in(self):
        self.assertEqual(call("GET", "/api/revenue")[0], 401)
        self.assertEqual(call("POST", "/api/revenue/listing", {"app_id": "rnr_pro"})[0], 401)

    def test_revenue_payload(self):
        os.environ["META_ACCESS_TOKEN"] = "EAAG-secret-" + "z" * 40
        try:
            status, d, _ = call("GET", "/api/revenue", cookie=self.cookie)
        finally:
            os.environ.pop("META_ACCESS_TOKEN")
        self.assertEqual(status, 200, d)
        self.assertNotIn("EAAG-secret", json.dumps(d))
        self.assertFalse(d["has_revenue_data"])
        self.assertEqual({a["name"] for a in d["apps"]}, {"NurseBound Pro", "Rapid Nursing Review Pro", "CCRN Adult"})
        rnr = next(a for a in d["apps"] if a["app_id"] == "rnr_pro")
        lifetime = next(p for p in rnr["products"] if p["plan_code"] == "lifetime")
        self.assertIsInstance(lifetime["prices"][0]["amount"], str)          # money travels as text, not float
        self.assertEqual(lifetime["prices"][0]["amount"].rstrip("0").rstrip("."), "119")
        self.assertEqual(lifetime["product_type"], "unverified")
        meta = next(s for s in d["sources"] if s["platform"] == "meta_ads")
        self.assertIn("META_AD_ACCOUNT_IDS", meta["credentials"]["missing"])
        self.assertIsNone(meta["last_success"])

    def test_edit_identifiers(self):
        ok = call("POST", "/api/revenue/listing", {"app_id": "ccrn_adult", "platform": "google_play",
                                                   "fields": {"package_name": "com.example.ccrn"}}, cookie=self.cookie)
        self.assertEqual(ok[0], 200, ok[1])
        bad = call("POST", "/api/revenue/listing", {"app_id": "ccrn_adult", "platform": "apple_app_store",
                                                    "fields": {"apple_app_id": "not-a-number"}}, cookie=self.cookie)
        self.assertEqual(bad[0], 400)
        self.assertIn("Apple App ID", bad[1]["error"])
        _, d, _ = call("GET", "/api/revenue", cookie=self.cookie)
        ccrn = next(a for a in d["apps"] if a["app_id"] == "ccrn_adult")
        self.assertEqual(ccrn["listings"]["google_play"]["package_name"], "com.example.ccrn")

    def test_check_store_ids(self):
        self.assertEqual(call("POST", "/api/revenue/check-ids")[0], 401)
        for k in ("ASC_ISSUER_ID", "ASC_KEY_ID", "ASC_PRIVATE_KEY", "ASC_VENDOR_NUMBER", "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"):
            os.environ.pop(k, None)
        status, d, _ = call("GET", "/api/revenue", cookie=self.cookie)
        if not any(a["listings"]["apple_app_store"].get("apple_app_id") or a["listings"]["google_play"].get("package_name") for a in d["apps"]):
            status, d, _ = call("POST", "/api/revenue/check-ids", cookie=self.cookie)
            self.assertEqual(status, 400)
            self.assertIn("Edit IDs", d["error"])
        call("POST", "/api/revenue/listing", {"app_id": "ccrn_adult", "platform": "apple_app_store",
                                              "fields": {"apple_app_id": "6823854842", "bundle_id": "com.alexkorir.ccrn"}}, cookie=self.cookie)
        call("POST", "/api/revenue/listing", {"app_id": "ccrn_adult", "platform": "google_play",
                                              "fields": {"package_name": "com.alexkorir.ccrn"}}, cookie=self.cookie)
        status, d, _ = call("POST", "/api/revenue/check-ids", cookie=self.cookie)
        self.assertEqual(status, 200, d)
        # the keys live on adbot-hourly, so both stores are left for the next hourly run
        self.assertEqual(set(d["queued"]), {"apple_app_store", "google_play"})
        self.assertEqual(d["checked"], {})
        self.assertTrue(d["next_run"].endswith(":07:00"))
        _, d, _ = call("GET", "/api/revenue", cookie=self.cookie)
        ccrn = next(a for a in d["apps"] if a["app_id"] == "ccrn_adult")
        self.assertIsNotNone(ccrn["listings"]["apple_app_store"]["check_requested_at"])
        self.assertIsNotNone(ccrn["listings"]["google_play"]["check_requested_at"])

    def test_retry_play_reports(self):
        self.assertEqual(call("POST", "/api/revenue/retry-play-reports")[0], 401)
        status, d, _ = call("POST", "/api/revenue/retry-play-reports", cookie=self.cookie)
        self.assertEqual(status, 200, d)
        _, d, _ = call("GET", "/api/revenue", cookie=self.cookie)
        play = next(x for x in d["sources"] if x["platform"] == "google_play")
        self.assertIsNotNone(play["reports"]["retry_requested_at"])
        self.assertEqual(play["reports"]["state"], "never_checked")
        self.assertIsNone(play["last_success"])

    def test_existing_screens_still_work(self):
        for path in ("/api/me", "/api/rules", "/api/log"):
            with self.subTest(path=path):
                self.assertEqual(call("GET", path, cookie=self.cookie)[0], 200)
        self.assertEqual(call("GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
