"""
trading_charts.py — price candles plus moving averages and oscillators for the Trading tab.

Where the candles come from (first that has enough data wins):
  1. trading.bars — candles the trading bot reports itself (StatusReporter.bars), so the
     chart shows exactly the broker's prices.
  2. Yahoo Finance's public chart endpoint (no key). Close to broker prices for FX majors,
     metals and indices, but not identical (different feed and spreads).

Indicators (computed here, on the full history, then trimmed to what the phone shows):
  overlays     EMA 20, EMA 50, SMA 200
  oscillators  RSI 14 (Wilder), MACD 12/26/9, Stochastic 14/3/3

Read-only: nothing here can touch the trading system.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from typing import Callable, Sequence

# timeframe -> (yahoo interval, yahoo range, hours to merge into one bar, cache seconds)
TIMEFRAMES = {
    "15m": ("15m", "5d", None, 60),
    "1h": ("60m", "1mo", None, 120),
    "4h": ("60m", "3mo", 4, 300),
    "1d": ("1d", "2y", None, 900),
}
DEFAULT_TF = "1h"
MIN_DB_BARS = 60          # fewer reported bars than this → fall back to the public feed
SHOW_BARS = 120           # bars sent to the phone (indicators are warmed up on everything before)

CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK", "SGD", "HKD", "ZAR", "MXN",
       "TRY", "PLN", "CZK", "HUF", "CNH", "CNY", "INR", "KES", "THB", "ILS"}
SPECIAL = {
    "XAUUSD": "GC=F", "GOLD": "GC=F", "XAGUSD": "SI=F", "SILVER": "SI=F",
    "USOIL": "CL=F", "WTI": "CL=F", "XTIUSD": "CL=F", "UKOIL": "BZ=F", "BRENT": "BZ=F", "XBRUSD": "BZ=F",
    "US30": "YM=F", "DJ30": "YM=F", "WS30": "YM=F", "NAS100": "NQ=F", "USTEC": "NQ=F", "US100": "NQ=F",
    "SPX500": "ES=F", "US500": "ES=F", "GER40": "^GDAXI", "DE40": "^GDAXI", "GER30": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
}
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "LTC", "ADA", "DOGE", "BNB"}


def yahoo_symbol(symbol: str) -> str:
    """Broker symbol ("EUR/USD", "EURUSD.m", "XAUUSD", "BTCUSD") -> Yahoo ticker."""
    raw = str(symbol or "").strip().upper()
    s = re.sub(r"[^A-Z0-9]", "", raw.split(".")[0])
    for suf in ("PRO", "MICRO", "ECN", "RAW"):
        if s.endswith(suf) and len(s) > len(suf) + 5:
            s = s[: -len(suf)]
    if s in SPECIAL:
        return SPECIAL[s]
    if len(s) == 7 and s[:3] in CCY and s[3:6] in CCY:      # EURUSDm, EURUSDc …
        s = s[:6]
    if len(s) == 6 and s[:3] in CCY and s[3:] in CCY:
        return s + "=X"
    for c in CRYPTO:
        if s.startswith(c) and s[len(c):] in ("USD", "USDT"):
            return f"{c}-USD"
    return raw or s


# --------------------------------------------------------------------------- #
# Indicators: plain lists, None where there isn't enough history yet
# --------------------------------------------------------------------------- #
Series = list  # list[float | None]


def sma(xs: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: Sequence[float | None], n: int) -> Series:
    """EMA seeded with the SMA of the first n values; leading None values are skipped."""
    out: Series = [None] * len(xs)
    k = 2.0 / (n + 1)
    start = next((i for i, x in enumerate(xs) if x is not None), None)
    if start is None or len(xs) - start < n:
        return out
    prev = sum(xs[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n

    def val(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = val(ag, al)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14, smooth_k: int = 3, d: int = 3) -> dict:
    raw: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        hh, ll = max(highs[i - n + 1:i + 1]), min(lows[i - n + 1:i + 1])
        raw[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
    k = _sma_opt(raw, smooth_k)
    return {"k": k, "d": _sma_opt(k, d)}


def _sma_opt(xs: Series, n: int) -> Series:
    out: Series = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        if all(v is not None for v in w):
            out[i] = sum(w) / n
    return out


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #
def merge_hours(bars: list[dict], hours: int) -> list[dict]:
    """Merge 1h bars into N-hour bars aligned to UTC midnight (00, 04, 08 … for 4h)."""
    out: list[dict] = []
    for b in bars:
        t = b["t"]
        key = t.replace(hour=t.hour - t.hour % hours, minute=0, second=0, microsecond=0)
        if out and out[-1]["t"] == key:
            o = out[-1]
            o["h"], o["l"], o["c"] = max(o["h"], b["h"]), min(o["l"], b["l"]), b["c"]
            o["v"] = (o.get("v") or 0) + (b.get("v") or 0)
        else:
            out.append({**b, "t": key})
    return out


def parse_yahoo(js: dict) -> list[dict]:
    res = ((js or {}).get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = (((r.get("indicators") or {}).get("quote")) or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [None] * len(ts))[i]
        out.append({"t": dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(tzinfo=None), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": v})
    return out


def fetch_yahoo(ticker: str, tf: str, get: Callable | None = None) -> list[dict]:
    interval, rng, merge, _ = TIMEFRAMES[tf]
    if get is None:
        import requests
        get = requests.get
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = get(url, params={"interval": interval, "range": rng, "includePrePost": "false"},
            headers={"User-Agent": "Mozilla/5.0 (AdBot control panel)"}, timeout=8)
    if getattr(r, "status_code", 200) != 200:
        raise RuntimeError(f"price feed returned HTTP {r.status_code}")
    bars = parse_yahoo(r.json())
    return merge_hours(bars, merge) if merge else bars


_cache: dict = {}
_cache_lock = threading.Lock()


def cached_feed(ticker: str, tf: str, get: Callable | None = None) -> list[dict]:
    ttl = TIMEFRAMES[tf][3]
    key = (ticker, tf)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    bars = fetch_yahoo(ticker, tf, get)
    with _cache_lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (time.time(), bars)
    return bars


def db_bars(rows_fn: Callable, bot_id: str, symbol: str, tf: str) -> list[dict]:
    """Candles the bot reported itself (trading.bars). A missing table gives []."""
    rs = rows_fn("select ts, open, high, low, close, volume from trading.bars where bot_id = ? and symbol = ? and timeframe = ? "
                 "order by ts desc limit 600", [bot_id, symbol, tf])
    out = []
    for r in reversed(rs):
        try:
            out.append({"t": r["ts"], "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"]), "v": r.get("volume")})
        except (TypeError, ValueError, KeyError):
            continue
    return out


# --------------------------------------------------------------------------- #
# The chart payload
# --------------------------------------------------------------------------- #
def build(bars: list[dict], show: int = SHOW_BARS) -> dict:
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    m = macd(closes)
    st = stochastic(highs, lows, closes)
    series = {
        "ema20": ema(closes, 20), "ema50": ema(closes, 50), "sma200": sma(closes, 200),
        "rsi": rsi(closes, 14), "macd": m["macd"], "macd_signal": m["signal"], "macd_hist": m["hist"],
        "stoch_k": st["k"], "stoch_d": st["d"],
    }
    cut = max(0, len(bars) - show)
    rnd = lambda v: None if v is None else round(v, 6)  # noqa: E731
    out = {"bars": [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars[cut:]]}
    for k, xs in series.items():
        out[k] = [rnd(v) for v in xs[cut:]]
    out["summary"] = summary(out)
    return out


def summary(d: dict) -> dict:
    """Plain-language read of the latest values (descriptive only, not a signal)."""
    last = lambda k: next((v for v in reversed(d.get(k) or []) if v is not None), None)  # noqa: E731
    price = d["bars"][-1]["c"] if d["bars"] else None
    e20, e50, s200, r, h, k = last("ema20"), last("ema50"), last("sma200"), last("rsi"), last("macd_hist"), last("stoch_k")
    trend = None
    if price is not None and e20 is not None and e50 is not None:
        trend = "up" if price > e20 > e50 else "down" if price < e20 < e50 else "mixed"
    return {
        "price": price, "trend": trend,
        "above_sma200": None if s200 is None or price is None else price > s200,
        "rsi": r, "rsi_zone": None if r is None else "overbought" if r >= 70 else "oversold" if r <= 30 else "neutral",
        "macd_hist": h, "macd_bias": None if h is None else "bullish" if h > 0 else "bearish" if h < 0 else "flat",
        "stoch_k": k, "stoch_zone": None if k is None else "overbought" if k >= 80 else "oversold" if k <= 20 else "neutral",
    }


def chart(symbol: str, tf: str, bot_id: str, rows_fn: Callable, get: Callable | None = None) -> dict:
    tf = tf if tf in TIMEFRAMES else DEFAULT_TF
    bars = db_bars(rows_fn, bot_id, symbol, tf)
    source = "bot"
    if len(bars) < MIN_DB_BARS:
        ticker = yahoo_symbol(symbol)
        bars = cached_feed(ticker, tf, get)
        source = f"Yahoo Finance ({ticker})"
    if not bars:
        return {"symbol": symbol, "tf": tf, "source": source, "bars": [], "error": "No price data for this symbol."}
    return {"symbol": symbol, "tf": tf, "source": source, **build(bars)}
