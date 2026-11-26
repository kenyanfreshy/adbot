"""
Health Watchdog (phase 6): a Telegram alert when something goes stale or disappears, the moment a run sees it.

What it checks (the decision engine already alerts on failed or stale ad imports and unmapped campaigns):
  vanished     a campaign the platform listed recently that its last listings no longer include (removed or renamed?)
  store lag    a store's proceeds stopping more than STORE_LAG_DAYS days ago
  store sync   store sync jobs that failed in the last day
  trading      the trading heartbeat older than TRADING_STALE_HOURS (only if the trading bot has ever run)
  claude       Claude spend today at 80% or more of AGENTS_DAILY_USD_CAP
  agents       agents that failed in the previous run

Each issue is announced once (one message for all new issues), and once more when it clears. Off with the
"Watchdog" alert toggle. Read-only: it changes nothing.
"""
from __future__ import annotations

import datetime as dt

STORE_LAG_DAYS = 4
TRADING_STALE_HOURS = 24
VANISHED_AFTER_HOURS = 24
STATE_KEY = "watchdog.open"


def _ts(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v).replace(" ", "T")[:26])


def checks(ctx) -> list[dict]:
    """Every current issue: {key, severity, text}. Each check is independent; one that can't run is skipped."""
    con, now, issues = ctx.con, ctx.now, []

    def safe(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001 - a missing table means that check doesn't apply
            pass

    def vanished():
        rows = con.execute("SELECT channel, campaign_id, name, last_seen, max(last_seen) OVER (PARTITION BY channel) AS newest "
                           "FROM control.ad_entities WHERE entity_type = 'campaign'").fetchall()
        for ch, cid, name, last, newest in rows:
            last, newest = _ts(last), _ts(newest)
            if newest - last > dt.timedelta(hours=VANISHED_AFTER_HOURS) and now - last < dt.timedelta(days=30):
                issues.append({"key": f"vanished:{ch}:{cid}", "severity": "warning",
                               "text": f"{ch} stopped listing campaign “{name or cid}” after {last:%b %d %H:%M} UTC. Removed or renamed?"})

    def store_lag():
        for platform, last in con.execute("SELECT source_platform, max(period_start) FROM revenue.sales_reports "
                                          "WHERE proceeds_amount IS NOT NULL GROUP BY 1").fetchall():
            d = last if isinstance(last, dt.date) else dt.date.fromisoformat(str(last)[:10])
            lag = (ctx.today - d).days
            if lag > STORE_LAG_DAYS:
                store = {"apple_app_store": "App Store", "google_play": "Google Play"}.get(platform, platform)
                issues.append({"key": f"storelag:{platform}:{d}", "severity": "warning",
                               "text": f"{store} proceeds stop at {d} ({lag} days behind)"})

    def store_sync():
        rows = con.execute("SELECT source, job_type, count(*), any_value(message) FROM revenue.synchronization_jobs "
                           "WHERE status = 'failed' AND started_at >= ? GROUP BY 1, 2", [now - dt.timedelta(days=1)]).fetchall()
        for src, jt, n, msg in rows:
            issues.append({"key": f"storesync:{src}:{jt}:{ctx.today}", "severity": "warning",
                           "text": f"Store sync {src} / {jt} failed {n}× in the last day" + (f": {str(msg)[:120]}" if msg else "")})

    def trading():
        last = _ts(con.execute("SELECT max(ts) FROM trading.heartbeat").fetchone()[0])
        if last is not None and now - last > dt.timedelta(hours=TRADING_STALE_HOURS):
            issues.append({"key": f"trading:{last:%Y-%m-%d}", "severity": "info",
                           "text": f"Trading bot heartbeat last seen {last:%b %d %H:%M} UTC ({(now - last).days} days ago)"})

    def claude():
        from .llm import usage_today
        u = usage_today(con, lambda: now) or {}
        cap = float(ctx.environ.get("AGENTS_DAILY_USD_CAP") or 1.0)
        if cap and (u.get("usd") or 0) >= 0.8 * cap:
            issues.append({"key": f"claude:{ctx.today}", "severity": "warning",
                           "text": f"Claude spend today ${u['usd']:.2f} of the ${cap:.2f} cap; agents stop calling Claude at the cap"})

    def agents():
        prev = con.execute("SELECT run_id FROM control.agent_runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1").fetchone()
        if prev:
            for agent, err in con.execute("SELECT agent, error FROM control.agent_outputs WHERE run_id = ? AND status = 'failed'",
                                          [prev[0]]).fetchall():
                issues.append({"key": f"agent:{agent}:{ctx.today}", "severity": "warning",
                               "text": f"Agent {agent.replace('_', ' ')} failed: {str(err or '')[:120]}"})

    for fn in (vanished, store_lag, store_sync, trading, claude, agents):
        safe(fn)
    return issues


def diff(previous: dict, current: list[dict]) -> tuple[list[dict], list[str]]:
    """New issues (not open before) and the texts of issues that cleared. Pure, for tests."""
    cur = {i["key"]: i for i in current}
    new = [i for k, i in cur.items() if k not in previous]
    cleared = [txt for k, txt in previous.items() if k not in cur]
    return new, cleared


def message(new: list[dict], cleared: list[str]) -> tuple[str, str, str]:
    icon = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}
    sev = "critical" if any(i["severity"] == "critical" for i in new) else ("warning" if any(i["severity"] == "warning" for i in new) else "info")
    title = f"Watchdog: {len(new)} new issue(s)" if new else "Watchdog: all clear"
    body = "\n".join([f"{icon.get(i['severity'], '•')} {i['text']}" for i in new] + [f"✅ Cleared: {t}" for t in cleared])
    return sev, title, body


def watchdog(ctx) -> dict:
    from control import state as cstate
    from control.alerts import Alerter
    issues = checks(ctx)
    previous = cstate.get(ctx.con, STATE_KEY) or {}
    previous = previous if isinstance(previous, dict) else {}
    new, cleared = diff(previous, issues)
    delivery = None
    if (new or cleared) and ctx.send:
        sev, title, body = message(new, cleared)
        key = "watchdog:" + "|".join(sorted(i["key"] for i in new) + sorted("ok:" + t for t in cleared))
        delivery = Alerter(ctx.con, ctx.transport, ctx.rules, clock=lambda: ctx.now).send("watchdog", sev, key[:400], title, body)
    if ctx.send:
        cstate.put(ctx.con, STATE_KEY, {i["key"]: i["text"] for i in issues}, by="agents")
    return {"issues": issues, "new": [i["key"] for i in new], "cleared": cleared, "delivery": delivery}
