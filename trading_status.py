"""
trading_status.py — read-only status reporter for the trading bot.

Copy this one file into the trading bot and call it from the Audit/Monitoring
service. It only WRITES a status summary into the AdBot database (MotherDuck);
the AdBot control panel READS it and shows the Trading tab. Nothing here can
place, change or close an order, and the dashboard never sees broker keys.

Safety by design:
  * Every call returns immediately: rows go onto an in-memory queue and a
    background thread writes them. A slow or broken database never delays a trade.
  * Every error is caught and logged, never raised into the trading code.
  * If MOTHERDUCK_TOKEN (or ADBOT_DUCKDB_PATH) isn't set, the reporter is a no-op.

Usage (inside the trading bot):

    from trading_status import StatusReporter
    status = StatusReporter(bot_id="claude-fx", mode="paper")       # or "live"

    status.heartbeat("running")                                     # every loop / minute
    status.account(equity=10250.0, balance=10000.0, day_pnl=42.5,
                   total_pnl=250.0, margin_used=800.0, currency="USD")
    status.positions([                                              # full current list, each snapshot
        {"symbol": "EUR/USD", "side": "long", "qty": 10000, "entry": 1.0812,
         "price": 1.0830, "stop": 1.0790, "target": 1.0860, "unrealized_pnl": 18.0,
         "opened_at": "2026-10-22T13:05:00Z", "strategy": "intraday-breakout"},
    ])
    status.signal(symbol="GBP/USD", direction="short", confidence=0.71,
                  decision="blocked", reason="Risk Engine: daily loss limit 80% used", source="claude")
    status.trade(trade_id="T-1042", symbol="EUR/USD", side="long", qty=10000,
                 entry=1.0812, exit=1.0841, pnl=29.0, opened_at=..., closed_at=..., strategy="intraday-breakout",
                 reason="target hit")
    status.risk(daily_loss_limit=200.0, daily_loss_used=60.0, max_exposure=50000.0,
                exposure=10000.0, kill_switch=False, notes="")

Environment: MOTHERDUCK_TOKEN (same database as AdBot; a separate read/write
token for the trading machine is best) and optionally MOTHERDUCK_DATABASE (default "adbot").
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import logging
import os
import queue
import re
import threading
import time
from typing import Any, Iterable

log = logging.getLogger("trading_status")

SCHEMA = "trading"
DDL = [
    f"create schema if not exists {SCHEMA}",
    f"""create table if not exists {SCHEMA}.heartbeat (ts timestamp, bot_id varchar, mode varchar, status varchar, message varchar)""",
    f"""create table if not exists {SCHEMA}.account (ts timestamp, bot_id varchar, mode varchar, equity double, balance double,
        margin_used double, day_pnl double, total_pnl double, currency varchar)""",
    f"""create table if not exists {SCHEMA}.positions (snapshot_at timestamp, bot_id varchar, mode varchar, symbol varchar, side varchar,
        qty double, entry double, price double, stop double, target double, unrealized_pnl double, opened_at timestamp, strategy varchar)""",
    f"""create table if not exists {SCHEMA}.trades (trade_id varchar, bot_id varchar, mode varchar, symbol varchar, side varchar, qty double,
        entry double, exit double, pnl double, opened_at timestamp, closed_at timestamp, strategy varchar, reason varchar,
        primary key (bot_id, trade_id))""",
    f"""create table if not exists {SCHEMA}.signals (ts timestamp, bot_id varchar, mode varchar, symbol varchar, direction varchar,
        confidence double, decision varchar, reason varchar, source varchar)""",
    f"""create table if not exists {SCHEMA}.risk (ts timestamp, bot_id varchar, mode varchar, daily_loss_limit double, daily_loss_used double,
        max_exposure double, exposure double, kill_switch boolean, notes varchar)""",
]
DECISIONS = {"taken", "skipped", "blocked"}


def _ts(v: Any) -> dt.datetime | None:
    """Accept datetime, ISO string or epoch seconds; store naive UTC."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.utcfromtimestamp(v)
    if isinstance(v, str):
        v = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    if v.tzinfo is not None:
        v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return v


def _f(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


class StatusReporter:
    def __init__(self, bot_id: str = "trading-bot", mode: str = "paper", max_queue: int = 5000):
        self.bot_id = str(bot_id)[:64]
        self.mode = mode if mode in ("paper", "live") else "paper"
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._con = None
        self._enabled = bool(os.getenv("MOTHERDUCK_TOKEN") or os.getenv("ADBOT_DUCKDB_PATH"))
        self._last_hb = 0.0
        if not self._enabled:
            log.info("trading_status: no MOTHERDUCK_TOKEN / ADBOT_DUCKDB_PATH; reporting is off")
            return
        self._t = threading.Thread(target=self._run, name="trading-status", daemon=True)
        self._t.start()
        atexit.register(self.flush, 5.0)

    # ---- public API: all non-blocking, never raise -------------------------
    def heartbeat(self, status: str = "running", message: str = "", min_interval: float = 30.0) -> None:
        now = time.time()
        if now - self._last_hb < min_interval and status == "running":
            return
        self._last_hb = now
        self._put("heartbeat", [self._now(), self.bot_id, self.mode, str(status)[:40], str(message)[:500]])

    def account(self, equity, balance=None, margin_used=None, day_pnl=None, total_pnl=None, currency="USD") -> None:
        self._put("account", [self._now(), self.bot_id, self.mode, _f(equity), _f(balance), _f(margin_used), _f(day_pnl), _f(total_pnl), str(currency)[:8]])

    def positions(self, items: Iterable[dict]) -> None:
        snap = self._now()
        rows = [[snap, self.bot_id, self.mode, str(p.get("symbol", ""))[:20], str(p.get("side", ""))[:8], _f(p.get("qty")), _f(p.get("entry")),
                 _f(p.get("price")), _f(p.get("stop")), _f(p.get("target")), _f(p.get("unrealized_pnl")), self._safe_ts(p.get("opened_at")),
                 str(p.get("strategy", ""))[:60]] for p in (items or [])]
        # an empty list is still a snapshot ("flat"), so write a marker row the panel recognises
        self._put("positions", rows or [[snap, self.bot_id, self.mode, None, None, None, None, None, None, None, None, None, None]])

    def trade(self, trade_id, symbol, side, qty, entry, exit=None, pnl=None, opened_at=None, closed_at=None, strategy="", reason="") -> None:
        self._put("trades", [str(trade_id)[:64], self.bot_id, self.mode, str(symbol)[:20], str(side)[:8], _f(qty), _f(entry), _f(exit), _f(pnl),
                             self._safe_ts(opened_at), self._safe_ts(closed_at), str(strategy)[:60], str(reason)[:300]])

    def signal(self, symbol, direction, confidence=None, decision="taken", reason="", source="") -> None:
        d = decision if decision in DECISIONS else "skipped"
        self._put("signals", [self._now(), self.bot_id, self.mode, str(symbol)[:20], str(direction)[:8], _f(confidence), d, str(reason)[:500], str(source)[:40]])

    def risk(self, daily_loss_limit=None, daily_loss_used=None, max_exposure=None, exposure=None, kill_switch=False, notes="") -> None:
        self._put("risk", [self._now(), self.bot_id, self.mode, _f(daily_loss_limit), _f(daily_loss_used), _f(max_exposure), _f(exposure), bool(kill_switch), str(notes)[:500]])

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (briefly) for queued rows to be written; for shutdown and tests."""
        if not self._enabled:
            return
        end = time.time() + timeout
        while not self._q.empty() and time.time() < end:
            time.sleep(0.05)

    # ---- internals ---------------------------------------------------------
    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.utcnow()

    @staticmethod
    def _safe_ts(v):
        try:
            return _ts(v)
        except Exception:  # noqa: BLE001
            return None

    def _put(self, table: str, row_or_rows) -> None:
        if not self._enabled:
            return
        try:
            self._q.put_nowait((table, row_or_rows))
        except queue.Full:
            log.warning("trading_status: queue full, dropping a %s update", table)

    def _connect(self):
        import duckdb
        if os.getenv("MOTHERDUCK_TOKEN"):
            db = os.getenv("MOTHERDUCK_DATABASE", "adbot")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db):
                raise ValueError("MOTHERDUCK_DATABASE must be letters, digits or _")
            con = duckdb.connect("md:", config={"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]})
            con.execute(f"create database if not exists {db}")
            con.execute(f"use {db}")
        else:
            con = duckdb.connect(os.environ["ADBOT_DUCKDB_PATH"])
        for q in DDL:
            con.execute(q)
        return con

    def _write(self, table: str, rows) -> None:
        if self._con is None:
            self._con = self._connect()
        rows = rows if rows and isinstance(rows[0], list) else [rows]
        n = len(rows[0])
        ph = ",".join("?" * n)
        if table == "trades":
            # the same trade is usually reported at open and again at close: keep the latest
            self._con.executemany(f"insert or replace into {SCHEMA}.trades values ({ph})", rows)
        else:
            self._con.executemany(f"insert into {SCHEMA}.{table} values ({ph})", rows)

    def _run(self) -> None:
        backoff = 1.0
        while True:
            table, rows = self._q.get()
            while True:
                try:
                    self._write(table, rows)
                    backoff = 1.0
                    break
                except Exception as e:  # noqa: BLE001 — never let reporting crash anything
                    log.warning("trading_status: write to %s failed (%s); retrying in %.0fs", table, e, backoff)
                    self._con = None
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    if backoff >= 60.0 and self._q.qsize() > 1000:
                        log.error("trading_status: database unreachable; dropping a %s update", table)
                        break


if __name__ == "__main__":
    # quick check: python trading_status.py  → writes one heartbeat
    logging.basicConfig(level="INFO")
    s = StatusReporter(bot_id=os.getenv("TRADING_BOT_ID", "trading-bot"), mode=os.getenv("TRADING_MODE", "paper"))
    s.heartbeat("test", "trading_status.py self-check", min_interval=0)
    s.flush()
    print("sent" if os.getenv("MOTHERDUCK_TOKEN") or os.getenv("ADBOT_DUCKDB_PATH") else "reporting is off (no MOTHERDUCK_TOKEN)")
