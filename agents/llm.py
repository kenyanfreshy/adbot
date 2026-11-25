"""
Claude API client for the agents: one Messages call at a time, a daily dollar cap, every call logged.

Settings (environment, on the adbot-hourly job):
  ANTHROPIC_API_KEY      required for any AI-written text; without it agents fall back to plain templates
  AGENTS_MODEL           default claude-sonnet-5-5
  AGENTS_DAILY_USD_CAP   default 1.00 (USD per UTC day, across all agents)

Cost is computed from the token counts the API returns and the list prices below (USD per million tokens, from
platform.claude.com/docs/en/about-claude/pricing). A model not in the table is charged at the highest price listed,
so the cap errs on the safe side. Before a call, the worst case (the prompt's estimated tokens plus max_tokens of
output) must fit under what's left of the cap; otherwise the call isn't made and is logged as refused_cap.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Callable, Optional

from control.schema import SCHEMA

log = logging.getLogger("adbot.agents.llm")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_CAP_USD = 1.00
PRICES = {   # model -> (input, output) USD per million tokens
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p[1])


def price(model: str) -> tuple[float, float]:
    return PRICES.get(model, FALLBACK_PRICE)


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Claude:
    def __init__(self, con, api_key: str, model: str = DEFAULT_MODEL, daily_cap_usd: float = DEFAULT_CAP_USD,
                 clock: Callable[[], dt.datetime] = _now, post: Optional[Callable] = None):
        self.con, self.key, self.model, self.cap, self.clock = con, api_key, model, float(daily_cap_usd), clock
        self._post = post

    def spent_today(self) -> float:
        start = self.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        v = self.con.execute(f"SELECT coalesce(sum(cost_usd), 0) FROM {SCHEMA}.llm_usage WHERE at >= ?", [start]).fetchone()[0]
        return float(v or 0)

    def _log(self, agent: str, status: str, tin: Optional[int] = None, tout: Optional[int] = None,
             usd: Optional[float] = None, error: Optional[str] = None) -> None:
        self.con.execute(f"INSERT INTO {SCHEMA}.llm_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         [uuid.uuid4().hex, self.clock(), agent, self.model, tin, tout, usd, status, (error or "")[:300] or None])

    def complete(self, agent: str, system: str, prompt: str, max_tokens: int = 500) -> Optional[str]:
        """The model's text, or None (cap reached, API error, empty answer). Never raises."""
        try:
            worst = cost(self.model, (len(system) + len(prompt)) // 3 + 50, max_tokens)
            left = self.cap - self.spent_today()
            if worst > left:
                self._log(agent, "refused_cap", error=f"needs up to ${worst:.4f}, ${max(left, 0):.4f} left of ${self.cap:.2f} today")
                return None
            post = self._post
            if post is None:
                import requests
                post = requests.post
            r = post(API_URL, headers={"x-api-key": self.key, "anthropic-version": API_VERSION, "content-type": "application/json"},
                     json={"model": self.model, "max_tokens": int(max_tokens), "system": system,
                           "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            if getattr(r, "status_code", 200) != 200:
                self._log(agent, "failed", error=f"HTTP {r.status_code}: {str(getattr(r, 'text', ''))[:200]}")
                return None
            body = r.json()
            u = body.get("usage") or {}
            tin = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
            tout = int(u.get("output_tokens") or 0)
            self._log(agent, "ok", tin, tout, round(cost(self.model, tin, tout), 6))
            text = "".join(b.get("text", "") for b in (body.get("content") or []) if b.get("type") == "text").strip()
            return text or None
        except Exception as e:  # noqa: BLE001 - an agent without AI text still works
            log.warning("Claude call for %s failed: %s", agent, e)
            try:
                self._log(agent, "failed", error=f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
            return None

    def complete_json(self, agent: str, system: str, prompt: str, max_tokens: int = 1500):
        """complete() asked for JSON only; the parsed object, or None."""
        return parse_json(self.complete(agent, system + " Answer with JSON only, no other text.", prompt, max_tokens))


def parse_json(text: Optional[str]):
    """The first JSON object or array in a model answer (tolerates ```json fences and text around it)."""
    import json
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    return None


def from_env(con, environ=None, clock: Callable[[], dt.datetime] = _now) -> Optional[Claude]:
    env = os.environ if environ is None else environ
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return None
    try:
        cap = float(env.get("AGENTS_DAILY_USD_CAP") or DEFAULT_CAP_USD)
    except ValueError:
        cap = DEFAULT_CAP_USD
    return Claude(con, key, (env.get("AGENTS_MODEL") or DEFAULT_MODEL).strip(), cap, clock)


def usage_today(con, clock: Callable[[], dt.datetime] = _now) -> dict:
    start = clock().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        calls, usd, refused = con.execute(
            f"SELECT count(*) FILTER (WHERE status = 'ok'), coalesce(sum(cost_usd), 0), count(*) FILTER (WHERE status = 'refused_cap') "
            f"FROM {SCHEMA}.llm_usage WHERE at >= ?", [start]).fetchone()
    except Exception:  # noqa: BLE001 - table not created yet
        return {"calls": 0, "usd": 0.0, "refused": 0}
    return {"calls": int(calls or 0), "usd": round(float(usd or 0), 4), "refused": int(refused or 0)}
