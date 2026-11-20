"""Trading tab charts: symbol mapping, indicator maths, 4h merging, data-source choice and the reporter's bars()."""
import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import trading_charts as tc  # noqa: E402


def test_yahoo_symbol():
    assert tc.yahoo_symbol("EURUSD") == "EURUSD=X"
    assert tc.yahoo_symbol("EUR/USD") == "EURUSD=X"
    assert tc.yahoo_symbol("GBPJPY.m") == "GBPJPY=X"
    assert tc.yahoo_symbol("EURUSDm") == "EURUSD=X"
    assert tc.yahoo_symbol("XAUUSD") == "GC=F"
    assert tc.yahoo_symbol("BTCUSD") == "BTC-USD"
    assert tc.yahoo_symbol("NAS100") == "NQ=F"
    assert tc.yahoo_symbol("AAPL") == "AAPL"


def test_sma_ema():
    xs = [float(i) for i in range(1, 11)]
    s = tc.sma(xs, 3)
    assert s[:2] == [None, None] and s[2] == 2.0 and s[-1] == 9.0
    e = tc.ema(xs, 3)
    assert e[2] == 2.0                      # seeded with the SMA
    assert abs(e[3] - (4 * .5 + 2 * .5)) < 1e-12
    assert tc.ema([None, None, 1.0, 2.0, 3.0], 3)[4] == 2.0


def test_rsi_bounds_and_direction():
    up = [100 + i for i in range(30)]
    assert tc.rsi(up)[-1] == 100.0
    down = [100 - i for i in range(30)]
    assert tc.rsi(down)[-1] == 0.0
    flat = [100.0] * 30
    assert tc.rsi(flat)[-1] == 50.0
    wave = [100 + math.sin(i / 3) for i in range(80)]
    r = tc.rsi(wave)
    assert r[13] is None and r[14] is not None
    assert all(0 <= v <= 100 for v in r if v is not None)


def test_rsi_matches_wilder_reference():
    # classic Wilder example (StockCharts RSI table): first RSI ≈ 70.53, second ≈ 66.32
    c = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    r = tc.rsi(c, 14)
    assert abs(r[14] - 70.53) < 0.1 and abs(r[15] - 66.32) < 0.1   # the table rounds its averages


def test_macd_and_stoch():
    closes = [100 + math.sin(i / 5) * 3 + i * .05 for i in range(120)]
    m = tc.macd(closes)
    i = len(closes) - 1
    assert abs(m["hist"][i] - (m["macd"][i] - m["signal"][i])) < 1e-12
    assert m["macd"][24] is None and m["macd"][25] is not None and m["signal"][33] is not None
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    st = tc.stochastic(highs, lows, closes)
    assert all(0 <= v <= 100 for v in st["k"] + st["d"] if v is not None)
    assert st["d"][-1] is not None


def _bars(n, start=dt.datetime(2026, 11, 1), hours=1):
    return [{"t": start + dt.timedelta(hours=hours * i), "o": 1 + i * 1e-4, "h": 1 + i * 1e-4 + 5e-4, "l": 1 + i * 1e-4 - 5e-4, "c": 1 + (i + 1) * 1e-4, "v": 1}
            for i in range(n)]


def test_merge_hours():
    m = tc.merge_hours(_bars(9, dt.datetime(2026, 11, 2, 1)), 4)
    assert [b["t"].hour for b in m] == [0, 4, 8]
    assert m[1]["o"] == _bars(9, dt.datetime(2026, 11, 2, 1))[3]["o"] and m[1]["v"] == 4


def test_build_trims_and_summarises():
    d = tc.build(_bars(400))
    assert len(d["bars"]) == tc.SHOW_BARS
    for k in ("ema20", "ema50", "sma200", "rsi", "macd", "macd_signal", "macd_hist", "stoch_k", "stoch_d"):
        assert len(d[k]) == tc.SHOW_BARS and d[k][-1] is not None, k
    s = d["summary"]
    assert s["trend"] == "up" and s["above_sma200"] is True and s["rsi_zone"] == "overbought"


class _Resp:
    status_code = 200
    def __init__(self, js): self._js = js
    def json(self): return self._js


def test_chart_prefers_bot_bars_then_feed():
    bars = _bars(100)
    db = [{"ts": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": 1} for b in reversed(bars)]
    calls = []
    def get(url, **kw):
        calls.append((url, kw["params"]))
        ts = [int((b["t"] - dt.datetime(1970, 1, 1)).total_seconds()) for b in bars]
        q = {k: [b[k[0]] for b in bars] for k in ("open", "high", "low", "close")}
        q["volume"] = [1] * len(bars)
        return _Resp({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [q]}}]}})
    d = tc.chart("EURUSD", "1h", "bot", lambda sql, p: db, get)
    assert d["source"] == "bot" and not calls and len(d["bars"]) == 100
    tc._cache.clear()
    d = tc.chart("EURUSD", "4h", "bot", lambda sql, p: [], get)
    assert d["source"].startswith("Yahoo") and calls[0][0].endswith("/EURUSD=X") and calls[0][1]["interval"] == "60m"
    assert len(d["bars"]) == 25            # 100 hourly bars → 25 four-hour bars
    tc.chart("EURUSD", "4h", "bot", lambda sql, p: [], get)
    assert len(calls) == 1                 # cached
    d = tc.chart("EURUSD", "weird", "bot", lambda sql, p: db, get)
    assert d["tf"] == "1h"


def test_reporter_bars_writes_and_overwrites(tmp_path, monkeypatch):
    try:
        import duckdb
    except ImportError:
        import pytest
        pytest.skip("duckdb not installed")
    monkeypatch.setenv("ADBOT_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    from trading_status import StatusReporter
    r = StatusReporter(bot_id="b", mode="paper")
    r.bars("EURUSD", "1h", [{"ts": "2026-11-19T10:00:00Z", "open": 1, "high": 2, "low": .5, "close": 1.5},
                            {"ts": "2026-11-19T11:00:00Z", "open": 1.5, "high": 2, "low": 1, "close": None}])   # bad row dropped
    r.bars("EURUSD", "1h", [{"ts": "2026-11-19T10:00:00Z", "open": 1, "high": 2.5, "low": .5, "close": 1.7}])     # same bar, updated
    r.bars("EURUSD", "7m", [{"ts": "2026-11-19T10:00:00Z", "open": 1, "high": 2, "low": 1, "close": 1}])          # unknown timeframe ignored
    r.flush(10)
    import time; time.sleep(.3)
    r._con.close() if r._con else None
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    assert c.execute("select timeframe, high, close from trading.bars").fetchall() == [("1h", 2.5, 1.7)]
