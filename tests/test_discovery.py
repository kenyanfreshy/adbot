"""Campaign discovery, campaign-name matching, the Keywords tab's empty states and Google Play header variants.

  * Apple Ads: campaigns, ad groups and targeting keywords are listed from the Campaign Management API, so ones
    with no activity are still recorded (control.campaign_map, control.ad_entities) and shown with zero metrics.
  * Campaign names with en dashes, hyphens, pipes and underscores map to the right app; NurseBound "Intl"
    campaigns take their country list from the campaign's own countries.
  * The Keywords tab says the right thing when a channel is connected but nothing is mapped.
  * The Play estimated sales report is parsed by its real header ("Package ID", "Base Plan or Purchase Option ID"),
    and a report kept unparsed earlier is parsed from its archived copy by `retry-reports`.

Fake platforms only: no network, no credentials. The first group of classes needs no database; the rest use DuckDB
(or tests/duckdb_cli_shim.py with DUCKDB_CLI set), like the other test files.
Run:  python -m unittest discover -s tests -v
"""
import base64
import datetime as dt
import io
import json
import os
import sys
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("APP_PASSWORD", "test-pass")

import dlt_pipeline as bot  # noqa: E402
from control import ingest  # noqa: E402
from revenue import google_play as gp  # noqa: E402

NOW = dt.datetime(2026, 11, 20, 6, 0)
TODAY = NOW.date()
ORG = "22866090"

# The org as described on 2026-11-19 (ids and names real, everything else made up).
CAMPAIGNS = [
    {"id": 2145125761, "name": "RNR – US – Search Results", "status": "ENABLED", "displayStatus": "RUNNING",
     "countriesOrRegions": ["US"], "supplySources": ["APPSTORE_SEARCH_RESULTS"], "deleted": False},
    {"id": 2145126611, "name": "NurseBound – Intl – Search Results", "status": "ENABLED", "displayStatus": "RUNNING",
     "countriesOrRegions": ["PH", "IN", "NG", "KE", "GH", "ZA", "GB", "CA", "AU", "NZ"],
     "supplySources": ["APPSTORE_SEARCH_RESULTS"], "deleted": False},
    {"id": 2145127351, "name": "CCRN – US – Search Results", "status": "ENABLED", "displayStatus": "RUNNING",
     "countriesOrRegions": ["US"], "supplySources": ["APPSTORE_SEARCH_RESULTS"], "deleted": False},
    {"id": 2144252473, "name": "VitalSync_SearchTab_Awareness_US", "status": "PAUSED", "displayStatus": "ON_HOLD",
     "countriesOrRegions": ["US"], "supplySources": ["APPSTORE_SEARCH_TAB"], "deleted": False},
]
ADGROUPS = {2145125761: [{"id": 11, "name": "Exact", "status": "ENABLED", "displayStatus": "RUNNING"}],
            2145126611: [{"id": 21, "name": "Exact", "status": "ENABLED", "displayStatus": "RUNNING"},
                         {"id": 22, "name": "Old", "status": "PAUSED", "deleted": True}],
            2145127351: [{"id": 31, "name": "Exact", "status": "ENABLED", "displayStatus": "RUNNING"}],
            2144252473: [{"id": 41, "name": "Tab", "status": "ENABLED"}]}
KEYWORDS = {11: [{"id": 111, "text": "nclex rn", "status": "ACTIVE", "matchType": "EXACT"},
                 {"id": 112, "text": "nclex practice questions", "status": "ACTIVE", "matchType": "EXACT"}],
            21: [{"id": 211, "text": "nurse jobs abroad", "status": "ACTIVE", "matchType": "BROAD"},
                 {"id": 212, "text": "old keyword", "status": "PAUSED", "matchType": "EXACT", "deleted": True}],
            31: [{"id": 311, "text": "ccrn exam prep", "status": "ACTIVE", "matchType": "EXACT"}]}


class FakeApple:
    """The Campaign Management API v5, as far as the importer uses it. Records every call."""

    def __init__(self, campaigns=CAMPAIGNS, activity=None, fail_adgroups=()):
        self.campaigns, self.activity, self.fail_adgroups = campaigns, activity or {}, set(fail_adgroups)
        self.calls = []

    @staticmethod
    def page(items, params):
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 1000))
        return {"data": items[off:off + lim], "pagination": {"totalResults": len(items), "startIndex": off, "itemsPerPage": lim}}

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert kw.get("headers", {}).get("X-AP-Context") == f"orgId={ORG}", kw.get("headers")
        path = url.split("/api/v5", 1)[1]
        parts = path.strip("/").split("/")
        params = kw.get("params") or {}
        if method == "GET" and parts == ["campaigns"]:
            return Resp(self.page(self.campaigns, params))
        if method == "GET" and len(parts) == 3 and parts[2] == "adgroups":
            cid = int(parts[1])
            if cid in self.fail_adgroups:
                raise RuntimeError(f"GET {url} -> 500: boom")
            return Resp(self.page(ADGROUPS.get(cid, []), params))
        if method == "GET" and len(parts) == 5 and parts[4] == "targetingkeywords":
            return Resp(self.page(KEYWORDS.get(int(parts[3]), []), params))
        if method == "POST" and path.startswith("/reports/campaigns"):
            body = kw["json"]
            assert body["returnRecordsWithNoMetrics"] is True
            if path.endswith("/keywords"):
                cid = int(parts[2])
                rows = []
                for g in ADGROUPS.get(cid, []):
                    for k in KEYWORDS.get(g["id"], []):
                        days = self.activity.get(k["id"], {})
                        gran = [{"date": d, "impressions": t * 10, "taps": t, "totalInstalls": 0,
                                 "localSpend": {"amount": f"{t * 0.5:.2f}", "currency": "USD"}} for d, t in days.items()] or \
                               [{"date": TODAY.isoformat(), "impressions": 0, "taps": 0, "totalInstalls": 0,
                                 "localSpend": {"amount": "0", "currency": "USD"}}]          # a record with no metrics
                        rows.append({"metadata": {"keywordId": k["id"], "keyword": k["text"], "adGroupId": g["id"],
                                                  "keywordStatus": k["status"]}, "granularity": gran})
                return Resp({"data": {"reportingDataResponse": {"row": rows}}})
            return Resp({"data": {"reportingDataResponse": {"row": [
                {"metadata": {"campaignId": c["id"]}, "granularity": [{"date": TODAY.isoformat(), "impressions": 0, "taps": 0,
                                                                       "localSpend": {"amount": "0", "currency": "USD"}}]}
                for c in self.campaigns]}}})
        raise AssertionError(f"unexpected call {method} {url}")


class Resp:
    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


def apple(fake, page=1000):
    env = {"ASA_CLIENT_ID": "c", "ASA_TEAM_ID": "t", "ASA_KEY_ID": "k", "ASA_ORG_ID": ORG, "ASA_PRIVATE_KEY": "x"}
    with mock.patch.dict(os.environ, env):
        a = bot.AppleSearchAds()
    a.PAGE = page
    a._token, a._exp = "fake-token", 10 ** 12          # no sign-in round trip in tests
    return a


def import_apple(fake, rules=None, page=1000):
    """Run the Apple job the way run_sync does; returns (rows, record, campaigns, entities)."""
    rules = rules or bot.Rules()
    a = apple(fake, page)
    recs, camps, ents = [], {}, {}
    with mock.patch.object(bot, "http", fake):
        rows = list(ingest.collect(a.jobs(TODAY - dt.timedelta(days=13), TODAY, rules), TODAY - dt.timedelta(days=13), TODAY,
                                   recs, camps, clock=lambda: NOW, entities=ents))
    return rows, recs[0], camps, ents


# --------------------------------------------------------------------------- #
# No database needed
# --------------------------------------------------------------------------- #
class NameMatchingTests(unittest.TestCase):
    def test_dashes_pipes_and_underscores(self):
        r = bot.Rules()
        cases = {
            "RNR – US – Search Results": "Rapid Nursing Review",       # en dash (Apple Ads' own names)
            "RNR - US - Search Results": "Rapid Nursing Review",       # hyphen
            "RNR | US | Search Results": "Rapid Nursing Review",       # pipe
            "RNR|US|Search": "Rapid Nursing Review",
            "RNR—US": "Rapid Nursing Review",                           # em dash
            "ＲＮＲ – US": "Rapid Nursing Review",                       # full-width letters
            "CCRN – US – Search Results": "CCRN Adult",
            "CCC – US – Search Results": "CCRN Adult",                  # the name before today's rename
            "NurseBound – Intl – Search Results": "NurseBound",
            "NurseBound | PH | Prospecting": "NurseBound",
            "VitalSync_SearchTab_Awareness_US": None,                   # another product
            "Brand – Search Results": None,
        }
        for name, app in cases.items():
            self.assertEqual(bot.app_for(name, r), app, name)

    def test_country_from_name_or_campaign(self):
        r = bot.Rules()
        intl = CAMPAIGNS[1]["countriesOrRegions"]
        self.assertEqual(bot.country_for("NurseBound – PH – Search", "NurseBound", r), "PH")
        self.assertEqual(bot.country_for("NurseBound—PH", "NurseBound", r), "PH")
        self.assertEqual(bot.country_for("NurseBound PH", "NurseBound", r), "PH")   # non-breaking space
        self.assertIsNone(bot.country_for("NurseBound – Intl – Search Results", "NurseBound", r, intl))   # many countries: no single one
        self.assertEqual(bot.countries_for("NurseBound – Intl – Search Results", "NurseBound", r, intl), intl)
        self.assertEqual(bot.country_for("NurseBound – Search", "NurseBound", r, ["ph"]), "PH")          # one targeted country
        self.assertEqual(bot.countries_for("NurseBound | IN | x", "NurseBound", r, intl), ["IN"])         # the name wins
        self.assertIsNone(bot.country_for("RNR – US", "Rapid Nursing Review", r, ["US"]))                 # no geo split for RNR


class AppleDiscoveryTests(unittest.TestCase):
    def test_idle_campaigns_and_keywords_are_discovered(self):
        rows, rec, camps, ents = import_apple(FakeApple())
        self.assertEqual(rows, [])                                    # nothing served: no performance rows loaded
        self.assertEqual(rec["status"], "succeeded")
        self.assertEqual(set(camps), {("Apple Search Ads", str(c["id"])) for c in CAMPAIGNS})      # all four, mapped or not
        self.assertEqual({k[1]: v["rule_app"] for k, v in camps.items()},
                         {"2145125761": "Rapid Nursing Review", "2145126611": "NurseBound", "2145127351": "CCRN Adult",
                          "2144252473": None})
        kw = {e["name"]: e for (_, t, _), e in ents.items() if t == "keyword"}
        self.assertEqual(set(kw), {"nclex rn", "nclex practice questions", "nurse jobs abroad", "ccrn exam prep"})   # deleted ones left out
        self.assertEqual(kw["nurse jobs abroad"]["countries"], ",".join(CAMPAIGNS[1]["countriesOrRegions"]))
        self.assertEqual(kw["ccrn exam prep"]["campaign_name"], "CCRN – US – Search Results")
        self.assertEqual(ents[("Apple Search Ads", "campaign", "campaign:2144252473")]["status"], "ON_HOLD")
        self.assertFalse([k for k in ents if k[1] == "adgroup" and ents[k]["campaign_id"] == "2144252473"])  # other product: not explored
        self.assertEqual(rec["listed"], {"campaign": 4, "adgroup": 3, "keyword": 4})
        self.assertEqual(ingest.quality(rec), "no_activity")
        self.assertIn("no activity", rec["message"])

    def test_reports_ask_for_records_with_no_metrics_and_load_only_activity(self):
        fake = FakeApple(activity={111: {TODAY.isoformat(): 4, (TODAY - dt.timedelta(days=1)).isoformat(): 2}})
        rows, rec, _, ents = import_apple(fake)
        self.assertEqual([(r["entity_id"], r["taps"]) for r in sorted(rows, key=lambda r: r["date"])], [("111", 2), ("111", 4)])
        self.assertEqual({r["app"] for r in rows}, {"Rapid Nursing Review"})
        self.assertEqual((rec["rows"], rec["activity_rows"]), (2, 2))
        self.assertEqual(ingest.quality(rec), "ok")
        bodies = [kw["json"] for m, u, kw in fake.calls if m == "POST"]
        self.assertTrue(bodies and all(b["returnRecordsWithNoMetrics"] is True for b in bodies))
        self.assertIn(("Apple Search Ads", "keyword", "112"), ents)       # its idle neighbour is still listed

    def test_campaign_list_is_paginated(self):
        fake = FakeApple()
        _, _, camps, _ = import_apple(fake, page=3)
        self.assertEqual(len(camps), 4)
        offsets = [kw["params"]["offset"] for m, u, kw in fake.calls if m == "GET" and u.endswith("/campaigns")]
        self.assertEqual(offsets, [0, 3])

    def test_a_listing_error_is_a_warning_not_a_failed_import(self):
        _, rec, camps, ents = import_apple(FakeApple(fail_adgroups={2145127351}))
        self.assertEqual(rec["status"], "succeeded")
        self.assertIn("ad groups of campaign 2145127351 not listed", rec["message"])
        self.assertIn(("Apple Search Ads", "2145127351"), camps)          # the campaign itself is still recorded
        self.assertNotIn(("Apple Search Ads", "keyword", "311"), ents)

    def test_manual_mapping_lists_children_of_a_campaign_the_names_dont_match(self):
        r = bot.Rules()
        r._cmap = {("Apple Search Ads", "2144252473"): "Rapid Nursing Review"}
        _, _, camps, ents = import_apple(FakeApple(), rules=r)
        self.assertIsNone(camps[("Apple Search Ads", "2144252473")]["rule_app"])   # the name rule still says nothing
        self.assertIn(("Apple Search Ads", "adgroup", "41"), ents)


class ZeroMetricItemTests(unittest.TestCase):
    def ents(self):
        _, _, _, ents = import_apple(FakeApple())
        return [{"channel": k[0], "entity_type": k[1], "entity_id": k[2], **v} for k, v in ents.items()]

    def test_listed_keywords_shown_with_zeros(self):
        items = ingest.zero_metric_items(self.ents(), set(), {}, lambda n: bot.app_for(n, bot.Rules()), bot.APPS)
        self.assertEqual(sorted((i["app"], i["keyword"]) for i in items), [
            ("CCRN Adult", "ccrn exam prep"), ("NurseBound", "nurse jobs abroad"),
            ("Rapid Nursing Review", "nclex practice questions"), ("Rapid Nursing Review", "nclex rn")])
        for i in items:
            self.assertEqual((i["taps"], i["installs"], i["spend"], i["revenue"], i["no_metrics"]), (0.0, 0.0, 0.0, None, True))

    def test_rows_with_metrics_win_and_campaigns_without_keywords_appear(self):
        ents = [e for e in self.ents() if e["campaign_id"] != "2145127351" or e["entity_type"] == "campaign"]   # CCRN: no keywords listed
        items = ingest.zero_metric_items(ents, {"Apple Search Ads|111"}, {}, lambda n: bot.app_for(n, bot.Rules()), bot.APPS)
        keys = {f"{i['channel']}|{i['entity_id']}" for i in items}
        self.assertNotIn("Apple Search Ads|111", keys)                       # has real rows: not duplicated
        self.assertIn("Apple Search Ads|campaign:2145127351", keys)          # campaign with no keywords: shown itself
        self.assertNotIn("Apple Search Ads|campaign:2145125761", keys)       # its keywords stand for it

    def test_ignored_and_unmapped_campaigns_left_out(self):
        cmap = {("Apple Search Ads", "2145125761"): ingest.IGNORED}
        items = ingest.zero_metric_items(self.ents(), set(), cmap, lambda n: bot.app_for(n, bot.Rules()), bot.APPS)
        self.assertFalse([i for i in items if i["campaign_id"] in ("2145125761", "2144252473")])
        cmap = {("Apple Search Ads", "2144252473"): "CCRN Adult"}               # mapped by hand: shown under that app
        items = ingest.zero_metric_items(self.ents(), set(), cmap, lambda n: bot.app_for(n, bot.Rules()), bot.APPS)
        self.assertIn(("CCRN Adult", "campaign:2144252473"), {(i["app"], i["entity_id"]) for i in items})


# A Play estimated sales report with the real header of salesreport_202611.csv (Nov 2026) and made-up values.
REAL_SALES_HEADER = ("Order Number,Order Charged Date,Order Charged Timestamp,Financial Status,Device Model,Product Title,Package ID,"
                     "Product Type,SKU ID,Currency of Sale,Item Price,Taxes Collected,Charged Amount,City of Buyer,State of Buyer,"
                     "Postal Code of Buyer,Country of Buyer,Base Plan or Purchase Option ID,Offer ID,Group ID,First USD 1M Eligible,"
                     "Promotion ID,Coupon Value,Discount Rate,Featured Product ID,Price Experiment ID,Sales Channel")
PKG = "com.alexkorir.rapidnursingreview"
REAL_SALES_CSV = "﻿" + "\n".join([
    REAL_SALES_HEADER,
    f"GPA.0000-1111-2222-33333,2026-11-05,1793900000,Charged,,Made-up Title,{PKG},Subscription,rnr_pro,USD,9.99,0.00,9.99,"
    "Springfield,XX,00000,US,monthly,intro-offer,,,,,,,,",
    f"GPA.0000-1111-2222-44444..0,2026-11-06,1793990000,Charged,,Made-up Title,{PKG},Subscription,rnr_pro,USD,9.99,0.00,9.99,"
    ",,,US,monthly,,,,,,,,,",
    "GPA.0000-1111-2222-55555,2026-11-07,1794080000,Refund,,Other,com.someone.else,In-app,x,EUR,5.00,0.00,5.00,,,,DE,,,,,,,,,,"])


def zipped(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


class Matcher:
    by_pkg = {PKG: "rnr_pro"}

    def __call__(self, pkg):
        return self.by_pkg.get((pkg or "").strip())


class PlayHeaderTests(unittest.TestCase):
    def test_real_header_is_parsed(self):
        text = gp.unzip_csv(zipped("salesreport_202611.csv", REAL_SALES_CSV))
        self.assertEqual(gp.header_of(text)[6], "Package ID")
        lines = gp.parse_sales(text, "rid", Matcher(), NOW)
        self.assertEqual([(l["app_id"], l["source_app_ref"], l["sub_period"], l["transaction_type"], l["subscription_kind"])
                          for l in lines], [
            ("rnr_pro", PKG, "monthly", "subscription", "New"),
            ("rnr_pro", PKG, "monthly", "subscription", "Renewal"),
            (None, "com.someone.else", None, "refund", None)])

    def test_older_header_and_spacing_variants_still_parse(self):
        old = "Order Number,Order Charged Date,Financial Status,Product ID,Product Type,SKU ID,Currency of Sale,Item Price,Charged Amount,Base Plan ID\n" \
              f"GPA.1,2026-10-05,Charged,{PKG},subscription,rnr_pro,USD,9.99,9.99,monthly"
        self.assertEqual(gp.parse_sales(old, "r", Matcher(), NOW)[0]["app_id"], "rnr_pro")
        spaced = old.replace("Product ID", '  product   id ').replace("Currency of Sale", "Currency Of Sale")
        self.assertEqual(gp.parse_sales(spaced, "r", Matcher(), NOW)[0]["app_id"], "rnr_pro")

    def test_missing_column_message_names_what_was_looked_for(self):
        with self.assertRaises(gp.PlayFormatError) as cm:
            gp.parse_sales(REAL_SALES_CSV.replace("Package ID", "Bundle"), "r", Matcher(), NOW)
        self.assertIn("pkg ('package id' / 'product id' / 'package name')", str(cm.exception))

    def test_earnings_accepts_package_id(self):
        earn = ("Description,Transaction Date,Transaction Type,Package ID,Sku Id,Buyer Country,Merchant Currency,Amount (Merchant Currency)\n"
                f"GPA.1,2026-11-05,Charge,{PKG},rnr_pro,US,USD,9.99")
        self.assertEqual(gp.parse_earnings(earn, "r", "2026-11", Matcher(), NOW)[0]["app_id"], "rnr_pro")


class EmptyStateTests(unittest.TestCase):
    def test_messages(self):
        try:
            import web
        except ImportError as e:   # pragma: no cover - web dependencies missing
            self.skipTest(f"web app dependencies missing: {e}")
        self.assertIn("once an ad channel is connected", web.keywords_empty_state([], 0, False))
        self.assertEqual(web.keywords_empty_state(["Apple Search Ads"], 0, False),
                         "Channels connected — no campaigns found for your apps yet.")
        self.assertIn("after the next import", web.keywords_empty_state(["Apple Search Ads"], 3, False))
        self.assertIsNone(web.keywords_empty_state(["Apple Search Ads"], 3, True))


# --------------------------------------------------------------------------- #
# With a database
# --------------------------------------------------------------------------- #
def db():
    try:
        import duckdb
    except ImportError:
        sys.path.insert(0, HERE)
        import duckdb_cli_shim as duckdb  # type: ignore
    return duckdb


def fresh():
    import control
    import revenue
    con = db().connect(":memory:")
    revenue.ensure(con)
    control.ensure(con)
    return con


class DiscoveryStorageTests(unittest.TestCase):
    def test_one_run_records_every_campaign_and_keyword(self):
        con = fresh()
        rows, rec, camps, ents = import_apple(FakeApple())
        ingest.record(con, [rec], camps, {"Apple Search Ads": True}, clock=lambda: NOW, entities=ents)
        names = {r[0]: r[1] for r in con.execute("SELECT campaign_name, rule_app FROM control.campaign_map").fetchall()}
        self.assertEqual(names, {"RNR – US – Search Results": "Rapid Nursing Review", "NurseBound – Intl – Search Results": "NurseBound",
                                 "CCRN – US – Search Results": "CCRN Adult", "VitalSync_SearchTab_Awareness_US": None})
        self.assertEqual([u["campaign_name"] for u in ingest.unmapped(con)], ["VitalSync_SearchTab_Awareness_US"])   # still for review
        self.assertEqual(len(ingest.mapped_campaigns(con, bot.APPS)), 3)
        self.assertEqual(con.execute("SELECT rows, quality FROM control.import_runs").fetchone(), (0, "no_activity"))
        listed = ingest.listed_entities(con, clock=lambda: NOW)
        self.assertEqual(len([e for e in listed if e["entity_type"] == "keyword"]), 4)
        self.assertEqual(ingest.connected_channels(con), ["Apple Search Ads"])
        # a second run updates, never duplicates; a rename is picked up
        renamed = [dict(c) for c in CAMPAIGNS]
        renamed[2]["name"] = "CCRN – US – Search Results v2"
        _, rec2, camps2, ents2 = import_apple(FakeApple(campaigns=renamed))
        ingest.record(con, [rec2], camps2, {"Apple Search Ads": True}, clock=lambda: NOW + dt.timedelta(hours=4), entities=ents2)
        self.assertEqual(con.execute("SELECT count(*) FROM control.campaign_map").fetchone()[0], 4)
        self.assertEqual(con.execute("SELECT count(*) FROM control.ad_entities WHERE entity_type = 'keyword'").fetchone()[0], 4)
        self.assertEqual(con.execute("SELECT campaign_name FROM control.ad_entities WHERE entity_id = '311'").fetchone()[0],
                         "CCRN – US – Search Results v2")


def call(web, method, path, body=None, cookie=None, query=""):
    """Minimal ASGI client (as in test_revenue_web.py). Returns (status, json, set_cookie)."""
    import asyncio
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"test"), (b"content-type", b"application/json"), (b"x-forwarded-proto", b"https"),
               (b"x-adbot-request", b"1")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": headers,
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
    return out["status"], json.loads(out["body"] or b"null"), set_cookie


class KeywordsTabTests(unittest.TestCase):
    """GET /api/keywords against a real database: zero-metric campaigns and keywords, and the empty states."""

    @classmethod
    def setUpClass(cls):
        import web
        cls.web = web

    def setUp(self):
        self.con = fresh()
        bot.ensure_tables(self.con)
        self.con.execute("CREATE SCHEMA IF NOT EXISTS ads")
        self.con.execute("CREATE TABLE IF NOT EXISTS ads.ad_performance (date VARCHAR, channel VARCHAR, entity_id VARCHAR, entity_type VARCHAR, "
                         "keyword VARCHAR, campaign_id VARCHAR, campaign VARCHAR, adgroup_id VARCHAR, app VARCHAR, status VARCHAR, "
                         "impressions DOUBLE, taps DOUBLE, installs DOUBLE, spend DOUBLE, revenue DOUBLE, currency VARCHAR, "
                         "account_id VARCHAR, import_id VARCHAR)")
        self._old = self.web._con
        self.web._con = self.con

    def tearDown(self):
        self.web._con = self._old

    def get(self, qs=""):
        status, data, sc = call(self.web, "POST", "/api/login", {"password": os.environ["APP_PASSWORD"]})
        self.assertEqual(status, 200)
        status, data, _ = call(self.web, "GET", "/api/keywords", cookie=sc.split(";")[0], query=qs)
        self.assertEqual(status, 200, data)
        return data

    def test_no_channel_connected(self):
        d = self.get()
        self.assertEqual(d["items"], [])
        self.assertIn("once an ad channel is connected", d["empty"])

    def test_connected_but_no_campaigns_for_the_apps(self):
        only_other = [CAMPAIGNS[3]]
        _, rec, camps, ents = import_apple(FakeApple(campaigns=only_other))
        ingest.record(self.con, [rec], camps, {"Apple Search Ads": True}, entities=ents)
        d = self.get()
        self.assertEqual(d["items"], [])
        self.assertEqual(d["empty"], "Channels connected — no campaigns found for your apps yet.")
        self.assertEqual(d["channels_connected"], ["Apple Search Ads"])

    def test_after_one_run_all_three_app_campaigns_and_keywords_show_with_zeros(self):
        _, rec, camps, ents = import_apple(FakeApple())
        ingest.record(self.con, [rec], camps, {"Apple Search Ads": True}, entities=ents)
        d = self.get()
        self.assertIsNone(d["empty"])
        got = {(i["app"], i["keyword"], i["campaign"]) for i in d["items"]}
        self.assertEqual(got, {
            ("Rapid Nursing Review", "nclex rn", "RNR – US – Search Results"),
            ("Rapid Nursing Review", "nclex practice questions", "RNR – US – Search Results"),
            ("NurseBound", "nurse jobs abroad", "NurseBound – Intl – Search Results"),
            ("CCRN Adult", "ccrn exam prep", "CCRN – US – Search Results")})
        for i in d["items"]:
            self.assertEqual((i["taps"], i["installs"], i["spend"]), (0, 0, 0))
            self.assertTrue(i["no_metrics"])
        nb = next(i for i in d["items"] if i["app"] == "NurseBound")
        self.assertEqual(nb["countries"], CAMPAIGNS[1]["countriesOrRegions"])
        self.assertIsNone(nb["country"])
        self.assertEqual({i["app"] for i in self.get("app=NurseBound")["items"]}, {"NurseBound"})
        self.assertEqual([u["campaign_name"] for u in ingest.unmapped(self.con)], ["VitalSync_SearchTab_Awareness_US"])


class PlayRetryTests(unittest.TestCase):
    """A sales report kept unparsed by the old parser is parsed from its archived copy by retry-reports."""

    def test_kept_report_is_parsed_on_retry(self):
        sys.path.insert(0, HERE)
        import test_revenue_google_play as t
        con = t.setup_con()
        name = "sales/salesreport_202611.zip"
        data = zipped("salesreport_202611.csv", REAL_SALES_CSV)
        # what the old parser left behind: two generations kept, 0 rows, "missing columns: pkg"
        for gen, when in (("1795074301878761", NOW - dt.timedelta(days=1)), ("1795140238983927", NOW)):
            con.execute(f"INSERT INTO revenue.report_files VALUES (?, 'google_play', 'ESTIMATED_SALES', NULL, 'MONTHLY', NULL, "
                        "DATE '2026-11-01', DATE '2026-11-30', 'UTC', 'estimated', 'x', 0, ?, NULL)", [f"google:{name}:{gen}", when])
            con.execute("INSERT INTO revenue.report_blobs VALUES (?, 'base64', ?, ?)",
                        [f"google:{name}:{gen}", base64.b64encode(data).decode(), when])
        fake = t.FakeGoogle(files={name: ("1795140238983927", data)})        # the bucket still holds the newest generation
        res = t.run(con, fake, force=False, retry_reports=True, reports_only=True)
        rep = res["reports"]
        self.assertEqual((rep["unparsed"], rep["reparsed"], rep["inserted"], rep["unmatched"]), (0, 1, 3, 1))
        self.assertFalse([u for u, p in fake.calls if p.get("alt") == "media"])           # nothing downloaded again
        files = dict(con.execute("SELECT report_id, row_count FROM revenue.report_files").fetchall())
        self.assertEqual(files[f"google:{name}:1795140238983927"], 3)
        self.assertEqual(con.execute("SELECT superseded_by FROM revenue.report_files WHERE report_id = ?",
                                     [f"google:{name}:1795074301878761"]).fetchone()[0], f"google:{name}:1795140238983927")
        self.assertEqual(con.execute("SELECT count(*) FROM revenue.sales_reports WHERE app_id = 'rnr_pro'").fetchone()[0], 2)
        # a second retry finds nothing left to do
        rep2 = t.run(con, t.FakeGoogle(files={name: ("1795140238983927", data)}), force=False, retry_reports=True, reports_only=True)["reports"]
        self.assertEqual((rep2["unparsed"], rep2["reparsed"], rep2["inserted"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
