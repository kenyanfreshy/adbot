"""Store ID check tests: 'Check store IDs' against a fake App Store Connect and a fake Google Play.

Throwaway keys, synthetic answers, no network. Run:  python -m unittest discover -s tests -v
"""
import datetime as dt
import json
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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import revenue
from revenue import apple, google_play, registry, store_check
from revenue.schema import SCHEMA as S

EC = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
CREDS = apple.Creds("57246542-96fe-1a63-e053-0824d011072a", "2X9R4HXF34", EC, "85000001")
SA = {"type": "service_account", "client_email": "adbot@rnr.iam.gserviceaccount.com", "private_key": RSA,
      "token_uri": "https://oauth2.googleapis.com/token"}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers = status, body, {}
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeApple:
    """Apps: Apple ID -> (bundle ID, name, [(state, created)])."""

    def __init__(self, apps, forbid=False):
        self.apps, self.forbid, self.calls = apps, forbid, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if self.forbid:
            return Resp(403, {"errors": [{"code": "FORBIDDEN_ERROR", "detail": "This request is forbidden for security reasons"}]})
        path = url.replace(apple.API, "")
        parts = path.strip("/").split("/")
        app = self.apps.get(parts[2])
        if app is None:
            return Resp(404, {"errors": [{"code": "NOT_FOUND", "detail": "There is no resource of type 'apps'"}]})
        if len(parts) == 3:
            return Resp(200, {"data": {"type": "apps", "id": parts[2], "attributes": {"bundleId": app[0], "name": app[1], "sku": "X"}}})
        return Resp(200, {"data": [{"type": "appStoreVersions", "attributes": {"platform": "IOS", "appStoreState": s, "createdDate": c}}
                                   for s, c in app[2]]})


class FakePlay:
    def __init__(self, packages, denied=(), token_status=200):
        self.packages, self.denied, self.token_status, self.calls = set(packages), set(denied), token_status, []

    def post(self, url, data=None, timeout=None):
        if self.token_status != 200:
            return Resp(self.token_status, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        return Resp(200, {"access_token": "ya29.fake", "expires_in": 3599})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        pkg = url.split("/applications/")[1].split("/")[0]
        if pkg in self.denied:
            return Resp(403, {"error": {"code": 403, "message": "The caller does not have permission"}})
        if pkg not in self.packages:
            return Resp(404, {"error": {"code": 404, "message": f"Package not found: {pkg}."}})
        return Resp(200, {"subscriptions": []})


def fresh():
    con = duckdb.connect(":memory:")
    revenue.ensure(con)
    return con


def listing(con, app_id, platform):
    cur = con.execute(f"SELECT * FROM {S}.store_listings WHERE app_id = ? AND platform = ?", [app_id, platform])
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def nosleep(_):
    pass


class StoreCheckTests(unittest.TestCase):
    def setUp(self):
        self.con = fresh()
        registry.update_listing(self.con, "ccrn_adult", "apple_app_store", {"apple_app_id": "6823854842", "bundle_id": "com.alexkorir.ccrn"})
        registry.update_listing(self.con, "ccrn_adult", "google_play", {"package_name": "com.alexkorir.ccrn"})
        registry.update_listing(self.con, "rnr_pro", "apple_app_store", {"apple_app_id": "6470001234", "bundle_id": "com.alexkorir.rnr"})
        registry.update_listing(self.con, "rnr_pro", "google_play", {"package_name": "com.alexkorir.rapidnursingreview"})
        registry.update_listing(self.con, "nursebound_pro", "apple_app_store", {"apple_app_id": "6470009999"})  # bundle left empty

    def clients(self, fa, fp):
        return apple.Client(CREDS, session=fa, sleep=nosleep, max_retries=1), google_play.Client(SA, session=fp, sleep=nosleep, max_retries=1)

    def test_results_for_each_case(self):
        fa = FakeApple({"6823854842": ("com.alexkorir.ccrn", "CCRN Adult", [("PREPARE_FOR_SUBMISSION", "2026-10-20T10:00:00Z")]),
                        "6470001234": ("com.alexkorir.rapidnursing", "Rapid Nursing Review", [
                            ("READY_FOR_SALE", "2026-09-01T00:00:00Z"), ("WAITING_FOR_REVIEW", "2026-10-21T00:00:00Z")]),
                        "6470009999": ("com.alexkorir.nursebound", "NurseBound", [])})
        fp = FakePlay({"com.alexkorir.ccrn"})
        ac, pc = self.clients(fa, fp)
        out = store_check.run(self.con, environ={}, apple_client=ac, play_client=pc)
        self.assertEqual(out["apple_app_store"], {"verified": 2, "mismatch": 1})
        self.assertEqual(out["google_play"], {"verified": 1, "not_found": 1})

        ccrn = listing(self.con, "ccrn_adult", "apple_app_store")
        self.assertEqual((ccrn["check_result"], ccrn["publication_status"], ccrn["status_source"]), ("verified", "PREPARE_FOR_SUBMISSION", "store_api"))
        self.assertIsNotNone(ccrn["status_checked_at"])
        rnr = listing(self.con, "rnr_pro", "apple_app_store")
        self.assertEqual(rnr["check_result"], "mismatch")
        self.assertIn("com.alexkorir.rapidnursing, not com.alexkorir.rnr", rnr["check_detail"])
        self.assertEqual(rnr["bundle_id"], "com.alexkorir.rnr")           # a mismatch never overwrites what you typed
        self.assertEqual(rnr["publication_status"], "READY_FOR_SALE")      # the live version wins over a newer one in review
        nb = listing(self.con, "nursebound_pro", "apple_app_store")
        self.assertEqual((nb["check_result"], nb["bundle_id"]), ("verified", "com.alexkorir.nursebound"))  # empty bundle filled in
        self.assertEqual(nb["publication_status"], "NO_VERSION")
        self.assertEqual(listing(self.con, "ccrn_adult", "google_play")["publication_status"], "in_play_console")
        self.assertEqual(listing(self.con, "rnr_pro", "google_play")["check_result"], "not_found")
        # nursebound has no Play package: not asked about
        self.assertFalse(any("nursebound" in u for u in fp.calls))
        self.assertTrue(self.con.execute(f"SELECT count(*) FROM {S}.audit_logs WHERE action = 'store_check.result'").fetchone()[0] >= 5)

        # a second run the same hour has nothing due
        again = store_check.run(self.con, environ={}, apple_client=ac, play_client=pc)
        self.assertEqual(again, {})

    def test_only_get_requests(self):
        fa = FakeApple({"6823854842": ("com.alexkorir.ccrn", "CCRN Adult", [])})
        fp = FakePlay({"com.alexkorir.ccrn"})
        ac, pc = self.clients(fa, fp)
        store_check.run(self.con, environ={}, apple_client=ac, play_client=pc)
        self.assertTrue(fa.calls and fp.calls)   # FakeApple/FakePlay only implement GET (+ the OAuth token POST)

    def test_forbidden_and_refused(self):
        ac, _ = self.clients(FakeApple({}, forbid=True), FakePlay(()))
        _, pc = self.clients(FakeApple({}), FakePlay({"com.alexkorir.ccrn"}, denied={"com.alexkorir.ccrn"}))
        store_check.run(self.con, environ={}, apple_client=ac, play_client=pc)
        self.assertEqual(listing(self.con, "ccrn_adult", "apple_app_store")["check_result"], "no_access")
        self.assertEqual(listing(self.con, "ccrn_adult", "apple_app_store")["publication_status"], "unknown")
        self.assertEqual(listing(self.con, "ccrn_adult", "google_play")["check_result"], "no_access")
        _, bad = self.clients(FakeApple({}), FakePlay((), token_status=400))
        store_check.request(self.con)
        store_check.run(self.con, environ={}, apple_client=ac, play_client=bad)
        self.assertEqual(listing(self.con, "ccrn_adult", "google_play")["check_result"], "error")

    def test_no_keys_answers_requests_only(self):
        self.assertEqual(store_check.run(self.con, environ={}), {})               # nothing requested: stays quiet
        self.assertEqual(store_check.request(self.con), 5)
        out = store_check.run(self.con, environ={})
        self.assertEqual(out, {"apple_app_store": {"no_keys": 3}, "google_play": {"no_keys": 2}})
        l = listing(self.con, "ccrn_adult", "apple_app_store")
        self.assertEqual(l["check_result"], "no_keys")
        self.assertIsNone(l["check_requested_at"])
        self.assertIsNone(l["status_checked_at"])                                  # still due once the keys are added
        self.assertIn("ASC_ISSUER_ID", l["check_detail"])
        self.assertEqual(store_check.run(self.con, environ={}), {})               # and no hourly "no keys" noise

    def test_next_hourly_run(self):
        self.assertEqual(store_check.next_hourly_run(dt.datetime(2026, 10, 25, 14, 3)), dt.datetime(2026, 10, 25, 14, 7))
        self.assertEqual(store_check.next_hourly_run(dt.datetime(2026, 10, 25, 14, 7)), dt.datetime(2026, 10, 25, 15, 7))


if __name__ == "__main__":
    unittest.main()
