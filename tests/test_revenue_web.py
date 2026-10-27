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


def call(method, path, body=None, cookie=None, csrf=True, origin=None, ip="127.0.0.1"):
    """Minimal ASGI client. Returns (status, json_or_text, set_cookie). Sends the app's same-site header like
    web/app.js does, unless csrf=False (a forged cross-site request)."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"test"), (b"content-type", b"application/json"), (b"x-forwarded-proto", b"https"),
               (b"x-forwarded-for", f"6.6.6.6, {ip}".encode())]
    if csrf:
        headers.append((b"x-adbot-request", b"1"))
    if origin:
        headers.append((b"origin", origin.encode()))
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
        # app race: present with no data yet, never zeros
        self.assertEqual((d["compare"]["series"], d["compare"]["extra_apps"]), ({}, []))
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


class ControlWebTests(unittest.TestCase):
    """The control endpoints behind sign-in: mode, stop latch, emergency pause, approvals, mapping, budget validation."""

    @classmethod
    def setUpClass(cls):
        status, _, sc = call("POST", "/api/login", {"password": "test-pass"})
        cls.cookie = sc.split(";")[0]

    def setUp(self):
        web._hits.clear()

    def test_requires_sign_in(self):
        for path in ("/api/control/mode", "/api/control/stop", "/api/control/emergency-pause", "/api/proposals/decide", "/api/campaigns/map"):
            with self.subTest(path=path):
                self.assertEqual(call("POST", path, {})[0], 401)
        self.assertEqual(call("GET", "/api/control")[0], 401)

    def test_overview_and_states(self):
        status, d, _ = call("GET", "/api/control", cookie=self.cookie)
        self.assertEqual(status, 200, d)
        self.assertTrue(d["dry_run"])
        states = {s["source"]: s["state"] for s in d["sources"]}
        self.assertEqual(states["TikTok Ads"], "unsupported")
        self.assertIn(states["Meta Ads"], ("not_configured", "connected_not_imported", "imported_zero", "imported_activity", "partial", "stale", "failed"))
        self.assertIsNone(d["kpis"]["revenue"])                 # nothing reported: unknown, not $0
        self.assertTrue(any("monthly budget" in e.lower() for e in d["budget_errors"]))
        self.assertFalse(d["rebalance"]["eligible"])

    def test_mode_and_stop_latch(self):
        self.assertEqual(call("POST", "/api/control/mode", {"mode": "nonsense"}, cookie=self.cookie)[0], 400)
        self.assertEqual(call("POST", "/api/control/mode", {"mode": "approval"}, cookie=self.cookie)[0], 403)   # needs the password again
        self.assertEqual(call("POST", "/api/control/mode", {"mode": "approval", "password": "test-pass"}, cookie=self.cookie)[0], 200)
        status, d, _ = call("POST", "/api/control/stop", {"reason": "test"}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertIn("keep running", d["note"])
        _, me, _ = call("GET", "/api/me", cookie=self.cookie)
        self.assertTrue(me["automation_stopped"])
        self.assertEqual(call("POST", "/api/control/resume", {}, cookie=self.cookie)[0], 403)            # needs the password again
        _, me, _ = call("GET", "/api/me", cookie=self.cookie)
        self.assertTrue(me["automation_stopped"])
        self.assertEqual(call("POST", "/api/control/resume", {"password": "test-pass"}, cookie=self.cookie)[0], 200)
        _, me, _ = call("GET", "/api/me", cookie=self.cookie)
        self.assertFalse(me["automation_stopped"])
        call("POST", "/api/control/mode", {"mode": "recommend"}, cookie=self.cookie)

    def test_emergency_needs_typed_confirmation(self):
        self.assertEqual(call("POST", "/api/control/emergency-pause", {}, cookie=self.cookie)[0], 400)
        status, d, _ = call("POST", "/api/control/emergency-pause", {"confirm": "PAUSE"}, cookie=self.cookie)
        self.assertEqual(status, 200, d)
        self.assertTrue(d["latch"]["active"])
        call("POST", "/api/control/emergency-clear", cookie=self.cookie)
        _, d, _ = call("GET", "/api/control", cookie=self.cookie)
        self.assertIsNone(d["emergency"])

    def test_decide_and_map_errors(self):
        self.assertEqual(call("POST", "/api/proposals/decide", {"proposal_id": "nope", "approve": True}, cookie=self.cookie)[0], 400)
        self.assertEqual(call("POST", "/api/proposals/decide", {"proposal_id": "0" * 32, "approve": "yes"}, cookie=self.cookie)[0], 400)
        self.assertEqual(call("POST", "/api/proposals/decide", {"proposal_id": "0" * 32, "approve": True}, cookie=self.cookie)[0], 409)
        self.assertEqual(call("POST", "/api/campaigns/map", {"channel": "Meta Ads", "campaign_id": "unseen", "app": "NurseBound"}, cookie=self.cookie)[0], 400)

    def test_budget_rules_validated_and_audited(self):
        bad = {"monthly_budget": 100, "app_monthly": {"Rapid Nursing Review": 90, "CCRN Adult": 20, "NurseBound": 0}}
        status, d, _ = call("POST", "/api/rules", bad, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertIn("more than", d["error"])
        self.assertEqual(call("POST", "/api/rules", {"auto_actions": ["delete_everything"]}, cookie=self.cookie)[0], 400)
        ok = {"monthly_budget": 1000, "app_monthly": {"Rapid Nursing Review": 450, "CCRN Adult": 250, "NurseBound": 300},
              "auto_actions": ["pause_keyword"], "reserve_pct": 0.1}
        self.assertEqual(call("POST", "/api/rules", ok, cookie=self.cookie)[0], 403)    # automatic actions need the password again
        status, d, _ = call("POST", "/api/rules", {**ok, "password": "test-pass"}, cookie=self.cookie)
        self.assertEqual(status, 200, d)
        self.assertIn("monthly_budget", d["changed"])
        _, r, _ = call("GET", "/api/rules", cookie=self.cookie)
        self.assertEqual(r["monthly_budget"], 1000)
        _, log, _ = call("GET", "/api/log", cookie=self.cookie)
        self.assertTrue(any(a["action"] == "rules.changed" for a in log["audit"]))
        status, d, _ = call("POST", "/api/rules", {"monthly_budget": None}, cookie=self.cookie)   # back to "not set"
        self.assertEqual(status, 200)

    def test_run_now_is_dry_and_locked(self):
        from control import state as cstate
        cstate.acquire(_CON, "hourly", "someone-else", ttl_minutes=5)
        self.assertEqual(call("POST", "/api/run-bot", cookie=self.cookie)[0], 409)
        cstate.release(_CON, "hourly", "someone-else")
        status, d, _ = call("POST", "/api/run-bot", cookie=self.cookie)
        self.assertEqual(status, 200, d)
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["executions"], {})          # a preview: the web service never executes anything


class ControlSecurityTests(unittest.TestCase):
    """Unauthorized and forged control requests, step-up, rate limits, demo credentials, shared storage."""

    @classmethod
    def setUpClass(cls):
        status, _, sc = call("POST", "/api/login", {"password": "test-pass"})
        cls.cookie = sc.split(";")[0]

    def setUp(self):
        web._hits.clear()

    def test_unauthorized_and_forged_sessions(self):
        for path in ("/api/control/resume", "/api/control/emergency-clear", "/api/run-bot", "/api/rules", "/api/override"):
            with self.subTest(path=path):
                self.assertEqual(call("POST", path, {"password": "test-pass"})[0], 401)
        forged = "adbot_session=ok.AAAAAA.forgedsignature"
        self.assertEqual(call("POST", "/api/control/stop", {}, cookie=forged)[0], 401)
        self.assertEqual(call("GET", "/api/control", cookie=forged)[0], 401)

    def test_csrf_header_and_origin(self):
        self.assertEqual(call("POST", "/api/control/stop", {}, cookie=self.cookie, csrf=False)[0], 403)
        self.assertEqual(call("POST", "/api/control/stop", {}, cookie=self.cookie, origin="https://evil.example")[0], 403)
        _, me, _ = call("GET", "/api/me", cookie=self.cookie)
        self.assertFalse(me["automation_stopped"])                 # the forged requests changed nothing
        self.assertEqual(call("POST", "/api/control/stop", {}, cookie=self.cookie, origin="https://test")[0], 200)
        self.assertEqual(call("POST", "/api/control/resume", {"password": "test-pass"}, cookie=self.cookie)[0], 200)

    def test_emergency_scope_is_never_widened(self):
        for body in ({"confirm": "PAUSE", "app": "Some Other App"}, {"confirm": "PAUSE", "channel": "TikTok Ads"},
                     {"confirm": "PAUSE", "channel": "Bing"}):
            with self.subTest(body=body):
                self.assertEqual(call("POST", "/api/control/emergency-pause", body, cookie=self.cookie)[0], 400)
        _, d, _ = call("GET", "/api/control", cookie=self.cookie)
        self.assertIsNone(d["emergency"])

    def test_rate_limit(self):
        codes = [call("POST", "/api/control/emergency-clear", {}, cookie=self.cookie, ip="10.9.9.9")[0] for _ in range(14)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)

    def test_login_limit_uses_proxy_hop_not_spoofable_first_entry(self):
        web._fails.clear()
        for i in range(10):
            call("POST", "/api/login", {"password": "wrong"}, ip="10.1.1.1")   # same real client, attacker rotates nothing useful
        self.assertEqual(call("POST", "/api/login", {"password": "test-pass"}, ip="10.1.1.1")[0], 429)
        web._fails.clear()

    def test_demo_password_refused_in_production(self):
        os.environ["RENDER"] = "true"
        try:
            self.assertEqual(call("POST", "/api/login", {"password": "test-pass"})[0], 503)
        finally:
            os.environ.pop("RENDER", None)

    def test_missing_shared_storage(self):
        with self.assertRaises(bot.SharedStorageMissing):
            bot.require_shared_storage({"RENDER": "true"})
        bot.require_shared_storage({"RENDER": "true", "MOTHERDUCK_TOKEN": "x"})      # shared: fine
        bot.require_shared_storage({})                                               # local development: fine
        orig = web.con

        def broken():
            raise bot.SharedStorageMissing("MOTHERDUCK_TOKEN is not set")
        web.con = broken
        try:
            self.assertEqual(call("POST", "/api/control/stop", {}, cookie=self.cookie)[0], 503)
        finally:
            web.con = orig

    def test_local_demo_server_refuses_production(self):
        import subprocess
        env = {**os.environ, "RENDER": "true"}
        p = subprocess.run([sys.executable, os.path.join(HERE, "local_server.py")], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refuses to run", p.stderr)


class AuthAuditTests(unittest.TestCase):
    """Session cookie flags, authentication on every API route, logout and expiry, header/origin checks."""

    def setUp(self):
        web._hits.clear()

    def test_cookie_flags_behind_https_proxy(self):
        status, _, sc = call("POST", "/api/login", {"password": "test-pass"})
        self.assertEqual(status, 200)
        attrs = [a.strip().lower() for a in sc.split(";")]
        self.assertTrue(attrs[0].startswith("adbot_session="))
        for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=2592000"):
            self.assertIn(flag, attrs)

    def test_every_api_route_requires_a_session_and_the_header_is_not_enough(self):
        from starlette.routing import Route
        open_routes = {"/api/login", "/api/logout"}
        checked = []
        for r in web.app.routes:
            if not isinstance(r, Route) or not r.path.startswith("/api/") or r.path in open_routes:
                continue
            for m in sorted(r.methods - {"HEAD"}):
                body = {"password": "test-pass", "confirm": "PAUSE", "mode": "approval"} if m == "POST" else None
                st = call(m, r.path, body, origin="https://test")[0]          # valid header + same origin, NO cookie
                checked.append((m, r.path, st))
        self.assertTrue(all(st == 401 for _, _, st in checked), [c for c in checked if c[2] != 401])
        self.assertEqual(len(checked), 22)      # every /api route + method except login/logout (update when routes change)

    def test_expired_and_tampered_sessions(self):
        import time as _t
        from unittest import mock
        old = _t.time() - (web.SESSION_DAYS * 86400 + 60)
        with mock.patch("time.time", return_value=old):
            stale = web._signer().sign(b"ok").decode()
        self.assertEqual(call("GET", "/api/me", cookie=f"adbot_session={stale}")[0], 401)
        good = call("POST", "/api/login", {"password": "test-pass"})[2].split(";")[0]
        self.assertEqual(call("GET", "/api/me", cookie=good)[0], 200)
        self.assertEqual(call("GET", "/api/me", cookie=good[:-2] + "xx")[0], 401)

    def test_logout_clears_cookie_but_token_is_not_revoked_server_side(self):
        """FINDING (documented, not fixed: scope frozen): logout only tells the browser to delete the cookie.
        A copy of the token keeps working until it expires (30 days) or APP_PASSWORD / SESSION_SECRET changes."""
        good = call("POST", "/api/login", {"password": "test-pass"})[2].split(";")[0]
        st, _, sc = call("POST", "/api/logout", {}, cookie=good)
        self.assertEqual(st, 200)
        self.assertIn("max-age=0", sc.lower())
        self.assertEqual(call("GET", "/api/me", cookie=good)[0], 200)      # replayed token still accepted

    def test_header_and_origin_rules(self):
        good = call("POST", "/api/login", {"password": "test-pass"})[2].split(";")[0]
        self.assertEqual(call("POST", "/api/control/emergency-clear", {}, cookie=good, csrf=False)[0], 403)
        self.assertEqual(call("POST", "/api/control/emergency-clear", {}, cookie=good, origin="https://test.evil.com")[0], 403)
        self.assertEqual(call("POST", "/api/control/emergency-clear", {}, cookie=good, origin="null")[0], 403)
        self.assertEqual(call("POST", "/api/control/emergency-clear", {}, cookie=good, origin="https://test")[0], 200)
        self.assertEqual(call("GET", "/api/control", cookie=good, csrf=False)[0], 200)   # reads need no header


if __name__ == "__main__":
    unittest.main()
