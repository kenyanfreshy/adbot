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
    auto_rebalance: bool = False              # configured on/off; it still only runs when eligible
    min_share: float = 0.10                   # each app keeps at least 10% of the portfolio cap
    max_share: float = 0.60                   # and at most 60%
    max_budget_step: float = 0.25             # a run may move an app's budget by at most 25%
    lookback_days: int = 7                    # decisions use the last N days
    # A labeled ESTIMATE shown next to installs. Empty / 0 = not set. Never used to decide anything:
    # revenue-based rules need revenue the platform actually reported.
    value_per_install: dict = field(default_factory=dict)
    # ---- shared budget policy (one policy across every managed channel) ----
    monthly_budget: float | None = None        # explicit monthly total; budget changes stay blocked until it is set
    app_monthly: dict = field(default_factory=dict)       # app -> monthly allocation (must add up to <= monthly_budget)
    channel_monthly: dict = field(default_factory=dict)   # channel -> monthly limit across apps (optional)
    campaign_daily_max: float | None = None   # no single campaign / ad set above this per day (optional)
    reserve_pct: float = 0.10                 # held back for spend that platforms report late
    budget_currency: str = "USD"
    # ---- change safeguards (enforced on the server, across runs and workers) ----
    max_daily_change_pct: float = 0.30        # cumulative budget change per campaign per 24 h
    cooldown_hours: float = 24.0              # after a change, leave that campaign alone this long
    max_actions_per_campaign_day: int = 2
    max_actions_per_account_day: int = 10
    deadband_pct: float = 0.10                # ignore budget changes smaller than this
    min_days_data: int = 3                    # days of mature data before a performance rule may act
    maturity_days: dict = field(default_factory=dict)     # channel -> days to leave out while conversions arrive
    stale_after_hours: float = 36.0
    min_conversions_for_revenue_rules: int = 5
    min_spend_for_rebalance: float = 50.0     # per app, mature window, before rebalancing may compare apps
    exploration_share: float = 0.10           # each app keeps at least this share while evidence is thin
    auto_actions: list = field(default_factory=list)       # actions bounded-automatic mode may apply on its own
    approval_ttl_hours: float = 24.0          # an approval expires if it hasn't been carried out within this time
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
        nullable = {"monthly_budget", "campaign_daily_max"}
        for k, v in (d or {}).items():
            if k in cls.__dataclass_fields__ and (v is not None or k in nullable):
                setattr(r, k, v)
        return r

    # campaign -> app choices made on the phone app (control.campaign_map); not part of the saved rules
    def manual_app(self, channel: str, campaign_id) -> str | None:
        v = getattr(self, "_cmap", {}).get((channel, str(campaign_id)))
        return v if v in APPS else None

    def vpi(self, app: str) -> float | None:
        """The value-per-install estimate for an app, or None when not set (0 means not set)."""
        v = (self.value_per_install or {}).get(app)
        return float(v) if v else None


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
                data = {**data, **(row[0] if isinstance(row[0], dict) else json.loads(row[0]))}
            for key, status in con.execute("select entity_key, status from keyword_overrides").fetchall():
                data.setdefault("overrides", {})[key] = status
        except Exception:  # tables not created yet
            pass
    rules = Rules.from_dict(data)
    if con is not None:
        try:
            from control import ingest
            rules._cmap = ingest.mapping(con)
        except Exception:  # noqa: BLE001 - control tables not created yet
            rules._cmap = {}
    return rules


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
                                "taps": 0, "impressions": 0, "installs": 0.0, "spend": 0.0, "revenue": 0.0, "revenue_known": True})
        for f in ("taps", "impressions", "installs", "spend"):
            a[f] += float(r.get(f) or 0)
        if r.get("revenue") is None or r.get("channel") == "Apple Search Ads":
            a["revenue_known"] = False      # not reported: unknown, never zero
        else:
            a["revenue"] += float(r["revenue"])
    for a in out.values():
        if not a["revenue_known"]:
            a["revenue"] = None
        vpi = rules.vpi(a.get("app") or "")
        # a labeled estimate for display only; decisions never use it
        a["revenue_estimate"] = a["installs"] * vpi if (vpi and a["installs"]) else None
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
    if a["spend"] >= rules.min_spend_for_acos and a["installs"] == 0:
        return "pause", f"${a['spend']:.2f} spent with no installs"
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
            wait = r.headers.get("Retry-After", "")
            time.sleep(min(120, float(wait)) if str(wait).isdigit() else min(60, 2 ** attempt * 3))   # bounded backoff
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
                           "currency": (g.get("localSpend") or {}).get("currency") or (c.get("budgetAmount") or {}).get("currency"),
                           "revenue": None}   # Apple Ads reports no revenue: unknown, never zero
            if len(rows) < 1000:
                break
            offset += 1000

    def jobs(self, start: dt.date, end: dt.date, rules: Rules) -> list:
        from control.ingest import Job
        return [Job("Apple Search Ads", self.org_id, lambda: self.keyword_rows(start, end, rules), "ORTZ (organization time zone)")]

    def keyword_rows(self, start: dt.date, end: dt.date, rules: Rules) -> Iterator[dict]:
        """Compatibility entry point: one reporting level per campaign."""
        campaigns = []
        for campaign in self.campaigns():
            app = rules.manual_app("Apple Search Ads", campaign.get("id")) or app_for(campaign.get("name", ""), rules)
            if app not in APPS:
                # not reported (it may belong to another product in the same Apple Ads organization), but listed
                # so it can be reviewed and mapped on the phone app instead of disappearing silently
                log.info("Apple Search Ads: campaign %s is not mapped to a managed app; listed for review", campaign["id"])
                yield {"_campaign_only": True, "campaign_id": str(campaign["id"]), "campaign": campaign.get("name"), "app": None}
                continue
            campaigns.append(campaign)
        yield from self.campaign_rows(
            [c for c in campaigns if not self.supports_keywords(c)], start, end, rules)
        for c in campaigns:
            if not self.supports_keywords(c):
                continue
            app = rules.manual_app("Apple Search Ads", c.get("id")) or app_for(c.get("name", ""), rules)
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
                               "installs": float(installs), "spend": spend,
                               "currency": (g.get("localSpend") or {}).get("currency"), "revenue": None}
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

    @classmethod
    def _value(cls, items: list | None, types: tuple):
        """Purchase value when Meta reported one; None (unknown) when no purchase value was reported at all."""
        vals = [float(i.get("value") or 0) for i in (items or []) if i.get("action_type") in types]
        return max(vals) if vals else None

    def jobs(self, start: dt.date, end: dt.date, rules: Rules) -> list:
        from control.ingest import Job
        return [Job("Meta Ads", acct, (lambda a=acct: self.adset_rows(start, end, rules, accounts=[a])), "ad account time zone")
                for acct in self.accounts]

    def adset_rows(self, start: dt.date, end: dt.date, rules: Rules, accounts: list | None = None) -> Iterator[dict]:
        for acct in (accounts or self.accounts):
            url = self._url(f"act_{acct}/insights")
            params = {"access_token": self.token, "level": "adset", "time_increment": 1, "limit": 500,
                      "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}),
                      "fields": "adset_id,adset_name,campaign_id,campaign_name,spend,clicks,impressions,actions,action_values,account_currency"}
            while url:
                j = http("GET", url, params=params).json()
                for x in j.get("data") or []:
                    yield {"date": x["date_start"], "channel": "Meta Ads", "entity_type": "ad_set",
                           "entity_id": x["adset_id"], "keyword": x.get("adset_name"),
                           "campaign_id": x.get("campaign_id"), "campaign": x.get("campaign_name"), "adgroup_id": x["adset_id"],
                           "app": rules.manual_app("Meta Ads", x.get("campaign_id"))
                                  or app_for(f"{x.get('campaign_name','')} {x.get('adset_name','')}", rules), "status": None,
                           "impressions": int(x.get("impressions") or 0), "taps": int(x.get("clicks") or 0),
                           "installs": self._sum(x.get("actions"), self.INSTALL_ACTIONS),
                           "spend": float(x.get("spend") or 0), "currency": x.get("account_currency"),
                           "revenue": self._value(x.get("action_values"), self.PURCHASE_ACTIONS)}
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

    @staticmethod
    def _value(conversions: float, value: float):
        """Google returns 0 value when conversion values aren't set up; with conversions > 0 that is 'not reported', not $0."""
        if value:
            return float(value)
        return 0.0 if not conversions else None

    def jobs(self, start: dt.date, end: dt.date, rules: Rules) -> list:
        from control.ingest import Job
        return [Job("Google Ads", cid, (lambda c=cid: self.rows(start, end, rules, customers=[c])), "account time zone")
                for cid in self.customer_ids]

    def rows(self, start: dt.date, end: dt.date, rules: Rules, customers: list | None = None) -> Iterator[dict]:
        rng = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        for cid in (customers or self.customer_ids):
            # Search keywords
            q = ("SELECT customer.currency_code, campaign.id, campaign.name, ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
                 "ad_group_criterion.status, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                 f"metrics.conversions, metrics.conversions_value FROM keyword_view WHERE {rng}")
            for r in self._stream(cid, q):
                yield {"date": r.segments.date, "channel": "Google Ads", "entity_type": "keyword",
                       "entity_id": f"{cid}~{r.ad_group.id}~{r.ad_group_criterion.criterion_id}", "keyword": r.ad_group_criterion.keyword.text,
                       "campaign_id": f"{cid}~{r.campaign.id}", "campaign": r.campaign.name, "adgroup_id": str(r.ad_group.id),
                       "app": rules.manual_app("Google Ads", f"{cid}~{r.campaign.id}") or app_for(r.campaign.name, rules),
                       "status": r.ad_group_criterion.status.name,
                       "impressions": int(r.metrics.impressions), "taps": int(r.metrics.clicks), "installs": float(r.metrics.conversions),
                       "spend": r.metrics.cost_micros / 1e6, "currency": r.customer.currency_code,
                       "revenue": self._value(float(r.metrics.conversions), float(r.metrics.conversions_value))}
            # App campaigns have no keywords: track them at campaign level
            q = ("SELECT customer.currency_code, campaign.id, campaign.name, campaign.status, segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros, "
                 f"metrics.conversions, metrics.conversions_value FROM campaign WHERE campaign.advertising_channel_type = 'MULTI_CHANNEL' AND {rng}")
            for r in self._stream(cid, q):
                yield {"date": r.segments.date, "channel": "Google Ads", "entity_type": "app_campaign",
                       "entity_id": f"{cid}~c{r.campaign.id}", "keyword": f"App campaign: {r.campaign.name}",
                       "campaign_id": f"{cid}~{r.campaign.id}", "campaign": r.campaign.name, "adgroup_id": None,
                       "app": rules.manual_app("Google Ads", f"{cid}~{r.campaign.id}") or app_for(r.campaign.name, rules),
                       "status": r.campaign.status.name,
                       "impressions": int(r.metrics.impressions), "taps": int(r.metrics.clicks), "installs": float(r.metrics.conversions),
                       "spend": r.metrics.cost_micros / 1e6, "currency": r.customer.currency_code,
                       "revenue": self._value(float(r.metrics.conversions), float(r.metrics.conversions_value))}

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
def db_target(environ=None) -> str:
    """'md:adbot' when MOTHERDUCK_TOKEN is set (shared by the web app and the cron job), else a local file."""
    env = os.environ if environ is None else environ
    if env.get("MOTHERDUCK_TOKEN"):
        return f"md:{env.get('MOTHERDUCK_DATABASE', 'adbot')}"
    return env.get("DUCKDB_PATH", "adbot.duckdb")


class SharedStorageMissing(RuntimeError):
    """Production needs one shared database for approvals, rules, stop flags, locks and audit records."""


def production(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("RENDER")) or str(env.get("ADBOT_ENV", "")).lower() == "production"


def storage_info(environ=None) -> dict:
    t = db_target(environ)
    shared = t.startswith("md:")
    return {"label": f"MotherDuck database '{t[3:]}'" if shared else f"local file {t}", "shared": shared,
            "production": production(environ)}


def require_shared_storage(environ=None) -> None:
    """On Render (RENDER is set by Render) or with ADBOT_ENV=production, refuse to run on a local DuckDB file:
    the web service and the hourly job would each get their own throw-away file, so a stop flag or approval
    written by one would never be seen by the other."""
    info = storage_info(environ)
    if info["production"] and not info["shared"]:
        raise SharedStorageMissing("MOTHERDUCK_TOKEN is not set: this production process would use a private local file. "
                                   "Set MOTHERDUCK_TOKEN (and the same MOTHERDUCK_DATABASE) on BOTH Render services.")


def connect():
    import duckdb
    require_shared_storage()
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
    # Control plane (schema "control"): data states, imports, campaign map, proposals, alerts, locks.
    try:
        import control
        control.ensure(con)
    except Exception:  # noqa: BLE001
        log.exception("control schema setup failed")


def ad_sources() -> dict:
    return {"Apple Search Ads": AppleSearchAds(), "Meta Ads": MetaAds(), "Google Ads": GoogleAds()}


def configured_sources(sources: dict | None = None) -> dict:
    """Which ad sources have credentials on this process."""
    sources = sources or ad_sources()
    return {name: bool(src.configured) for name, src in sources.items()}


def build_jobs(start: dt.date, end: dt.date, rules: Rules, sources: dict | None = None) -> list:
    sources = sources or ad_sources()
    jobs = []
    for name, src in sources.items():
        if not src.configured:
            log.info("%s: credentials not set, skipped", name)
            continue
        jobs += src.jobs(start, end, rules)
    return jobs


def run_sync(days: int, rules: Rules, sources: dict | None = None) -> dict:
    """Import the last `days` days (the refresh window re-reads recent days, so late conversions and revised
    reports replace earlier numbers; dlt merges on channel + entity + date, so nothing is counted twice).
    Returns the import records for control.ingest.record(); a load failure is reported, not raised."""
    import dlt
    from control.ingest import collect
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    sources = sources or ad_sources()
    jobs = build_jobs(start, end, rules, sources)
    records: list[dict] = []
    campaigns: dict = {}

    from control.ingest import existing_revisions, keep_newest_revisions
    try:
        c0 = connect()
        stored = existing_revisions(c0, start, end, DATASET, TABLE)
        c0.close()
    except Exception:  # noqa: BLE001
        stored = {}
    dropped: list = []

    @dlt.resource(name=TABLE, write_disposition="merge", primary_key=["channel", "entity_id", "date"])
    def performance():
        rows_ = list(collect(jobs, start, end, records, campaigns))
        for row in keep_newest_revisions(rows_, stored, dropped):
            row["app"] = row.get("app") or "Unassigned"
            yield row

    target = db_target()
    if target.startswith("md:"):
        destination = dlt.destinations.motherduck(credentials=f"md:///{target[3:]}?token={os.environ['MOTHERDUCK_TOKEN']}")
    else:
        destination = dlt.destinations.duckdb(credentials=target)
    pipeline = dlt.pipeline(pipeline_name="adbot", destination=destination, dataset_name=DATASET)
    load_error = None
    try:
        info = pipeline.run(performance())
        log.info("Loaded: %s", info)
    except Exception as e:  # noqa: BLE001 - recorded on every import run of this sync
        load_error = f"{type(e).__name__}: {str(e)[:300]}"
        log.exception("dlt load failed")
    if dropped:
        log.info("Dropped %d duplicate / out-of-order report revisions", len(dropped))
    return {"records": records, "campaigns": campaigns, "configured": configured_sources(sources), "load_error": load_error,
            "window": (start, end), "dropped_revisions": len(dropped)}


AD_PLATFORM = {"Apple Search Ads": "apple_ads", "Meta Ads": "meta_ads", "Google Ads": "google_ads"}


def report_ad_status(con, result: dict) -> None:
    """Record imports (control.*) and mirror each ad account's state on the Revenue tab (revenue.store_accounts).
    An account is 'verified' only when an import finished; a finished import with no rows says so."""
    try:
        from control import ingest
        ingest.record(con, result["records"], result["campaigns"], result["configured"], result.get("load_error"))
    except Exception:  # noqa: BLE001
        log.exception("could not record import runs")
    try:
        from revenue import registry as rreg
        from revenue.schema import SCHEMA as R
        for name, platform in AD_PLATFORM.items():
            mine = [r for r in result["records"] if r["source"] == name]
            if not mine:
                continue  # credentials not set: nothing to report
            bad = [r for r in mine if r["status"] == "failed"]
            rows = sum(r["rows"] for r in mine if r["status"] == "succeeded")
            act = sum(r["activity_rows"] for r in mine if r["status"] == "succeeded")
            status = "failed" if len(bad) == len(mine) else ("partial" if bad else "succeeded")
            msg = ("; ".join(r["message"] or "" for r in bad)[:300] if bad else "") or (
                f"{rows} rows, {act} with activity" if rows else "Import finished: the platform returned no rows for the window")
            job = rreg.start_job(con, platform, "ad_sync")
            rreg.finish_job(con, job, status, rows, 0, message=msg)
            ok = status != "failed"
            con.execute(f"UPDATE {R}.store_accounts SET status = ?, last_error = ?, last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END, "
                        "updated_at = ? WHERE platform = ? AND purpose = 'reporting'",
                        ["verified" if ok else "error", None if ok else rreg.scrub_text(msg)[:500], ok, rreg.now(), rreg.now(), platform])
    except Exception:  # noqa: BLE001
        log.exception("could not record ad sync status")


# --------------------------------------------------------------------------- #
# Bot run: the control-plane decision engine (control/engine.py)
# --------------------------------------------------------------------------- #
def run_bot(con, rules: Rules, dry_run: bool | None = None, adapters: dict | None = None, alerter=None, environ=None,
            lease: tuple | None = None, execute: bool = True) -> dict:
    """One decision cycle. Live writes need DRY_RUN=false AND a mode that allows them; see control/engine.py.
    `dry_run` is kept for old callers: when given True it forces a dry run."""
    from control import adapters as adapters_mod
    from control import engine
    env = dict(os.environ if environ is None else environ)
    if dry_run:
        env["DRY_RUN"] = "true"
    if adapters is None:
        adapters = adapters_mod.live_adapters()
    res = engine.run_cycle(con, rules, apps=APPS, rule_app_fn=lambda name: app_for(name, rules), adapters=adapters,
                           alerter=alerter, environ=env, lease=lease, storage=storage_info(env)["label"],
                           execute_changes=execute)
    plan = {a: {"daily": v.get("daily_allowance"), "share": None, "efficiency": None}
            for a, v in (res.get("pacing", {}).get("apps") or {}).items() if v.get("daily_allowance") is not None}
    return {**res, "paused": res["executions"].get("simulated", 0) + res["executions"].get("applied", 0), "plan": plan}


def check_trading(con, rules: Rules, now: dt.datetime | None = None) -> None:
    """Safety net run by every scheduled job (every WORKER_EVERY_HOURS hours): alert if the trading bot has gone
    quiet while the FX market is open, or if its kill switch was turned on since the previous run.
    Read-only; it never touches the trading bot."""
    try:
        rows = con.execute("select bot_id, max(ts), arg_max(mode, ts) from trading.heartbeat group by bot_id").fetchall()
    except Exception:
        return  # trading reporting not set up yet
    demo_alerts = os.getenv("TRADING_ALERT_DEMO", "").lower() in ("1", "true", "yes")
    rows = [(b, last) for b, last, mode in rows
            if demo_alerts or str(mode or "").strip().lower() not in ("paper", "demo", "practice", "sim", "simulation", "test")]
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)   # naive UTC, like the heartbeat timestamps
    # A kill switch turned on since the previous run must alert, so the look-back covers the whole gap between
    # runs (plus slack for a late start), not a fixed hour: with runs every 4 hours a 1-hour window missed most.
    from revenue.store_check import every_hours
    since_last_run = (every_hours() * 3600) + 15 * 60
    wd, h = now.weekday(), now.hour
    fx_open = not (wd == 5 or (wd == 6 and h < 21) or (wd == 4 and h >= 22))
    def _dt(v):  # the real driver returns datetimes; tolerate ISO text (test shim, hand-written rows)
        return dt.datetime.fromisoformat(str(v).replace(" ", "T").rstrip("Z")) if v is not None and not isinstance(v, dt.datetime) else v

    for bot_id, last in rows:
        last = _dt(last)
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
        if ks and str(ks[0]).lower() in ("true", "1") and (now - _dt(ks[2])).total_seconds() < since_last_run:
            telegram(f"🛑 <b>Trading bot {bot_id}</b>: kill switch is ON. {ks[1] or ''}", rules)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=int(os.getenv("SYNC_DAYS", "14")))
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    con = connect()
    rules = load_rules(con)
    con.close()  # dlt opens its own connection while loading
    if args.test_telegram:
        ok = telegram("✅ <b>AdBot test</b>\nTelegram alerts are working for Rapid Nursing Review, CCRN Adult and NurseBound.", rules)
        print("sent" if ok else "not sent (check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        return 0 if ok else 1
    from control import state as cstate
    con = connect()
    owner = cstate.owner_id()
    if not cstate.acquire(con, "hourly", owner, ttl_minutes=55):
        log.warning("Another AdBot run holds the lock (%s); this run exits without importing or deciding", cstate.lock_holder(con, "hourly"))
        return 0
    con.close()
    try:
        if not args.no_sync:
            result_sync = run_sync(args.days, rules)
            log.info("Import runs: %s", [(r["source"], r["account_id"], r["status"], r["rows"]) for r in result_sync["records"]])
        con = connect()
        if not cstate.renew(con, "hourly", owner, ttl_minutes=55):
            log.error("The run lock expired or was taken during the import; this run makes no decisions")
            return 1
        rules = load_rules(con)
        if not args.no_sync:
            report_ad_status(con, result_sync)
        result = run_bot(con, rules, lease=("hourly", owner))
    finally:
        try:
            c2 = connect()
            cstate.release(c2, "hourly", owner)
        except Exception:  # noqa: BLE001
            log.exception("could not release the run lock (it expires on its own)")
    check_trading(con, rules)
    # Store ID check (read-only): answers "Check store IDs" from the phone app and re-checks once a day.
    try:
        from revenue import store_check
        log.info("Store ID check: %s", store_check.run(con))
    except Exception:  # noqa: BLE001
        log.exception("Store ID check failed")
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
    log.info("Decision cycle (mode %s, dry_run=%s): proposals %s, executions %s", result["mode"], result["dry_run"],
             result["proposals"], result["executions"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
