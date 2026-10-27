"""Trading safety-net alerts (dlt_pipeline.check_trading): the kill-switch look-back must cover the whole gap
between scheduled runs, so a switch turned on between two 4-hourly runs still alerts once."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    import duckdb
except ImportError:
    sys.path.insert(0, HERE)
    import duckdb_cli_shim as duckdb  # type: ignore

import dlt_pipeline
from dlt_pipeline import Rules

# A Wednesday, so the FX market is open.
NOW = dt.datetime(2026, 10, 28, 12, 7)


def make_con(heartbeat_age_min: float, kill_switch_age_h: float | None, mode: str = "live"):
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA trading")
    con.execute("CREATE TABLE trading.heartbeat (bot_id VARCHAR, ts TIMESTAMP, mode VARCHAR, status VARCHAR, message VARCHAR)")
    con.execute("CREATE TABLE trading.risk (bot_id VARCHAR, ts TIMESTAMP, kill_switch BOOLEAN, notes VARCHAR)")
    con.execute("INSERT INTO trading.heartbeat VALUES (?, ?, ?, 'running', NULL)",
                ["fx1", NOW - dt.timedelta(minutes=heartbeat_age_min), mode])
    if kill_switch_age_h is not None:
        con.execute("INSERT INTO trading.risk VALUES (?, ?, true, 'daily loss limit')",
                    ["fx1", NOW - dt.timedelta(hours=kill_switch_age_h)])
    return con


class KillSwitchWindow(unittest.TestCase):
    def alerts(self, con, every_hours: str) -> list[str]:
        sent: list[str] = []
        with mock.patch.dict(os.environ, {"WORKER_EVERY_HOURS": every_hours}), \
                mock.patch.object(dlt_pipeline, "telegram", side_effect=lambda text, rules=None: sent.append(text) or True):
            dlt_pipeline.check_trading(con, Rules(), now=NOW)
        return sent

    def test_switch_set_between_four_hourly_runs_alerts(self):
        # Turned on 3 hours ago: the previous run (4 h ago) couldn't see it, so this run must alert.
        sent = self.alerts(make_con(heartbeat_age_min=1, kill_switch_age_h=3), every_hours="4")
        self.assertEqual(len([t for t in sent if "kill switch is ON" in t]), 1)

    def test_switch_already_reported_by_previous_run_stays_quiet(self):
        sent = self.alerts(make_con(heartbeat_age_min=1, kill_switch_age_h=5), every_hours="4")
        self.assertFalse([t for t in sent if "kill switch" in t])

    def test_hourly_schedule_keeps_one_hour_window(self):
        self.assertTrue([t for t in self.alerts(make_con(1, 0.5), every_hours="1") if "kill switch" in t])
        self.assertFalse([t for t in self.alerts(make_con(1, 3), every_hours="1") if "kill switch" in t])

    def test_quiet_bot_alerts_and_paper_bots_are_ignored(self):
        self.assertTrue([t for t in self.alerts(make_con(heartbeat_age_min=45, kill_switch_age_h=None), "4") if "hasn't reported" in t])
        self.assertEqual(self.alerts(make_con(heartbeat_age_min=45, kill_switch_age_h=1, mode="paper"), "4"), [])


if __name__ == "__main__":
    unittest.main()
