"""
Alerts: deduplicated, severity-based cooldowns, every delivery (or failure) recorded.

Transports
  telegram  real messages (TELEGRAM_BOT_TOKEN + chat id)
  log       write to the job log only (the default when Telegram isn't configured)
  fake      collect in memory (tests and development; never sends anything)
Pick with ALERT_TRANSPORT; tests always pass a FakeTransport.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Optional

from .schema import SCHEMA

log = logging.getLogger("adbot.alerts")

COOLDOWN = {"info": dt.timedelta(hours=24), "warning": dt.timedelta(hours=6), "critical": dt.timedelta(hours=1)}
CATEGORIES = {   # category -> (rules.alerts toggle, default on)
    "import_failure": ("import_failure", True),
    "stale_data": ("stale_data", True),
    "unmapped": ("unmapped", True),
    "budget": ("budget", True),
    "emergency": ("emergency", True),          # always on: can't be switched off
    "execution_failure": ("execution_failure", True),
    "proposals": ("kill_switch", True),        # proposed changes (old toggle name kept)
    "applied": ("kill_switch", True),
    "daily_summary": ("daily_summary", True),
}
ALWAYS_ON = {"emergency", "execution_failure"}


class FakeTransport:
    name = "fake"

    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("delivery failed (fake)")
        self.sent.append(text)


class LogTransport:
    name = "log"

    def send(self, text: str) -> None:
        log.info("ALERT: %s", text)


class TelegramTransport:
    name = "telegram"

    def __init__(self, token: str, chat: str):
        self.token, self.chat = token, chat

    def send(self, text: str) -> None:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram answered HTTP {r.status_code}")


def transport_from_env(rules=None, environ=None):
    env = os.environ if environ is None else environ
    kind = str(env.get("ALERT_TRANSPORT", "") or "").lower()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID") or (getattr(rules, "telegram_chat_id", "") if rules else "")
    if kind == "fake":
        return FakeTransport()
    if kind == "log" or not (token and chat):
        return LogTransport()
    return TelegramTransport(token, chat)


class Alerter:
    def __init__(self, con, transport, rules=None, clock=None):
        self.con, self.t, self.rules = con, transport, rules
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

    def enabled(self, category: str) -> bool:
        if category in ALWAYS_ON:
            return True
        toggle, default = CATEGORIES.get(category, (category, True))
        return bool(((getattr(self.rules, "alerts", None) or {}).get(toggle, default)))

    def send(self, category: str, severity: str, dedup_key: str, title: str, body: str = "") -> str:
        """Returns the delivery status: delivered | suppressed | failed | logged | disabled."""
        now = self.clock()
        if not self.enabled(category):
            status, err = "disabled", None
        else:
            last = self.con.execute(f"SELECT max(created_at) FROM {SCHEMA}.alerts WHERE dedup_key = ? "
                                    "AND delivery_status IN ('delivered', 'logged')", [dedup_key]).fetchone()[0]
            if isinstance(last, str):
                last = dt.datetime.fromisoformat(last.replace(" ", "T"))
            if last and now - last < COOLDOWN.get(severity, COOLDOWN["warning"]):
                status, err = "suppressed", None
            else:
                icon = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}.get(severity, "")
                try:
                    self.t.send(f"{icon} <b>{title}</b>\n{body}".strip())
                    status, err = ("logged" if getattr(self.t, "name", "") == "log" else "delivered"), None
                except Exception as e:  # noqa: BLE001 - shown on the Log tab
                    status, err = "failed", f"{type(e).__name__}: {str(e)[:200]}"
        self.con.execute(f"INSERT INTO {SCHEMA}.alerts VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [uuid.uuid4().hex, dedup_key, category, severity, title[:200], (body or "")[:2000], now,
                          getattr(self.t, "name", "?"), status, err])
        return status


def recent(con, limit: int = 100) -> list[dict]:
    cur = con.execute(f"SELECT created_at, category, severity, title, body, transport, delivery_status, error FROM {SCHEMA}.alerts "
                      f"ORDER BY created_at DESC LIMIT {int(limit)}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
