"""
trading_sidecar.py — read-only bridge from the FX trading system to the AdBot Trading tab.

Runs on the trading machine as its OWN program, in its OWN virtual environment.
It does not import, modify or restart anything in fx_trading_system. Every
minute it:

  1. reads the trading system's PostgreSQL database in a READ-ONLY session
     (the server itself rejects any write: default_transaction_read_only=on),
  2. reads .runtime/runtime_status.json (the observability supervisor's status file),
  3. sends a summary to the AdBot database through trading_status.StatusReporter.

What it reports
  heartbeat  supervisor status: running / degraded / stopped (and which services are down)
  account    latest account_snapshots row (equity, balance, margin), today's and total realised P&L
  positions  monitored_positions that are OPEN / PARTIALLY_CLOSED / MISSING / UNKNOWN
  trades     positions closed in the last 7 days (re-sent, so late corrections show up)
  signals    QUALIFIED candidates and every candidate the Risk Engine decided on
             (APPROVED/MODIFIED = taken, REJECTED = blocked, with its reasons)
  risk       v_current_trading_state (SUSPENDED / EMERGENCY_STOP = kill switch on),
             today's loss against FXBOT_MAX_DAILY_LOSS_PCT x equity, and drawdown

Database login
  Read from fx_trading_system\\.env — only the five FXBOT_POSTGRES_* keys, nothing else —
  unless TRADING_PG_DSN is set. For extra safety create a read-only Postgres role and put
  its DSN in TRADING_PG_DSN, e.g.  host=localhost dbname=fx_trading user=fx_readonly password=...

Setup (PowerShell, once):
    cd C:\\Users\\<you>\\adbot-sidecar
    py -3.11 -m venv .venv
    .venv\\Scripts\\pip install psycopg2-binary "duckdb==1.5.6"
    $env:MOTHERDUCK_TOKEN = "<token>"        # or set it permanently in System > Environment Variables
    .venv\\Scripts\\python trading_sidecar.py --trading-dir "C:\\Users\\<you>\\OneDrive\\Desktop\\fx_trading_system" --once

Then run it continuously (drop --once), e.g. from Task Scheduler "At startup".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from trading_status import StatusReporter

log = logging.getLogger("trading_sidecar")
ACTIVE_STATES = ("OPEN", "PARTIALLY_CLOSED", "MISSING", "POSITION_STATE_UNKNOWN")
PG_KEYS = {"FXBOT_POSTGRES_HOST": "host", "FXBOT_POSTGRES_PORT": "port", "FXBOT_POSTGRES_DB": "dbname",
           "FXBOT_POSTGRES_USER": "user", "FXBOT_POSTGRES_PASSWORD": "password"}


# --------------------------------------------------------------------------- #
# Config: only the keys we need, never the whole .env
# --------------------------------------------------------------------------- #
def read_env_keys(env_path: Path, wanted: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k in wanted:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def pg_connect(trading_dir: Path):
    import psycopg2
    import psycopg2.extras
    ro = "-c default_transaction_read_only=on -c statement_timeout=15000"
    dsn = os.getenv("TRADING_PG_DSN")
    if dsn:
        conn = psycopg2.connect(dsn, options=ro, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10)
    else:
        env = read_env_keys(trading_dir / ".env", set(PG_KEYS))
        kw = {PG_KEYS[k]: v for k, v in env.items()}
        if "dbname" not in kw:
            raise SystemExit("No database settings found: set TRADING_PG_DSN or check --trading-dir points at fx_trading_system")
        conn = psycopg2.connect(options=ro, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10, **kw)
    conn.set_session(readonly=True, autocommit=True)  # belt and braces: the session itself refuses writes
    return conn


def q(conn, sql: str, params=None) -> list[dict]:
    """Run one read-only query; a missing table/view just returns no rows."""
    try:
        with conn.cursor() as c:
            c.execute(sql, params or ())
            return [dict(r) for r in c.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.warning("query skipped (%s): %s", type(e).__name__, str(e).splitlines()[0][:200])
        return []


def num(v):
    if v is None:
        return None
    return float(v) if isinstance(v, (Decimal, int, float)) else v


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #
def supervisor_status(trading_dir: Path) -> tuple[str, str]:
    p = trading_dir / ".runtime" / "runtime_status.json"
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "unknown", "runtime_status.json not found"
    except Exception as e:  # noqa: BLE001
        return "unknown", f"runtime_status.json unreadable: {e}"
    updated = dt.datetime.fromisoformat(str(s.get("updated_at", "")).replace("Z", "+00:00"))
    age_min = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() / 60
    kids = s.get("children") or []
    down = [k["name"] for k in kids if not k.get("running") and not k.get("disabled")]
    if age_min > 10:
        return "stopped", f"supervisor last updated {age_min:.0f} min ago"
    if down:
        return "degraded", "not running: " + ", ".join(down)
    return "running", f"{len(kids)} services up"


def one_pass(conn, trading_dir: Path, rep_by_mode: dict, bot_id: str, daily_loss_pct: float) -> None:
    acct = q(conn, "SELECT account_id, trading_mode::text AS mode, balance, equity, margin, free_margin, recorded_at "
                   "FROM account_snapshots ORDER BY recorded_at DESC LIMIT 1")
    mode = (acct[0]["mode"] if acct else "DEMO").upper()
    rep = rep_by_mode.setdefault(mode, StatusReporter(bot_id=bot_id, mode="live" if mode == "LIVE" else "paper"))

    status, message = supervisor_status(trading_dir)
    rep.heartbeat(status, message, min_interval=0)

    day = q(conn, "SELECT realized_pnl_today FROM v_daily_realized_pnl")
    tot = q(conn, "SELECT COALESCE((SELECT SUM(realized_pnl_account_ccy) FROM positions),0) "
                  "+ COALESCE((SELECT SUM(realized_pnl_account_ccy) FROM unlinked_realized_pnl),0) AS total")
    day_pnl = num(day[0]["realized_pnl_today"]) if day else None
    if acct:
        a = acct[0]
        rep.account(equity=num(a["equity"]), balance=num(a["balance"]), margin_used=num(a["margin"]),
                    day_pnl=day_pnl, total_pnl=num(tot[0]["total"]) if tot else None, currency="USD")

    pos = q(conn, "SELECT canonical_symbol, direction, COALESCE(last_known_volume, original_volume) AS qty, entry_price, "
                  "last_known_price, last_known_sl, last_known_tp, last_known_unrealized_pnl, opened_at, current_state::text AS state "
                  "FROM monitored_positions WHERE current_state::text = ANY(%s)", (list(ACTIVE_STATES),))
    rep.positions([{"symbol": p["canonical_symbol"], "side": str(p["direction"]).lower(), "qty": num(p["qty"]),
                    "entry": num(p["entry_price"]), "price": num(p["last_known_price"]), "stop": num(p["last_known_sl"]),
                    "target": num(p["last_known_tp"]), "unrealized_pnl": num(p["last_known_unrealized_pnl"]),
                    "opened_at": p["opened_at"], "strategy": "" if p["state"] == "OPEN" else p["state"]} for p in pos])

    trades = q(conn, "SELECT p.mt5_ticket, p.symbol, p.side::text AS side, p.volume, p.entry_price, p.exit_price, "
                     "p.realized_pnl_account_ccy AS pnl, p.opened_at, p.closed_at, p.exit_reason::text AS exit_reason, cs.setup_type "
                     "FROM positions p LEFT JOIN candidate_setups cs ON cs.trace_id = p.trace_id "
                     "WHERE p.closed_at >= now() - interval '7 days'")
    for t in trades:
        rep.trade(trade_id=str(t["mt5_ticket"]), symbol=t["symbol"], side=str(t["side"]).lower(), qty=num(t["volume"]),
                  entry=num(t["entry_price"]), exit=num(t["exit_price"]), pnl=num(t["pnl"]), opened_at=t["opened_at"],
                  closed_at=t["closed_at"], strategy=t["setup_type"] or "", reason=t["exit_reason"] or "")

    risk_state = q(conn, "SELECT trading_state::text AS state, reason, triggered_by, since FROM v_current_trading_state")
    dd = q(conn, "SELECT drawdown_pct FROM v_current_drawdown ORDER BY recorded_at DESC LIMIT 1")
    if risk_state:
        st = risk_state[0]
        eq = num(acct[0]["equity"]) if acct else None
        limit = round(eq * daily_loss_pct, 2) if eq else None
        used = max(0.0, -(day_pnl or 0.0))
        notes = f"{st['state']}: {st['reason'] or ''}".strip()
        if dd and dd[0]["drawdown_pct"] is not None:
            notes += f" · drawdown {num(dd[0]['drawdown_pct']) * 100:.1f}%"
        rep.risk(daily_loss_limit=limit, daily_loss_used=used, max_exposure=None, exposure=None,
                 kill_switch=st["state"] in ("SUSPENDED", "EMERGENCY_STOP"), notes=notes)


_last_signal_id = 0


def send_signals(conn, rep_by_mode: dict, bot_id: str, first_run: bool) -> None:
    """Qualified candidates and anything the Risk Engine ruled on, newest first, without repeats."""
    global _last_signal_id
    since = "now() - interval '24 hours'" if first_run else "now() - interval '2 hours'"
    rows = q(conn, f"""
        SELECT cs.id, cs.created_at, ms.symbol, cs.setup_type, cs.candidate_status, cs.rejection_reason,
               cl.parsed_action::text AS action, cl.confidence, rd.decision::text AS decision, rd.reasons
        FROM candidate_setups cs
        JOIN market_snapshots ms ON ms.snapshot_id = cs.snapshot_id
        LEFT JOIN LATERAL (SELECT decision, reasons FROM risk_decisions r WHERE r.trace_id = cs.trace_id ORDER BY checked_at DESC LIMIT 1) rd ON TRUE
        LEFT JOIN LATERAL (SELECT parsed_action, confidence FROM claude_signals s WHERE s.trace_id = cs.trace_id ORDER BY created_at DESC LIMIT 1) cl ON TRUE
        WHERE cs.id > %s AND cs.created_at >= {since}
          AND (cs.candidate_status = 'QUALIFIED' OR rd.decision IS NOT NULL)
        ORDER BY cs.id
        LIMIT 500""", (_last_signal_id,))
    rep = next(iter(rep_by_mode.values()), None)
    if rep is None:
        return
    for r in rows:
        _last_signal_id = max(_last_signal_id, r["id"])
        if r["decision"] in ("APPROVED", "MODIFIED"):
            decision, reason = "taken", f"Risk Engine {r['decision'].lower()}"
        elif r["decision"] == "REJECTED":
            decision, reason = "blocked", "; ".join(r["reasons"] or []) or "Risk Engine rejected"
        else:
            decision, reason = "skipped", r["rejection_reason"] or "qualified, no risk decision yet"
        rep.signal(symbol=r["symbol"], direction=(r["action"] or "").lower(), confidence=num(r["confidence"]),
                   decision=decision, reason=reason[:500], source=r["setup_type"] or "scanner")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trading-dir", required=True, type=Path, help="Path to fx_trading_system")
    ap.add_argument("--bot-id", default=os.getenv("TRADING_BOT_ID", "fx-trading-system"))
    ap.add_argument("--interval", type=int, default=60, help="Seconds between passes (min 30)")
    ap.add_argument("--once", action="store_true", help="Run one pass and exit (for testing)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if not (os.getenv("MOTHERDUCK_TOKEN") or os.getenv("ADBOT_DUCKDB_PATH")):
        print("Set MOTHERDUCK_TOKEN first (the same database AdBot uses).")
        return 2
    try:
        daily_loss_pct = float(read_env_keys(args.trading_dir / ".env", {"FXBOT_MAX_DAILY_LOSS_PCT"}).get("FXBOT_MAX_DAILY_LOSS_PCT", "0.02"))
    except ValueError:
        daily_loss_pct = 0.02
    reps: dict = {}
    conn, first = None, True
    while True:
        try:
            if conn is None or conn.closed:
                conn = pg_connect(args.trading_dir)
            one_pass(conn, args.trading_dir, reps, args.bot_id, daily_loss_pct)
            send_signals(conn, reps, args.bot_id, first)
            first = False
            log.info("pass ok")
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 — keep going; this program can't affect trading either way
            log.warning("pass failed: %s", e)
            try:
                conn and conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
        if args.once:
            for r in reps.values():
                r.flush(10)
            return 0
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())
