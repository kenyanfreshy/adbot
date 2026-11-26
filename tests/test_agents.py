"""Agent team (agents/): the Brain's handoffs and failure handling, each phase-1 agent on a real (in-memory)
database, the Claude client's daily cap, and the morning page's grounding rule and once-a-day schedule.

No network: Claude and Telegram are fakes. Run:  python -m unittest tests.test_agents -v
"""
import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from agents import brain, llm, morning  # noqa: E402
from control import alerts  # noqa: E402
from dlt_pipeline import Rules  # noqa: E402

NOW = dt.datetime(2026, 11, 25, 16, 7)          # 08:07 in Los Angeles
TODAY = NOW.date()
ENV = {"DRY_RUN": "true", "AGENTS_TZ": "America/Los_Angeles", "AGENTS_MORNING_HOUR": "8"}


class FakeClaude:
    def __init__(self, text="Spend is under the limit and RNR leads on proceeds.", answers=None):
        self.text, self.calls, self.answers = text, [], answers or {}

    def complete(self, agent, system, prompt, max_tokens=500):
        self.calls.append((agent, prompt))
        return self.text

    def complete_json(self, agent, system, prompt, max_tokens=1500):
        self.calls.append((agent, prompt))
        a = self.answers.get(agent)
        return a(prompt) if callable(a) else a


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body


# --------------------------------------------------------------------------- #
# No database needed
# --------------------------------------------------------------------------- #
class BrainTests(unittest.TestCase):
    def test_handoffs_failures_and_skips(self):
        seen = {}

        def a(ctx):
            return {"x": 2}

        def b(ctx):
            seen["b"] = ctx.out["a"]["x"]
            return {"y": ctx.out["a"]["x"] * 3}

        def boom(ctx):
            raise RuntimeError("nope")

        team = [brain.Agent("a", "A", "M", "", a), brain.Agent("b", "B", "M", "", b, needs=("a",)),
                brain.Agent("c", "C", "M", "", boom), brain.Agent("d", "D", "M", "", a, needs=("c",))]
        res = brain.run(None, Rules(), environ=ENV, llm=False, transport=alerts.FakeTransport(), clock=lambda: NOW,
                        agents=team, record=False)
        self.assertEqual({k: v["status"] for k, v in res["agents"].items()}, {"a": "ok", "b": "ok", "c": "failed", "d": "skipped"})
        self.assertEqual(seen["b"], 2)
        self.assertEqual(res["agents"]["b"]["output"], {"y": 6})
        self.assertIn("nope", res["agents"]["c"]["error"])
        self.assertEqual(res["status"], "partial")

    def test_team_is_read_only_by_construction(self):
        # every agent module: no adapter, no execution, no platform client imported
        import inspect
        from agents import (books, changes, checklist, creative, departments, launch, mockups, money, outcomes, profit, research,
                            studio, watchdog, winners, writing)
        for m in (brain, changes, creative, departments, launch, money, morning, research, studio, winners, writing, llm,
                  books, checklist, mockups, outcomes, profit, watchdog):
            src = inspect.getsource(m)
            for bad in ("adapters", "execute_", "pause_keyword", "set_campaign_budget", "set_adset_budget", "run_bot("):
                self.assertNotIn(bad, src, f"{m.__name__} mentions {bad}")

    def test_local_time(self):
        ctx = brain.Context(con=None, rules=Rules(), now=NOW, environ=ENV)
        self.assertEqual(ctx.local_now, dt.datetime(2026, 11, 25, 8, 7))


class GroundingTests(unittest.TestCase):
    FACTS = json.dumps({"spend_watch": {"mtd_total": 412.0, "usable": 450.0, "days_left": 6}, "x": "2026-11-24"})

    def test_numbers_must_come_from_the_facts(self):
        self.assertTrue(morning.grounded("You've spent $412 of $450 with 6 days left.", self.FACTS))
        self.assertTrue(morning.grounded("Spend is 412.00 so far.", self.FACTS))
        self.assertTrue(morning.grounded("Nothing unusual today.", self.FACTS))
        self.assertFalse(morning.grounded("Spend is up 31% this week.", self.FACTS))
        self.assertFalse(morning.grounded("You've spent $413.", self.FACTS))


class ClaudeClientTests(unittest.TestCase):
    class Con:
        """Just enough of a connection for llm.Claude: an in-memory llm_usage log."""
        def __init__(self):
            self.rows = []

        def execute(self, sql, params=None):
            if sql.startswith("INSERT"):
                self.rows.append(params)
                return self
            self._r = [(sum(r[6] or 0 for r in self.rows),)]
            return self

        def fetchone(self):
            return self._r[0]

    def test_cost_cap_and_logging(self):
        con, posts = self.Con(), []

        def post(url, headers=None, json=None, timeout=None):
            posts.append((url, headers, json))
            return Resp({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1000, "output_tokens": 200}})

        c = llm.Claude(con, "k", "claude-sonnet-5-5", daily_cap_usd=0.01, clock=lambda: NOW, post=post)
        self.assertEqual(c.complete("t", "sys", "hello", max_tokens=200), "ok")
        url, headers, body = posts[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]), ("k", "2023-06-01"))
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertAlmostEqual(con.rows[0][6], (1000 * 2 + 200 * 10) / 1e6)           # $0.004
        self.assertEqual(con.rows[0][7], "ok")
        self.assertEqual(c.complete("t", "sys", "hello", max_tokens=200), "ok")         # $0.008 so far
        self.assertIsNone(c.complete("t", "sys", "hello", max_tokens=200))              # would pass $0.01
        self.assertEqual(con.rows[-1][7], "refused_cap")
        self.assertEqual(len(posts), 2)

    def test_errors_never_raise_and_unknown_models_cost_the_most(self):
        con = self.Con()
        c = llm.Claude(con, "k", "claude-unknown", clock=lambda: NOW, post=lambda *a, **k: Resp({"error": "x"}, 529))
        self.assertIsNone(c.complete("t", "s", "p"))
        self.assertEqual(con.rows[-1][7], "failed")
        self.assertEqual(llm.price("claude-unknown"), llm.price("claude-opus-5-5"))
        self.assertIsNone(llm.from_env(con, {}))
        self.assertEqual(llm.from_env(con, {"ANTHROPIC_API_KEY": "k", "AGENTS_DAILY_USD_CAP": "2.5"}).cap, 2.5)


# --------------------------------------------------------------------------- #
# With a database (DuckDB, as in the other test files)
# --------------------------------------------------------------------------- #
def db():
    import test_control
    return test_control


class _OnData(unittest.TestCase):
    """Shared setup: an in-memory database with 10 days of ad rows and some App Store sales."""

    def setUp(self):
        tc = db()
        self.tc = tc
        self.con = tc.fresh()
        shift = (TODAY - tc.TODAY).days
        days = lambda n: [d - shift for d in range(1, n + 1)]   # noqa: E731 - test_control counts back from its own TODAY
        rows = (tc.apple_kw("k1", days(10), taps=10, installs=2.0, spend=5.0, campaign="RNR – US – Search Results", cid="2145125761")
                + tc.apple_kw("k2", days(10), taps=10, installs=0.0, spend=8.0, campaign="RNR – US – Search Results", cid="2145125761")
                + tc.meta_adset("a1", days(10), clicks=30, installs=3.0, spend=4.0))
        tc.put_rows(self.con, rows)
        from revenue.schema import SCHEMA as S
        cols = ("line_key", "source_platform", "app_id", "source_app_ref", "currency", "period_start", "period_end", "data_status",
                "transaction_type", "units", "proceeds_amount", "proceeds_currency", "sku", "parent_ref", "imported_at",
                "reporting_timezone", "source_report_id")
        for i, (d, ttype, units, proceeds) in enumerate([(2, "subscription", 2, 30.0), (3, "download", 40, None), (4, "refund", -1, -9.0)]):
            day = TODAY - dt.timedelta(days=d)
            self.con.execute(f"INSERT INTO {S}.sales_reports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [f"k{i}", "apple_app_store", "rnr_pro", "x", "USD", day, day, "estimated", ttype, units, proceeds,
                              "USD" if proceeds is not None else None, "rnr_pro_monthly", None, NOW, "UTC", "r1"])
        self.rules = Rules()
        self.rules.monthly_budget = 500.0

    def run_team(self, env=None, llm_=None, transport=None, now=NOW):
        t = transport or alerts.FakeTransport()
        from unittest import mock
        with mock.patch.object(brain, "_build_client", lambda name, env: None):      # no platform keys in tests
            return brain.run(self.con, self.rules, environ={**ENV, **(env or {})}, llm=llm_ if llm_ is not None else False,
                             transport=t, clock=lambda: now), t



class TeamOnDataTests(_OnData):
    def test_one_run_end_to_end(self):
        claude = FakeClaude("Spend is under the limit with 6 days left.")
        res, t = self.run_team(llm_=claude)
        st = {k: v["status"] for k, v in res["agents"].items()}
        self.assertEqual(set(st.values()), {"ok"}, res)
        out = {k: v["output"] for k, v in res["agents"].items()}

        sw = out["spend_watch"]
        self.assertEqual(sw["yesterday_total"], 17.0)                   # 5 + 8 + 4
        self.assertEqual(sw["yesterday_by_channel"], {"Apple Search Ads": 13.0, "Meta Ads": 4.0})
        self.assertTrue(sw["under_limit"])

        rnr = out["real_return"]["apps"]["Rapid Nursing Review"]["7d"]
        self.assertEqual((rnr["spend"], rnr["proceeds"], rnr["downloads"], rnr["paid"]), (91.0, 21.0, 40, 2))  # 13/day x 7; 30 - 9 refund
        self.assertEqual(rnr["return"], round(21 / 91, 2))

        ws = out["winner_spotter"]
        self.assertEqual(ws["winners"][0]["name"], "adset a1")         # $4 / 3 installs beats $5 / 2
        self.assertEqual([w["name"] for w in ws["watch"]], ["kw k2"])

        self.assertEqual(out["change_review"]["needs_your_ok"], 0)
        self.assertTrue(any(s["name"] == "Apple Search Ads" for s in out["department_check"]["sources"]))

        mp = out["morning_page"]
        self.assertTrue(mp["sent"] or mp["delivery"] == "logged", mp)
        self.assertTrue(mp["used_llm"])
        self.assertEqual(len(t.sent), 1)
        self.assertIn("morning page", t.sent[0])
        self.assertIn("Spend is under the limit", t.sent[0])
        self.assertIn("$17.00", t.sent[0])
        self.assertIn("dry run", t.sent[0])
        self.assertEqual(self.con.execute("SELECT count(*) FROM control.morning_pages").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT status FROM control.agent_runs").fetchone()[0], "succeeded")
        self.assertEqual(self.con.execute("SELECT count(*) FROM control.agent_outputs").fetchone()[0], len(brain.team()))

    def test_once_a_day_and_not_before_the_hour(self):
        res, t = self.run_team(now=dt.datetime(2026, 11, 25, 12, 7))      # 04:07 local
        self.assertFalse(res["agents"]["morning_page"]["output"]["sent"])
        self.assertEqual(t.sent, [])
        self.run_team()                                                    # 08:07 local: sent
        res, t = self.run_team(now=dt.datetime(2026, 11, 25, 20, 7))      # 12:07 local: already written
        self.assertIn("already written", res["agents"]["morning_page"]["output"]["reason"])
        self.assertEqual(t.sent, [])

    def test_ungrounded_summary_is_dropped_and_toggle_respected(self):
        res, t = self.run_team(llm_=FakeClaude("Installs rose 47% overnight."))
        mp = res["agents"]["morning_page"]["output"]
        self.assertFalse(mp["used_llm"])
        self.assertIn("not in the facts", mp["summary_dropped"])
        self.assertNotIn("47%", t.sent[0])
        self.rules.alerts = {**self.rules.alerts, "morning_page": False}
        res, t = self.run_team(now=NOW + dt.timedelta(days=1))
        self.assertEqual(res["agents"]["morning_page"]["output"]["delivery"], "disabled")
        self.assertEqual(t.sent, [])

    def test_a_failing_agent_never_stops_the_page_inputs_or_the_bot(self):
        self.con.execute("DROP TABLE control.proposals")
        res, t = self.run_team()
        self.assertEqual(res["agents"]["change_review"]["status"], "failed")
        self.assertEqual(res["agents"]["morning_page"]["status"], "skipped")
        self.assertEqual(res["agents"]["spend_watch"]["status"], "ok")
        self.assertEqual(res["status"], "partial")


# --------------------------------------------------------------------------- #
# Phases 2-4: research, writing, launch drafts, creative briefs
# --------------------------------------------------------------------------- #
class StudioPureTests(unittest.TestCase):
    def test_claim_flags(self):
        from agents.studio import claim_flags
        self.assertEqual(claim_flags("Study smarter on your commute"), [])
        f = claim_flags("Pass the NCLEX on your first try", "Best CCRN app", "2,000 practice questions", "Free trial")
        self.assertEqual(len(f), 4)

    def test_copy_limits(self):
        from agents.writing import check_copy
        clean, dropped, flags = check_copy({
            "meta": {"primary_text": ["Short and fine", "x" * 126], "headline": ["Ok headline"]},
            "google_app": {"headline": ["Thirty chars is the max here..", "This headline is far too long for Google"], "description": ["Fine"]},
            "apple_ads": {"keywords": ["nclex", "nclex practice"]}})
        self.assertEqual(clean["meta"]["primary_text"], ["Short and fine"])
        self.assertEqual(clean["google_app"]["headline"], ["Thirty chars is the max here.."])
        self.assertEqual(len(dropped), 2)
        self.assertEqual(clean["apple_ads"]["keywords"], ["nclex", "nclex practice"])

    def test_parse_json(self):
        self.assertEqual(llm.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json('Here: {"a": [1, 2]} done'), {"a": [1, 2]})
        self.assertIsNone(llm.parse_json("no json"))


class FakeAppleStore:
    def pages(self, path, params):
        assert path == "/v1/apps/6822172335/customerReviews", path
        yield {"data": [{"id": "r1", "attributes": {"rating": 5, "title": "Great for NCLEX", "body": "The rationales helped me study on breaks.",
                                                     "territory": "USA", "createdDate": "2026-11-20T10:00:00-08:00"}},
                        {"id": "r2", "attributes": {"rating": 2, "title": "Crashes", "body": "Crashes when I open a quiz.",
                                                     "territory": "USA", "createdDate": "2026-11-21T10:00:00-08:00"}}]}


class FakePlay:
    def pages(self, url, key, params=None):
        if "rapidnursingreview" not in url:
            return iter([])
        return iter([{"reviewId": "g1", "comments": [{"userComment": {"text": "Love the timed exams", "starRating": 4,
                                                                      "lastModified": {"seconds": "1795000000"}, "reviewerLanguage": "en"}}]}])


class FakeAppleAds:
    def search_terms(self, cid, start, end):
        if cid != "2145125761":
            return iter([])
        return iter([{"term": "nclex prep", "source": "AUTO", "keyword": None, "adgroup_id": "11", "impressions": 300, "taps": 20, "installs": 4, "spend": 15.0},
                     {"term": "nursing jobs", "source": "AUTO", "keyword": None, "adgroup_id": "11", "impressions": 200, "taps": 18, "installs": 0, "spend": 14.0},
                     {"term": "nclex rn", "source": "TARGETED", "keyword": "nclex rn", "adgroup_id": "11", "impressions": 90, "taps": 5, "installs": 1, "spend": 4.0}])


class StudioChainTests(_OnData):
    """Reviews + search terms -> customer voice -> angles -> (approve) copy + listing -> (approve) launch draft + brief."""

    def setUp(self):
        super().setUp()
        self.con.execute("UPDATE revenue.store_listings SET apple_app_id = '6822172335' WHERE app_id = 'rnr_pro' AND platform = 'apple_app_store'")
        self.con.execute("UPDATE revenue.store_listings SET package_name = 'com.alexkorir.rapidnursingreview' WHERE app_id = 'rnr_pro' AND platform = 'google_play'")
        self.con.execute("INSERT INTO control.ad_entities VALUES ('Apple Search Ads', 'campaign', 'campaign:2145125761', 'org', '2145125761', "
                         "'RNR – US – Search Results', NULL, 'RNR – US – Search Results', 'RUNNING', NULL, 'US', 'Rapid Nursing Review', ?, ?)", [NOW, NOW])
        self.rules.app_monthly = {"Rapid Nursing Review": 300.0, "CCRN Adult": 100.0, "NurseBound": 100.0}
        rnr_ids = lambda p: [x["id"] for x in json.loads(p)["reviews"]][:1] or [x["id"] for x in json.loads(p)["search_terms"]][:1]  # noqa: E731
        self.claude = FakeClaude(answers={
            "customer_voice": lambda p: {"likes": [{"point": "Rationales help", "evidence": rnr_ids(p)}],
                                         "complaints": [{"point": "Made up", "evidence": ["apple_app_store:nope"]}]},
            "angle_writer": lambda p: {"angles": [{"title": "Study on your breaks", "promise": "Short quizzes with rationales.",
                                                   "audience": "Working nursing students", "evidence": rnr_ids(p)},
                                                  {"title": "Invented", "promise": "x", "audience": "y", "evidence": ["term:not real"]}]},
            "ad_copy": {"meta": {"primary_text": ["Quiz yourself on your break with clear rationales."], "headline": ["NCLEX prep on breaks"],
                                 "description": ["Short quizzes"]},
                        "google_app": {"headline": ["NCLEX prep on breaks", "Quiz with rationales"], "description": ["Short quizzes that explain every answer."]},
                        "apple_ads": {"keywords": ["nclex quiz", "nclex rn"]}},
            "listing_writer": {"app_store_promotional_text": "Short NCLEX quizzes with clear rationales.", "play_short_description": "NCLEX quizzes with rationales"},
            "creative_briefs": {"images": [{"scene": "Nurse in scrubs on a break with a phone", "overlay": "Study on your break"}],
                                "video": {"scenes": [{"seconds": "0-3", "shows": "Break room", "on_screen_text": "5 minutes free?"}]},
                                "voiceover": "Got a break? Take a quick quiz."},
        })
        self.clients = {"apple_store": FakeAppleStore(), "google_play": FakePlay(), "apple_ads": FakeAppleAds(), "meta_get": None}

    def run_team(self, env=None, llm_=None, transport=None, now=NOW):
        t = transport or alerts.FakeTransport()
        from unittest import mock
        with mock.patch.object(brain, "_build_client", lambda name, env: self.clients.get(name)):
            return brain.run(self.con, self.rules, environ={**ENV, **(env or {})}, llm=llm_ or self.claude, transport=t,
                             clock=lambda: now), t

    def test_full_chain_with_sign_off_between_steps(self):
        from agents import studio
        res, t = self.run_team()
        out = {k: v.get("output") for k, v in res["agents"].items()}
        self.assertEqual({k for k, v in res["agents"].items() if v["status"] != "ok"}, set(), res)
        self.assertEqual(self.con.execute("SELECT count(*) FROM control.reviews").fetchone()[0], 3)
        self.assertEqual(out["review_miner"]["fetch"]["new"], 3)
        self.assertEqual([x["term"] for x in out["search_ad_watch"]["add_as_keyword"]], ["nclex prep"])
        self.assertEqual([x["term"] for x in out["search_ad_watch"]["negative_candidates"]], ["nursing jobs"])
        self.assertEqual(out["competitor_ad_spy"]["status"], "off")
        voice = studio.items(self.con, kind="voice")
        self.assertEqual(len(voice), 1)
        self.assertEqual(voice[0]["content"]["complaints"], [])                    # invented evidence dropped
        angles = studio.items(self.con, kind="angle")
        self.assertEqual([a["title"] for a in angles], ["Study on your breaks"])  # one per app had evidence: RNR only
        self.assertEqual(out["ad_copy"]["written"], 0)                             # nothing approved yet
        self.assertIn("Studio", t.sent[0])

        studio.decide(self.con, angles[0]["item_id"], True)
        later = NOW + dt.timedelta(hours=4)
        res, _ = self.run_team(now=later)
        copy = studio.items(self.con, kind="copy")
        self.assertEqual(len(copy), 1)
        self.assertEqual(copy[0]["parent_id"], angles[0]["item_id"])
        self.assertEqual(len(studio.items(self.con, kind="listing")), 1)
        self.assertEqual(studio.items(self.con, kind="draft"), [])                 # copy not approved yet
        self.assertEqual(self.con.execute("SELECT count(*) FROM control.studio_items WHERE kind = 'angle'").fetchone()[0], 1)  # weekly: not rewritten

        studio.decide(self.con, copy[0]["item_id"], True)
        res, _ = self.run_team(now=later + dt.timedelta(hours=4))
        draft = studio.items(self.con, kind="draft")[0]["content"]
        self.assertIn("nothing is created", draft["launch"])
        self.assertEqual(draft["errors"], [])
        self.assertGreater(draft["test_budget_daily"], 0)
        added = [k["text"] for k in draft["apple_ads"]["add_keywords"]]
        self.assertIn("nclex quiz", added)
        self.assertIn("nclex prep", added)                                         # from Search Ad Watch
        self.assertEqual(draft["apple_ads"]["add_negative_keywords"], ["nursing jobs"])
        self.assertEqual(draft["apple_ads"]["campaign"]["campaign_id"], "2145125761")
        brief = studio.items(self.con, kind="brief")[0]
        self.assertTrue(any("number" in f for f in brief["flags"]))               # "5 minutes free?" is flagged
        self.assertIn("Layout mockups", brief["content"]["generation"])
        self.assertEqual([m["shape"] for m in brief["content"]["mockups"]], ["1x1", "4x5", "9x16"])
        self.assertEqual(len(brief["content"]["sizes"]), 8)

    def test_unconfigured_sources_are_reported_not_hidden(self):
        self.clients = {}
        res, _ = self.run_team(llm_=FakeClaude())
        rm = res["agents"]["review_miner"]["output"]["fetch"]
        self.assertEqual((rm["apple_app_store"], rm["google_play"]), ("not configured", "not configured"))
        self.assertEqual(res["agents"]["search_ad_watch"]["output"]["fetch"], "Apple Ads not configured")

    def test_decide_rules(self):
        from agents import studio
        iid = studio.add(self.con, "angle", "NurseBound", "test", "t", {"promise": "p"})
        self.assertEqual(studio.decide(self.con, iid, False)["status"], "rejected")
        self.assertEqual(studio.decide(self.con, iid, True)["status"], "approved")
        self.con.execute("UPDATE control.studio_items SET status = 'superseded' WHERE item_id = ?", [iid])
        with self.assertRaises(ValueError):
            studio.decide(self.con, iid, True)



# --------------------------------------------------------------------------- #
# Phases 5-10: profit, watchdog, outcomes, checklist, mockups, books
# --------------------------------------------------------------------------- #
class PhasesPureTests(unittest.TestCase):
    APPS = ["Rapid Nursing Review", "CCRN Adult", "NurseBound"]

    def test_value_per_download_and_scores(self):
        from agents import profit
        rows = [("rnr", "USD", "download", 40, None), ("rnr", "USD", "subscription", 2, 30.0), ("rnr", "USD", "refund", -1, -10.0),
                ("rnr", "EUR", "subscription", 1, 9.0), ("ccrn", None, "download", 5, None), ("other", "USD", "download", 99, 99.0)]
        v = profit.value_per_download(rows, {"rnr": "Rapid Nursing Review", "ccrn": "CCRN Adult"}, self.APPS, "USD")
        self.assertEqual(v["Rapid Nursing Review"]["value_per_download"], 0.5)        # (30 - 10) / 40
        self.assertEqual(v["Rapid Nursing Review"]["other_currency_lines"], 1)
        self.assertEqual(v["Rapid Nursing Review"]["confidence"], "ok")
        self.assertEqual((v["CCRN Adult"]["value_per_download"], v["CCRN Adult"]["confidence"]), (0.0, "early"))
        self.assertIsNone(v["NurseBound"]["value_per_download"])
        ads = profit.score_ads([{"channel": "Apple Search Ads", "entity_id": "k1", "keyword": "nclex", "spend": 4.0, "installs": 10, "taps": 20},
                                {"channel": "Apple Search Ads", "entity_id": "k1", "keyword": "nclex", "spend": 1.0, "installs": 0, "taps": 2},
                                {"channel": "Meta Ads", "entity_id": "a", "spend": 3.0, "installs": 1, "taps": 9, "currency": "EUR"}],
                               v, lambda r: "Rapid Nursing Review", "USD")
        self.assertEqual(len(ads), 1)                                                   # the EUR row is not mixed in
        self.assertEqual((ads[0]["spend"], ads[0]["est_value"], ads[0]["est_return"]), (5.0, 5.0, 1.0))

    def test_watchdog_diff_and_message(self):
        from agents import watchdog
        prev = {"a": "old A", "b": "old B"}
        new, cleared = watchdog.diff(prev, [{"key": "b", "severity": "warning", "text": "B"}, {"key": "c", "severity": "info", "text": "C"}])
        self.assertEqual([i["key"] for i in new], ["c"])
        self.assertEqual(cleared, ["old A"])
        sev, title, body = watchdog.message(new, cleared)
        self.assertEqual(sev, "info")
        self.assertIn("1 new", title)
        self.assertIn("✅ Cleared: old A", body)

    def test_outcome_verdicts(self):
        from agents.outcomes import verdict
        self.assertEqual(verdict(6.5, 2.5, lower_is_better=True), "better")
        self.assertEqual(verdict(2.0, 2.2, lower_is_better=True), "flat")
        self.assertEqual(verdict(2.0, 3.0, lower_is_better=True), "worse")
        self.assertEqual(verdict(10, 14, lower_is_better=False), "better")
        self.assertEqual(verdict(None, 3, lower_is_better=True), "no_data")

    def test_checklist(self):
        from agents import checklist
        it = {"kind": "draft", "flags": ["Contains a number"], "content": {
            "currency": "USD", "errors": [], "test_budget_daily": 5.0,
            "apple_ads": {"campaign": {"campaign": "RNR – US – Search Results"}, "add_keywords": [
                {"text": "nclex quiz", "suggested_max_cpt": 1.25}, {"text": "nclex prep", "suggested_max_cpt": 1.25}],
                "add_negative_keywords": ["nursing jobs"]},
            "meta": {"primary_text": "Quiz on your break", "headline": "NCLEX prep", "countries": ["US"], "daily_budget": 5.0},
            "google_app": {"headlines": ["A", "B"], "descriptions": ["D"], "countries": ["US"], "daily_budget": 5.0}}}
        steps = checklist.build(it)
        self.assertEqual(steps[0]["platform"], "First")
        kw = next(s for s in steps if s["copy"] == "nclex quiz\nnclex prep")
        self.assertIn("$1.25", kw["detail"])
        self.assertTrue(any(s["copy"] == "nursing jobs" for s in steps))
        self.assertEqual({s["platform"] for s in steps}, {"First", "Apple Ads", "Meta", "Google Ads", "After"})
        self.assertEqual(checklist.build({"kind": "angle", "content": {}}), [])
        b = checklist.build({"kind": "book", "content": {"harvest": {"Book 1 auto": ["icu nurse book"]}, "negatives": {}}})
        self.assertEqual(b[0]["copy"], "icu nurse book")

    def test_mockups_are_safe_svg(self):
        import xml.etree.ElementTree as ET
        from agents import mockups
        for shape in mockups.SHAPES:
            svg = mockups.render("NurseBound", 'Study <script>alert(1)</script> & "pass"', "A nurse on a break", shape)
            root = ET.fromstring(svg)                                       # well-formed
            self.assertTrue(root.tag.endswith("svg"))
            self.assertNotIn("<script", svg)
        self.assertEqual(len(mockups.for_brief({"images": [{}, {}]})), 6)


class FakeHTTP:
    """Records calls; answers like Login with Amazon and the Amazon Ads API."""
    def __init__(self, dup=False):
        self.calls, self.dup = [], dup

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data or json, headers))
        if "o2/token" in url:
            return Resp({"access_token": "tok"})
        if self.dup:
            return Resp({"detail": "The Request is a duplicate of : rep-9"}, 425)
        return Resp({"reportId": "rep-1", "status": "PENDING"})

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None, headers))
        return Resp([{"profileId": 77, "countryCode": "US"}])


class AmazonClientTests(unittest.TestCase):
    ENV = {"AMAZON_ADS_CLIENT_ID": "cid", "AMAZON_ADS_CLIENT_SECRET": "sec", "AMAZON_ADS_REFRESH_TOKEN": "ref"}

    def test_configured_token_headers_and_duplicates(self):
        from agents import books
        self.assertFalse(books.configured({}))
        self.assertTrue(books.configured(self.ENV))
        http = FakeHTTP()
        c = books.AmazonAds(self.ENV, http=http)
        self.assertEqual(c.profiles()[0]["profileId"], 77)
        c.profile_id = "77"
        rid = c.request_report("x", TODAY - dt.timedelta(days=14), TODAY, "spCampaigns", ["campaign"], books.CAMPAIGN_COLS)
        self.assertEqual(rid, "rep-1")
        _, url, body, headers = http.calls[-1]
        self.assertEqual(url, "https://advertising-api.amazon.com/reporting/reports")
        self.assertEqual((headers["Amazon-Advertising-API-Scope"], headers["Authorization"]), ("77", "Bearer tok"))
        self.assertEqual(body["configuration"]["reportTypeId"], "spCampaigns")
        self.assertEqual(sum(1 for m, u, *_ in http.calls if "o2/token" in u), 1)       # token fetched once
        dup = books.AmazonAds({**self.ENV, "AMAZON_ADS_REGION": "eu"}, http=FakeHTTP(dup=True))
        dup.profile_id = "1"
        self.assertEqual(dup.request_report("x", TODAY, TODAY, "spSearchTerm", ["searchTerm"], books.TERM_COLS), "rep-9")
        self.assertTrue(dup.host.endswith("-eu.amazon.com"))


class FakeAmazon:
    profile_id = "77"

    def __init__(self):
        self.asked = []

    def profiles(self):
        return [{"profileId": 77}]

    def request_report(self, name, start, end, rtype, group_by, columns):
        self.asked.append(rtype)
        return "id-" + rtype

    def report(self, rid):
        return {"status": "COMPLETED", "url": "https://s3/" + rid}

    def download(self, url):
        d = (TODAY - dt.timedelta(days=2)).isoformat()
        if url.endswith("spCampaigns"):
            return [{"date": d, "campaignId": 5, "campaignName": "Book 1 auto", "impressions": 900, "clicks": 40, "cost": 20.0,
                     "purchases7d": 3, "sales7d": 29.97, "unitsSoldClicks7d": 3, "kindleEditionNormalizedPagesRead14d": 410}]
        return [{"date": d, "campaignId": 5, "campaignName": "Book 1 auto", "adGroupId": 6, "keyword": "", "searchTerm": "ICU nurse book",
                 "impressions": 100, "clicks": 6, "cost": 3.0, "purchases7d": 2, "sales7d": 19.98},
                {"date": d, "campaignId": 5, "campaignName": "Book 1 auto", "adGroupId": 6, "keyword": "", "searchTerm": "free nursing pdf",
                 "impressions": 300, "clicks": 22, "cost": 9.0, "purchases7d": 0, "sales7d": 0},
                {"date": d, "campaignId": 5, "campaignName": "Book 1 auto", "adGroupId": 6, "keyword": "", "searchTerm": "b0abc12345",
                 "impressions": 50, "clicks": 2, "cost": 1.0, "purchases7d": 1, "sales7d": 9.99}]


class PhasesOnDataTests(_OnData):
    def _run(self, agents, env=None, clients=None, now=NOW, transport=None):
        from unittest import mock
        t = transport or alerts.FakeTransport()
        team = {a.key: a for a in brain.team()}
        with mock.patch.object(brain, "_build_client", lambda name, env: (clients or {}).get(name)):
            res = brain.run(self.con, self.rules, environ={**ENV, **(env or {})}, llm=False, transport=t, clock=lambda: now,
                            agents=[team[k] for k in agents])
        return res, t

    def test_new_agents_in_the_full_run(self):
        res, t = self.run_team()
        out = {k: v["output"] for k, v in res["agents"].items()}
        pt = out["profit_tracker"]
        rnr = pt["apps"]["Rapid Nursing Review"]
        self.assertEqual((rnr["downloads"], rnr["proceeds"], rnr["value_per_download"]), (40, 21.0, 0.53))
        self.assertTrue(any(x["name"] == "kw k1" for x in pt["earning"] + pt["losing"]) or pt["totals_28d"]["spend"] > 0)
        self.assertEqual(out["book_ads"]["status"], "off")
        self.assertEqual(out["watchdog"]["issues"], [])
        self.assertEqual(out["outcome_tracker"]["measured_now"], 0)

    def test_watchdog_announces_once_and_when_cleared(self):
        for cid, seen in (("1", NOW), ("2", NOW - dt.timedelta(days=3))):
            self.con.execute("INSERT INTO control.ad_entities VALUES ('Apple Search Ads', 'campaign', ?, 'org', ?, ?, NULL, ?, 'RUNNING', "
                             "NULL, 'US', NULL, ?, ?)", [f"campaign:{cid}", cid, f"Camp {cid}", f"Camp {cid}", seen, seen])
        res, t = self._run(["watchdog"])
        self.assertEqual(len(t.sent), 1)
        self.assertIn("Camp 2", t.sent[0])
        res, t = self._run(["watchdog"], now=NOW + dt.timedelta(hours=4))
        self.assertEqual(t.sent, [])                                               # same issue: not repeated
        self.con.execute("UPDATE control.ad_entities SET last_seen = ? WHERE campaign_id = '2'", [NOW + dt.timedelta(hours=8)])
        res, t = self._run(["watchdog"], now=NOW + dt.timedelta(hours=8))
        self.assertIn("Cleared", t.sent[0])
        self.rules.alerts = {**self.rules.alerts, "watchdog": False}
        self.con.execute("UPDATE control.ad_entities SET last_seen = ? WHERE campaign_id = '2'", [NOW - dt.timedelta(days=3)])
        res, t = self._run(["watchdog"], now=NOW + dt.timedelta(hours=12))
        self.assertEqual(res["agents"]["watchdog"]["output"]["delivery"], "disabled")
        self.assertEqual(t.sent, [])

    def test_outcome_of_an_approved_draft(self):
        from agents import studio, outcomes
        iid = studio.add(self.con, "draft", "Rapid Nursing Review", "launch_drafter", "Launch draft · x",
                         {"apple_ads": {"add_keywords": [{"text": "kw k1"}], "add_negative_keywords": []}}, now=NOW)
        studio.decide(self.con, iid, True, now=NOW - dt.timedelta(days=10))
        res, _ = self._run(["outcome_tracker"])
        ot = res["agents"]["outcome_tracker"]["output"]
        self.assertEqual(ot["measured_now"], 1)                                    # 7 days due, 14 not yet
        v, before, after = self.con.execute("SELECT verdict, before, after FROM control.outcomes").fetchone()
        self.assertEqual(v, "better")                                              # $2.50 per install vs the app's $6.50 before
        self.assertEqual(json.loads(after)["cpi"], 2.5)
        self.assertEqual(json.loads(before)["cpi"], 6.5)
        res, _ = self._run(["outcome_tracker"])
        self.assertEqual(res["agents"]["outcome_tracker"]["output"]["measured_now"], 0)   # written once
        self.assertEqual(ot["decisions"]["draft"]["approved"], 1)
        rej = studio.add(self.con, "angle", "Rapid Nursing Review", "angle_writer", "Pass guaranteed", {}, now=NOW)
        studio.decide(self.con, rej, False)
        self.assertEqual(outcomes.past_decisions(self.con, "Rapid Nursing Review")["rejected"], ["Pass guaranteed"])

    def test_books_request_then_download_then_draft(self):
        from agents import studio
        env = {"AMAZON_ADS_CLIENT_ID": "c", "AMAZON_ADS_CLIENT_SECRET": "s", "AMAZON_ADS_REFRESH_TOKEN": "r"}
        fake = FakeAmazon()
        res, _ = self._run(["book_ads"], env=env, clients={"amazon_ads": fake})
        out = res["agents"]["book_ads"]["output"]
        self.assertEqual(out["fetch"]["requested"], ["campaigns", "terms"])
        self.assertEqual(sorted(fake.asked), ["spCampaigns", "spSearchTerm"])
        res, _ = self._run(["book_ads"], env=env, clients={"amazon_ads": fake}, now=NOW + dt.timedelta(hours=4))
        out = res["agents"]["book_ads"]["output"]
        self.assertEqual(out["fetch"]["downloaded"], {"campaigns": 1, "terms": 3})
        self.assertEqual((out["7d"]["spend"], out["7d"]["orders"], out["7d"]["kenp_pages"]), (20.0, 3, 410))
        self.assertAlmostEqual(out["7d"]["acos"], round(20 / 29.97, 3))
        self.assertEqual(out["harvest"], {"Book 1 auto": ["icu nurse book"]})       # the ASIN row is not a phrase
        self.assertEqual(out["negatives"], {"Book 1 auto": ["free nursing pdf"]})
        book = studio.items(self.con, kind="book")
        self.assertEqual(len(book), 1)
        self.assertIn("nothing is sent to Amazon", book[0]["content"]["launch"])
        # book spend never reaches the app ads table the budgets and engine read
        self.assertEqual(self.con.execute("SELECT count(*) FROM ads.ad_performance WHERE channel ILIKE '%amazon%'").fetchone()[0], 0)
