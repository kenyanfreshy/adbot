"""
AdBot phone web app — a small JSON API plus an installable front end (web/).

Replaces the Streamlit screen with a real home-screen web app: full-screen on
iPhone/Android, its own icon, works one-handed. Same database and the same bot
logic as dlt_pipeline.py; nothing about the hourly job changes.

Run locally:   uvicorn web:app --reload          (needs APP_PASSWORD and MOTHERDUCK_TOKEN or DUCKDB_PATH)
On Render:     uvicorn web:app --host 0.0.0.0 --port $PORT   (see render.yaml)

Security
  * One password (APP_PASSWORD). A successful login sets a signed, HttpOnly,
    Secure, SameSite=Lax cookie valid for 30 days, so the home-screen app stays
    signed in. Changing APP_PASSWORD signs everyone out.
  * Failed logins are slowed down per client.
  * Every /api/* route except /api/login requires the cookie.
  * Ad/broker keys never reach the browser; Telegram is sent from the server.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import dlt_pipeline as bot
from revenue import registry as rev_registry
from revenue import store_check
from revenue.schema import SCHEMA as REV

ROOT = Path(__file__).parent
WEB = ROOT / "web"
COOKIE = "adbot_session"
SESSION_DAYS = 30
APPS = bot.APPS
CHANNELS = bot.CHANNELS

# --------------------------------------------------------------------------- #
# Database access (one shared connection, serialised; DuckDB connections aren't thread-safe)
# --------------------------------------------------------------------------- #
_db_lock = threading.Lock()
_con = None


def con():
    global _con
    if _con is None:
        _con = bot.connect()
    return _con


def rows(sql: str, params=None) -> list[dict]:
    """Run a query and return a list of dicts; a missing table returns []."""
    with _db_lock:
        try:
            cur = con().execute(sql, params or [])
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "does not exist" in msg or "not found" in msg or "catalog error" in msg:
                return []
            raise


def execute(sql: str, params=None) -> None:
    with _db_lock:
        con().execute(sql, params or [])


def jsonable(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


def J(data, status=200):
    return JSONResponse(json.loads(json.dumps(data, default=jsonable)), status_code=status,
                        headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _password() -> str:
    return os.getenv("APP_PASSWORD", "")


def _signer() -> TimestampSigner:
    # derived from the password, so changing APP_PASSWORD invalidates every session
    secret = os.getenv("SESSION_SECRET") or hashlib.sha256(("adbot-session:" + _password()).encode()).hexdigest()
    return TimestampSigner(secret, salt="adbot-login")


def authed(request: Request) -> bool:
    tok = request.cookies.get(COOKIE)
    if not tok or not _password():
        return False
    try:
        _signer().unsign(tok, max_age=SESSION_DAYS * 86400)
        return True
    except (BadSignature, SignatureExpired):
        return False


_fails: dict[str, list[float]] = {}


async def login(request: Request):
    if not _password():
        return J({"error": "APP_PASSWORD is not set on the server."}, 503)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 900]
    if len(recent) >= 10:
        return J({"error": "Too many attempts. Try again in 15 minutes."}, 429)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    pw = str(body.get("password", ""))
    if not hmac.compare_digest(hashlib.sha256(pw.encode()).digest(), hashlib.sha256(_password().encode()).digest()):
        recent.append(time.time())
        _fails[ip] = recent
        time.sleep(min(4.0, 0.5 * len(recent)))
        return J({"error": "That password isn't right."}, 401)
    _fails.pop(ip, None)
    resp = J({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, _signer().sign(b"ok").decode(), max_age=SESSION_DAYS * 86400, httponly=True,
                    secure=secure, samesite="lax", path="/")
    return resp


async def logout(request: Request):
    resp = J({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


def guarded(fn):
    async def wrapper(request: Request):
        if not authed(request):
            return J({"error": "signin"}, 401)
        try:
            return await fn(request)
        except Exception as e:  # noqa: BLE001
            return J({"error": f"{type(e).__name__}: {str(e)[:300]}"}, 500)
    return wrapper


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")


def perf(days: int) -> list[dict]:
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    out = rows(f"select * from {bot.DATASET}.{bot.TABLE} where cast(date as varchar) >= ?", [since])
    for r in out:
        r["date"] = str(r.get("date"))[:10]
        r["app"] = r.get("app") or "Unassigned"
        for f in ("impressions", "taps", "installs", "spend", "revenue"):
            try:
                r[f] = float(r.get(f) or 0)
            except (TypeError, ValueError):
                r[f] = 0.0
    return out


def filters(request: Request):
    qp = request.query_params
    days = max(1, min(90, int(qp.get("days", "14") or 14)))
    app = qp.get("app", "all")
    ch = qp.get("channel", "all")
    keep = lambda r: (app == "all" or r["app"] == app) and (ch == "all" or r["channel"] == ch)  # noqa: E731
    return days, app, ch, keep


def totals(rs: list[dict], rules: bot.Rules) -> dict:
    sp = sum(r["spend"] for r in rs)
    tp = sum(r["taps"] for r in rs)
    ins = sum(r["installs"] for r in rs)
    rev = sum(r["revenue"] for r in rs)
    est = False
    if not rev and ins:
        by = {}
        for r in rs:
            by[r["app"]] = by.get(r["app"], 0) + r["installs"]
        rev = sum(v * float(rules.value_per_install.get(a, 0) or 0) for a, v in by.items())
        est = bool(rev)
    return {"spend": sp, "taps": tp, "installs": ins, "revenue": rev, "revenue_estimated": est,
            "acos": sp / rev if rev else None, "cpt": sp / tp if tp else None, "roas": rev / sp if sp else None}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@guarded
async def me(request: Request):
    bots = trading_bots()
    return J({"ok": True, "dry_run": dry_run(), "apps": APPS, "channels": CHANNELS,
              "trading_live": any(b["live"] for b in bots), "trading_demo": any(not b["live"] for b in bots)})


DEMO_MODES = ("paper", "demo", "practice", "sim", "simulation", "test")


def trading_bots() -> list[dict]:
    """Bots reporting to the Trading tab, newest first, each marked live or demo (by its latest heartbeat's mode)."""
    out = rows("select bot_id, arg_max(mode, ts) as mode, max(ts) as last from trading.heartbeat group by bot_id order by last desc")
    for b in out:
        b["live"] = str(b.get("mode") or "").strip().lower() not in DEMO_MODES
    return out


@guarded
async def ads(request: Request):
    days, app, ch, keep = filters(request)
    rules = bot.load_rules(con())
    allr = perf(days * 2)
    cut = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    sel = [r for r in allr if keep(r)]
    cur = [r for r in sel if r["date"] >= cut]
    prev = [r for r in sel if r["date"] < cut]
    daily = {}
    for r in cur:
        d = daily.setdefault(r["date"], {"date": r["date"], "taps": 0.0, "installs": 0.0, "spend": 0.0})
        for f in ("taps", "installs", "spend"):
            d[f] += r[f]
    series = []
    for i in range(days):
        day = (dt.date.today() - dt.timedelta(days=days - 1 - i)).isoformat()
        series.append(daily.get(day, {"date": day, "taps": 0, "installs": 0, "spend": 0}))
    # plan uses every app (channel filter applies), over the bot's lookback window
    look = (dt.date.today() - dt.timedelta(days=rules.lookback_days - 1)).isoformat()
    per_app = {}
    for r in allr:
        if (ch == "all" or r["channel"] == ch) and r["date"] >= look:
            p = per_app.setdefault(r["app"], {"spend": 0.0, "revenue": 0.0, "installs": 0.0})
            for f in ("spend", "revenue", "installs"):
                p[f] += r[f]
    plan = bot.rebalance(per_app, rules)
    by_app = {}
    for r in cur:
        b = by_app.setdefault((r["app"], r["channel"]), {"app": r["app"], "channel": r["channel"], "spend": 0.0, "taps": 0.0, "installs": 0.0, "revenue": 0.0})
        for f in ("spend", "taps", "installs", "revenue"):
            b[f] += r[f]
    # NurseBound country split
    geo = []
    nb_rows = [r for r in cur if r["app"] == "NurseBound" and (ch == "all" or r["channel"] == ch)]
    per_c, untagged = {}, 0.0
    for r in nb_rows:
        c = bot.country_for(f"{r.get('campaign') or ''} {r.get('keyword') or ''}", "NurseBound", rules)
        if not c:
            untagged += r["spend"]
            continue
        p = per_c.setdefault(c, {"spend": 0.0, "revenue": 0.0, "installs": 0.0})
        for f in ("spend", "revenue", "installs"):
            p[f] += r[f]
    split = bot.geo_split("NurseBound", plan.get("NurseBound", {}).get("daily", 0), per_c, rules)
    for g in rules.geo_targets.get("NurseBound", []):
        p = per_c.get(g["code"], {})
        geo.append({**g, "plan": split.get(g["code"], 0.0), "spend": p.get("spend", 0.0), "installs": p.get("installs", 0.0)})
    return J({"days": days, "has_data": bool(allr), "kpis": totals(cur, rules), "prev": totals(prev, rules), "series": series,
              "by_app": sorted(by_app.values(), key=lambda b: -b["spend"]), "plan": plan,
              "caps": rules.app_caps, "global_cap": rules.global_daily_cap, "auto_rebalance": rules.auto_rebalance,
              "target_acos": rules.target_acos, "max_acos": rules.max_acos, "geo": geo, "geo_untagged_spend": untagged,
              "dry_run": dry_run()})


@guarded
async def keywords(request: Request):
    days, app, ch, keep = filters(request)
    rules = bot.load_rules(con())
    cut = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    cur = [r for r in perf(days) if keep(r) and r["date"] >= cut]
    agg = bot.aggregate(cur, rules)
    q = (request.query_params.get("q") or "").lower().strip()
    out = []
    for k, a in agg.items():
        if q and q not in f"{a.get('keyword') or ''} {a.get('campaign') or ''} {a.get('app') or ''}".lower():
            continue
        action, reason = bot.keyword_decision(a, rules)
        out.append({**{f: a.get(f) for f in ("channel", "entity_type", "keyword", "campaign", "app", "status", "taps", "installs", "spend", "revenue", "cpt", "acos", "country")},
                    "key": k, "bot": action, "reason": reason, "override": rules.overrides.get(k, "auto"),
                    "revenue_estimated": bool(a.get("revenue_estimated"))})
    out.sort(key=lambda x: -(x["spend"] or 0))
    return J({"items": out[:500], "max_cpt": rules.max_cpt, "target_acos": rules.target_acos, "max_acos": rules.max_acos})


@guarded
async def override(request: Request):
    body = await request.json()
    key, status = str(body.get("key", ""))[:300], body.get("status")
    if not key or status not in ("auto", "paused", "protected"):
        return J({"error": "key and status (auto, paused, protected) are required"}, 400)
    if status == "auto":
        execute("delete from keyword_overrides where entity_key = ?", [key])
    else:
        execute("insert or replace into keyword_overrides values (?, ?, ?, ?)", [key, status, "phone app", dt.datetime.utcnow()])
    return J({"ok": True})


RULE_FIELDS = {  # name: (type, min, max)
    "global_daily_cap": (float, 0, 10000), "max_cpt": (float, 0, 1000), "min_taps_for_cpt": (int, 0, 100000),
    "zero_install_taps": (int, 1, 100000), "max_acos": (float, 0, 100), "min_spend_for_acos": (float, 0, 100000),
    "target_acos": (float, 0, 100), "auto_rebalance": (bool, None, None), "min_share": (float, 0, 1), "max_share": (float, 0, 1),
    "max_budget_step": (float, 0, 1), "lookback_days": (int, 1, 90), "telegram_chat_id": (str, None, None),
}


@guarded
async def rules_get(request: Request):
    r = asdict(bot.load_rules(con()))
    r.pop("overrides", None)
    return J(r)


def _validate_rules(body: dict, base: bot.Rules) -> bot.Rules:
    d = asdict(base)
    d.pop("overrides", None)
    for k, (typ, lo, hi) in RULE_FIELDS.items():
        if k not in body:
            continue
        v = body[k]
        if typ is bool:
            v = bool(v)
        elif typ is str:
            v = str(v or "").strip()[:64]
        else:
            v = typ(v)
            if not (lo <= v <= hi):
                raise ValueError(f"{k} must be between {lo} and {hi}")
        d[k] = v
    for k in ("app_caps", "app_priority", "value_per_install"):
        if isinstance(body.get(k), dict):
            d[k] = {a: max(0.0, float(body[k].get(a, d[k].get(a, 0)))) for a in APPS}
    if isinstance(body.get("alerts"), dict):
        d["alerts"] = {a: bool(body["alerts"].get(a, True)) for a in ("daily_summary", "kill_switch", "rebalance", "high_cpt")}
    if isinstance(body.get("geo_targets"), dict) and isinstance(body["geo_targets"].get("NurseBound"), list):
        cur = {g["code"]: g for g in d["geo_targets"].get("NurseBound", [])}
        for g in body["geo_targets"]["NurseBound"]:
            c = str(g.get("code", ""))[:2].upper()
            if c in cur:
                cur[c]["weight"] = max(0.0, float(g.get("weight", cur[c]["weight"])))
                cur[c]["enabled"] = bool(g.get("enabled", True))
        d["geo_targets"]["NurseBound"] = list(cur.values())
    if d["target_acos"] > d["max_acos"]:
        raise ValueError("Target ACoS must be at or below the ACoS ceiling")
    if d["min_share"] * len(APPS) > 1 or d["max_share"] * len(APPS) < 1:
        raise ValueError("Min share must be ≤ 33% and max share ≥ 34% with three apps")
    return bot.Rules.from_dict(d)


@guarded
async def rules_post(request: Request):
    body = await request.json()
    base = bot.load_rules(con())
    if body.get("import"):  # a rules.json exported from the dashboard
        data = dict(body["import"])
        for key, status in (data.pop("overrides", None) or {}).items():
            if status in ("paused", "protected"):
                execute("insert or replace into keyword_overrides values (?, ?, ?, ?)", [key, status, "imported", dt.datetime.utcnow()])
        body = data
    try:
        new = _validate_rules(body, base)
    except (ValueError, TypeError) as e:
        return J({"error": str(e)}, 400)
    d = asdict(new)
    d.pop("overrides", None)
    execute("insert into adbot_rules values (?, ?)", [dt.datetime.utcnow(), json.dumps(d)])
    return J({"ok": True, "rules": d})


@guarded
async def log(request: Request):
    acts = rows("select * from bot_actions order by ts desc limit 200")
    budgets = rows("select ts, app, daily from app_budgets where ts > ? order by ts", [dt.datetime.utcnow() - dt.timedelta(days=30)])
    return J({"actions": acts, "budgets": budgets, "dry_run": dry_run()})


@guarded
async def trading(request: Request):
    hb_all = trading_bots()
    if request.query_params.get("demo") != "1":   # demo / paper accounts are hidden unless asked for
        hidden = sum(1 for b in hb_all if not b["live"])
        hb_all = [b for b in hb_all if b["live"]]
        if not hb_all:
            return J({"connected": False, "demo_hidden": hidden})
    if not hb_all:
        return J({"connected": False})
    bot_id = request.query_params.get("bot") or hb_all[0]["bot_id"]
    hb = rows("select * from trading.heartbeat where bot_id = ? order by ts desc limit 1", [bot_id])
    acct = rows("select * from trading.account where bot_id = ? order by ts desc limit 1", [bot_id])
    today = dt.datetime.utcnow().date()
    start = rows("select equity from trading.account where bot_id = ? and cast(ts as date) = ? order by ts limit 1", [bot_id, today])
    risk = rows("select * from trading.risk where bot_id = ? order by ts desc limit 1", [bot_id])
    eq = rows("select ts, equity from trading.account where bot_id = ? and ts > ? order by ts", [bot_id, dt.datetime.utcnow() - dt.timedelta(days=30)])
    snap = rows("select max(snapshot_at) as s from trading.positions where bot_id = ?", [bot_id])
    pos = []
    if snap and snap[0]["s"]:
        pos = rows("select symbol, side, qty, entry, price, stop, target, unrealized_pnl, opened_at, strategy from trading.positions "
                   "where bot_id = ? and snapshot_at = ? and symbol is not null order by abs(unrealized_pnl) desc", [bot_id, snap[0]["s"]])
    trades = rows("select closed_at, opened_at, symbol, side, qty, entry, exit, pnl, strategy, reason from trading.trades "
                  "where bot_id = ? and coalesce(closed_at, opened_at) > ? order by coalesce(closed_at, opened_at) desc limit 100",
                  [bot_id, dt.datetime.utcnow() - dt.timedelta(days=7)])
    sigs = rows("select ts, symbol, direction, confidence, decision, reason, source from trading.signals "
                "where bot_id = ? and ts > ? order by ts desc limit 100", [bot_id, dt.datetime.utcnow() - dt.timedelta(days=1)])
    # thin the equity series to ≤ 300 points for phones
    if len(eq) > 300:
        step = len(eq) / 300
        eq = [eq[int(i * step)] for i in range(300)] + [eq[-1]]
    return J({"connected": True, "bots": [h["bot_id"] for h in hb_all], "bot_id": bot_id, "now": dt.datetime.utcnow(),
              "heartbeat": hb[0] if hb else None, "account": acct[0] if acct else None,
              "day_start_equity": start[0]["equity"] if start else None, "risk": risk[0] if risk else None,
              "equity": eq, "positions": pos, "trades": trades, "signals": sigs})


@guarded
async def telegram_test(request: Request):
    ok = bot.telegram("✅ <b>AdBot test</b> from the phone app.", bot.load_rules(con()))
    return J({"ok": ok, "error": None if ok else "Not sent. Check TELEGRAM_BOT_TOKEN on Render and the chat ID in Rules."})


@guarded
async def run_bot_now(request: Request):
    with _db_lock:
        res = bot.run_bot(con(), bot.load_rules(con()), dry_run())
    return J({"ok": True, "paused": res.get("paused", 0), "plan": {a: v["daily"] for a, v in (res.get("plan") or {}).items()}, "dry_run": dry_run()})


# --------------------------------------------------------------------------- #
# Revenue (Stage B: registry + data-source status; numbers arrive with Stage C/D)
# --------------------------------------------------------------------------- #
def _money_str(v):
    return None if v is None else str(v)


FACT_TABLES = ("sales_reports", "financial_reports", "subscription_events", "refund_events", "advertising_spend")


@guarded
async def revenue_get(request: Request):
    with _db_lock:
        reg = rev_registry.registry(con())
        counts = {}
        for t in FACT_TABLES:
            plat = "platform" if t == "advertising_spend" else "source_platform"
            cur = con().execute(f"select {plat} as p, count(*) as n from {REV}.{t} group by 1")
            for p, n in cur.fetchall():
                counts.setdefault(p, {})[t] = int(n)
    for a in reg["apps"]:
        for p in a["products"]:
            # the phone only needs the US price; store prices exist for ~170 regions
            us = [x for x in p["prices"] if x.get("territory") == "US"]
            p["price_regions"] = len(p["prices"])
            p["prices"] = us or p["prices"][:1]
            for x in p["prices"]:
                x["amount"] = _money_str(x["amount"])
    for s in reg["sources"]:
        s["rows"] = counts.get(s["platform"], {})
    return J({**reg, "has_revenue_data": any(counts.get(p, {}).get(t) for p in ("apple_app_store", "google_play")
                                             for t in ("sales_reports", "financial_reports")),
              "stage": "B", "dry_run": dry_run()})


@guarded
async def revenue_listing(request: Request):
    body = await request.json()
    try:
        with _db_lock:
            changed = rev_registry.update_listing(con(), str(body.get("app_id", "")), str(body.get("platform", "")),
                                                  dict(body.get("fields") or {}), actor="admin")
    except rev_registry.RegistryError as e:
        return J({"error": str(e)}, 400)
    return J({"ok": True, "changed": changed})


@guarded
async def revenue_retry_play_reports(request: Request):
    """'Retry reports' on the Google Play card: the next hourly run retries the reports step only
    (products keep their own daily guard). The service account key lives on adbot-hourly."""
    from revenue import google_play
    with _db_lock:
        google_play.request_report_retry(con(), actor="admin")
    return J({"ok": True, "next_run": store_check.next_hourly_run().isoformat()})


@guarded
async def revenue_check_ids(request: Request):
    """'Check store IDs': confirm each app's Apple ID / bundle ID / Play package with the stores. Read-only.

    The store keys normally live on adbot-hourly only. A store whose key is also set on this
    web service is checked right now; the other is left as a request for the next hourly run."""
    def work() -> dict:
        with _db_lock:
            n = store_check.request(con(), actor="admin")
            todo = {p: store_check.targets(con(), p) for p in ("apple_app_store", "google_play")}
        if not n:
            return {"error": "Enter the Apple ID or Play package for at least one app first (Edit IDs)."}
        cl = store_check.clients(quick=True)
        done, queued = [], {}
        for platform, rows in todo.items():
            if not rows:
                continue
            if cl[platform] == "no_keys":
                queued[platform] = len(rows)      # adbot-hourly has the keys: it answers at :07
                continue
            done += store_check.check_rows(cl[platform], rows)
        checked: dict = {}
        with _db_lock:
            for row, res in done:
                store_check.save(con(), row, res, "admin")
                checked.setdefault(row["platform"], {}).setdefault(res["result"], 0)
                checked[row["platform"]][res["result"]] += 1
        return {"ok": True, "checked": checked, "queued": queued,
                "next_run": store_check.next_hourly_run().isoformat() if queued else None}

    out = await run_in_threadpool(work)
    return J(out, 400 if out.get("error") else 200)


# --------------------------------------------------------------------------- #
# Front end
# --------------------------------------------------------------------------- #
async def index(request: Request):
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


async def sw(request: Request):
    # served from the root so it controls the whole app
    return FileResponse(WEB / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


async def manifest(request: Request):
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


async def health(request: Request):
    return Response("ok")


routes = [
    Route("/", index),
    Route("/healthz", health),
    Route("/sw.js", sw),
    Route("/manifest.webmanifest", manifest),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/logout", logout, methods=["POST"]),
    Route("/api/me", me),
    Route("/api/ads", ads),
    Route("/api/keywords", keywords),
    Route("/api/override", override, methods=["POST"]),
    Route("/api/rules", rules_get, methods=["GET"]),
    Route("/api/rules", rules_post, methods=["POST"]),
    Route("/api/log", log),
    Route("/api/trading", trading),
    Route("/api/telegram-test", telegram_test, methods=["POST"]),
    Route("/api/run-bot", run_bot_now, methods=["POST"]),
    Route("/api/revenue", revenue_get),
    Route("/api/revenue/listing", revenue_listing, methods=["POST"]),
    Route("/api/revenue/check-ids", revenue_check_ids, methods=["POST"]),
    Route("/api/revenue/retry-play-reports", revenue_retry_play_reports, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static"),
    Mount("/web", StaticFiles(directory=str(WEB)), name="web"),
]

app = Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=800)])
