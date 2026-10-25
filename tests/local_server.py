"""Run the phone app locally against made-up data (no database needed):  python tests/local_server.py
Then open http://127.0.0.1:8765 and sign in with 'test-pass'. For development only."""
import datetime as dt, json, os, random, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("APP_PASSWORD", "test-pass")
import dlt_pipeline as bot

random.seed(4)
today = dt.date.today()
PERF = []
KW = {"Rapid Nursing Review": ["RNR | nclex rn", "RNR | nclex practice questions", "RNR | nclex app"],
      "CCRN Adult": ["CCRN | ccrn exam prep", "CCRN | critical care nursing"],
      "NurseBound": ["NurseBound | PH | Prospecting", "NurseBound | IN | Nurses", "NurseBound | NG | Interests", "NurseBound | GB | NMC"]}
for i in range(40):
    d = (today - dt.timedelta(days=i)).isoformat()
    for app, kws in KW.items():
        for j, k in enumerate(kws):
            ch = ["Apple Search Ads", "Google Ads", "Meta Ads"][j % 3]
            taps = random.randint(3, 20); cpt = random.uniform(.4, 1.6 if j == 1 else 1.0); inst = 0 if (j == 2 and app == "CCRN Adult") else int(taps * random.uniform(.1, .35))
            PERF.append({"date": d, "channel": ch, "entity_id": f"{app[:3]}{j}", "entity_type": "keyword", "keyword": k.split("|")[-1].strip(), "campaign_id": f"c{app[:2]}{j}",
                         "campaign": k, "adgroup_id": "g", "app": app, "status": "ACTIVE", "impressions": taps * 20, "taps": taps, "installs": inst,
                         "spend": round(taps * cpt, 2), "revenue": round(inst * random.uniform(3, 9), 2)})
now = dt.datetime.utcnow()
TR = {
 "heartbeat": [{"ts": now - dt.timedelta(minutes=1), "bot_id": "fx-trading-system", "mode": "paper", "status": "running", "message": "5 services up"}],
 "account": [{"ts": now - dt.timedelta(hours=h), "bot_id": "fx-trading-system", "mode": "paper", "equity": 10000 + 40 * max(0, 30 - h / 24) ** .5 + random.uniform(-15, 15), "balance": 10000,
              "margin_used": 120.0, "day_pnl": 18.4, "total_pnl": 212.75, "currency": "USD"} for h in range(0, 30 * 24, 6)],
 "positions": [{"symbol": "EURUSD", "side": "buy", "qty": 0.1, "entry": 1.0812, "price": 1.0831, "stop": 1.079, "target": 1.086, "unrealized_pnl": 19.0, "opened_at": now, "strategy": ""},
               {"symbol": "GBPJPY", "side": "sell", "qty": 0.05, "entry": 191.2, "price": 191.45, "stop": 192.0, "target": 189.8, "unrealized_pnl": -8.2, "opened_at": now, "strategy": ""}],
 "trades": [{"closed_at": now - dt.timedelta(hours=k * 9), "opened_at": now, "symbol": s, "side": "buy", "qty": .1, "entry": 1, "exit": 1, "pnl": p, "strategy": "breakout", "reason": "TAKE_PROFIT" if p > 0 else "STOP_LOSS"}
            for k, (s, p) in enumerate([("AUDUSD", 24.1), ("EURUSD", -11.3), ("USDJPY", 8.9)])],
 "signals": [{"ts": now - dt.timedelta(minutes=20 * k), "symbol": s, "direction": dr, "confidence": c, "decision": de, "reason": r, "source": "trend_continuation"}
             for k, (s, dr, c, de, r) in enumerate([("GBPUSD", "sell", .71, "blocked", "daily loss limit 80% used"), ("EURUSD", "buy", .66, "taken", "Risk Engine approved"), ("AUDUSD", "buy", .52, "skipped", "REGIME_MISMATCH")])],
 "risk": [{"ts": now, "daily_loss_limit": 200.0, "daily_loss_used": 46.0, "max_exposure": None, "exposure": None, "kill_switch": False, "notes": "NORMAL: ok · drawdown 1.4%"}],
}
STORE = {"rules": None, "overrides": {}, "actions": [{"ts": now, "dry_run": True, "kind": "pause", "channel": "Google Ads", "app": "CCRN Adult", "entity_key": "Google Ads|CCR2",
          "keyword": "critical care nursing", "detail": "16 taps and no installs", "applied": False, "error": None}]}

class Cur:
    def __init__(s, rows): s._r = rows; s.description = [(k,) for k in (rows[0].keys() if rows else [])]
    def fetchall(s): return [tuple(r.values()) for r in s._r]
    def fetchone(s): r = s.fetchall(); return r[0] if r else None
try:
    import duckdb as _duck
except ImportError:  # sandbox without the duckdb package: DuckDB CLI shim
    sys.path.insert(0, os.path.dirname(__file__)); import duckdb_cli_shim as _duck
import revenue
REVCON = _duck.connect(":memory:")
revenue.ensure(REVCON)

class FakeCon:
    def execute(self, sql, p=None):
        l = sql.lower()
        if "revenue." in l or "information_schema" in l: return REVCON.execute(sql, p)
        if l.startswith("insert into adbot_rules"): STORE["rules"] = p[1]; return Cur([])
        if "keyword_overrides" in l and l.startswith("insert"): STORE["overrides"][p[0]] = p[1]; return Cur([])
        if l.startswith("delete from keyword_overrides"): STORE["overrides"].pop(p[0], None); return Cur([])
        if "from adbot_rules" in l: return Cur([{"rules": STORE["rules"]}] if STORE["rules"] else [])
        if "from keyword_overrides" in l: return Cur([{"entity_key": k, "status": v} for k, v in STORE["overrides"].items()])
        if "ad_performance" in l: return Cur([r for r in PERF if r["date"] >= p[0]])
        if "from bot_actions" in l: return Cur(STORE["actions"])
        if "from app_budgets" in l: return Cur([])
        if "group by bot_id" in l: return Cur([{"bot_id": "fx-trading-system", "mode": TR["heartbeat"][0]["mode"], "last": now}])
        m = re.search(r"from trading\.(\w+)", l)
        if m:
            t = m.group(1)
            if "max(snapshot_at)" in l: return Cur([{"s": now}])
            rows = TR[t]
            if t == "account" and "limit 1" in l: rows = [rows[0]]
            if t == "account" and "select ts, equity" in l: rows = [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(TR["account"])]
            if t == "account" and "select equity" in l: rows = [{"equity": TR["account"][-1]["equity"]}]
            return Cur(rows)
        return Cur([])
bot.connect = lambda: FakeCon()
bot.telegram = lambda text, rules=None: False
if __name__ == "__main__":
    import uvicorn, web
    uvicorn.run(web.app, host="127.0.0.1", port=int(os.getenv("PORT", "8765")), log_level="warning")
