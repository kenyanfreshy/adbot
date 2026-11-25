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
    def __init__(self, text="Spend is under the limit and RNR leads on proceeds."):
        self.text, self.calls = text, []

    def complete(self, agent, system, prompt, max_tokens=500):
        self.calls.append((agent, prompt))
        return self.text


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
        from agents import changes, departments, money, winners
        for m in (brain, changes, departments, money, morning, winners, llm):
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


class TeamOnDataTests(unittest.TestCase):
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
        return brain.run(self.con, self.rules, environ={**ENV, **(env or {})}, llm=llm_ if llm_ is not None else False,
                         transport=t, clock=lambda: now), t

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
        self.assertEqual(self.con.execute("SELECT count(*) FROM control.agent_outputs").fetchone()[0], 6)

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
