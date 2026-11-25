"""
The Brain: runs the agent team in order, passes each agent's output to the ones that need it, records everything.

  python -m agents.brain            run the team once (what the scheduled job does) and print each agent's result
  python -m agents.brain --preview  print the morning page it would send, without sending or saving it

An agent is a function ctx -> dict. Its dict is stored as JSON in control.agent_outputs and is available to later
agents as ctx.out["<key>"]. An agent whose inputs failed is skipped; one that raises is recorded as failed and the
rest carry on. Brain.run never raises.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from control.schema import SCHEMA

log = logging.getLogger("adbot.agents")

DEFAULT_TZ = "America/Los_Angeles"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _json(v) -> str:
    def dflt(o):
        if isinstance(o, (dt.date, dt.datetime)):
            return o.isoformat()
        if isinstance(o, set):
            return sorted(o)
        return str(o)
    return json.dumps(v, default=dflt, sort_keys=True)


@dataclass
class Agent:
    key: str                     # stable id, used for handoffs and storage
    name: str                    # shown to people
    module: str                  # which part of the team it belongs to
    does: str                    # one line: what it works out
    run: Callable[["Context"], dict]
    needs: tuple = ()            # keys of agents whose output it reads


@dataclass
class Context:
    con: object
    rules: object
    now: dt.datetime                       # UTC, naive
    environ: dict
    llm: object = None                     # agents.llm.Claude or None
    transport: object = None               # control.alerts transport for the morning page
    send: bool = True                      # False: build the morning page but don't send or save it
    out: dict = field(default_factory=dict)
    clients: dict = field(default_factory=dict)  # read-only platform clients; tests put fakes here
    _rows: Optional[list] = None

    def client(self, name: str):
        """A read-only client: apple_store (App Store Connect), google_play, apple_ads, meta_get. None if not configured."""
        if name not in self.clients:
            self.clients[name] = _build_client(name, self.environ)
        return self.clients[name]

    @property
    def today(self) -> dt.date:
        return self.now.date()

    @property
    def tz(self) -> str:
        return (self.environ.get("AGENTS_TZ") or DEFAULT_TZ).strip()

    @property
    def local_now(self) -> dt.datetime:
        try:
            from zoneinfo import ZoneInfo
            return self.now.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo(self.tz)).replace(tzinfo=None)
        except Exception:  # noqa: BLE001 - unknown zone: UTC
            return self.now

    def ad_rows(self) -> list[dict]:
        """Ad performance rows (newest revision of each day) from the start of last month to today, loaded once."""
        if self._rows is None:
            from control import engine as ceng
            start = (self.today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
            start = min(start, self.today - dt.timedelta(days=35))
            self._rows = ceng.load_rows(self.con, start, self.today)
        return self._rows


def _build_client(name: str, env: dict):
    try:
        if name == "apple_store":
            from revenue import apple
            creds = apple.Creds.from_env(env)
            return apple.Client(creds) if creds else None
        if name == "google_play":
            from revenue import google_play
            sa = google_play.load_service_account(env)
            return google_play.Client(sa) if sa else None
        if name == "apple_ads":
            import dlt_pipeline as bot
            a = bot.AppleSearchAds()
            return a if a.configured else None
        if name == "meta_get":
            token = (env.get("META_ACCESS_TOKEN") or "").strip()
            if not token:
                return None
            import requests
            version = env.get("META_API_VERSION") or "v21.0"

            def get(path, params):
                r = requests.get(f"https://graph.facebook.com/{version}/{path}", params={**params, "access_token": token}, timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(f"Meta HTTP {r.status_code}: {r.text[:200]}")
                return r.json()
            return get
    except Exception:  # noqa: BLE001 - a missing or broken key just means that source is skipped
        log.exception("could not build client %s", name)
    return None


def team() -> list[Agent]:
    from . import changes, creative, departments, launch, money, morning, research, winners, writing
    return [
        Agent("department_check", "Department Check", "Brain", "Checks every data source and what needs attention",
              departments.department_check),
        Agent("spend_watch", "Spend Watch", "Launch & Optimize", "What you spent: yesterday, month to date, against the budget",
              money.spend_watch),
        Agent("real_return", "Real Return", "Profit Tracking", "What sold vs what you spent, per app",
              money.real_return),
        Agent("winner_spotter", "Winner Spotter", "Profit Tracking", "Cheapest installs, and spend with nothing to show",
              winners.winner_spotter),
        Agent("change_review", "Change Review", "Launch & Optimize", "Changes AdBot proposed that need your OK",
              changes.change_review),
        # Research (phase 2)
        Agent("review_miner", "Review Miner", "Research", "Your App Store and Google Play reviews", research.review_miner),
        Agent("search_ad_watch", "Search Ad Watch", "Research", "Search terms behind your Apple Ads: add or exclude",
              research.search_ad_watch),
        Agent("competitor_ad_spy", "Competitor Ad Spy", "Research", "Competitors' active ads (Meta Ad Library)",
              research.competitor_ad_spy),
        Agent("customer_voice", "Customer Voice", "Research", "What customers like, dislike and the words they use",
              research.customer_voice),
        Agent("angle_writer", "Angle Writer", "Research", "Three reasons to download, backed by evidence", writing.angle_writer),
        # Creative and launch (phases 2-4); everything is a draft for your review
        Agent("ad_copy", "Ad Copy", "Creative Studio", "Copy for each approved angle, within platform limits", writing.ad_copy),
        Agent("listing_writer", "Listing Writer", "Landing", "Store listing text for each approved angle", writing.listing_writer),
        Agent("test_budget", "Test Budget", "Launch & Optimize", "What a new test could spend per day", launch.test_budget),
        Agent("launch_drafter", "Launch Drafter", "Launch & Optimize", "Campaign drafts from approved copy (never launched)",
              launch.launch_drafter, needs=("test_budget",)),
        Agent("resizer", "Resizer", "Creative Studio", "Sizes each placement needs", creative.resizer),
        Agent("creative_briefs", "Creative Briefs", "Creative Studio", "Image, video and voiceover briefs for approved copy",
              creative.creative_briefs),
        Agent("morning_page", "Morning Page", "Brain", "One page every morning on Telegram",
              morning.morning_page, needs=("department_check", "spend_watch", "real_return", "winner_spotter", "change_review")),
    ]


def run(con, rules, *, environ=None, llm=None, transport=None, clock: Callable[[], dt.datetime] = _now,
        send: bool = True, agents: Optional[list[Agent]] = None, record: bool = True) -> dict:
    """Run the team once. Returns {run_id, status, agents: {key: {status, output|error}}}. Never raises."""
    env = dict(os.environ if environ is None else environ)
    run_id = uuid.uuid4().hex
    results: dict = {}
    try:
        if llm is None:
            from . import llm as llm_mod
            llm = llm_mod.from_env(con, env, clock)
        if transport is None:
            from control import alerts
            transport = alerts.transport_from_env(rules, env)
        ctx = Context(con=con, rules=rules, now=clock(), environ=env, llm=llm, transport=transport, send=send)
        if record:
            con.execute(f"INSERT INTO {SCHEMA}.agent_runs VALUES (?, ?, NULL, 'running', NULL)", [run_id, ctx.now])
        for a in (agents if agents is not None else team()):
            started = clock()
            missing = [n for n in a.needs if results.get(n, {}).get("status") != "ok"]
            if missing:
                res = {"status": "skipped", "error": "needs " + ", ".join(missing)}
            else:
                try:
                    out = a.run(ctx)
                    ctx.out[a.key] = out
                    res = {"status": "ok", "output": out}
                except Exception as e:  # noqa: BLE001 - recorded, the others carry on
                    log.exception("agent %s failed", a.key)
                    res = {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}
            results[a.key] = res
            if record:
                con.execute(f"INSERT INTO {SCHEMA}.agent_outputs VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [run_id, a.key, started, clock(), res["status"],
                             _json(res["output"]) if "output" in res else None, res.get("error")])
        statuses = {k: v["status"] for k, v in results.items()}
        status = "succeeded" if all(s != "failed" for s in statuses.values()) else (
            "failed" if all(s == "failed" for s in statuses.values()) else "partial")
        if record:
            con.execute(f"UPDATE {SCHEMA}.agent_runs SET finished_at = ?, status = ?, summary = ? WHERE run_id = ?",
                        [clock(), status, _json(statuses), run_id])
        return {"run_id": run_id, "status": status, "agents": results}
    except Exception as e:  # noqa: BLE001 - the agents never stop the ad bot
        log.exception("agent run failed")
        return {"run_id": run_id, "status": "failed", "error": f"{type(e).__name__}: {e}", "agents": results}


def last_runs(con, limit: int = 10) -> list[dict]:
    try:
        cur = con.execute(f"SELECT run_id, started_at, finished_at, status, summary FROM {SCHEMA}.agent_runs "
                          f"ORDER BY started_at DESC LIMIT {int(limit)}")
    except Exception:  # noqa: BLE001
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preview", action="store_true", help="print the morning page; don't send or save anything")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    import dlt_pipeline as bot
    con = bot.connect()
    rules = bot.load_rules(con)
    env = dict(os.environ)
    if args.preview:
        env["AGENTS_FORCE_MORNING"] = "1"
    res = run(con, rules, environ=env, send=not args.preview, record=not args.preview)
    if args.preview:
        page = ((res["agents"].get("morning_page") or {}).get("output") or {}).get("text")
        print(page or json.dumps({k: v.get("error") for k, v in res["agents"].items()}, indent=2))
    else:
        for k, v in res["agents"].items():
            print(f"{k:18} {v['status']:8} {v.get('error') or ''}")
    return 0 if res["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
