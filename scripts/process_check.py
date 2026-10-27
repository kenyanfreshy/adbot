"""
Multi-process checks for the control plane: each role below runs in its OWN operating-system process,
started at the same time where the check is about concurrency. No ad platform is contacted (a
file-backed fake platform stands in), no notification is sent, DRY_RUN stays "true" except inside the
fake-platform crash check, which only ever talks to that fake.

    python scripts/process_check.py                          # throw-away local DuckDB file
    python scripts/process_check.py --motherduck adbot_staging    # separate MotherDuck database (MOTHERDUCK_TOKEN)

What "concurrent" means on each target, printed with every result:
  * local DuckDB file, real duckdb package: only one process can hold the file, so simultaneous processes
    are SERIALISED by the operating-system file lock (they retry until they get it). Not true concurrency.
  * local DuckDB file through the CLI test shim: every statement is its own process, so statements from
    different workers interleave; still serialised per statement by the file lock.
  * MotherDuck: real concurrent transactions from separate processes. This is the production setup.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DRY_RUN", "true")
os.environ["ALERT_TRANSPORT"] = "fake"
os.environ.pop("RENDER", None)

LOCKY = ("could not set lock", "conflicting lock", "database is locked", "lock on file", "transaction conflict", "conflict on")


def duck():
    import duckdb
    return duckdb


def driver_label() -> str:
    d = duck()
    return "duckdb CLI shim (test only)" if getattr(d, "__version__", "") == "cli-shim" else f"duckdb {d.__version__}"


class Retrying:
    """Retries a statement that hit the file / transaction lock (another process has it)."""

    def __init__(self, con, stats):
        self.con, self.stats = con, stats

    def execute(self, sql, params=None):
        for i in range(400):
            try:
                return self.con.execute(sql, params or [])
            except Exception as e:  # noqa: BLE001
                if any(k in str(e).lower() for k in LOCKY) and i < 399:
                    self.stats["lock_retries"] = self.stats.get("lock_retries", 0) + 1
                    time.sleep(0.05)
                    continue
                raise

    def close(self):
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, k):
        return getattr(self.con, k)


def open_con(target: dict, stats: dict):
    d = duck()
    for i in range(600):
        try:
            if target["md"]:
                c = d.connect("md:", config={"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]})
                c.execute(f"USE {target['md']}")
            else:
                c = d.connect(target["db"])
            return Retrying(c, stats)
        except Exception as e:  # noqa: BLE001
            if any(k in str(e).lower() for k in LOCKY):
                stats["open_retries"] = stats.get("open_retries", 0) + 1
                time.sleep(0.05)
                continue
            raise
    raise RuntimeError("could not open the database")


# --------------------------------------------------------------------------- #
# A fake ad platform whose state lives in a JSON file, so it survives a crashed process
# --------------------------------------------------------------------------- #
class FileFakePlatform:
    def __init__(self, path, crash_after_write=False):
        self.path, self.crash = path, crash_after_write

    def _load(self):
        with open(self.path) as f:
            return json.load(f)

    def _save(self, s):
        with open(self.path, "w") as f:
            json.dump(s, f)

    def read_state(self, p):
        return dict(self._load()["states"].get(p["entity_key"], {})) or None

    def budget_check(self, p, pre):
        return [] if pre and pre.get("daily_budget") is not None else ["no readable daily budget"]

    def apply(self, p):
        s = self._load()
        st = s["states"].setdefault(p["entity_key"], {})
        if p["action"].startswith("pause_"):
            st["status"] = "PAUSED"
        else:
            st["daily_budget"] = float(p["proposed_value"])
        s["writes"].append({"entity_key": p["entity_key"], "action": p["action"], "pid": os.getpid()})
        self._save(s)
        if self.crash:
            os._exit(9)      # the platform applied it; the worker dies before recording anything


# --------------------------------------------------------------------------- #
# Child roles (each runs in its own process)
# --------------------------------------------------------------------------- #
def child(role: str, target: dict, args: list[str]) -> dict:
    stats: dict = {"pid": os.getpid()}
    from control import engine, state
    con = open_con(target, stats)
    try:
        if role == "lease":
            owner, ttl_min, then = args[0], float(args[1]), args[2]
            try:
                ok = state.acquire(con, "pc_lock", owner, ttl_minutes=ttl_min)
                stats["result"] = "ACQUIRED" if ok else "REFUSED"
            except Exception as e:  # noqa: BLE001 - a write-write conflict is a refusal, never a double hold
                stats["result"], stats["error"] = "REFUSED_CONFLICT", str(e)[:120]
            if stats["result"] == "ACQUIRED" and then == "crash":
                print(json.dumps(stats), flush=True)
                os._exit(9)
            if stats["result"] == "ACQUIRED" and then.startswith("hold"):
                con.close()
                time.sleep(float(then[4:]))
                con = open_con(target, stats)
                stats["still_held"] = state.holds(con, "pc_lock", owner)
                state.release(con, "pc_lock", owner)
        elif role == "reserve":
            worker, use_lease = args[0], args[1] == "lease"
            if use_lease:
                try:
                    got = state.acquire(con, "pc_budget_lock", worker, ttl_minutes=1)
                except Exception:  # noqa: BLE001
                    got = False
                if not got:
                    stats["result"] = "NO_LEASE"
                    return stats
            ctx = engine.ExecContext(con, _Rules(), worker=worker, apps=["Rapid Nursing Review"],
                                     pace={"apps": {"Rapid Nursing Review": {"daily_allowance": 30.0}}},
                                     current_budgets={"Rapid Nursing Review": {"x": 20.0}})            # 10.0 of headroom
            room = engine._headroom(con, ctx, "Rapid Nursing Review")
            time.sleep(0.3)                     # widen the window between check and reserve
            if room is None or 6.0 > room + 0.005:
                stats["result"] = f"NO_ROOM ({room:.2f} left)"
            else:
                con.execute("INSERT INTO control.budget_reservations VALUES (?,?,?,?,?,?,?,?,?)",
                            [f"r-{worker}", "Rapid Nursing Review", ctx.now.date(), "USD", 6.0, f"p-{worker}", worker, ctx.now, "held"])
                stats["result"] = "RESERVED 6.00"
            if use_lease:
                state.release(con, "pc_budget_lock", worker)
        elif role == "web_stop":
            stats["result"] = _web(con, target, "POST", "/api/control/stop", {"reason": "process check"})
        elif role == "web_read":
            stats["result"] = _web(con, target, "GET", "/api/control", None)
        elif role == "worker_cycle":
            import dlt_pipeline as bot
            r = bot.Rules()
            r._cmap = {}
            from control import alerts
            s = engine.run_cycle(con, r, apps=bot.APPS, rule_app_fn=lambda n: bot.app_for(n, r), adapters={},
                                 alerter=alerts.Alerter(con, alerts.FakeTransport(), r), environ={"DRY_RUN": "true"},
                                 worker=f"worker-{os.getpid()}", storage="process-check")
            stats["result"] = {"automation_stopped": s["automation_stopped"]}
        elif role in ("crash_write", "recover"):
            platform = args[0]
            os.environ["DRY_RUN"] = "false"     # only the file-backed fake platform is reachable in this role
            ad = FileFakePlatform(platform, crash_after_write=(role == "crash_write"))
            ctx = engine.ExecContext(con, _Rules(), adapters={"Apple Search Ads": ad}, dry=False, worker=f"w-{os.getpid()}",
                                     apps=["Rapid Nursing Review"], mode="approval")
            if role == "crash_write":
                pid = con.execute("SELECT proposal_id FROM control.proposals WHERE entity_key = 'Apple Search Ads|pc1'").fetchone()[0]
                engine.execute(con, {"proposal_id": pid}, ctx=ctx)
                stats["result"] = "NOT CRASHED (unexpected)"
            else:
                stats["result"] = engine.reconcile(con, {"Apple Search Ads": ad}, ctx.now, ctx)
    finally:
        con.close()
    return stats


class _Rules:
    approval_ttl_hours = 24

    def __getattr__(self, k):
        return None


def _web(con, target, method, path, body):
    """The real Starlette app, in this process, against the shared database."""
    import asyncio
    import dlt_pipeline as bot
    bot.connect = lambda: con
    os.environ["APP_PASSWORD"] = "process-check-only"
    import web
    web._con = None

    def call(m, p, b=None, cookie=None):
        raw = json.dumps(b).encode() if b is not None else b""
        hdr = [(b"host", b"pc"), (b"content-type", b"application/json"), (b"x-adbot-request", b"1")]
        if cookie:
            hdr.append((b"cookie", cookie.encode()))
        out = {"s": None, "h": [], "b": b""}
        sent = False

        async def rcv():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        async def snd(msg):
            if msg["type"] == "http.response.start":
                out["s"], out["h"] = msg["status"], msg["headers"]
            elif msg["type"] == "http.response.body":
                out["b"] += msg.get("body", b"")
        asyncio.run(web.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": m, "scheme": "https",
                             "path": p, "raw_path": p.encode(), "query_string": b"", "headers": hdr, "client": ("127.0.0.1", 1),
                             "server": ("pc", 443), "root_path": ""}, rcv, snd))
        return out["s"], json.loads(out["b"] or b"null"), next((v.decode() for k, v in out["h"] if k == b"set-cookie"), None)
    _, _, ck = call("POST", "/api/login", {"password": "process-check-only"})
    s, d, _ = call(method, path, body, ck.split(";")[0])
    if path == "/api/control":
        st = d.get("automation_stopped") or {}
        return {"status": s, "stop_saved": bool(st.get("persisted_at")), "acknowledged_by": st.get("acknowledged_by")}
    return {"status": s, "persisted_at": (d.get("automation_stopped") or {}).get("persisted_at")}


# --------------------------------------------------------------------------- #
# Parent: start the processes, check the outcome
# --------------------------------------------------------------------------- #
def spawn(role, target, *args):
    env = dict(os.environ)
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", role, "--target", json.dumps(target), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def collect(procs):
    out = []
    for p in procs:
        o, e = p.communicate(timeout=600)
        line = [x for x in o.splitlines() if x.startswith("{")]
        out.append({"exit": p.returncode, **(json.loads(line[-1]) if line else {"stderr": e[-400:]})})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--child")
    ap.add_argument("--target")
    ap.add_argument("--db", default=os.path.join(tempfile.mkdtemp(prefix="adbot-pc-"), "pc.duckdb"))
    ap.add_argument("--motherduck")
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    if a.child:
        print(json.dumps(child(a.child, json.loads(a.target), a.rest), default=str), flush=True)
        return
    if a.motherduck and a.motherduck.lower() == os.environ.get("MOTHERDUCK_DATABASE", "adbot").lower():
        sys.exit("Refusing the production database name; use e.g. --motherduck adbot_staging")
    target = {"md": a.motherduck, "db": a.db}
    mode = ("MotherDuck: real concurrent transactions" if a.motherduck else
            "local file: processes are serialised by the file lock (simultaneous start, not true concurrency)")
    print(f"driver: {driver_label()}\ntarget: {a.motherduck or a.db}\nconcurrency: {mode}\n")
    import control
    import revenue
    stats = {}
    con = open_con(target, stats)
    revenue.ensure(con)
    control.ensure(con)
    for t in ("locks", "budget_reservations", "intents", "executions", "proposals"):
        con.execute(f"DELETE FROM control.{t} WHERE " + {"locks": "name LIKE 'pc_%'", "budget_reservations": "worker LIKE 'pc%'",
                                                          "intents": "entity_key LIKE '%|pc1'", "executions": "proposal_id LIKE 'pc%'",
                                                          "proposals": "entity_key LIKE '%|pc1'"}[t])
    con.execute("DELETE FROM control.control_state WHERE key = 'automation_stop'")
    con.close()
    fails = []

    def report(name, ok, detail):
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {json.dumps(detail, default=str)[:900]}")
        if not ok:
            fails.append(name)

    # 1. lease: 4 processes started together; exactly one may hold it
    res = collect([spawn("lease", target, f"pc{i}", "1", "hold2") for i in range(4)])
    winners = [r for r in res if r.get("result") == "ACQUIRED"]
    report("lease race: 4 simultaneous processes, one holder", len(winners) == 1 and all(r.get("still_held") for r in winners), res)

    # 2. crashed holder: blocks others until the lease expires, then the next worker gets it
    res = collect([spawn("lease", target, "pc-crash", "0.05", "crash")])         # 3-second lease, process dies holding it
    blocked = collect([spawn("lease", target, "pc-next", "1", "now")])
    time.sleep(4)
    later = collect([spawn("lease", target, "pc-later", "1", "now")])
    report("lock expiry after a crashed holder", res[0]["exit"] == 9 and blocked[0]["result"].startswith("REFUSED")
           and later[0]["result"] == "ACQUIRED", {"crashed": res, "while_valid": blocked, "after_expiry": later})

    # 3. budget reservations from separate processes: with the job lease (how the worker runs) and without it
    con = open_con(target, stats)
    con.execute("DELETE FROM control.budget_reservations WHERE worker LIKE 'pc%'")
    con.execute("DELETE FROM control.locks WHERE name = 'pc_budget_lock'")
    con.close()
    res = collect([spawn("reserve", target, f"pc-r{i}", "lease") for i in range(3)])
    con = open_con(target, stats)
    total = con.execute("SELECT coalesce(sum(amount), 0) FROM control.budget_reservations WHERE worker LIKE 'pc-r%'").fetchone()[0]
    con.execute("DELETE FROM control.budget_reservations WHERE worker LIKE 'pc%'")
    con.close()
    report("budget reservations, 3 processes WITH the job lease (headroom 10, each wants 6)", float(total) <= 10.0, {"total": total, "procs": res})
    res = collect([spawn("reserve", target, f"pc-n{i}", "nolease") for i in range(3)])
    con = open_con(target, stats)
    total2 = con.execute("SELECT coalesce(sum(amount), 0) FROM control.budget_reservations WHERE worker LIKE 'pc-n%'").fetchone()[0]
    con.close()
    print(f"INFO  budget reservations WITHOUT the lease (not a production path): total reserved {total2} of 10.0 headroom"
          f"{' -> check-then-insert race reproduced' if float(total2) > 10 else ''}\n      {json.dumps(res, default=str)[:600]}")

    # 4. shared state: the web app (one process) saves a stop; the worker (another process) sees and acknowledges it;
    #    a third process reads it back through the web app
    w = collect([spawn("web_stop", target)])
    k = collect([spawn("worker_cycle", target)])
    r = collect([spawn("web_read", target)])
    report("shared state: web stop -> worker acknowledges -> web reads the acknowledgement",
           w[0].get("result", {}).get("status") == 200 and k[0].get("result", {}).get("automation_stopped") is True
           and r[0].get("result", {}).get("acknowledged_by", "").startswith("worker-"), {"web_stop": w, "worker": k, "web_read": r})
    con = open_con(target, stats)
    from control import engine
    engine.resume(con, "process-check")

    # 5. crash after the platform applied a change, before the worker recorded it; another process recovers
    import dlt_pipeline as bot
    r_ = bot.Rules()
    platform = os.path.join(tempfile.mkdtemp(), "platform.json")
    with open(platform, "w") as f:
        json.dump({"states": {"Apple Search Ads|pc1": {"status": "ACTIVE"}}, "writes": []}, f)
    p = engine._proposal("manual", {"channel": "Apple Search Ads", "entity_key": "Apple Search Ads|pc1", "entity_type": "keyword",
                                    "entity_id": "pc1", "app": "Rapid Nursing Review", "campaign_id": "pc-c", "account_id": "a",
                                    "name": "pc1"}, "pause_keyword", "process check", "manual_override", {})
    p["fingerprint"] = engine.fingerprint(p)
    engine._prepare(p, r_, engine.policy_version(r_))
    now = engine._now()
    engine._insert(con, {**p, "proposal_id": "pc" + os.urandom(15).hex(), "created_at": now, "run_id": "pc", "status": "approved",
                         "metrics": p["metrics_json"], "blockers": "[]", "decided_by": "admin", "decided_at": now,
                         "approved_hash": p["proposal_hash"], "approval_expires_at": now + __import__("datetime").timedelta(hours=1)})
    con.close()
    c1 = collect([spawn("crash_write", target, platform)])
    time.sleep(1)
    c2 = collect([spawn("recover", target, platform)])
    with open(platform) as f:
        plat = json.load(f)
    con = open_con(target, stats)
    st = con.execute("SELECT status FROM control.proposals WHERE entity_key = 'Apple Search Ads|pc1'").fetchone()[0]
    it = con.execute("SELECT state FROM control.intents WHERE entity_key = 'Apple Search Ads|pc1'").fetchall()
    con.close()
    report("crash after remote success: another process reconciles by reading, nothing re-sent",
           c1[0]["exit"] == 9 and st == "verified" and len(plat["writes"]) == 1 and it == [("verified",)],
           {"crash_exit": c1[0]["exit"], "recover": c2, "status": st, "intent": it, "platform_writes": len(plat["writes"])})

    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASSED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
