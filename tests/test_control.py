"""Control-plane tests: data states, imports, mapping, budget policy, decision engine, execution, emergency
controls, alerts. Fake platforms only (control.adapters.FakeAdapter, FakeTransport): no network, no credentials.

Run:  python -m unittest discover -s tests -v
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

import control
import revenue
from control import alerts, budget, engine, ingest, metrics, state, status
from control.adapters import FakeAdapter
from dlt_pipeline import APPS, Rules, app_for

NOW = dt.datetime(2026, 10, 26, 9, 0)
TODAY = NOW.date()
LIVE = {"DRY_RUN": "false", "ALERT_TRANSPORT": "fake"}
DRY = {"DRY_RUN": "true", "ALERT_TRANSPORT": "fake"}
COLS = ["date", "channel", "entity_id", "entity_type", "keyword", "campaign_id", "campaign", "adgroup_id", "app", "status",
        "impressions", "taps", "installs", "spend", "revenue", "currency", "account_id", "import_id", "_dlt_load_id"]


def fresh():
    con = duckdb.connect(":memory:")
    revenue.ensure(con)
    control.ensure(con)
    con.execute("CREATE SCHEMA IF NOT EXISTS ads")
    con.execute("CREATE TABLE ads.ad_performance (date VARCHAR, channel VARCHAR, entity_id VARCHAR, entity_type VARCHAR, keyword VARCHAR, "
                "campaign_id VARCHAR, campaign VARCHAR, adgroup_id VARCHAR, app VARCHAR, status VARCHAR, impressions DOUBLE, taps DOUBLE, "
                "installs DOUBLE, spend DOUBLE, revenue DOUBLE, currency VARCHAR, account_id VARCHAR, import_id VARCHAR, _dlt_load_id VARCHAR)")
    return con


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(float(v))
    return "'" + str(v).replace("'", "''") + "'"


def put_rows(con, rows):
    vals = ", ".join("(" + ", ".join(lit(r.get(c)) for c in COLS) + ")" for r in rows)
    con.execute(f"INSERT INTO ads.ad_performance ({', '.join(COLS)}) VALUES {vals}")


def days_back(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


def apple_kw(kid, days, taps=10, installs=0.0, spend=15.0, campaign="RNR | Search", cid="111", status_="ACTIVE", currency="USD", load="1"):
    return [{"date": days_back(d), "channel": "Apple Search Ads", "entity_id": kid, "entity_type": "keyword", "keyword": f"kw {kid}",
             "campaign_id": cid, "campaign": campaign, "adgroup_id": "9", "app": None, "status": status_, "impressions": 100,
             "taps": taps, "installs": installs, "spend": spend, "revenue": None, "currency": currency, "account_id": "org1",
             "_dlt_load_id": load} for d in days]


def meta_adset(aid, days, clicks=30, installs=0.0, spend=12.0, revenue=None, campaign="NurseBound | PH | Prospecting", acct="act1", currency="USD", load="1"):
    return [{"date": days_back(d), "channel": "Meta Ads", "entity_id": aid, "entity_type": "ad_set", "keyword": f"adset {aid}",
             "campaign_id": f"c{aid}", "campaign": campaign, "adgroup_id": aid, "app": None, "status": None, "impressions": 500,
             "taps": clicks, "installs": installs, "spend": spend, "revenue": revenue, "currency": currency, "account_id": acct,
             "_dlt_load_id": load} for d in days]


def mark_sources(con, when=NOW, sources=("Apple Search Ads", "Meta Ads", "Google Ads"), fail=(), rows=10):
    recs = []
    for s in sources:
        recs.append({"import_id": f"imp-{s}", "source": s, "account_id": "a1", "started_at": when, "finished_at": when,
                     "status": "failed" if s in fail else "succeeded", "rows": 0 if s in fail else rows, "activity_rows": 0 if s in fail else rows,
                     "window_start": TODAY - dt.timedelta(days=13), "window_end": TODAY, "min_date": days_back(13), "max_date": days_back(1),
                     "currencies": {"USD"}, "timezone": "ORTZ", "unmapped_rows": 0, "message": "boom" if s in fail else None})
    ingest.record(con, recs, {}, {s: True for s in sources}, clock=lambda: when)


def rules(**over):
    r = Rules.from_dict({"monthly_budget": 1000.0, "app_monthly": {"Rapid Nursing Review": 450.0, "CCRN Adult": 250.0, "NurseBound": 300.0},
                         **over})
    r._cmap = {}
    return r


def cycle(con, r, env=DRY, adapters=None, now=NOW, transport=None):
    t = transport or alerts.FakeTransport()
    r._cmap = ingest.mapping(con)
    return engine.run_cycle(con, r, apps=APPS, rule_app_fn=lambda n: app_for(n, r), adapters=adapters or {},
                            alerter=alerts.Alerter(con, t, r, clock=lambda: now), environ=env, now=now)


def props(con, where="1=1"):
    cur = con.execute(f"SELECT * FROM control.proposals WHERE {where} ORDER BY created_at")
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, x)) for x in cur.fetchall()]


# --------------------------------------------------------------------------- #
class StatusModelTests(unittest.TestCase):
    def test_states(self):
        c = status.classify
        self.assertEqual(c(configured=False)["state"], "not_configured")
        self.assertEqual(c(configured=True, supported=False)["state"], "unsupported")
        self.assertEqual(c(configured=True)["state"], "connected_not_imported")
        self.assertEqual(c(configured=True, last_attempt=NOW, last_status="failed", now=NOW)["state"], "failed")
        self.assertEqual(c(configured=True, last_attempt=NOW, last_success=NOW, last_status="succeeded", last_activity_rows=0, now=NOW)["state"], "imported_zero")
        self.assertEqual(c(configured=True, last_attempt=NOW, last_success=NOW, last_status="succeeded", last_activity_rows=4,
                           latest_date_covered=TODAY, now=NOW)["state"], "imported_activity")
        self.assertEqual(c(configured=True, last_attempt=NOW, last_success=NOW, last_status="partial", now=NOW)["state"], "partial")
        old = NOW - dt.timedelta(hours=40)
        self.assertEqual(c(configured=True, last_attempt=old, last_success=old, last_status="succeeded", last_activity_rows=3, now=NOW)["state"], "stale")
        # a failure after an older success is still "failed", and the last success is kept
        st = c(configured=True, last_attempt=NOW, last_success=old, last_status="failed", now=NOW)
        self.assertEqual((st["state"], st["last_success"]), ("failed", old))


class IngestTests(unittest.TestCase):
    def test_empty_successful_report_is_not_activity_and_failures_are_kept(self):
        con = fresh()
        recs, camps = [], {}
        jobs = [ingest.Job("Apple Search Ads", "org1", lambda: iter([])),
                ingest.Job("Meta Ads", "act1", lambda: iter([{**meta_adset("5", [2])[0], "spend": 0.0, "taps": 0.0, "impressions": 0.0}])),
                ingest.Job("Meta Ads", "act2", lambda: (_ for _ in ()).throw(RuntimeError("token expired Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")))]
        rows = list(ingest.collect(jobs, TODAY - dt.timedelta(days=13), TODAY, recs, camps, clock=lambda: NOW))
        self.assertEqual(len(rows), 1)
        ingest.record(con, recs, camps, {"Apple Search Ads": True, "Meta Ads": True, "Google Ads": False}, clock=lambda: NOW)
        r = {x[0]: x for x in con.execute("SELECT source, status, rows, activity_rows, quality, message FROM control.import_runs ORDER BY source, status").fetchall()}
        st = engine.source_statuses(con, rules(), NOW)
        self.assertEqual(st["Apple Search Ads"]["state"], "imported_zero")       # finished, nothing there
        self.assertEqual(st["Meta Ads"]["state"], "partial")                    # one account failed
        self.assertEqual(st["Google Ads"]["state"], "not_configured")
        self.assertEqual(st["TikTok Ads"]["state"], "unsupported")
        msgs = " ".join(str(x[5]) for x in con.execute("SELECT * FROM control.import_runs").fetchall())
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", msgs)          # tokens scrubbed from messages
        self.assertIn("redacted", con.execute("SELECT message FROM control.source_state WHERE source = 'Meta Ads'").fetchone()[0])

    def test_load_failure_fails_runs(self):
        con = fresh()
        recs = []
        list(ingest.collect([ingest.Job("Apple Search Ads", "org1", lambda: iter(apple_kw("1", [1])))], TODAY, TODAY, recs, {}, clock=lambda: NOW))
        ingest.record(con, recs, {}, {"Apple Search Ads": True}, load_error="disk full", clock=lambda: NOW)
        self.assertEqual(engine.source_statuses(con, rules(), NOW)["Apple Search Ads"]["state"], "failed")

    def test_unmapped_campaigns_listed_and_manual_mapping_applies_to_history(self):
        con = fresh()
        recs, camps = [], {}
        rows = [{"_campaign_only": True, "campaign_id": "999", "campaign": "VitalSync | Search", "app": None}] + apple_kw("7", [5], campaign="Spring promo", cid="777")
        for r in rows[1:]:
            r["app"] = None
        list(ingest.collect([ingest.Job("Apple Search Ads", "org1", lambda: iter(rows))], TODAY, TODAY, recs, camps, clock=lambda: NOW))
        ingest.record(con, recs, camps, {"Apple Search Ads": True}, clock=lambda: NOW)
        names = {u["campaign_name"] for u in ingest.unmapped(con)}
        self.assertEqual(names, {"VitalSync | Search", "Spring promo"})
        put_rows(con, apple_kw("7", range(4, 12), campaign="Spring promo", cid="777", spend=40))
        mark_sources(con)
        r = rules()
        cycle(con, r)
        self.assertEqual(props(con), [])                    # unmapped: excluded from decisions
        s = cycle(con, r)
        self.assertGreater(s["pacing"]["unattributed_spend_mtd"], 0)   # but its spend is visible
        ingest.set_mapping(con, "Apple Search Ads", "777", "Rapid Nursing Review", APPS, by="admin")
        ingest.set_mapping(con, "Apple Search Ads", "999", None, APPS, ignored=True)
        self.assertEqual(ingest.unmapped(con), [])
        cycle(con, r)
        p = props(con)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["app"], "Rapid Nursing Review")
        with self.assertRaises(ValueError):
            ingest.set_mapping(con, "Apple Search Ads", "777", "Not an app", APPS)


class DataRuleTests(unittest.TestCase):
    def test_duplicate_and_revised_rows_are_not_double_counted(self):
        con = fresh()
        put_rows(con, apple_kw("1", [3], spend=10, load="1") + apple_kw("1", [3], spend=25, load="2"))
        rows = engine.load_rows(con, TODAY - dt.timedelta(days=10), TODAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spend"], 25.0)            # the revised (newer) report wins

    def test_missing_revenue_is_not_zero(self):
        con = fresh()
        mark_sources(con)
        # Apple: revenue never reported; lots of spend, installs fine → no revenue rule, a note instead
        put_rows(con, apple_kw("1", range(2, 9), taps=30, installs=2, spend=20))
        # Meta ad set A: revenue not reported (NULL) → unknown; B: reported as $0 with 10 installs → ACoS rule may act
        put_rows(con, meta_adset("A", range(4, 10), clicks=5, installs=2, spend=10, revenue=None))
        put_rows(con, meta_adset("B", range(4, 10), clicks=5, installs=2, spend=10, revenue=0.0))
        s = cycle(con, rules())
        p = props(con)
        self.assertEqual([x["entity_key"] for x in p], ["Meta Ads|B"])
        self.assertIn("reported $0", p[0]["reason"])
        self.assertTrue(any(n.get("rule") == "acos_ceiling" for n in s["notes"]))
        summ = metrics.ad_summary(engine.load_rows(con, TODAY - dt.timedelta(days=30), TODAY), rules())["by_currency"][0]
        self.assertAlmostEqual(summ["revenue_coverage"], 60 / (60 + 60 + 140))   # only B's spend has reported revenue

    def test_value_per_install_estimate_never_drives_decisions(self):
        con = fresh()
        mark_sources(con)
        put_rows(con, meta_adset("A", range(4, 10), clicks=5, installs=3, spend=10, revenue=None))
        cycle(con, rules(value_per_install={"NurseBound": 0.01}))
        self.assertEqual(props(con), [])

    def test_currency_mismatch_blocks_money_rules(self):
        con = fresh()
        mark_sources(con)
        put_rows(con, apple_kw("1", range(2, 9), taps=3, installs=0, spend=20, currency="EUR"))
        cycle(con, rules())
        p = props(con)
        self.assertEqual(p[0]["status"], "blocked")
        self.assertIn("in EUR", p[0]["blockers"])

    def test_immature_days_are_left_out(self):
        con = fresh()
        mark_sources(con)
        # all spend within Meta's 3-day maturity lag: no conversions yet, but no decision either
        put_rows(con, meta_adset("A", [0, 1, 2], clicks=40, installs=0, spend=30))
        cycle(con, rules())
        self.assertEqual(props(con), [])
        put_rows(con, meta_adset("A", [3, 4, 5], clicks=40, installs=0, spend=30))
        cycle(con, rules())
        self.assertEqual(len(props(con)), 1)

    def test_stale_or_partial_sources_block_optimization(self):
        con = fresh()
        mark_sources(con, when=NOW - dt.timedelta(hours=50))
        put_rows(con, apple_kw("1", range(2, 9), taps=3, installs=0, spend=20))
        cycle(con, rules())
        p = props(con)
        self.assertEqual(p[0]["status"], "blocked")
        self.assertTrue(any("stale" in b.lower() for b in json.loads(p[0]["blockers"])))

    def test_not_enough_days(self):
        con = fresh()
        mark_sources(con)
        put_rows(con, apple_kw("1", [2], taps=3, installs=0, spend=40))
        cycle(con, rules())
        self.assertIn("Not enough mature data", props(con)[0]["blockers"])

    def test_channel_specific_rules(self):
        con = fresh()
        mark_sources(con)
        # Meta link-click cost above the CPT ceiling is NOT a reason to pause an ad set (keyword logic stays on keywords)
        put_rows(con, meta_adset("A", range(4, 10), clicks=1, installs=1, spend=5))
        cycle(con, rules(max_cpt=1.0))
        self.assertEqual(props(con), [])


class BudgetTests(unittest.TestCase):
    def test_validation(self):
        self.assertIn("No monthly budget", budget.validate(Rules(), APPS, ["Meta Ads"])[0])
        r = rules(app_monthly={"Rapid Nursing Review": 900.0, "CCRN Adult": 200.0})
        errs = " ".join(budget.validate(r, APPS, ["Meta Ads"]))
        self.assertIn("NurseBound", errs)
        r = rules(app_monthly={"Rapid Nursing Review": 900.0, "CCRN Adult": 200.0, "NurseBound": 10.0})
        self.assertIn("more than the $1,000.00", " ".join(budget.validate(r, APPS, ["Meta Ads"])))
        self.assertIn("at least $1", " ".join(budget.validate(rules(campaign_daily_max=0.5), APPS, ["Meta Ads"])))
        self.assertEqual(budget.validate(rules(), APPS, ["Meta Ads"]), [])

    def test_pacing_uses_calendar_days_and_reserve(self):
        p = budget.pacing(rules(), {"Rapid Nursing Review": 100.0}, {}, dt.date(2026, 2, 20), APPS)
        self.assertEqual(p["days_in_month"], 28)
        self.assertEqual(p["days_left"], 9)
        rnr = p["apps"]["Rapid Nursing Review"]
        self.assertAlmostEqual(rnr["daily_allowance"], round((450 * 0.9 - 100) / 9, 2))
        self.assertEqual(budget.days_in_month(dt.date(2026, 10, 1)), 31)

    def test_split_never_multiplies_an_app_allowance(self):
        s = budget.split_allowance(30.0, {"a": 10, "b": 30, "c": 0}, None)
        self.assertAlmostEqual(sum(s.values()), 30.0, places=1)
        s = budget.split_allowance(30.0, {"a": 10, "b": 30}, 5.0)
        self.assertEqual(s, {"a": 5.0, "b": 5.0})          # capped, and the rest is NOT pushed onto others

    def test_rebalance_blocked_without_revenue(self):
        r = rules(auto_rebalance=True)
        res = budget.rebalance(r, {a: {"spend": 500, "revenue": None, "conversions": 50} for a in APPS}, APPS, True, "recommend", False)
        self.assertFalse(res["eligible"])
        self.assertTrue(any("revenue isn't reported" in b for b in res["blockers"]))
        tiny = {a: {"spend": 5, "revenue": 50, "conversions": 1, "currency": "USD"} for a in APPS}
        self.assertFalse(budget.rebalance(r, tiny, APPS, True, "recommend", False)["eligible"])
        good = {"Rapid Nursing Review": {"spend": 300, "revenue": 900, "conversions": 40, "currency": "USD"},
                "CCRN Adult": {"spend": 300, "revenue": 150, "conversions": 20, "currency": "USD"},
                "NurseBound": {"spend": 300, "revenue": 300, "conversions": 30, "currency": "USD"}}
        res = budget.rebalance(r, good, APPS, True, "recommend", False)
        self.assertTrue(res["eligible"])
        self.assertLessEqual(sum(res["proposed"].values()), 1000.0 + 0.01)
        self.assertGreater(res["proposed"]["Rapid Nursing Review"], 450)
        self.assertLessEqual(res["proposed"]["Rapid Nursing Review"], 450 * 1.25 + 0.01)   # step limit
        self.assertFalse(budget.rebalance(rules(), good, APPS, True, "recommend", False)["eligible"])   # configured off


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.con = fresh()
        mark_sources(self.con)
        put_rows(self.con, apple_kw("1", range(2, 9), taps=3, installs=0, spend=20))

    def test_dry_run_never_writes(self):
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}})
        state.set_mode(self.con, "bounded_auto", "test")
        r = rules(auto_actions=["pause_keyword"])
        s = cycle(self.con, r, env=DRY, adapters={"Apple Search Ads": fake})
        self.assertEqual(fake.writes, [])
        self.assertEqual(s["executions"], {"simulated": 1})
        self.assertEqual(props(self.con)[0]["status"], "simulated")
        # no daily repeat of the same simulated change
        cycle(self.con, r, env=DRY, adapters={"Apple Search Ads": fake}, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(len(props(self.con)), 1)

    def test_recommend_mode_only_proposes(self):
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}})
        s = cycle(self.con, rules(auto_actions=["pause_keyword"]), env=LIVE, adapters={"Apple Search Ads": fake})
        self.assertEqual(fake.writes, [])
        self.assertEqual(props(self.con)[0]["status"], "proposed")
        self.assertEqual(s["mode"], "recommend")

    def test_approval_flow_live_with_verification(self):
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}})
        state.set_mode(self.con, "approval", "test")
        r = rules()
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake})
        p = props(self.con)[0]
        self.assertEqual(p["status"], "proposed")
        self.assertEqual(fake.writes, [])
        engine.decide(self.con, p["proposal_id"], True, "admin", dry=False)
        with self.assertRaises(ValueError):
            engine.decide(self.con, p["proposal_id"], True, "admin", dry=False)   # can't approve twice
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake}, now=NOW + dt.timedelta(minutes=5))
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(props(self.con)[0]["status"], "applied")
        ex = self.con.execute("SELECT result, pre_state, post_state FROM control.executions").fetchone()
        self.assertEqual(ex[0], "applied")
        self.assertIn("ACTIVE", ex[1])
        self.assertIn("PAUSED", ex[2])

    def test_rejection_is_respected(self):
        state.set_mode(self.con, "approval", "test")
        r = rules()
        cycle(self.con, r, env=LIVE)
        engine.decide(self.con, props(self.con)[0]["proposal_id"], False, "admin", dry=False)
        cycle(self.con, r, env=LIVE, now=NOW + dt.timedelta(hours=2))
        self.assertEqual([p["status"] for p in props(self.con)], ["rejected"])

    def test_already_done_is_not_resent(self):
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "PAUSED"}})
        state.set_mode(self.con, "bounded_auto", "test")
        cycle(self.con, rules(auto_actions=["pause_keyword"]), env=LIVE, adapters={"Apple Search Ads": fake})
        self.assertEqual(fake.writes, [])
        self.assertEqual(props(self.con)[0]["status"], "already_done")

    def test_partial_failures_and_ambiguous_timeouts(self):
        put_rows(self.con, apple_kw("2", range(2, 9), taps=3, installs=0, spend=20, cid="112"))
        put_rows(self.con, apple_kw("3", range(2, 9), taps=3, installs=0, spend=20, cid="113"))
        fake = FakeAdapter({k: {"status": "ACTIVE"} for k in ("Apple Search Ads|1", "Apple Search Ads|2", "Apple Search Ads|3")},
                           fail={"Apple Search Ads|2": "error", "Apple Search Ads|3": "ambiguous_applied"})
        state.set_mode(self.con, "bounded_auto", "test")
        r = rules(auto_actions=["pause_keyword"])
        s = cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake})
        by = {p["entity_key"]: p["status"] for p in props(self.con)}
        self.assertEqual(by, {"Apple Search Ads|1": "applied", "Apple Search Ads|2": "failed", "Apple Search Ads|3": "uncertain"})
        self.assertEqual(s["executions"], {"applied": 1, "failed": 1, "uncertain": 1})
        n_writes = len(fake.writes)
        # next run: the uncertain one is reconciled by reading state, not re-sent
        s2 = cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake}, now=NOW + dt.timedelta(hours=1))
        self.assertIn({"proposal_id": [p for p in props(self.con) if p["entity_key"] == "Apple Search Ads|3"][0]["proposal_id"],
                       "result": "applied"}, s2["reconciled"])
        self.assertEqual([w for w in fake.writes[n_writes:] if w["entity_key"] == "Apple Search Ads|3"], [])

    def test_uncertain_blocks_new_changes_until_reconciled(self):
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}}, fail={"Apple Search Ads|1": "ambiguous"})
        state.set_mode(self.con, "bounded_auto", "test")
        r = rules(auto_actions=["pause_keyword"])
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake})
        self.assertEqual(props(self.con)[0]["status"], "uncertain")
        # reconcile finds it NOT applied → failed; a fresh proposal may follow later, never a blind retry in the same step
        fake.fail = {}
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake}, now=NOW + dt.timedelta(hours=1))
        st = [p["status"] for p in props(self.con)]
        self.assertEqual(st[0], "failed")
        self.assertEqual(len(fake.writes), 2)          # the second write came from a new, separately recorded proposal

    def test_concurrent_claims_execute_once(self):
        state.set_mode(self.con, "approval", "test")
        cycle(self.con, rules(), env=LIVE)
        p = props(self.con)[0]
        engine.decide(self.con, p["proposal_id"], True, "admin", dry=False)
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}})
        p = props(self.con)[0]
        r1 = engine.execute(self.con, p, {"Apple Search Ads": fake}, False, NOW)
        r2 = engine.execute(self.con, p, {"Apple Search Ads": fake}, False, NOW)   # second worker, same proposal
        self.assertEqual((r1, r2), ("applied", "skipped"))
        self.assertEqual(len(fake.writes), 1)

    def test_manual_override_and_protection(self):
        put_rows(self.con, apple_kw("2", range(2, 9), taps=3, installs=5, spend=1, cid="112"))
        r = rules(overrides={"Apple Search Ads|1": "protected", "Apple Search Ads|2": "paused"})
        fake = FakeAdapter({"Apple Search Ads|2": {"status": "ACTIVE"}})
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fake})     # recommend mode
        p = props(self.con)
        self.assertEqual([(x["entity_key"], x["kind"], x["status"]) for x in p], [("Apple Search Ads|2", "manual", "applied")])
        self.assertEqual(fake.writes[0]["entity_key"], "Apple Search Ads|2")


class LimitTests(unittest.TestCase):
    def _setup(self, spend=30):
        con = fresh()
        mark_sources(con)
        rows = []
        for i, cid in enumerate(("201", "202")):
            rows += [{"date": days_back(d), "channel": "Meta Ads", "entity_id": cid, "entity_type": "ad_set", "keyword": f"set {cid}",
                      "campaign_id": f"camp{cid}", "campaign": "RNR | Prospecting", "adgroup_id": cid, "status": None, "impressions": 900,
                      "taps": 50, "installs": 10, "spend": spend, "revenue": None, "currency": "USD", "account_id": "act1", "_dlt_load_id": "1"}
                     for d in range(0, 7)]
        put_rows(con, rows)
        return con

    def test_cumulative_daily_change_limit_and_cooldown_over_repeated_runs(self):
        con = self._setup()
        fake = FakeAdapter({"Meta Ads|201": {"status": "ACTIVE", "daily_budget": 60.0}, "Meta Ads|202": {"status": "ACTIVE", "daily_budget": 60.0}})
        state.set_mode(con, "bounded_auto", "test")
        r = rules(auto_actions=["set_budget_decrease"], max_daily_change_pct=0.30, cooldown_hours=6, max_actions_per_campaign_day=2)
        values = []
        for h in range(0, 30, 2):   # a run every 2 hours for 30 hours
            cycle(con, r, env=LIVE, adapters={"Meta Ads": fake}, now=NOW + dt.timedelta(hours=h))
            values.append(fake.states["Meta Ads|201"]["daily_budget"])
        # the pacing target is far below 60, but no 24 h window may cut more than 30% in total
        writes = [w for w in fake.writes if w["entity_key"] == "Meta Ads|201"]
        self.assertGreaterEqual(len(writes), 2)
        self.assertAlmostEqual(float(writes[0]["value"]), 42.0)                      # 60 → 42 (30% cap)
        first = NOW
        t = [first + dt.timedelta(hours=h) for h in range(0, 30, 2)]
        changes = [(t[i], values[i - 1] if i else 60.0, values[i]) for i in range(len(values)) if (values[i - 1] if i else 60.0) != values[i]]
        for i, (ti, a, b) in enumerate(changes):
            later = [c for c in changes if ti <= c[0] < ti + dt.timedelta(hours=24)]
            total = sum(abs(y - x) / x for _, x, y in later)
            self.assertLessEqual(total, 0.30 + 1e-6)
        gaps = [(changes[i + 1][0] - changes[i][0]).total_seconds() / 3600 for i in range(len(changes) - 1)]
        self.assertTrue(all(g >= 6 for g in gaps))                                      # cooldown held

    def test_increase_needs_its_own_permission(self):
        con = self._setup(spend=1)
        fake = FakeAdapter({"Meta Ads|201": {"status": "ACTIVE", "daily_budget": 2.0}, "Meta Ads|202": {"status": "ACTIVE", "daily_budget": 2.0}})
        state.set_mode(con, "bounded_auto", "test")
        cycle(con, rules(auto_actions=["set_budget_decrease"]), env=LIVE, adapters={"Meta Ads": fake})
        self.assertEqual(fake.writes, [])
        self.assertTrue(all(p["status"] == "proposed" for p in props(con)))


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        self.con = fresh()
        mark_sources(self.con)
        put_rows(self.con, apple_kw("1", range(2, 9), taps=3, installs=0, spend=20, cid="301"))
        put_rows(self.con, meta_adset("A", range(4, 10), clicks=40, installs=0, spend=10))

    def test_stop_automation_blocks_optimization_only(self):
        state.set_mode(self.con, "bounded_auto", "test")
        state.stop_automation(self.con, "admin", "testing")
        fake = FakeAdapter({"Apple Search Ads|1": {"status": "ACTIVE"}})
        cycle(self.con, rules(auto_actions=["pause_keyword"]), env=LIVE, adapters={"Apple Search Ads": fake})
        self.assertEqual(fake.writes, [])
        self.assertTrue(all(p["status"] == "blocked" for p in props(self.con)))
        self.assertTrue(state.automation_stopped(self.con))                     # latched across runs
        cycle(self.con, rules(auto_actions=["pause_keyword"]), env=LIVE, adapters={"Apple Search Ads": fake}, now=NOW + dt.timedelta(hours=3))
        self.assertEqual(fake.writes, [])
        state.reset_automation_stop(self.con, "admin")
        self.assertIsNone(state.automation_stopped(self.con))

    def test_emergency_pause_is_separate_explicit_and_not_blocked(self):
        state.stop_automation(self.con, "admin", "testing")
        r = rules()
        r._cmap = {}
        res = engine.request_emergency_pause(self.con, r, "admin", APPS, today=TODAY, dry=False, rule_app_fn=lambda n: app_for(n, r))
        self.assertEqual(res["requested"], 2)                                    # Apple campaign 301 + Meta ad set A
        fa = FakeAdapter({"Apple Search Ads|campaign:301": {"status": "ENABLED"}}, fail={})
        fm = FakeAdapter({"Meta Ads|A": {"status": "ACTIVE"}}, fail={"Meta Ads|A": "error"})
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fa, "Meta Ads": fm})   # recommend mode + stopped
        em = {p["entity_key"]: p["status"] for p in props(self.con, "kind = 'emergency'")}
        self.assertEqual(em, {"Apple Search Ads|campaign:301": "applied", "Meta Ads|A": "failed"})   # per-campaign results
        self.assertTrue(engine.emergency_active(self.con))
        engine.clear_emergency(self.con, "admin")
        self.assertIsNone(engine.emergency_active(self.con))
        self.assertEqual(fa.states["Apple Search Ads|campaign:301"]["status"], "PAUSED")     # nothing resumed

    def test_spend_safeguard_bypasses_cooldown_and_missing_revenue_never_pauses(self):
        r = rules(monthly_budget=100.0, app_monthly={"Rapid Nursing Review": 50.0, "CCRN Adult": 25.0, "NurseBound": 25.0})
        state.set_mode(self.con, "bounded_auto", "test")
        fa = FakeAdapter({"Apple Search Ads|campaign:301": {"status": "ENABLED"}})
        fm = FakeAdapter({"Meta Ads|A": {"status": "ACTIVE"}})
        cycle(self.con, r, env=LIVE, adapters={"Apple Search Ads": fa, "Meta Ads": fm})
        sg = props(self.con, "kind = 'safeguard'")
        self.assertEqual(sorted(p["entity_key"] for p in sg), ["Apple Search Ads|campaign:301", "Meta Ads|A"])
        self.assertTrue(all(p["status"] == "proposed" for p in sg))            # safeguard pauses need permission too
        r2 = rules(monthly_budget=100.0, app_monthly={"Rapid Nursing Review": 50.0, "CCRN Adult": 25.0, "NurseBound": 25.0},
                   auto_actions=["safeguard_pause"])
        cycle(self.con, r2, env=LIVE, adapters={"Apple Search Ads": fa, "Meta Ads": fm}, now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(fa.states["Apple Search Ads|campaign:301"]["status"], "PAUSED")
        # with no budget breach and no revenue at all, nothing gets paused for "zero revenue"
        con = fresh()
        mark_sources(con)
        put_rows(con, apple_kw("1", range(2, 9), taps=30, installs=3, spend=30, cid="301"))
        cycle(con, rules(), env=LIVE)
        self.assertEqual(props(con), [])


class AlertTests(unittest.TestCase):
    def test_dedup_cooldown_and_failures(self):
        con = fresh()
        t = alerts.FakeTransport()
        clock = [NOW]
        a = alerts.Alerter(con, t, rules(), clock=lambda: clock[0])
        self.assertEqual(a.send("import_failure", "critical", "k1", "Meta failed"), "delivered")
        self.assertEqual(a.send("import_failure", "critical", "k1", "Meta failed"), "suppressed")
        clock[0] = NOW + dt.timedelta(hours=2)
        self.assertEqual(a.send("import_failure", "critical", "k1", "Meta failed"), "delivered")
        self.assertEqual(a.send("daily_summary", "info", "d", "Summary"), "delivered")
        clock[0] = NOW + dt.timedelta(hours=10)
        self.assertEqual(a.send("daily_summary", "info", "d", "Summary"), "suppressed")
        bad = alerts.Alerter(con, alerts.FakeTransport(fail=True), rules())
        self.assertEqual(bad.send("budget", "warning", "b", "Budget"), "failed")
        self.assertEqual([x["delivery_status"] for x in alerts.recent(con)][0], "failed")
        off = alerts.Alerter(con, t, rules(alerts={"daily_summary": False}))
        self.assertEqual(off.send("daily_summary", "info", "d2", "x"), "disabled")
        self.assertEqual(off.send("emergency", "critical", "e", "x"), "delivered")   # can't be switched off
        self.assertEqual(len(t.sent), 4)

    def test_no_real_transport_in_dev(self):
        self.assertIsInstance(alerts.transport_from_env(None, {"ALERT_TRANSPORT": "fake", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1"}),
                              alerts.FakeTransport)
        self.assertIsInstance(alerts.transport_from_env(None, {}), alerts.LogTransport)


class LockTests(unittest.TestCase):
    def test_lease_and_restart(self):
        con = fresh()
        self.assertTrue(state.acquire(con, "hourly", "w1"))
        self.assertFalse(state.acquire(con, "hourly", "w2"))                # second worker can't take it
        con.execute("UPDATE control.locks SET expires_at = ? WHERE name = 'hourly'", [dt.datetime(2000, 1, 1)])
        self.assertTrue(state.acquire(con, "hourly", "w2"))                 # a crashed holder's lease expires
        state.release(con, "hourly", "w1")                                  # not the owner: no effect
        self.assertEqual(state.lock_holder(con, "hourly")["owner"], "w2")
        # state survives a "restart" (a new connection to the same database)
        state.set_mode(con, "approval", "t")
        con2 = duckdb.connect(con.path) if hasattr(con, "path") else con
        self.assertEqual(state.mode(con2), "approval")


if __name__ == "__main__":
    unittest.main()
