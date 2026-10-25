"""
AdBot pipeline for Rapid Nursing Review, CCRN Adult and NurseBound.

What one run does:
  1. Pulls daily keyword / ad-set / campaign performance from Apple Search Ads,
     Meta Ads and Google Ads (each source is skipped when its credentials are
     not set) and merges it into DuckDB (local file) or MotherDuck.
  2. Applies the bot rules: per-keyword kill-switch (CPT ceiling, zero-installs
     after N taps, ACoS ceiling), manual overrides, and budget rebalancing across
     the three apps inside the daily caps.
  3. Sends Telegram alerts for what it did (or would do, in dry-run mode).

Safety: DRY_RUN defaults to "true". Nothing is paused and no budget is changed
until you set DRY_RUN=false. Every decision is written to the bot_actions table.

Usage:
  python dlt_pipeline.py                 # sync last 14 days + run the bot
  python dlt_pipeline.py --days 30       # longer backfill
  python dlt_pipeline.py --no-sync       # bot only, on data already loaded
  python dlt_pipeline.py --test-telegram # send a test alert and exit
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Iterator

import requests

log = logging.getLogger("adbot")

APPS = ["Rapid Nursing Review", "CCRN Adult", "NurseBound"]

# NurseBound ad targeting: countries that internationally educated nurses come from, on their way to the
# US, Canada, Australia and New Zealand. weight = share of NurseBound's daily budget (they're normalised).
# Put the country code in each campaign or ad set name (e.g. "NurseBound | PH | Prospecting") so the bot
# can report and budget by country.
NURSEBOUND_GEO = [
    {"code": "PH", "tier": 1, "weight": 28, "dest": ["US", "CA", "AU", "NZ"], "enabled": True},
    {"code": "IN", "tier": 1, "weight": 24, "dest": ["US", "CA", "AU", "NZ"], "enabled": True},
    {"code": "NG", "tier": 2, "weight": 10, "dest": ["US", "CA", "AU"], "enabled": True, "flag": "who_safeguards"},
    {"code": "GB", "tier": 2, "weight": 9,  "dest": ["CA", "AU", "NZ"], "enabled": True},
    {"code": "KE", "tier": 3, "weight": 5,  "dest": ["CA", "AU", "NZ"], "enabled": True},
    {"code": "CN", "tier": 3, "weight": 4,  "dest": ["AU", "NZ", "US"], "enabled": True, "flag": "china_reach"},
    {"code": "ZA", "tier": 3, "weight": 4,  "dest": ["CA", "AU", "NZ"], "enabled": True},
    {"code": "ZW", "tier": 3, "weight": 3,  "dest": ["CA", "AU", "NZ"], "enabled": True, "flag": "who_safeguards"},
    {"code": "JM", "tier": 4, "weight": 3,  "dest": ["US"], "enabled": True},
    {"code": "NP", "tier": 4, "weight": 3,  "dest": ["AU"], "enabled": True},
    {"code": "FJ", "tier": 4, "weight": 2,  "dest": ["NZ"], "enabled": True},
    {"code": "CA", "tier": 4, "weight": 2,  "dest": ["US"], "enabled": True},
    {"code": "US", "tier": 4, "weight": 1,  "dest": ["CA"], "enabled": True},
    {"code": "AU", "tier": 4, "weight": 1,  "dest": ["NZ"], "enabled": True},
    {"code": "NZ", "tier": 4, "weight": 1,  "dest": ["AU"], "enabled": True},
]
CHANNELS = ["Apple Search Ads", "Meta Ads", "Google Ads"]
TABLE = "ad_performance"
DATASET = os.getenv("ADBOT_DATASET", "ads")


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
@dataclass
class Rules:
    global_daily_cap: float = 33.33           # $/day across all apps (≈ $1,000/month)
    app_caps: dict = field(default_factory=lambda: {"Rapid Nursing Review": 15.0, "CCRN Adult": 8.33, "NurseBound": 10.0})
    # priority multiplies each app's performance score, so Rapid Nursing Review gets the most unless it does far worse
    app_priority: dict = field(default_factory=lambda: {"Rapid Nursing Review": 1.5, "CCRN Adult": 0.75, "NurseBound": 1.0})
    max_cpt: float = 1.25                     # pause a keyword when its cost per tap goes above this
    min_taps_for_cpt: int = 5                 # ...but only once it has this many taps
    zero_install_taps: int = 15               # pause when it has this many taps and 0 installs
    max_acos: float = 0.60                    # pause when ACoS (spend / revenue) is above this, once spend >= min_spend_for_acos
    min_spend_for_acos: float = 10.0
    target_acos: float = 0.40                 # shown as the goal on dashboards
    auto_rebalance: bool = True
    min_share: float = 0.10                   # each app keeps at least 10% of the portfolio cap
    max_share: float = 0.60                   # and at most 60%
    max_budget_step: float = 0.25             # a run may move an app's budget by at most 25%
    lookback_days: int = 7                    # decisions use the last N days
    value_per_install: dict = field(default_factory=lambda: {a: 0.0 for a in APPS})  # used when a platform reports no revenue
    app_match: dict = field(default_factory=lambda: {
        "Rapid Nursing Review": ["rnr", "rapid", "nclex"],
        "CCRN Adult": ["ccrn", "critical care", "ccc"],
        "NurseBound": ["nursebound", "nurse bound", "nb"],
    })
    telegram_chat_id: str = ""
    alerts: dict = field(default_factory=lambda: {"daily_summary": True, "kill_switch": True, "rebalance": True, "high_cpt": True})
    overrides: dict = field(default_factory=dict)  # entity_key -> "paused" | "protected" | "active"
    geo_targets: dict = field(default_factory=lambda: {"NurseBound": [dict(g) for g in NURSEBOUND_GEO]})

    @classmethod
    def from_dict(cls, d: dict | None) -> "Rules":
        r = cls()
        for k, v in (d or {}).items():
            if hasattr(r, k) and v is not None:
                setattr(r, k, v)
        return r


def load_rules(con=None) -> Rules:
    """Rules come from (highest first): the adbot_rules table written by app.py,
    the RULES_JSON env var (a rules.json exported from the dashboard), or defaults."""
    data: dict = {}
    env = os.getenv("RULES_JSON")
    if env:
        try:
            data = json.loads(env) if env.strip().startswith("{") else json.load(open(env))
        except Exception as e:  # noqa: BLE001
            log.warning("RULES_JSON could not be read: %s", e)
    if con is not None:
        try:
            row = con.execute("select rules from adbot_rules order by updated_at desc limit 1").fetchone()
            if row:
                data = {**data, **json.loads(row[0])}
            for key, status in con.execute("select entity_key, status from keyword_overrides").fetchall():
                data.setdefault("overrides", {})[key] = status
        except Exception:  # tables not created yet
            pass
    return Rules.from_dict(data)


def app_for(name: str, rules: Rules) -> str | None:
    """Map a campaign / ad set name to one of the three apps using rules.app_match."""
    low = (name or "").lower()
    for app, needles in rules.app_match.items():
        for n in needles:
            if re.search(r"(^|[^a-z0-9])" + re.escape(n.lower()) + r"($|[^a-z0-9])", low):
                return app
    return None


# --------------------------------------------------------------------------- #
# Decision engine (pure functions, no network) — mirrored in the dashboard
# --------------------------------------------------------------------------- #
def country_for(name: str, app: str | None, rules: Rules) -> str | None:
    """Find a targeted country code (e.g. PH) written as its own token in a campaign / ad set name."""
    codes = [g["code"] for g in rules.geo_targets.get(app or "", [])]
    for tok in re.split(r"[^A-Za-z]+", name or ""):
        if len(tok) == 2 and tok.upper() == tok and tok in codes:
            return tok
    return None


def geo_split(app: str, daily: float, per_country: dict[str, dict], rules: Rules) -> dict[str, float]:
    """Split one app's daily budget across its target countries: weight x (efficiency vs. the app median).
    Countries with no spend yet keep their plain weight so they can earn data."""
    targets = [g for g in rules.geo_targets.get(app, []) if g.get("enabled", True) and float(g.get("weight", 0)) > 0]
    if not targets:
        return {}
    eff = {}
    for g in targets:
        p = per_country.get(g["code"], {})
        sp = float(p.get("spend", 0))
        eff[g["code"]] = (float(p.get("revenue", 0)) or float(p.get("installs", 0))) / sp if sp else None
    known = sorted(v for v in eff.values() if v)
    med = known[len(known) // 2] if known else None
    score = {g["code"]: float(g["weight"]) * (min(2.0, max(0.5, eff[g["code"]] / med)) if med and eff[g["code"]] is not None else 1.0) for g in targets}
    tot = sum(score.values())
    return {c: round(daily * v / tot, 2) for c, v in score.items()}


def entity_key(r: dict) -> str:
    return f"{r['channel']}|{r['entity_id']}"


def aggregate(rows: Iterable[dict], rules: Rules) -> dict[str, dict]:
    """Sum daily rows per keyword / ad set."""
    out: dict[str, dict] = {}
    for r in rows:
        k = entity_key(r)
        a = out.setdefault(k, {**{f: r.get(f) for f in ("channel", "entity_id", "entity_type", "keyword", "campaign_id", "campaign", "adgroup_id", "app", "status")},
                                "taps": 0, "impressions": 0, "installs": 0.0, "spend": 0.0, "revenue": 0.0})
        for f in ("taps", "impressions", "installs", "spend", "revenue"):
            a[f] += float(r.get(f) or 0)
    for a in out.values():
        if not a["revenue"] and a["installs"] and a.get("app"):
            a["revenue"] = a["installs"] * float(rules.value_per_install.get(a["app"], 0) or 0)
            a["revenue_estimated"] = bool(a["revenue"])
        a["cpt"] = a["spend"] / a["taps"] if a["taps"] else None
        a["acos"] = a["spend"] / a["revenue"] if a["revenue"] else None
        a["country"] = country_for(f"{a.get('campaign') or ''} {a.get('keyword') or ''}", a.get("app"), rules)
    return out


def keyword_decision(a: dict, rules: Rules) -> tuple[str, str]:
    """Return (action, reason). action is 'pause', 'keep' or 'protected'."""
    if a.get("channel") == "Apple Search Ads" and a.get("entity_type") == "campaign":
        return "keep", "Campaign-level Apple reporting is monitor-only"
    ov = rules.overrides.get(entity_key(a))
    if ov == "protected":
        return "protected", "Protected by manual override"
    if ov == "paused":
        return "pause", "Paused by manual override"
    if a["taps"] >= rules.min_taps_for_cpt and a["cpt"] is not None and a["cpt"] > rules.max_cpt:
        return "pause", f"CPT ${a['cpt']:.2f} is above the ${rules.max_cpt:.2f} ceiling"
    if a["taps"] >= rules.zero_install_taps and a["installs"] == 0:
        return "pause", f"{int(a['taps'])} taps and no installs"
    if a["spend"] >= rules.min_spend_for_acos and a["acos"] is not None and a["acos"] > rules.max_acos:
        return "pause", f"ACoS {a['acos']*100:.0f}% is above {rules.max_acos*100:.0f}%"
    if a["spend"] >= rules.min_spend_for_acos and not a["revenue"] and a["installs"] == 0:
        return "pause", f"${a['spend']:.2f} spent with no installs or revenue"
    return "keep", ""


def rebalance(per_app: dict[str, dict], rules: Rules, current: dict[str, float] | None = None) -> dict[str, dict]:
    """Split the global daily cap across apps by efficiency.

    Efficiency = revenue / spend when revenue is known, else installs / spend, times the app's priority.
    Apps with no spend yet get a neutral weight (the portfolio median) so they can earn data.
    Shares are clamped to [min_share, max_share], each app is held to its own cap,
    and a single run moves an app by at most max_budget_step from its current budget.
    """
    apps = list(rules.app_caps.keys()) or APPS
    eff = {}
    for app in apps:
        p = per_app.get(app, {})
        sp, rev, ins = float(p.get("spend", 0)), float(p.get("revenue", 0)), float(p.get("installs", 0))
        if sp > 0 and rev > 0:
            eff[app] = rev / sp
        elif sp > 0:
            eff[app] = ins / sp
        else:
            eff[app] = None
    known = sorted(v for v in eff.values() if v is not None)
    neutral = known[len(known) // 2] if known else 1.0
    w = {a: (eff[a] if eff[a] is not None else neutral) * float(rules.app_priority.get(a, 1.0) or 0) for a in apps}
    if sum(w.values()) <= 0:
        w = {a: 1.0 for a in apps}
    total_w = sum(w.values())
    share = {a: w[a] / total_w for a in apps}
    # clamp and renormalise a few times so the bounds hold
    for _ in range(10):
        share = {a: min(rules.max_share, max(rules.min_share, s)) for a, s in share.items()}
        t = sum(share.values())
        share = {a: s / t for a, s in share.items()}
    cap = float(rules.global_daily_cap)
    # water-fill: an app that hits its own cap hands the rest of its share to the others
    target_of: dict[str, float] = {}
    free, room = list(apps), cap
    while free:
        tot = sum(share[a] for a in free)
        over = [a for a in free if room * share[a] / tot > float(rules.app_caps.get(a, cap))]
        if not over:
            for a in free:
                target_of[a] = room * share[a] / tot
            break
        for a in over:
            target_of[a] = float(rules.app_caps.get(a, cap))
            room -= target_of[a]
            free.remove(a)
    out = {}
    for a in apps:
        target = target_of[a]
        cur = (current or {}).get(a)
        if cur:
            lo, hi = cur * (1 - rules.max_budget_step), cur * (1 + rules.max_budget_step)
            target = min(max(target, lo), hi)
        out[a] = {"share": share[a], "daily": round(target, 2), "efficiency": eff[a]}
    # never exceed the global cap after per-app clamps
    s = sum(v["daily"] for v in out.values())
    if s > cap:
        for v in out.values():
            v["daily"] = round(v["daily"] * cap / s, 2)
    return out


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram(text: str, rules: Rules | None = None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID") or (rules.telegram_chat_id if rules else "")
    if not token or not chat:
        log.info("Telegram not configured; message was:\n%s", text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text[:300])
        return r.status_code == 200
    except requests.RequestException as e:
        log.error("Telegram request failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
# HTTP helper with retries
# --------------------------------------------------------------------------- #
def http(method: str, url: str, *, retries: int = 4, **kw) -> requests.Response:
    kw.setdefault("timeout", 60)
    for attempt in range(retries):
        r = requests.request(method, url, **kw)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(min(60, 2 ** attempt * 3))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
        return r
    raise RuntimeError(f"{method} {url} failed after {retries} attempts")


# --------------------------------------------------------------------------- #
# Apple Search Ads (Campaign Management API v5)
# --------------------------------------------------------------------------- #
def _rewrap_pem(raw: str) -> str:
    """Rebuild a PEM key that lost its line breaks in a one-line settings box."""
    k = raw.strip().strip('"').strip("'").replace("\\n", "\n").replace("\r", "")
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", k, re.S)
    if not m:
        return k
    body = "".join(m.group(2).split())
    return f"-----BEGIN {m.group(1)}-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + f"\n-----END {m.group(1)}-----\n"


def asa_private_key() -> str:
    """ASA_PRIVATE_KEY, or a Render Secret File (ASA_PRIVATE_KEY_FILE, or /etc/secrets/*.pem with 'asa' in the name)."""
    path = os.getenv("ASA_PRIVATE_KEY_FILE", "")
    if not path:
        try:
            pems = [f for f in os.listdir("/etc/secrets") if f.lower().endswith(".pem") and "asa" in f.lower()]
            path = os.path.join("/etc/secrets", pems[0]) if len(pems) == 1 else ""
        except OSError:
            path = ""
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _rewrap_pem(fh.read())
    return _rewrap_pem(os.getenv("ASA_PRIVATE_KEY", "")) if os.getenv("ASA_PRIVATE_KEY") else ""


class AppleSearchAds:
    BASE = "https://api.searchads.apple.com/api/v5"

    def __init__(self):
        self.client_id = os.getenv("ASA_CLIENT_ID", "")
        self.team_id = os.getenv("ASA_TEAM_ID", "")
        self.key_id = os.getenv("ASA_KEY_ID", "")
        self.org_id = os.getenv("ASA_ORG_ID", "")
        self.private_key = asa_private_key()
        self._token, self._exp = None, 0

    @property
    def configured(self) -> bool:
        return all([self.client_id, self.team_id, self.key_id, self.org_id, self.private_key])

    def _client_secret(self) -> str:
        import jwt  # PyJWT
        now = int(time.time())
        return jwt.encode({"sub": self.client_id, "aud": "https://appleid.apple.com", "iat": now, "exp": now + 86400 * 180, "iss": self.team_id},
                          self.private_key, algorithm="ES256", headers={"kid": self.key_id})

    def headers(self) -> dict:
        if not self._token or time.time() > self._exp - 60:
            r = http("POST", "https://appleid.apple.com/auth/oauth2/token",
                     data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._client_secret(), "scope": "searchadsorg"},
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
            j = r.json()
            self._token, self._exp = j["access_token"], time.time() + int(j.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}", "X-AP-Context": f"orgId={self.org_id}", "Content-Type": "application/json"}

    def campaigns(self) -> list[dict]:
        out, offset = [], 0
        while True:
            j = http("GET", f"{self.BASE}/campaigns", params={"limit": 1000, "offset": offset}, headers=self.headers()).json()
            out += j.get("data") or []
            total = (j.get("pagination") or {}).get("totalResults", len(out))
            offset += 1000
            if offset >= total:
                return out

    @staticmethod
    def supports_keywords(campaign: dict) -> bool:
        # Missing or unknown placement metadata must not trigger keyword requests.
        return "APPSTORE_SEARCH_RESULTS" in (campaign.get("supplySources") or [])

    def campaign_rows(self, campaigns: list[dict], start: dt.date, end: dt.date,
                      rules: Rules) -> Iterator[dict]:
        """Campaign totals only for placements excluded from keyword reporting.

        Keep these in the existing performance schema, with namespaced IDs so
        campaign IDs cannot collide with keyword IDs. These rows are monitor-only.
        """
        selected = {str(c["id"]): c for c in campaigns}
        if not selected:
            return
        offset = 0
        while True:
            body = {"startTime": start.isoformat(), "endTime": end.isoformat(),
                    "granularity": "DAILY", "timeZone": "ORTZ",
                    "returnRecordsWithNoMetrics": False, "returnRowTotals": False,
                    "selector": {"orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                                 "pagination": {"offset": offset, "limit": 1000}}}
            j = http("POST", f"{self.BASE}/reports/campaigns", json=body, headers=self.headers()).json()
            rows = (((j.get("data") or {}).get("reportingDataResponse") or {}).get("row")) or []
            for row in rows:
                m = row.get("metadata") or {}
                cid = str(m.get("campaignId"))
                c = selected.get(cid)
                if c is None:
                    continue  # Search Results are already represented by keyword rows.
                name = c.get("name") or m.get("campaignName") or ""
                for g in row.get("granularity") or []:
                    installs = g.get("totalInstalls", g.get("tapInstalls", g.get("installs", 0))) or 0
                    yield {"date": g.get("date"), "channel": "Apple Search Ads",
                           "entity_type": "campaign", "entity_id": f"campaign:{cid}",
                           "keyword": name, "campaign_id": cid, "campaign": name,
                           "adgroup_id": None, "app": app_for(name, rules),
                           "status": c.get("status") or m.get("campaignStatus"),
                           "impressions": int(g.get("impressions") or 0),
                           "taps": int(g.get("taps") or 0), "installs": float(installs),
                           "spend": float((g.get("localSpend") or {}).get("amount") or 0),
                           "revenue": 0.0}
            if len(rows) < 1000:
                break
            offset += 1000

    def keyword_rows(self, start: dt.date, end: dt.date, rules: Rules) -> Iterator[dict]:
        """Compatibility entry point: one reporting level per campaign."""
        campaigns = []
        for campaign in self.campaigns():
            app = app_for(campaign.get("name", ""), rules)
            if app not in APPS:
                log.info("Apple Search Ads: skipping campaign %s (not mapped to a supported app)", campaign["id"])
                continue
            campaigns.append(campaign)
        yield from self.campaign_rows(
            [c for c in campaigns if not self.supports_keywords(c)], start, end, rules)
        for c in campaigns:
            if not self.supports_keywords(c):
                continue
            app = app_for(c.get("name", ""), rules)
            offset = 0
            while True:
                body = {"startTime": start.isoformat(), "endTime": end.isoformat(), "granularity": "DAILY", "timeZone": "ORTZ",
                        "returnRecordsWithNoMetrics": False, "returnRowTotals": False,
                        "selector": {"orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}], "pagination": {"offset": offset, "limit": 1000}}}
                j = http("POST", f"{self.BASE}/reports/campaigns/{c['id']}/keywords", json=body, headers=self.headers()).json()
                rows = (((j.get("data") or {}).get("reportingDataResponse") or {}).get("row")) or []
                for row in rows:
                    m = row.get("metadata") or {}
                    for g in row.get("granularity") or []:
                        spend = float(((g.get("localSpend") or {}).get("amount")) or 0)
                        installs = g.get("totalInstalls", g.get("tapInstalls", g.get("installs", 0))) or 0
                        yield {"date": g.get("date"), "channel": "Apple Search Ads", "entity_type": "keyword",
                               "entity_id": str(m.get("keywordId")), "keyword": m.get("keyword"),
                               "campaign_id": str(c["id"]), "campaign": c.get("name"), "adgroup_id": str(m.get("adGroupId")),
                               "app": app, "status": m.get("keywordStatus") or m.get("keywordDisplayStatus"),
                               "impressions": int(g.get("impressions") or 0), "taps": int(g.get("taps") or 0),
                               "installs": float(installs), "spend": spend, "revenue": 0.0}
                if len(rows) < 1000:
                    break
                offset += 1000

    def pause_keyword(self, a: dict) -> None:
        if a.get("entity_type") != "keyword":
            raise ValueError("Apple keyword pause requires a keyword entity")
        http("PUT", f"{self.BASE}/campaigns/{a['campaign_id']}/adgroups/{a['adgroup_id']}/targetingkeywords/bulk",
             json=[{"id": int(a["entity_id"]), "status": "PAUSED"}], headers=self.headers())

    def set_campaign_budget(self, campaign_id: str, daily: float, currency: str = "USD") -> None:
        http("PUT", f"{self.BASE}/campaigns/{campaign_id}",
             json={"campaign": {"dailyBudgetAmount": {"amount": f"{daily:.2f}", "currency": currency}}}, headers=self.headers())


# --------------------------------------------------------------------------- #
# Meta Ads (Marketing API)
# --------------------------------------------------------------------------- #
class MetaAds:
    VERSION = os.getenv("META_API_VERSION", "v21.0")
    INSTALL_ACTIONS = ("mobile_app_install", "app_install", "omni_app_install")
    PURCHASE_ACTIONS = ("omni_purchase", "app_custom_event.fb_mobile_purchase", "offsite_conversion.fb_pixel_purchase", "purchase")

    def __init__(self):
        self.token = os.getenv("META_ACCESS_TOKEN", "")
        self.accounts = [a.strip().removeprefix("act_") for a in os.getenv("META_AD_ACCOUNT_IDS", "").split(",") if a.strip()]

    @property
    def configured(self) -> bool:
        return bool(self.token and self.accounts)

    def _url(self, path: str) -> str:
        return f"https://graph.facebook.com/{self.VERSION}/{path}"

    @staticmethod
    def _sum(items: list | None, types: tuple) -> float:
        # Meta reports the same conversion under several overlapping types; take the largest, not the sum.
        vals = [float(i.get("value") or 0) for i in (items or []) if i.get("action_type") in types]
        return max(vals) if vals else 0.0

    def adset_rows(self, start: dt.date, end: dt.date, rules: Rules) -> Iterator[dict]:
        for acct in self.accounts:
            url = self._url(f"act_{acct}/insights")
            params = {"access_token": self.token, "level": "adset", "time_increment": 1, "limit": 500,
                      "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
                      "fields": "adset_id,adset_name,campaign_id,campaign_name,spend,clicks,impressions,actions,action_values"}
            while url:
                j = http("GET", url, params=params).json()
                for x in j.get("data") or []:
                    yield {"date": x["date_start"], "channel": "Meta Ads", "entity_type": "ad_set",
                           "entity_id": x["adset_id"], "keyword": x.get("adset_name"),
                           "campaign_id": x.get("campaign_id"), "campaign": x.get("campaign_name"), "adgroup_id": x["adset_id"],
                           "app": app_for(f"{x.get('campaign_name','')} {x.get('adset_name','')}", rules), "status": None,
                           "impressions": int(x.get("impressions") or 0), "taps": int(x.get("clicks") or 0),
                           "installs": self._sum(x.get("actions"), self.INSTALL_ACTIONS),
                           "spend": float(x.get("spend") or 0), "revenue": self._sum(x.get("action_values"), self.PURCHASE_ACTIONS)}
                url = (j.get("paging") or {}).get("next")
                params = None  # the next link already carries every parameter

    def pause_keyword(self, a: dict) -> None:
        http("POST", self._url(a["entity_id"]), data={"status": "PAUSED", "access_token": self.token})

    def set_adset_budget(self, adset_id: str, daily: float) -> None:
        http("POST", self._url(adset_id), data={"daily_budget": str(int(round(daily * 100))), "access_token": self.token})


# --------------------------------------------------------------------------- #
# Google Ads (google-ads client library)
# --------------------------------------------------------------------------- #
class GoogleAds:
    def __init__(self):
        self.customer_ids = [c.strip().replace("-", "") for c in os.getenv("GOOGLE_ADS_CUSTOMER_IDS", "").split(",") if c.strip()]
        self.cfg = {
            # Since 2026-09-09 Google ties API access to the Cloud project behind the OAuth client;
            # developer tokens are optional and ignored. Sent only if one is still set.
            "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN", ""),
            "use_proto_plus": True,
        }
        if os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"):
            self.cfg["developer_token"] = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
        if os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"):
            self.cfg["login_customer_id"] = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", "")
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.customer_ids and all(self.cfg[k] for k in ("client_id", "client_secret", "refresh_token")))

    @property
    def client(self):
        if self._client is None:
            from google.ads.googleads.client import GoogleAdsClient
            self._client = GoogleAdsClient.load_from_dict(self.cfg)
        return self._client

    def _stream(self, cid: str, query: str):
        svc = self.client.get_service("GoogleAdsService")
        for batch in svc.search_stream(customer_id=cid, query=query):
            yield from batch.results

    def rows(self, start: dt.date, end: dt.date, rules: Rules) -> Iterator[dict]:
        rng = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        for cid in self.customer_ids:
            # Search keywords
            q = ("SELECT campaign.id, campaign.name, ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
                 "ad_group_criterion.status, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                 f"metrics.conversions, metrics.conversions_value FROM keyword_view WHERE {rng}")
            for r in self._stream(cid, q):
                yield {"date": r.segments.date, "channel": "Google Ads", "entity_type": "keyword",
                       "entity_id": f"{cid}~{r.ad_group.id}~{r.ad_group_criterion.criterion_id}", "keyword": r.ad_group_criterion.keyword.text,
                       "campaign_id": f"{cid}~{r.campaign.id}", "campaign": r.campaign.name, "adgroup_id": str(r.ad_group.id),
                       "app": app_for(r.campaign.name, rules), "status": r.ad_group_criterion.status.name,
                       "impressions": int(r.metrics.impressions), "taps": int(r.metrics.clicks), "installs": float(r.metrics.conversions),
                       "spend": r.metrics.cost_micros / 1e6, "revenue": float(r.metrics.conversions_value)}
            # App campaigns have no keywords: track them at campaign level
            q = ("SELECT campaign.id, campaign.name, campaign.status, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                 f"metrics.conversions, metrics.conversions_value FROM campaign WHERE campaign.advertising_channel_type = 'MULTI_CHANNEL' AND {rng}")
            for r in self._stream(cid, q):
                yield {"date": r.segments.date, "channel": "Google Ads", "entity_type": "app_campaign",
                       "entity_id": f"{cid}~c{r.campaign.id}", "keyword": f"App campaign: {r.campaign.name}",
                       "campaign_id": f"{cid}~{r.campaign.id}", "campaign": r.campaign.name, "adgroup_id": None,
                       "app": app_for(r.campaign.name, rules), "status": r.campaign.status.name,
                       "impressions": int(r.metrics.impressions), "taps": int(r.metrics.clicks), "installs": float(r.metrics.conversions),
                       "spend": r.metrics.cost_micros / 1e6, "revenue": float(r.metrics.conversions_value)}

    def pause_keyword(self, a: dict) -> None:
        cid, *rest = a["entity_id"].split("~")
        c = self.client
        if a["entity_type"] == "app_campaign":
            svc = c.get_service("CampaignService")
            op = c.get_type("CampaignOperation")
            op.update.resource_name = svc.campaign_path(cid, rest[0].lstrip("c"))
            op.update.status = c.enums.CampaignStatusEnum.PAUSED
            c.copy_from(op.update_mask, c.get_type("FieldMask")(paths=["status"]))
            svc.mutate_campaigns(customer_id=cid, operations=[op])
            return
        ad_group_id, criterion_id = rest
        svc = c.get_service("AdGroupCriterionService")
        op = c.get_type("AdGroupCriterionOperation")
        op.update.resource_name = svc.ad_group_criterion_path(cid, ad_group_id, criterion_id)
        op.update.status = c.enums.AdGroupCriterionStatusEnum.PAUSED
        c.copy_from(op.update_mask, c.get_type("FieldMask")(paths=["status"]))
        svc.mutate_ad_group_criteria(customer_id=cid, operations=[op])

    def set_campaign_budget(self, campaign_key: str, daily: float) -> None:
        cid, campaign_id = campaign_key.split("~")
        c = self.client
        q = f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {int(campaign_id)}"
        budget = next(iter(self._stream(cid, q))).campaign.campaign_budget
        svc = c.get_service("CampaignBudgetService")
        op = c.get_type("CampaignBudgetOperation")
        op.update.resource_name = budget
        op.update.amount_micros = int(round(daily * 1e6))
        c.copy_from(op.update_mask, c.get_type("FieldMask")(paths=["amount_micros"]))
        svc.mutate_campaign_budgets(customer_id=cid, operations=[op])


# --------------------------------------------------------------------------- #
# Storage (DuckDB or MotherDuck) + dlt load
# --------------------------------------------------------------------------- #
def db_target() -> str:
    """'md:adbot' when MOTHERDUCK_TOKEN is set (shared by the web app and the cron job), else a local file."""
    if os.getenv("MOTHERDUCK_TOKEN"):
        return f"md:{os.getenv('MOTHERDUCK_DATABASE', 'adbot')}"
    return os.getenv("DUCKDB_PATH", "adbot.duckdb")


def connect():
    import duckdb
    target = db_target()
    if target.startswith("md:"):
        # connect to MotherDuck itself first, create the database on first run, then switch to it
        db = target[3:]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db):
            raise ValueError(f"MOTHERDUCK_DATABASE must be letters, digits or _ (got {db!r})")
        con = duckdb.connect("md:", config={"motherduck_token": os.environ["MOTHERDUCK_TOKEN"]})
        con.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
        con.execute(f"USE {db}")
    else:
        con = duckdb.connect(target)
    ensure_tables(con)
    return con


def ensure_tables(con) -> None:
    con.execute(f"create schema if not exists {DATASET}")
    con.execute("create table if not exists adbot_rules (updated_at timestamp, rules json)")
    con.execute("create table if not exists keyword_overrides (entity_key varchar primary key, status varchar, note varchar, updated_at timestamp)")
    con.execute("create table if not exists bot_actions (ts timestamp, dry_run boolean, kind varchar, channel varchar, app varchar, "
                "entity_key varchar, keyword varchar, detail varchar, applied boolean, error varchar)")
    con.execute("create table if not exists app_budgets (ts timestamp, app varchar, daily double, share double, efficiency double)")
    # Revenue intelligence tables (schema "revenue"). A failure here must never stop the ad bot.
    try:
        import revenue
        revenue.ensure(con)
    except Exception:  # noqa: BLE001
        log.exception("revenue schema setup failed; the ad bot carries on without it")


def run_sync(days: int, rules: Rules) -> dict:
    import dlt
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    sources = {"Apple Search Ads": AppleSearchAds(), "Meta Ads": MetaAds(), "Google Ads": GoogleAds()}
    counts = {}

    @dlt.resource(name=TABLE, write_disposition="merge", primary_key=["channel", "entity_id", "date"])
    def performance():
        for name, src in sources.items():
            if not src.configured:
                log.info("%s: credentials not set, skipped", name)
                continue
            n = 0
            try:
                gen = src.keyword_rows(start, end, rules) if name == "Apple Search Ads" else src.adset_rows(start, end, rules) if name == "Meta Ads" else src.rows(start, end, rules)
                for row in gen:
                    row["date"] = str(row["date"])
                    row["app"] = row.get("app") or "Unassigned"
                    n += 1
                    yield row
            except Exception as e:  # one broken source must not stop the others
                log.exception("%s sync failed", name)
                counts[name + " error"] = str(e)[:300]
            counts[name] = n

    target = db_target()
    if target.startswith("md:"):
        destination = dlt.destinations.motherduck(credentials=f"md:///{target[3:]}?token={os.environ['MOTHERDUCK_TOKEN']}")
    else:
        destination = dlt.destinations.duckdb(credentials=target)
    pipeline = dlt.pipeline(pipeline_name="adbot", destination=destination, dataset_name=DATASET)
    info = pipeline.run(performance())
    log.info("Loaded: %s", info)
    return counts


AD_PLATFORM = {"Apple Search Ads": "apple_ads", "Meta Ads": "meta_ads", "Google Ads": "google_ads"}


def report_ad_status(con, counts: dict) -> None:
    """Tell the Revenue tab which ad accounts synced (the keys live on this job, not the web app)."""
    try:
        from revenue import registry as rreg
        from revenue.schema import SCHEMA as R
        for name, platform in AD_PLATFORM.items():
            if name not in counts and f"{name} error" not in counts:
                continue  # credentials not set: nothing to report
            err = counts.get(f"{name} error")
            job = rreg.start_job(con, platform, "ad_sync")
            rreg.finish_job(con, job, "failed" if err else "succeeded", int(counts.get(name, 0) or 0), 0,
                            message=err or f"{counts.get(name, 0)} rows")
            con.execute(f"UPDATE {R}.store_accounts SET status = ?, last_error = ?, last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END, "
                        "updated_at = ? WHERE platform = ? AND purpose = 'reporting'",
                        ["error" if err else "verified", rreg.scrub_text(err)[:500] if err else None, not err, rreg.now(), rreg.now(), platform])
    except Exception:  # noqa: BLE001
        log.exception("could not record ad sync status")


# --------------------------------------------------------------------------- #
# Bot run
# --------------------------------------------------------------------------- #
def run_bot(con, rules: Rules, dry_run: bool) -> dict:
    since = (dt.date.today() - dt.timedelta(days=rules.lookback_days - 1)).isoformat()
    try:
        df = con.execute(f"select * from {DATASET}.{TABLE} where cast(date as varchar) >= ?", [since]).fetch_df()
    except Exception:
        log.warning("No performance data yet. Run a sync first.")
        return {}
    rows = df.to_dict("records")
    # Exclude previously stored out-of-scope Apple campaigns from decisions and totals.
    rows = [r for r in rows if r.get("channel") != "Apple Search Ads"
            or app_for(r.get("campaign") or "", rules) in APPS]
    agg = aggregate(rows, rules)
    sources = {"Apple Search Ads": AppleSearchAds(), "Meta Ads": MetaAds(), "Google Ads": GoogleAds()}
    now = dt.datetime.utcnow()
    paused, alerts = [], []

    # skip anything already paused by the bot, and don't repeat a dry-run alert more than once a day
    already = {k for (k,) in con.execute("select distinct entity_key from bot_actions where kind='pause' and (applied or ts > ?)",
                                         [now - dt.timedelta(hours=24)]).fetchall()}
    for k, a in agg.items():
        action, reason = keyword_decision(a, rules)
        if action != "pause" or k in already or str(a.get("status") or "").upper() in ("PAUSED", "REMOVED"):
            continue
        applied, err = False, None
        if not dry_run:
            try:
                sources[a["channel"]].pause_keyword(a)
                applied = True
            except Exception as e:  # noqa: BLE001
                err = str(e)[:500]
        con.execute("insert into bot_actions values (?,?,?,?,?,?,?,?,?,?)", [now, dry_run, "pause", a["channel"], a.get("app"), k, a.get("keyword"), reason, applied, err])
        paused.append((a, reason, applied, err))

    # rebalance
    per_app: dict[str, dict] = {}
    for a in agg.values():
        p = per_app.setdefault(a.get("app") or "Unassigned", {"spend": 0.0, "revenue": 0.0, "installs": 0.0})
        for f in ("spend", "revenue", "installs"):
            p[f] += a[f]
    last = {app: d for app, d in con.execute("select app, arg_max(daily, ts) from app_budgets group by app").fetchall()}
    plan = rebalance(per_app, rules, last) if rules.auto_rebalance else {}
    for app, v in plan.items():
        con.execute("insert into app_budgets values (?,?,?,?,?)", [now, app, v["daily"], v["share"], v["efficiency"]])
    if plan and not dry_run:
        apply_budgets(plan, agg, sources, con, now, rules)

    # alerts
    tag = "🧪 DRY RUN · " if dry_run else ""
    if paused and rules.alerts.get("kill_switch", True):
        lines = [f"{'⏸' if ap or dry_run else '⚠️'} <b>{a.get('keyword')}</b> ({a['channel']}, {a.get('app')}): {r}" + (f" — failed: {e}" if e else "") for a, r, ap, e in paused[:25]]
        alerts.append(f"{tag}<b>Kill-switch</b> {'would pause' if dry_run else 'paused'} {len(paused)}:\n" + "\n".join(lines))
    hi = [a for a in agg.values() if a["cpt"] and a["cpt"] > rules.max_cpt * 0.85 and a["cpt"] <= rules.max_cpt and a["taps"] >= rules.min_taps_for_cpt]
    if hi and rules.alerts.get("high_cpt", True):
        alerts.append(f"{tag}<b>High CPT warning</b> (within 15% of ${rules.max_cpt:.2f}):\n" + "\n".join(f"• {a.get('keyword')} ${a['cpt']:.2f}" for a in hi[:15]))
    if plan and rules.alerts.get("rebalance", True):
        alerts.append(f"{tag}<b>Budget plan</b> (cap ${rules.global_daily_cap:.2f}/day):\n" + "\n".join(f"• {app}: ${v['daily']:.2f}/day ({v['share']*100:.0f}%)" for app, v in plan.items()))
    if rules.alerts.get("daily_summary", True) and dt.datetime.now().hour < 10:
        sp = sum(a["spend"] for a in agg.values()); rev = sum(a["revenue"] for a in agg.values()); ins = sum(a["installs"] for a in agg.values())
        alerts.append(f"{tag}<b>Last {rules.lookback_days} days</b>: spend ${sp:,.2f} · installs {ins:,.0f} · revenue ${rev:,.2f} · ACoS {(sp/rev*100 if rev else float('nan')):.0f}%")
    for msg in alerts:
        telegram(msg, rules)
    return {"paused": len(paused), "plan": plan}


def apply_budgets(plan: dict, agg: dict, sources: dict, con, now, rules: Rules) -> None:
    """Split each app's daily budget across its active campaigns in proportion to their recent spend."""
    camp: dict[tuple, float] = {}
    for a in agg.values():
        if a.get("channel") == "Apple Search Ads" and a.get("entity_type") == "campaign":
            continue  # Newly supported placements are reporting-only.
        if a.get("app") not in plan or str(a.get("status") or "").upper() in ("PAUSED", "REMOVED"):
            continue
        # Apple and Google budgets live on the campaign; Meta budgets on the ad set
        budget_id = a["entity_id"] if a["channel"] == "Meta Ads" else a.get("campaign_id")
        key = (a["channel"], a["app"], budget_id)
        camp[key] = camp.get(key, 0.0) + a["spend"]
    country_of = {(a["channel"], a.get("app"), a["entity_id"] if a["channel"] == "Meta Ads" else a.get("campaign_id")): a.get("country") for a in agg.values()}
    for app, v in plan.items():
        mine = {k: s for k, s in camp.items() if k[1] == app}
        if not mine:
            continue
        # apps with country targets: first split the app budget by country, then by spend inside each country
        per_country: dict[str, dict] = {}
        for a in agg.values():
            if a.get("app") == app and a.get("country"):
                p = per_country.setdefault(a["country"], {"spend": 0.0, "revenue": 0.0, "installs": 0.0})
                for f in ("spend", "revenue", "installs"):
                    p[f] += a[f]
        geo = geo_split(app, v["daily"], per_country, rules) if rules.geo_targets.get(app) else {}
        groups: dict[str | None, dict] = {}
        for k, s in mine.items():
            c = country_of.get(k) if geo else None
            groups.setdefault(c if c in geo else None, {})[k] = s
        budgets: dict[tuple, float] = {}
        untagged_budget = v["daily"] - sum(geo[c] for c in groups if c in geo)
        for c, members in groups.items():
            pool = geo[c] if c in geo else max(0.0, untagged_budget)
            spent = sum(members.values())
            for k, s in members.items():
                budgets[k] = pool * (s / spent if spent else 1 / len(members))
        for (channel, _, cid), daily in budgets.items():
            daily = round(daily, 2)
            if daily < 1:
                continue  # platforms reject tiny budgets; leave these alone
            err = None
            try:
                if channel == "Apple Search Ads":
                    sources[channel].set_campaign_budget(cid, daily)
                elif channel == "Meta Ads":
                    sources[channel].set_adset_budget(cid, daily)
                else:
                    sources[channel].set_campaign_budget(cid, daily)
            except Exception as e:  # e.g. Meta ad sets using campaign budget optimisation
                err = str(e)[:500]
            con.execute("insert into bot_actions values (?,?,?,?,?,?,?,?,?,?)", [now, False, "budget", channel, app, f"{channel}|{cid}", None, f"daily budget ${daily:.2f}", err is None, err])


def check_trading(con, rules: Rules) -> None:
    """Hourly safety net: alert if the trading bot has gone quiet while the FX market is open,
    or if its kill switch is on. Read-only; it never touches the trading bot."""
    try:
        rows = con.execute("select bot_id, max(ts), arg_max(mode, ts) from trading.heartbeat group by bot_id").fetchall()
    except Exception:
        return  # trading reporting not set up yet
    demo_alerts = os.getenv("TRADING_ALERT_DEMO", "").lower() in ("1", "true", "yes")
    rows = [(b, last) for b, last, mode in rows
            if demo_alerts or str(mode or "").strip().lower() not in ("paper", "demo", "practice", "sim", "simulation", "test")]
    now = dt.datetime.utcnow()
    wd, h = now.weekday(), now.hour
    fx_open = not (wd == 5 or (wd == 6 and h < 21) or (wd == 4 and h >= 22))
    for bot_id, last in rows:
        try:
            st = con.execute("select status, message from trading.heartbeat where bot_id = ? order by ts desc limit 1", [bot_id]).fetchone()
        except Exception:
            st = None
        if fx_open and st and st[0] in ("stopped", "degraded"):
            telegram(f"⚠️ <b>Trading bot {bot_id}</b> is {st[0]}: {st[1] or ''}", rules)
        if fx_open and last and (now - last).total_seconds() > 20 * 60:
            telegram(f"⚠️ <b>Trading bot {bot_id}</b> hasn't reported for {int((now - last).total_seconds() // 60)} min while the FX market is open.", rules)
        try:
            ks = con.execute("select kill_switch, notes, ts from trading.risk where bot_id = ? order by ts desc limit 1", [bot_id]).fetchone()
        except Exception:
            ks = None
        if ks and ks[0] and (now - ks[2]).total_seconds() < 3600:
            telegram(f"🛑 <b>Trading bot {bot_id}</b>: kill switch is ON. {ks[1] or ''}", rules)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "14")))
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    dry_run = os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")
    con = connect()
    rules = load_rules(con)
    con.close()  # dlt opens its own connection while loading
    if args.test_telegram:
        ok = telegram("✅ <b>AdBot test</b>\nTelegram alerts are working for Rapid Nursing Review, CCRN Adult and NurseBound.", rules)
        print("sent" if ok else "not sent (check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        return 0 if ok else 1
    if not args.no_sync:
        counts = run_sync(args.days, rules)
        log.info("Rows per source: %s", counts)
    con = connect()
    rules = load_rules(con)
    if not args.no_sync:
        report_ad_status(con, counts)
    result = run_bot(con, rules, dry_run)
    check_trading(con, rules)
    # App Store revenue (read-only). Skipped quietly until the ASC_* settings exist; never stops the ad bot.
    try:
        from revenue import apple
        log.info("App Store Connect sync: %s", apple.run(con))
    except Exception:  # noqa: BLE001
        log.exception("App Store Connect sync failed")
    try:
        from revenue import google_play
        log.info("Google Play sync: %s", google_play.run(con))
    except Exception:  # noqa: BLE001
        log.exception("Google Play sync failed")
    log.info("Bot result (dry_run=%s): %s", dry_run, {k: (v if k != "plan" else {a: x['daily'] for a, x in v.items()}) for k, v in result.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
