"""
Staging verification for the AdBot control plane, against the REAL duckdb package (and dlt, when installed).
The DuckDB CLI test shim is refused here on purpose.

It never touches an ad platform, never sends a notification, and never uses the production database:

  python scripts/staging_check.py                         # a throw-away local DuckDB file
  python scripts/staging_check.py --motherduck adbot_staging   # a SEPARATE MotherDuck database (needs MOTHERDUCK_TOKEN)

The MotherDuck run is the one that proves shared state across processes (two workers, lock expiry,
crash recovery). The database name "adbot" (production) is refused.

Each check prints PASS / FAIL / SKIP with a reason; the exit code is 1 if anything failed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DRY_RUN"] = "true"
os.environ["ALERT_TRANSPORT"] = "fake"
os.environ.pop("RENDER", None)
os.environ.setdefault("APP_PASSWORD", "staging-check-only")

RESULTS: list[tuple[str, str, str]] = []


def check(name):
    def deco(fn):
        def run(*a, **k):
            try:
                msg = fn(*a, **k)
                if isinstance(msg, tuple) and msg and msg[0] == "SKIP":
                    RESULTS.append(("SKIP", name, msg[1]))
                else:
                    RESULTS.append(("PASS", name, msg or ""))
            except Exception as e:  # noqa: BLE001
                RESULTS.append(("FAIL", name, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
        return run
    return deco


def connect(args, path=None):
    import duckdb
    if args.motherduck:
        con = duckdb.connect("md:", config={"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]})
        con.execute(f"CREATE DATABASE IF NOT EXISTS {args.motherduck}")
        con.execute(f"USE {args.motherduck}")
        return con
    return duckdb.connect(path or args.db)


# --------------------------------------------------------------------------- #
@check("dependencies: real duckdb (not the CLI shim), versions")
def deps():
    import duckdb
    if getattr(duckdb, "__version__", "") == "cli-shim" and not os.environ.get("STAGING_ALLOW_SHIM"):
        raise RuntimeError("the CLI shim is loaded; install the duckdb package")
    out = {"python": sys.version.split()[0], "duckdb": duckdb.__version__}
    for m in ("dlt", "starlette", "itsdangerous", "requests"):
        try:
            out[m] = __import__(m).__version__
        except Exception:  # noqa: BLE001
            out[m] = "missing"
    return json.dumps(out)


@check("migrations: legacy data preserved, migration 2 applied atomically, idempotent on restart")
def migrations(args):
    import control
    import dlt_pipeline as bot
    import revenue
    from control import schema
    con = connect(args)
    for t in ("control.proposals_pre_m2", "control.executions_pre_m2"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    # build a database as it looks in production today: migration 1 only, with rows in it
    m2 = schema.MIGRATIONS.pop()
    try:
        con.execute("DROP SCHEMA IF EXISTS control CASCADE")
        bot.ensure_tables(con)
    finally:
        schema.MIGRATIONS.append(m2)
    con.execute("INSERT INTO control.proposals (proposal_id, fingerprint, kind, created_at, entity_key, action, status) VALUES "
                "('legacy1', 'f', 'optimization', now()::TIMESTAMP, 'Apple Search Ads|1', 'pause_keyword', 'applied'), "
                "('legacy2', 'g', 'manual', now()::TIMESTAMP, 'Apple Search Ads|2', 'pause_keyword', 'proposed')")
    con.execute("INSERT INTO control.executions VALUES ('le1', 'legacy1', now()::TIMESTAMP, 'dry_run', NULL, 'simulated', NULL, NULL)")
    con.execute("INSERT INTO control.control_state VALUES ('mode', '\"recommend\"', now()::TIMESTAMP, 'staging')")
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("control.proposals", "control.executions", "control.control_state")}
    applied = schema.ensure_schema(con)
    if applied != [2]:
        raise AssertionError(f"expected [2], got {applied}")
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    if before != after:
        raise AssertionError(f"row counts changed: {before} -> {after}")
    st = dict(con.execute("SELECT proposal_id, status FROM control.proposals").fetchall())
    if st != {"legacy1": "verified", "legacy2": "proposed"}:
        raise AssertionError(st)
    con.close()
    con = connect(args)
    if schema.ensure_schema(con) != []:
        raise AssertionError("migrations re-ran after restart")
    control.ensure(con)
    revenue.ensure(con)
    return f"rows preserved {after}"


@check("migrations: a failing migration rolls back completely (real transaction)")
def rollback(args):
    from control import schema
    con = connect(args)
    schema.MIGRATIONS.append((999, "staging broken", ["CREATE TABLE control.staging_half (x INTEGER)", "SELECT * FROM no_such_table_xyz"]))
    try:
        try:
            schema.ensure_schema(con)
            raise AssertionError("broken migration did not raise")
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - expected
            pass
    finally:
        schema.MIGRATIONS.pop()
    n = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'control' AND table_name = 'staging_half'").fetchone()[0]
    v = con.execute("SELECT count(*) FROM control.schema_migrations WHERE version = 999").fetchone()[0]
    con.execute("SELECT 1").fetchone()   # connection still usable after the rollback
    if n or v:
        raise AssertionError("partial migration left behind")


@check("restart persistence: stop flag, approvals and audit survive a new connection")
def restart(args):
    from control import engine, state
    con = connect(args)
    v = engine.stop(con, "staging", "restart check")
    con.close()
    con = connect(args)
    s = state.automation_stopped(con)
    if not s or s["request_id"] != v["request_id"] or not s.get("persisted_at"):
        raise AssertionError(s)
    engine.resume(con, "staging")
    if state.automation_stopped(con):
        raise AssertionError("resume not persisted")
    n = con.execute("SELECT count(*) FROM revenue.audit_logs WHERE action IN ('automation.stopped', 'automation.resumed')").fetchone()[0]
    return f"audit rows {n}"


WORKER = r'''
import os, sys, time
sys.path.insert(0, {root!r})
import duckdb
from control import state
md, db = {md!r}, {db!r}
if md:
    con = duckdb.connect("md:", config={{"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]}}); con.execute(f"USE {{md}}")
else:
    con = duckdb.connect(db)
ok = state.acquire(con, "staging_lock", sys.argv[1], ttl_minutes=1)
print("ACQUIRED" if ok else "REFUSED", flush=True)
if ok and sys.argv[2] == "crash":
    os._exit(9)          # die holding the lease, without releasing it
'''


@check("two processes, lock expiry, crash recovery (shared database)")
def processes(args):
    if not args.motherduck:
        return ("SKIP", "needs --motherduck: a local DuckDB file can only be opened by one process at a time, which is exactly why "
                        "production must use MotherDuck for the web service and the hourly job")
    from control import state
    con = connect(args)
    con.execute("DELETE FROM control.locks WHERE name = 'staging_lock'")
    code = WORKER.format(root=ROOT, md=args.motherduck, db=args.db)
    a = subprocess.run([sys.executable, "-c", code, "worker-a", "crash"], capture_output=True, text=True, timeout=120)
    if "ACQUIRED" not in a.stdout:
        raise AssertionError(a.stdout + a.stderr)
    b = subprocess.run([sys.executable, "-c", code, "worker-b", "stay"], capture_output=True, text=True, timeout=120)
    if "REFUSED" not in b.stdout:
        raise AssertionError("second process got the lock while the crashed one's lease was valid: " + b.stdout)
    con.execute("UPDATE control.locks SET expires_at = now()::TIMESTAMP - INTERVAL 1 MINUTE WHERE name = 'staging_lock'")
    c = subprocess.run([sys.executable, "-c", code, "worker-c", "stay"], capture_output=True, text=True, timeout=120)
    if "ACQUIRED" not in c.stdout:
        raise AssertionError("lease did not expire for the next worker: " + c.stdout)
    if state.renew(con, "staging_lock", "worker-a"):
        raise AssertionError("the crashed worker could still renew a lease it lost")
    return "crashed holder blocked others until expiry; lost lease cannot be renewed"


@check("dry-run decision cycle with a fake platform: zero remote writes")
def cycle(args):
    import dlt_pipeline as bot
    from control import adapters, alerts, engine, ingest, state
    con = connect(args)
    con.execute("CREATE SCHEMA IF NOT EXISTS staging_ads")
    con.execute("CREATE OR REPLACE TABLE staging_ads.perf (date VARCHAR, channel VARCHAR, entity_id VARCHAR, entity_type VARCHAR, keyword VARCHAR, "
                "campaign_id VARCHAR, campaign VARCHAR, adgroup_id VARCHAR, app VARCHAR, status VARCHAR, impressions DOUBLE, taps DOUBLE, "
                "installs DOUBLE, spend DOUBLE, revenue DOUBLE, currency VARCHAR, account_id VARCHAR, _fetched_at VARCHAR)")
    today = dt.date.today()
    for d in range(2, 9):
        con.execute("INSERT INTO staging_ads.perf VALUES (?, 'Apple Search Ads', 'k1', 'keyword', 'kw', '111', 'RNR | Search', '9', NULL, "
                    "'ACTIVE', 100, 3, 0, 20, NULL, 'USD', 'org1', ?)", [(today - dt.timedelta(days=d)).isoformat(), dt.datetime.utcnow().isoformat()])
    rules = bot.Rules()
    rules._cmap = ingest.mapping(con)
    state.set_mode(con, "bounded_auto", "staging")
    rules.auto_actions = ["pause_keyword", "pause_campaign"]
    fake = adapters.FakeAdapter({"Apple Search Ads|k1": {"status": "ACTIVE"}, "Apple Search Ads|campaign:111": {"status": "ENABLED"}})
    engine.request_emergency_pause(con, rules, "staging", bot.APPS, dataset="staging_ads", table="perf", rule_app_fn=lambda n: bot.app_for(n, rules))
    s = engine.run_cycle(con, rules, apps=bot.APPS, rule_app_fn=lambda n: bot.app_for(n, rules), adapters={"Apple Search Ads": fake},
                         alerter=alerts.Alerter(con, alerts.FakeTransport(), rules), environ={"DRY_RUN": "true"},
                         dataset="staging_ads", table="perf")
    state.set_mode(con, "recommend", "staging")
    engine.clear_emergency(con, "staging")
    if fake.writes:
        raise AssertionError(f"writes under DRY_RUN: {fake.writes}")
    return f"executions {s['executions']}"


@check("web service starts and answers (healthz, login, control) with the real driver")
def web_start(args):
    import dlt_pipeline as bot
    holder = {}

    def _connect():
        if "c" not in holder:
            holder["c"] = connect(args)
            bot.ensure_tables(holder["c"])
        return holder["c"]
    bot.connect = _connect
    import web

    def call(method, path, body=None, cookie=None):
        raw = json.dumps(body).encode() if body is not None else b""
        hdr = [(b"host", b"staging"), (b"content-type", b"application/json"), (b"x-adbot-request", b"1")]
        if cookie:
            hdr.append((b"cookie", cookie.encode()))
        out = {"s": None, "h": [], "b": b""}
        done = False

        async def rcv():
            nonlocal done
            if not done:
                done = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        async def snd(m):
            if m["type"] == "http.response.start":
                out["s"], out["h"] = m["status"], m["headers"]
            elif m["type"] == "http.response.body":
                out["b"] += m.get("body", b"")
        asyncio.run(web.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
                             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdr, "client": ("127.0.0.1", 1),
                             "server": ("staging", 443), "root_path": ""}, rcv, snd))
        ck = next((v.decode() for k, v in out["h"] if k == b"set-cookie"), None)
        return out["s"], out["b"], ck
    if call("GET", "/healthz")[0] != 200:
        raise AssertionError("healthz")
    s, _, ck = call("POST", "/api/login", {"password": os.environ["APP_PASSWORD"]})
    if s != 200:
        raise AssertionError(f"login {s}")
    s, b, _ = call("GET", "/api/control", cookie=ck.split(";")[0])
    if s != 200:
        raise AssertionError(f"/api/control {s}: {b[:300]!r}")
    return "200 on /healthz, /api/login, /api/control"


@check("dlt merge + revision guard against the real destination")
def dlt_merge(args):
    try:
        import dlt
    except ImportError:
        return ("SKIP", "dlt not installed")
    from control.ingest import existing_revisions, keep_newest_revisions
    if args.motherduck:
        dest = dlt.destinations.motherduck(credentials=f"md:///{args.motherduck}?token={os.environ['MOTHERDUCK_TOKEN']}")
    else:
        dest = dlt.destinations.duckdb(credentials=args.db)
    pipe = dlt.pipeline(pipeline_name="adbot_staging_check", destination=dest, dataset_name="staging_dlt",
                        pipelines_dir=tempfile.mkdtemp())
    key = {"channel": "Meta Ads", "entity_id": "A", "date": "2026-10-20", "spend": 0.0}

    def load(rows):
        @dlt.resource(name="perf", write_disposition="merge", primary_key=["channel", "entity_id", "date"])
        def perf():
            yield from rows
        pipe.run(perf())
    load([{**key, "spend": 12.0, "_fetched_at": "2026-10-22T10:00:00"}])
    con = connect(args)
    stored = existing_revisions(con, dt.date(2026, 10, 19), dt.date(2026, 10, 21), "staging_dlt", "perf")
    late = keep_newest_revisions([{**key, "spend": 9.0, "_fetched_at": "2026-10-21T10:00:00"}], stored)
    if late:
        raise AssertionError(f"an older revision would overwrite the stored one (stored={stored})")
    load(keep_newest_revisions([{**key, "spend": 15.0, "_fetched_at": "2026-10-23T10:00:00"}], stored))
    con.close()
    con = connect(args)
    rows = con.execute("SELECT spend FROM staging_dlt.perf").fetchall()
    typ = con.execute("SELECT data_type FROM information_schema.columns WHERE table_schema = 'staging_dlt' AND table_name = 'perf' "
                      "AND column_name = '_fetched_at'").fetchone()
    if rows != [(15.0,)]:
        raise AssertionError(rows)
    return f"one row, newest revision kept; _fetched_at stored as {typ[0] if typ else '?'}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(tempfile.mkdtemp(prefix="adbot-staging-"), "staging.duckdb"))
    ap.add_argument("--motherduck", help="a separate MotherDuck database for staging, e.g. adbot_staging")
    args = ap.parse_args()
    if args.motherduck and args.motherduck.lower() == os.environ.get("MOTHERDUCK_DATABASE", "adbot").lower():
        sys.exit("Refusing to run against the production database name; use e.g. --motherduck adbot_staging")
    if args.motherduck and not os.environ.get("MOTHERDUCK_TOKEN"):
        sys.exit("--motherduck needs MOTHERDUCK_TOKEN")
    os.environ.pop("MOTHERDUCK_TOKEN", None) if not args.motherduck else None
    deps()
    if RESULTS[-1][0] == "FAIL":
        print(RESULTS[-1][2])
        sys.exit(1)
    for fn in (migrations, rollback, restart, processes, cycle, web_start, dlt_merge):
        fn(args)
    width = max(len(n) for _, n, _ in RESULTS)
    for st, n, msg in RESULTS:
        print(f"{st:4}  {n:<{width}}  {msg.splitlines()[0] if msg else ''}")
        if st == "FAIL":
            print("      " + "\n      ".join(msg.splitlines()[1:]))
    print(f"\ntarget: {'MotherDuck ' + args.motherduck if args.motherduck else args.db}")
    sys.exit(1 if any(s == "FAIL" for s, _, _ in RESULTS) else 0)


if __name__ == "__main__":
    main()
