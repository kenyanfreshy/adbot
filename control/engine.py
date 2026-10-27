"""
The decision engine:

  DATA → QUALITY CHECKS → RECOMMENDATION → POLICY VALIDATION → EXECUTION → VERIFICATION → AUDIT

* DATA: performance rows (deduplicated per channel + entity + day, newest import wins), the shared
  source status, the campaign→app mapping, the budget policy.
* QUALITY: a rule only sees mature days (each channel's conversion-maturity lag is left out), from a
  source whose status is usable, in the currency the thresholds are written in, with enough days.
* RECOMMENDATION: channel-specific rules (control.channels) produce proposals with their evidence.
  Revenue-based rules need revenue the platform actually reported. Missing revenue is never zero.
* POLICY: deterministic server-side checks attach blockers: mode, stop latch, overrides, support for
  the action, cooldowns, per-campaign/account daily action limits, cumulative daily budget change,
  deadband, budget feasibility, pending uncertain executions.
* EXECUTION: only approved proposals (approval mode, manual overrides, emergency pauses) or proposals
  bounded-automatic mode may apply on its own, each bound to the exact values it was approved with.
  DRY_RUN=true turns every execution into a simulation: no adapter is called, emergency pauses included.
* VERIFICATION: re-read the remote state before writing and after; an uncertain result is reconciled
  before anything is retried.
* AUDIT: proposals, decisions, executions and outcomes are kept in control.* and revenue.audit_logs.

The engine never resumes anything that is paused.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import uuid
from typing import Optional

from . import alerts as alerts_mod
from . import budget as budget_mod
from . import ingest, state, status
from .adapters import AmbiguousResult, UnsupportedAction, desired_reached
from .channels import CHANNELS, MANAGED, pause_action, rules_for, supports
from .schema import SCHEMA

log = logging.getLogger("adbot.engine")

RULE_VERSION = "2026-10-27.1"
OPEN = ("proposed", "blocked", "approved", "executing", "uncertain", "applied")
CHANGE_RESULTS = ("accepted", "verified", "simulated", "uncertain", "reconciled_verified", "applied", "reconciled_applied")
RULE_TEXT = {
    "cpt_ceiling": "Cost per tap/click above the ceiling",
    "no_conversions_after_clicks": "No conversions after many taps/clicks",
    "no_conversions_after_spend": "No conversions after the spend threshold",
    "acos_ceiling": "Ad cost of sales above the ceiling (platform-attributed revenue)",
    "manual_override": "Paused by you on the Keywords tab",
    "budget_pacing": "Budget above the app's daily pacing allowance",
    "campaign_daily_max": "Budget above the per-campaign daily maximum",
    "spend_safeguard": "Month-to-date spend reached the monthly limit",
    "emergency_pause": "Emergency pause requested",
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _ts(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v).replace("Z", "").replace(" ", "T"))


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


AUDIT_ACTORS = ("system", "admin", "ai_agent", "scheduler")   # revenue.audit_logs CHECK constraint


def _audit(con, actor, action, target, detail, dry):
    try:
        from revenue import registry
        if actor not in AUDIT_ACTORS:      # keep who it was, under an actor the table accepts
            detail = {**(detail if isinstance(detail, dict) else {"detail": detail}), "by": actor}
            actor = "admin" if str(actor).startswith("admin") else "system"
        registry.audit(con, actor, action, target, detail, dry_run=dry)
    except Exception:  # noqa: BLE001 - audit must never stop the engine
        log.exception("audit write failed")


# --------------------------------------------------------------------------- #
# DATA
# --------------------------------------------------------------------------- #
def source_statuses(con, rules, now=None) -> dict:
    rows = {r["source"]: r for r in _dicts(con.execute(f"SELECT * FROM {SCHEMA}.source_state"))}
    out = {}
    for ch, info in CHANNELS.items():
        r = rows.get(ch)
        if r is None:   # the worker (which holds the credentials) hasn't reported this source to this database yet
            st = status.classify(configured=False, supported=info["supported"], now=now)
            if info["supported"]:
                st.update(label="Not reported yet", reported=False,
                          detail="The hourly job hasn't reported this source to this database yet. Only the hourly job has the "
                                 "platform keys, so the web panel can't tell whether they're set until it runs.")
            out[ch] = {"source": ch, **st}
        else:
            out[ch] = status.from_source_row(r, now=now, stale_after_hours=float(rules.stale_after_hours))
    return out


def perf_table_exists(con, dataset: str, table: str) -> list[str]:
    cur = con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ?", [dataset, table])
    return [r[0] for r in cur.fetchall()]


def load_rows(con, start: dt.date, end: dt.date, dataset: str = "ads", table: str = "ad_performance") -> list[dict]:
    """Rows between start and end, one per channel + entity + day (the newest import wins)."""
    cols = perf_table_exists(con, dataset, table)
    if not cols:
        return []
    keys = [k for k in ("_fetched_at", "_dlt_load_id", "import_id") if k in cols] or ["date"]
    order = ", ".join(f"{k} DESC NULLS LAST" for k in keys)
    sel = ", ".join(c for c in cols if c not in ("_dlt_id",))
    sql = (f"SELECT * FROM (SELECT {sel}, row_number() OVER (PARTITION BY channel, entity_id, CAST(date AS VARCHAR) "
           f"ORDER BY {order}) AS rn FROM {dataset}.{table}) WHERE rn = 1 AND CAST(date AS VARCHAR) BETWEEN ? AND ?")
    out = []
    for r in _dicts(con.execute(sql, [start.isoformat(), end.isoformat()])):
        r["date"] = str(r.get("date"))[:10]
        for f in ("impressions", "taps", "installs", "spend"):
            r[f] = _f(r.get(f))
        rv = r.get("revenue")
        r["revenue"] = None if rv is None else _f(rv)
        r["currency"] = (str(r["currency"]).upper() if r.get("currency") else None)
        out.append(r)
    return out


def maturity(rules, channel: str) -> int:
    return int((rules.maturity_days or {}).get(channel, CHANNELS.get(channel, {}).get("maturity_days", 1)))


def aggregate(rows: list[dict], rules, cmap: dict, today: dt.date, apps: list[str], rule_app_fn) -> dict:
    """Per entity over each channel's MATURE window. Also returns month-to-date spend and budget entities."""
    ents: dict[str, dict] = {}
    mtd_app: dict[str, float] = {}
    mtd_channel: dict[str, float] = {}
    unattributed = 0.0
    budget_ents: dict[str, dict] = {}
    month_start = today.replace(day=1)
    for r in rows:
        ch = r.get("channel")
        if ch not in CHANNELS:
            continue
        app = ingest.resolve_app(r, cmap, rule_app_fn)
        if app == ingest.IGNORED:
            continue
        managed = app in apps
        d = dt.date.fromisoformat(r["date"])
        if d >= month_start:
            if managed:
                mtd_app[app] = mtd_app.get(app, 0.0) + r["spend"]
            else:
                unattributed += r["spend"]
            mtd_channel[ch] = mtd_channel.get(ch, 0.0) + r["spend"]
        # budget-level entity (campaign / ad set) for pacing, safeguards and emergency pauses
        if managed:
            be = budget_entity(r)
            if be:
                b = budget_ents.setdefault(be["entity_key"], {**be, "app": app, "spend_7d": 0.0, "status": None, "_sd": ""})
                if (today - d).days < 7:
                    b["spend_7d"] += r["spend"]
                # the entity's own status only comes from its own rows (a keyword's status isn't its campaign's)
                if str(r.get("entity_id")) == be["entity_id"] and r["date"] >= b["_sd"]:
                    b["_sd"], b["status"] = r["date"], r.get("status")
        if not managed:
            continue
        lag = maturity(rules, ch)
        mature_end = today - dt.timedelta(days=lag)
        win_start = mature_end - dt.timedelta(days=int(rules.lookback_days) - 1)
        if d > mature_end or d < win_start:
            continue
        k = f"{ch}|{r['entity_id']}"
        a = ents.setdefault(k, {"entity_key": k, "channel": ch, "entity_id": str(r["entity_id"]), "entity_type": r.get("entity_type"),
                                "name": r.get("keyword") or r.get("campaign"), "campaign_id": str(r.get("campaign_id")),
                                "campaign": r.get("campaign"), "adgroup_id": r.get("adgroup_id"), "account_id": r.get("account_id"),
                                "app": app, "status": r.get("status"), "_last": "", "taps": 0.0, "impressions": 0.0,
                                "installs": 0.0, "spend": 0.0, "revenue": 0.0, "revenue_known": True, "currencies": set(),
                                "dates": set(), "window_start": win_start, "window_end": mature_end})
        for f in ("taps", "impressions", "installs", "spend"):
            a[f] += r[f]
        if r["revenue"] is None or CHANNELS[ch]["revenue"] == "none":
            a["revenue_known"] = False
        else:
            a["revenue"] += r["revenue"]
        if r.get("currency"):
            a["currencies"].add(r["currency"])
        a["dates"].add(r["date"])
        if r["date"] >= a["_last"]:
            a["_last"], a["status"] = r["date"], r.get("status")
    for a in ents.values():
        if not a["revenue_known"]:
            a["revenue"] = None
        a["cpt"] = a["spend"] / a["taps"] if a["taps"] else None
        if a["revenue"] is None:
            a["acos"] = None                      # unknown: no ACoS at all
        elif a["revenue"] > 0:
            a["acos"] = a["spend"] / a["revenue"]
        else:
            a["acos"] = float("inf") if a["spend"] > 0 else None   # revenue reported as $0
    return {"entities": ents, "mtd_app": mtd_app, "mtd_channel": mtd_channel, "unattributed_mtd": unattributed,
            "budget_entities": budget_ents}


def budget_entity(r: dict) -> Optional[dict]:
    """The campaign / ad set that carries the budget for a performance row."""
    ch = r.get("channel")
    if ch == "Meta Ads":
        return {"entity_key": f"Meta Ads|{r['entity_id']}", "entity_id": str(r["entity_id"]), "entity_type": "ad_set",
                "campaign_id": str(r.get("campaign_id")), "account_id": r.get("account_id"), "name": r.get("keyword") or r.get("campaign")}
    if ch == "Apple Search Ads" and r.get("campaign_id"):
        cid = str(r["campaign_id"])
        return {"entity_key": f"Apple Search Ads|campaign:{cid}", "entity_id": f"campaign:{cid}", "entity_type": "campaign",
                "campaign_id": cid, "account_id": r.get("account_id"), "name": r.get("campaign")}
    if ch == "Google Ads" and r.get("campaign_id") and "~" in str(r["campaign_id"]):
        cust, camp = str(r["campaign_id"]).split("~", 1)
        return {"entity_key": f"Google Ads|{cust}~c{camp}", "entity_id": f"{cust}~c{camp}", "entity_type": "app_campaign",
                "campaign_id": str(r["campaign_id"]), "account_id": r.get("account_id") or cust, "name": r.get("campaign")}
    return None


# --------------------------------------------------------------------------- #
# RECOMMENDATION
# --------------------------------------------------------------------------- #
def _proposal(kind, a, action, reason, rule_id, metrics, *, current=None, proposed=None, expected=None, freshness=None, sufficiency=None):
    return {"kind": kind, "app": a.get("app"), "channel": a["channel"], "account_id": a.get("account_id"),
            "campaign_id": a.get("campaign_id"), "entity_key": a["entity_key"], "entity_type": a.get("entity_type"),
            "entity_id": a.get("entity_id"), "entity_name": a.get("name"), "action": action,
            "current_value": None if current is None else str(current), "proposed_value": None if proposed is None else str(proposed),
            "metrics": metrics, "window_start": a.get("window_start"), "window_end": a.get("window_end"),
            "freshness": freshness, "sufficiency": sufficiency, "rule_id": rule_id, "rule_version": RULE_VERSION,
            "reason": reason, "expected_effect": expected, "blockers": []}


def performance_proposals(agg: dict, rules, statuses: dict) -> tuple[list[dict], list[dict]]:
    """(proposals, notes). Notes explain rules that couldn't run, e.g. revenue not reported."""
    props, notes = [], []
    for k, a in agg["entities"].items():
        ch, et = a["channel"], a.get("entity_type")
        if str(a.get("status") or "").upper() in ("PAUSED", "REMOVED", "DELETED", "ARCHIVED"):
            continue   # never resumed, nothing to pause
        ov = (rules.overrides or {}).get(k)
        if ov == "protected":
            continue
        act = pause_action(ch, et)
        metrics = {"taps": a["taps"], "impressions": a["impressions"], "conversions": a["installs"], "spend": round(a["spend"], 2),
                   "revenue": a["revenue"], "revenue_known": a["revenue_known"], "cpt": a["cpt"],
                   "acos": None if a["acos"] in (None, float("inf")) else a["acos"],
                   "currency": ",".join(sorted(a["currencies"])) or None, "days": len(a["dates"]),
                   "conversion_label": CHANNELS[ch]["conversion_label"], "click_label": CHANNELS[ch]["click_label"],
                   "adgroup_id": a.get("adgroup_id")}
        st = statuses.get(ch, {})
        fresh = f"{st.get('label')} · last success {st.get('last_success') or 'never'}"
        if ov == "paused":
            if act:
                props.append(_proposal("manual", a, act, "Paused by you", "manual_override", metrics, freshness=fresh,
                                       sufficiency="Manual instruction; no evidence required",
                                       expected="Stops spend on this item until you resume it in the platform"))
            continue
        if not act:
            continue
        enough_days = len(a["dates"]) >= int(rules.min_days_data)
        suff = f"{len(a['dates'])} mature day(s) in {a['window_start']}–{a['window_end']}; needs {rules.min_days_data}"
        lbl_c, lbl_v = CHANNELS[ch]["click_label"], CHANNELS[ch]["conversion_label"]
        for rid in rules_for(ch, et):
            reason = None
            if rid == "cpt_ceiling" and a["taps"] >= rules.min_taps_for_cpt and a["cpt"] is not None and a["cpt"] > rules.max_cpt:
                reason = f"Cost per {lbl_c[:-1] if lbl_c.endswith('s') else lbl_c} ${a['cpt']:.2f} is above the ${rules.max_cpt:.2f} ceiling ({int(a['taps'])} {lbl_c})"
            elif rid == "no_conversions_after_clicks" and a["taps"] >= rules.zero_install_taps and a["installs"] == 0:
                reason = f"{int(a['taps'])} {lbl_c} and no {lbl_v}"
            elif rid == "no_conversions_after_spend" and a["spend"] >= rules.min_spend_for_acos and a["installs"] == 0:
                reason = f"${a['spend']:.2f} spent with no {lbl_v}"
            elif rid == "acos_ceiling":
                if not a["revenue_known"]:
                    if a["spend"] >= rules.min_spend_for_acos:
                        notes.append({"entity_key": k, "rule": rid, "note": "Revenue isn't reported for this item, so the ACoS rule can't run"})
                    continue
                if a["installs"] < rules.min_conversions_for_revenue_rules:
                    continue
                if a["spend"] >= rules.min_spend_for_acos and a["acos"] is not None and a["acos"] > rules.max_acos:
                    reason = (f"ACoS {a['acos']*100:.0f}% is above {rules.max_acos*100:.0f}% (platform-attributed revenue ${a['revenue']:.2f})"
                              if a["revenue"] else f"The platform reported $0 attributed revenue on ${a['spend']:.2f} spend")
            if not reason:
                continue
            p = _proposal("optimization", a, act, reason, rid, metrics, freshness=fresh, sufficiency=suff,
                          expected=f"Estimate: saves up to ~${a['spend'] / max(1, len(a['dates'])):.2f}/day of spend at the recent rate")
            if not enough_days:
                p["blockers"].append(f"Not enough mature data: {suff}")
            props.append(p)
            break   # one pause proposal per item is enough
    return props, notes


def safeguard_proposals(agg: dict, rules, pace: dict, apps: list[str]) -> tuple[list[dict], list[dict]]:
    """Spend at or over a monthly limit → pause that scope's active budget entities. Returns (proposals, warnings)."""
    props, warns = [], []
    over_apps = set()
    tot = pace.get("total")
    if tot and tot.get("used_pct") is not None:
        if tot["used_pct"] >= 1.0:
            over_apps = set(apps)
            warns.append({"severity": "critical", "key": "budget:total:100", "text": f"Month-to-date spend ${tot['spent_mtd']:,.2f} reached the ${tot['monthly']:,.2f} monthly budget"})
        elif tot["used_pct"] >= 0.9:
            warns.append({"severity": "warning", "key": "budget:total:90", "text": f"Month-to-date spend is {tot['used_pct']*100:.0f}% of the monthly budget"})
    for a, v in pace.get("apps", {}).items():
        if v.get("used_pct") is None:
            continue
        if v["used_pct"] >= 1.0:
            over_apps.add(a)
            warns.append({"severity": "critical", "key": f"budget:{a}:100", "text": f"{a}: month-to-date spend ${v['spent_mtd']:,.2f} reached its ${v['monthly']:,.2f} allocation"})
        elif v["used_pct"] >= 0.9:
            warns.append({"severity": "warning", "key": f"budget:{a}:90", "text": f"{a}: {v['used_pct']*100:.0f}% of its monthly allocation spent"})
    for c, v in pace.get("channels", {}).items():
        if v.get("used_pct") is not None and v["used_pct"] >= 0.9:
            warns.append({"severity": "critical" if v["used_pct"] >= 1 else "warning", "key": f"budget:{c}:{'100' if v['used_pct'] >= 1 else '90'}",
                          "text": f"{c}: {v['used_pct']*100:.0f}% of its monthly limit spent"})
    for be in agg["budget_entities"].values():
        if be["app"] not in over_apps or str(be.get("status") or "").upper() in ("PAUSED", "REMOVED", "DELETED", "ARCHIVED"):
            continue
        ch = be["entity_key"].split("|")[0]
        act = pause_action(ch, be["entity_type"])
        if not act:
            continue
        a = {**be, "channel": ch}
        props.append(_proposal("safeguard", a, act, f"{be['app']}: monthly spend limit reached", "spend_safeguard",
                               {"spend_7d": round(be["spend_7d"], 2)}, sufficiency="Spend limit; no performance evidence needed",
                               expected="Stops further spend in this campaign; spend already reported late may still arrive"))
    return props, warns


def budget_proposals(agg: dict, rules, pace: dict, adapters: dict, apps: list[str], stopped: bool, emergency: bool,
                     current_out: Optional[dict] = None) -> tuple[list[dict], list[str]]:
    """Per-entity daily budgets that fit each app's pacing allowance. Needs the current remote budget."""
    props, notes = [], []
    if budget_mod.validate(rules, apps, MANAGED):
        return props, notes
    by_app: dict[str, list] = {}
    for be in agg["budget_entities"].values():
        if str(be.get("status") or "").upper() in ("PAUSED", "REMOVED", "DELETED", "ARCHIVED"):
            continue
        ch = be["entity_key"].split("|")[0]
        if not supports(ch, be["entity_type"], "set_budget"):
            continue
        by_app.setdefault(be["app"], []).append({**be, "channel": ch})
    for app, members in by_app.items():
        allow = (pace.get("apps", {}).get(app) or {}).get("daily_allowance")
        if allow is None:
            continue
        current = {}
        for m in members:
            ad = adapters.get(m["channel"])
            if ad is None:
                continue
            try:
                stt = ad.read_state({**m, "entity_key": m["entity_key"], "action": "set_budget", "metrics": "{}"})
            except Exception as e:  # noqa: BLE001
                notes.append(f"{m['name']}: couldn't read its current budget ({type(e).__name__})")
                continue
            if stt and stt.get("daily_budget") is not None:
                current[m["entity_key"]] = float(stt["daily_budget"])
        if current_out is not None:
            current_out[app] = dict(current)
        if not current:
            notes.append(f"{app}: current campaign budgets aren't readable on this process, so only the pacing plan is shown")
            continue
        targets = budget_mod.split_allowance(allow, {m["entity_key"]: m["spend_7d"] for m in members if m["entity_key"] in current},
                                             rules.campaign_daily_max)
        for m in members:
            k = m["entity_key"]
            if k not in current:
                continue
            cur, tgt = current[k], max(1.0, targets[k])
            if abs(tgt - cur) < max(0.01, cur * float(rules.deadband_pct)):
                continue
            increase = tgt > cur
            if increase and (stopped or emergency):
                continue
            rid = "campaign_daily_max" if rules.campaign_daily_max is not None and cur > float(rules.campaign_daily_max) and tgt <= float(rules.campaign_daily_max) else "budget_pacing"
            reason = (f"{app} daily allowance is ${allow:,.2f} across its campaigns; this one gets ${tgt:,.2f} by its share of recent spend"
                      if rid == "budget_pacing" else f"Above the ${float(rules.campaign_daily_max):,.2f} per-campaign daily maximum")
            p = _proposal("optimization", {**m, "window_start": None, "window_end": None}, "set_budget", reason, rid,
                          {"spend_7d": round(m["spend_7d"], 2), "allowance": allow, "currency": rules.budget_currency},
                          current=round(cur, 2), proposed=round(tgt, 2),
                          sufficiency="Pacing from the monthly allocation and observed month-to-date spend",
                          expected=f"Estimate: requested budget changes by ${tgt - cur:+,.2f}/day; actual spend may differ")
            p["direction"] = "increase" if increase else "decrease"
            props.append(p)
    return props, notes


# --------------------------------------------------------------------------- #
# POLICY
# --------------------------------------------------------------------------- #
def _history(con, since: dt.datetime) -> list[dict]:
    return _dicts(con.execute(
        f"SELECT e.proposal_id, e.attempted_at, e.result, p.status AS pstatus, p.entity_key, p.campaign_id, p.account_id, p.action, p.current_value, p.proposed_value, p.kind "
        f"FROM {SCHEMA}.executions e JOIN {SCHEMA}.proposals p USING (proposal_id) WHERE e.attempted_at > ?", [since]))


def validate(p: dict, rules, *, mode: str, stopped: bool, statuses: dict, history: list[dict], now: dt.datetime,
             open_uncertain: set, emergency: bool) -> None:
    """Attach blockers (plain language) and next_eligible_at. Deterministic; runs on the server only."""
    b = p["blockers"]
    kind = p["kind"]
    if mode == "observe" and kind in ("optimization",):
        b.append("Operating mode is Observe")
    if stopped and kind != "emergency":
        b.append("Automation is stopped")
    if not supports(p["channel"], p.get("entity_type"), p["action"]):
        b.append(f"{p['channel']} {p.get('entity_type')} doesn't support {p['action']} in AdBot")
    if p["entity_key"] in open_uncertain:
        b.append("An earlier change to this item has an uncertain result; it must be reconciled first")
    st = statuses.get(p["channel"], {})
    if kind == "optimization" and st.get("state") not in status.USABLE:
        b.append(f"{p['channel']} data is {st.get('label', 'unknown').lower()}")
    cur = (p.get("metrics") or {}).get("currency")
    if kind == "optimization" and cur and cur != rules.budget_currency and p["rule_id"] in ("cpt_ceiling", "no_conversions_after_spend", "acos_ceiling", "budget_pacing"):
        b.append(f"Thresholds are in {rules.budget_currency} but this data is in {cur}")
    if kind == "optimization" and p["action"] == "set_budget" and emergency and p.get("direction") == "increase":
        b.append("An emergency pause is active")
    if kind in ("safeguard", "emergency", "manual"):
        return   # protective / human actions: no cooldowns or daily limits
    day = now - dt.timedelta(hours=24)
    # changes that count: applied, simulated, or still uncertain (a reconciled "not applied" doesn't count)
    first: dict = {}
    for h in history:     # one change = one proposal, however many execution rows it has (accepted → verified)
        if h["result"] in CHANGE_RESULTS and h["pstatus"] in ("applied", "verified", "simulated", "uncertain", "executing"):
            if h["proposal_id"] not in first or _ts(h["attempted_at"]) < _ts(first[h["proposal_id"]]["attempted_at"]):
                first[h["proposal_id"]] = h
    history = list(first.values())
    mine = [h for h in history if h["entity_key"] == p["entity_key"]]
    last = max((_ts(h["attempted_at"]) for h in mine), default=None)
    cool = dt.timedelta(hours=float(rules.cooldown_hours))
    nxt = []
    if last and now - last < cool:
        b.append(f"Changed {int((now - last).total_seconds() // 3600)} h ago; cooldown is {rules.cooldown_hours:g} h")
        nxt.append(last + cool)
    camp = [h for h in history if h["campaign_id"] == p.get("campaign_id") and _ts(h["attempted_at"]) > day and h["result"] in CHANGE_RESULTS]
    if len(camp) >= int(rules.max_actions_per_campaign_day):
        b.append(f"{len(camp)} changes to this campaign in 24 h (limit {rules.max_actions_per_campaign_day})")
        nxt.append(min(_ts(h["attempted_at"]) for h in camp) + dt.timedelta(hours=24))
    acct = [h for h in history if h["account_id"] == p.get("account_id") and _ts(h["attempted_at"]) > day and h["result"] in CHANGE_RESULTS]
    if p.get("account_id") and len(acct) >= int(rules.max_actions_per_account_day):
        b.append(f"{len(acct)} changes in this ad account in 24 h (limit {rules.max_actions_per_account_day})")
        nxt.append(min(_ts(h["attempted_at"]) for h in acct) + dt.timedelta(hours=24))
    if p["action"] == "set_budget" and p.get("current_value") is not None:
        used = 0.0
        for h in mine:
            if h["action"] == "set_budget" and _ts(h["attempted_at"]) > day and h["current_value"]:
                c0 = _f(h["current_value"])
                used += abs(_f(h["proposed_value"]) - c0) / c0 if c0 else 0.0
        cur_v, prop_v = _f(p["current_value"]), _f(p["proposed_value"])
        room = float(rules.max_daily_change_pct) - used
        if cur_v and abs(prop_v - cur_v) / cur_v > room + 1e-9:
            if room <= float(rules.deadband_pct):
                b.append(f"Daily budget-change limit reached ({used*100:.0f}% of {rules.max_daily_change_pct*100:.0f}% used)")
                nxt.append(now + dt.timedelta(hours=24))
            else:   # clamp to what's left of today's allowance
                step = cur_v * room
                newv = round(cur_v + step if prop_v > cur_v else max(1.0, cur_v - step), 2)
                p["reason"] += f" (limited to {room*100:.0f}% today; full target ${prop_v:,.2f})"
                p["proposed_value"] = str(newv)
    if nxt:
        p["next_eligible_at"] = max(nxt)


def fingerprint(p: dict) -> str:
    raw = "|".join(str(p.get(k)) for k in ("kind", "entity_key", "action", "proposed_value", "rule_id"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def auto_allowed(p: dict, rules) -> bool:
    allowed = set(rules.auto_actions or [])
    if p["kind"] == "safeguard":
        return "safeguard_pause" in allowed
    if p["action"] == "set_budget":
        return ("set_budget_increase" if p.get("direction") == "increase" else "set_budget_decrease") in allowed
    return p["action"] in allowed


# --------------------------------------------------------------------------- #
# Binding: what an approval is an approval OF
# --------------------------------------------------------------------------- #
POLICY_FIELDS = ("max_cpt", "min_taps_for_cpt", "zero_install_taps", "max_acos", "min_spend_for_acos",
                 "min_conversions_for_revenue_rules", "lookback_days", "min_days_data", "maturity_days", "monthly_budget",
                 "app_monthly", "channel_monthly", "campaign_daily_max", "reserve_pct", "budget_currency",
                 "max_daily_change_pct", "cooldown_hours", "max_actions_per_campaign_day", "max_actions_per_account_day",
                 "deadband_pct", "auto_actions", "stale_after_hours")
REAPPROVAL = "Needs a new approval"
HASH_FIELDS = ("kind", "channel", "account_id", "campaign_id", "entity_key", "entity_type", "action", "current_value",
               "proposed_value", "currency", "rule_id", "rule_version", "policy_version", "window_start", "window_end")


def policy_version(rules) -> str:
    d = {k: getattr(rules, k, None) for k in POLICY_FIELDS}
    return RULE_VERSION + ":" + hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _canon_metrics(m) -> str:
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            return m
    return json.dumps(m or {}, sort_keys=True, default=str)


def proposal_hash(p: dict) -> str:
    """Exact values + evidence + account/campaign + rule and policy version. Recomputed before every write."""
    parts = ["" if p.get(k) is None else str(p.get(k))[:10] if k.startswith("window_") else str(p.get(k)) for k in HASH_FIELDS]
    parts.append(_canon_metrics(p.get("metrics")))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _blk(row) -> list:
    b = row.get("blockers")
    if isinstance(b, str):
        try:
            return json.loads(b)
        except ValueError:
            return [b]
    return list(b or [])


def _invalidate(con, pid: str, why: str, now: dt.datetime, extra: Optional[list] = None) -> None:
    """Approved → blocked: the approval no longer stands and a person must approve again."""
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'blocked', reapproval_required = TRUE, approved_hash = NULL, "
                "approval_expires_at = NULL, blockers = ?, outcome = ?, outcome_at = ? WHERE proposal_id = ?",
                [json.dumps([f"{REAPPROVAL}: {why}"] + list(extra or [])), why[:500], now, pid])


# --------------------------------------------------------------------------- #
# Persistence of proposals (deduplicated)
# --------------------------------------------------------------------------- #
INSERT_COLS = ["proposal_id", "fingerprint", "kind", "created_at", "run_id", "app", "channel", "account_id", "campaign_id", "entity_key",
               "entity_type", "entity_name", "action", "current_value", "proposed_value", "metrics", "window_start", "window_end",
               "freshness", "sufficiency", "rule_id", "rule_version", "reason", "expected_effect", "blockers", "next_eligible_at",
               "status", "decided_by", "decided_at", "outcome", "outcome_at", "currency", "policy_version", "proposal_hash",
               "approved_hash", "approval_expires_at", "reapproval_required", "request_id"]


def _insert(con, row: dict) -> None:
    row.setdefault("reapproval_required", False)
    con.execute(f"INSERT INTO {SCHEMA}.proposals ({', '.join(INSERT_COLS)}) VALUES ({', '.join('?' * len(INSERT_COLS))})",
                [row.get(c) for c in INSERT_COLS])


def _prepare(c: dict, rules, pv: str) -> None:
    c["policy_version"] = pv
    c["currency"] = c.get("currency") or (c.get("metrics") or {}).get("currency") or (rules.budget_currency if c["action"] == "set_budget" else None)
    c["metrics_json"] = _canon_metrics(c.get("metrics"))
    c["proposal_hash"] = proposal_hash({**c, "metrics": c["metrics_json"]})


def store(con, cands: list[dict], run_id: str, now: dt.datetime, rules, dry: bool) -> list[dict]:
    """Insert new proposals, refresh identical open ones, supersede changed ones, expire ones no longer produced.
    Respects recent human rejections and recent identical executions (no daily repeats). An approval only
    survives a cycle when the same rule still fires with the same values under the same policy version."""
    pv = policy_version(rules)
    ttl = dt.timedelta(hours=float(getattr(rules, "approval_ttl_hours", 24) or 24))
    open_rows = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE status IN ({', '.join('?' * len(OPEN))})", list(OPEN)))
    by_key = {(r["entity_key"], r["action"]): r for r in open_rows}
    recent = {r["fingerprint"]: r for r in _dicts(con.execute(
        f"SELECT fingerprint, status, outcome_at, decided_at FROM {SCHEMA}.proposals WHERE status IN ('rejected', 'simulated', 'verified', 'already_done') "
        f"AND coalesce(outcome_at, decided_at, created_at) > ?", [now - dt.timedelta(days=7)]))}
    seen, out = set(), []
    for c in cands:
        fp = fingerprint(c)
        c["fingerprint"] = fp
        _prepare(c, rules, pv)
        key = (c["entity_key"], c["action"])
        seen.add(key)
        prev_closed = recent.get(fp)
        if prev_closed and prev_closed["status"] == "rejected":
            continue
        if prev_closed and prev_closed["status"] in ("simulated", "verified", "already_done") and c["kind"] not in ("emergency",):
            t = _ts(prev_closed["outcome_at"] or prev_closed["decided_at"])
            if t and now - t < dt.timedelta(hours=float(rules.cooldown_hours)):
                continue
        old = by_key.get(key)
        if old and old["kind"] in ("emergency", "manual", "safeguard") and c["kind"] == "optimization" \
                and old["status"] in ("approved", "proposed", "executing", "uncertain", "applied"):
            continue   # a human / protective request for the same change already exists; don't replace it
        status_ = "blocked" if c["blockers"] else ("approved" if c["kind"] == "manual" else "proposed")
        if old and old["fingerprint"] == fp:
            if old["status"] in ("proposed", "blocked"):
                blockers = list(c["blockers"])
                if old.get("reapproval_required"):
                    status_ = "blocked"
                    blockers = [b for b in _blk(old) if b.startswith(REAPPROVAL)][:1] + blockers
                con.execute(f"UPDATE {SCHEMA}.proposals SET metrics = ?, blockers = ?, next_eligible_at = ?, status = ?, freshness = ?, "
                            "sufficiency = ?, reason = ?, policy_version = ?, proposal_hash = ?, currency = ? WHERE proposal_id = ?",
                            [c["metrics_json"], json.dumps(blockers), c.get("next_eligible_at"), status_, c.get("freshness"),
                             c.get("sufficiency"), c["reason"], pv, c["proposal_hash"], c["currency"], old["proposal_id"]])
                if status_ == "approved":       # a manual override, unblocked: bound to exactly these values
                    con.execute(f"UPDATE {SCHEMA}.proposals SET approved_hash = proposal_hash, approval_expires_at = ?, "
                                "decided_by = 'admin (override)', decided_at = ? WHERE proposal_id = ?", [now + ttl, now, old["proposal_id"]])
                old.update(status=status_, blockers=json.dumps(blockers), proposal_hash=c["proposal_hash"])
            elif old["status"] == "approved" and old["kind"] in ("optimization", "safeguard") and old.get("policy_version") != pv:
                _invalidate(con, old["proposal_id"], "the rules changed after it was approved", now)
                old.update(status="blocked")
            out.append({**old, "_cand": c})
            continue
        if old:
            if old["status"] in ("executing", "uncertain", "applied"):
                continue   # wait for that one to settle
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'superseded', outcome = 'replaced by a newer proposal', outcome_at = ? "
                        "WHERE proposal_id = ?", [now, old["proposal_id"]])
        manual = c["kind"] == "manual" and status_ == "approved"
        row = {**c, "proposal_id": uuid.uuid4().hex, "created_at": now, "run_id": run_id, "status": status_,
               "metrics": c["metrics_json"], "blockers": json.dumps(c["blockers"]),
               "decided_by": "admin (override)" if manual else None, "decided_at": now if manual else None,
               "approved_hash": c["proposal_hash"] if manual else None, "approval_expires_at": now + ttl if manual else None}
        _insert(con, row)
        _audit(con, "system", "proposal.created", c["entity_key"], {"action": c["action"], "kind": c["kind"], "rule": c["rule_id"],
                                                                   "status": status_, "blockers": c["blockers"]}, dry)
        out.append({**row, "_cand": c})
    for key, old in by_key.items():
        if key in seen:
            continue
        if old["status"] in ("proposed", "blocked") and old["kind"] in ("optimization", "safeguard", "manual"):
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'expired', outcome = 'conditions no longer met', outcome_at = ? "
                        "WHERE proposal_id = ?", [now, old["proposal_id"]])
        elif old["status"] == "approved" and old["kind"] in ("optimization", "safeguard", "manual"):
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'expired', outcome = 'evidence changed after approval; not applied', "
                        "outcome_at = ? WHERE proposal_id = ?", [now, old["proposal_id"]])
    return out


# --------------------------------------------------------------------------- #
# EXECUTION + VERIFICATION
#
# Lifecycle:  proposed → approved → executing → applied (platform accepted) → verified (re-read confirms)
#             blocked / expired / failed / uncertain (outcome unknown: reconciled by READING, never re-sent)
# Before every write: re-read the stop flag, renew the job lease, re-check the approval binding and expiry,
# campaign scope, data quality, emergency state and budget headroom, then read the remote state.
# A write-ahead intent is persisted before the request goes out, so a crash leaves evidence to reconcile.
# --------------------------------------------------------------------------- #
VERIFY_GRACE = dt.timedelta(hours=3)


class ExecContext:
    def __init__(self, con, rules, *, adapters=None, dry=True, now=None, worker="worker", lease=None, apps=(), cmap=None,
                 statuses=None, pace=None, current_budgets=None, budgets_read_at=None, emergency=None, live_fps=None,
                 rule_app_fn=None, lease_ttl=55, mode="recommend"):
        self.con, self.rules, self.adapters, self.dry = con, rules, adapters or {}, dry
        self.now = now or _now()
        self.worker, self.lease, self.apps, self.cmap = worker, lease, list(apps), cmap or {}
        self.statuses, self.pace = statuses or {}, pace or {}
        self.current_budgets, self.budgets_read_at = current_budgets or {}, budgets_read_at or self.now
        self.emergency, self.live_fps, self.mode = emergency, live_fps, mode
        self.rule_app_fn = rule_app_fn or (lambda n: None)
        self.lease_ttl = lease_ttl
        self.policy_version = policy_version(rules)


def _claim(con, pid: str, from_status: tuple, worker: str, now) -> bool:
    token = "claim:" + uuid.uuid4().hex
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'executing', outcome = ?, claimed_by = ?, claimed_at = ? WHERE proposal_id = ? "
                f"AND status IN ({', '.join('?' * len(from_status))})", [token, worker, now, pid, *from_status])
    r = con.execute(f"SELECT outcome FROM {SCHEMA}.proposals WHERE proposal_id = ?", [pid]).fetchone()
    return bool(r and r[0] == token)


def _record(con, p: dict, now, exec_mode: str, result: str, pre=None, post=None, error=None, intent_id=None, worker=None, dry=True):
    con.execute(f"INSERT INTO {SCHEMA}.executions (exec_id, proposal_id, attempted_at, mode, pre_state, result, post_state, error, "
                "intent_id, worker) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [uuid.uuid4().hex, p["proposal_id"], now, exec_mode, json.dumps(pre, default=str) if pre is not None else None,
                 result, json.dumps(post, default=str) if post is not None else None, (error or None) and str(error)[:500],
                 intent_id, worker])
    _audit(con, "system", f"execution.{result}", p["entity_key"], {"action": p["action"], "value": p.get("proposed_value"),
                                                                  "mode": exec_mode, "proposal_id": p["proposal_id"]}, dry)


def _finish(con, p: dict, status_: str, outcome: str, now, exec_mode: str, result: str, pre=None, post=None, error=None, dry=True,
            intent_id=None, worker=None):
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = ?, outcome = ?, outcome_at = ? WHERE proposal_id = ?",
                [status_, outcome[:500], now, p["proposal_id"]])
    _record(con, p, now, exec_mode, result, pre, post, error, intent_id, worker, dry)


def _intent(con, p, ctx, pre) -> str:
    iid = uuid.uuid4().hex
    req = {"action": p["action"], "entity_id": p.get("entity_id"), "campaign_id": p.get("campaign_id"),
           "value": p.get("proposed_value"), "currency": p.get("currency")}
    con.execute(f"INSERT INTO {SCHEMA}.intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [iid, p["proposal_id"], ctx.now, ctx.worker, p["channel"], p["entity_key"], p["action"], json.dumps(req),
                 json.dumps(pre, default=str) if pre is not None else None, "pending", ctx.now, None])
    back = con.execute(f"SELECT state FROM {SCHEMA}.intents WHERE intent_id = ?", [iid]).fetchone()
    if not back or back[0] != "pending":
        raise state.StorageError("write-ahead intent wasn't persisted; nothing sent")
    return iid


def _intent_set(con, iid, st, error=None):
    if iid:
        con.execute(f"UPDATE {SCHEMA}.intents SET state = ?, updated_at = ?, error = ? WHERE intent_id = ?",
                    [st, _now(), (error or None) and str(error)[:300], iid])


def _scope_ok(p: dict, ctx) -> Optional[str]:
    if p["channel"] not in MANAGED:
        return f"{p['channel']} isn't a managed channel"
    if p.get("app") not in ctx.apps:
        return f"{p.get('app')} isn't a managed app"
    camp = str(p.get("campaign_id") or "")
    m = (ctx.cmap or {}).get((p["channel"], camp))
    if m == ingest.IGNORED:
        return "This campaign is now marked 'not ours' (ignored)"
    if m and m != p.get("app"):
        return f"This campaign is now mapped to {m}"
    return None


def _headroom(con, ctx, app: str) -> Optional[float]:
    allow = ((ctx.pace.get("apps") or {}).get(app) or {}).get("daily_allowance")
    if allow is None:
        return None
    con.execute(f"UPDATE {SCHEMA}.budget_reservations SET state = 'released' WHERE state = 'held' AND created_at < ?",
                [ctx.now - dt.timedelta(minutes=ctx.lease_ttl)])          # a crashed job's holds don't block forever
    held = con.execute(f"SELECT coalesce(sum(amount), 0) FROM {SCHEMA}.budget_reservations WHERE app = ? AND day = ? "
                       "AND (state = 'held' OR (state = 'consumed' AND created_at >= ?))",
                       [app, ctx.now.date(), ctx.budgets_read_at]).fetchone()[0]
    return float(allow) - sum((ctx.current_budgets.get(app) or {}).values()) - float(held or 0)


def preflight(p: dict, ctx, *, final: bool = False) -> tuple[list[str], bool]:
    """(blockers, invalidate_approval). Runs against FRESH shared state right before a write.
    `final` = the last check immediately before the request is sent (stop flag + lease only)."""
    con, now = ctx.con, _now() if final else ctx.now
    b: list[str] = []
    stop = state.automation_stopped(con)
    if stop:
        since = _ts(stop.get("requested_at") or stop.get("since"))
        if not (p["kind"] == "emergency" and since and _ts(p["created_at"]) > since):
            return ["Automation was stopped" + (f" at {since:%H:%M} UTC" if since else "")], True
    if ctx.lease and not state.renew(con, ctx.lease[0], ctx.lease[1], ctx.lease_ttl):
        raise state.LeaseLost(f"lease '{ctx.lease[0]}' lost; no further writes from this run")
    if final:
        return b, False
    if p.get("approved_hash") != proposal_hash(p) or p.get("proposal_hash") != p.get("approved_hash"):
        return ["What was approved no longer matches this proposal (values, evidence, campaign or policy changed)"], True
    exp = _ts(p.get("approval_expires_at"))
    if not exp or exp < now:
        return [f"The approval expired (approvals last {getattr(ctx.rules, 'approval_ttl_hours', 24):g} h)"], True
    if p["kind"] in ("optimization", "safeguard") and p.get("policy_version") != ctx.policy_version:
        return ["The rules changed after it was approved"], True
    sc = _scope_ok(p, ctx)
    if sc:
        return [sc], True
    if p["kind"] in ("optimization", "safeguard") and ctx.mode not in ("approval", "bounded_auto"):
        return [f"Mode is {ctx.mode}: {p['kind']} changes aren't applied"], False
    if p["kind"] == "optimization":
        st = ctx.statuses.get(p["channel"], {})
        if st.get("state") not in status.USABLE:
            b.append(f"{p['channel']} data is {st.get('label', 'unknown').lower()}")
        if ctx.live_fps is not None and p["fingerprint"] not in ctx.live_fps:
            return ["The rule no longer fires on the latest data"], True
    if p["action"] == "set_budget" and ctx.emergency and _f(p.get("proposed_value")) > _f(p.get("current_value")):
        b.append("An emergency pause is active: no budget increases")
    if not supports(p["channel"], p.get("entity_type"), p["action"]):
        b.append(f"{p['channel']} {p.get('entity_type')} doesn't support {p['action']}")
    return b, False


def execute(con, p: dict, adapters: dict = None, dry: bool = True, now: dt.datetime = None, from_status=("approved",), ctx=None) -> str:
    """Carry out one approved proposal. Returns the final proposal status."""
    if ctx is None:   # old callers / tests: a context with no extra checks beyond the approval itself
        ctx = ExecContext(con, _Rules(), adapters=adapters, dry=dry, now=now)
    now, dry, adapters = ctx.now, ctx.dry, ctx.adapters
    row = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE proposal_id = ?", [p["proposal_id"]]))
    if not row or row[0]["status"] not in from_status:
        return "skipped"
    p = row[0]
    blockers, invalid = preflight(p, ctx)
    if blockers:
        if invalid:
            _invalidate(con, p["proposal_id"], blockers[0], now, blockers[1:])
            _record(con, p, now, "dry_run" if dry else "live", "blocked", error="; ".join(blockers), worker=ctx.worker, dry=dry)
            return "blocked"
        con.execute(f"UPDATE {SCHEMA}.proposals SET outcome = ? WHERE proposal_id = ?", ["Waiting: " + "; ".join(blockers)[:480], p["proposal_id"]])
        return "approved"
    if not _claim(con, p["proposal_id"], from_status, ctx.worker, now):
        return "skipped"   # another worker has it, or it changed
    verb = {"pause_keyword": "pause keyword", "pause_ad_set": "pause ad set", "pause_campaign": "pause campaign",
            "set_budget": f"set daily budget to {_f(p.get('proposed_value')):,.2f} {p.get('currency') or ''}".strip()}.get(p["action"], p["action"])
    if dry:
        # DRY_RUN: no adapter is called at all, emergency pauses included
        _finish(con, p, "simulated", f"Dry run: would {verb}. Nothing was sent to {p['channel']}.", now, "dry_run", "simulated",
                dry=True, worker=ctx.worker)
        return "simulated"
    ad = adapters.get(p["channel"])
    if ad is None:
        con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'approved', outcome = ?, claimed_by = NULL WHERE proposal_id = ?",
                    [f"Waiting: no {p['channel']} credentials on this process", p["proposal_id"]])
        return "approved"
    pre = None
    try:
        pre = ad.read_state(p)
    except Exception as e:  # noqa: BLE001
        if not p["action"].startswith("pause_") or p["kind"] not in ("emergency", "safeguard"):
            _finish(con, p, "failed", f"Couldn't read the current state, so nothing was changed: {type(e).__name__}", now, "live",
                    "skipped", error=str(e), dry=False, worker=ctx.worker)
            return "failed"
    if pre is not None and desired_reached(p, pre):
        _finish(con, p, "already_done", "Already in the requested state; nothing sent", now, "live", "already_done", pre=pre, dry=False,
                worker=ctx.worker)
        return "already_done"
    if pre is not None and p["action"] == "set_budget" and str(pre.get("status") or "").upper() in ("PAUSED", "REMOVED", "DELETED", "ARCHIVED"):
        _finish(con, p, "expired", f"The campaign is {pre.get('status')} now; budget not changed", now, "live", "skipped", pre=pre,
                dry=False, worker=ctx.worker)
        return "expired"
    reservation = None
    if p["action"] == "set_budget":
        why = ad.budget_check(p, pre) if hasattr(ad, "budget_check") else ["budget writes not verified for this adapter"]
        if why:
            _finish(con, p, "blocked", "Budget write blocked: " + "; ".join(why), now, "live", "blocked", pre=pre, dry=False, worker=ctx.worker)
            return "blocked"
        if pre.get("daily_budget") is not None and p.get("current_value") is not None and abs(_f(pre["daily_budget"]) - _f(p["current_value"])) > 0.005:
            _invalidate(con, p["proposal_id"], f"the budget was changed to {_f(pre['daily_budget']):,.2f} outside AdBot after this was proposed", now)
            _record(con, p, now, "live", "blocked", pre=pre, error="current value changed", worker=ctx.worker, dry=False)
            return "blocked"
        delta = _f(p["proposed_value"]) - _f(pre["daily_budget"])
        if delta > 0:
            room = _headroom(con, ctx, p.get("app"))
            if room is None or delta > room + 0.005:
                _finish(con, p, "blocked", f"Budget not available: +{delta:,.2f} needed, {max(0.0, room or 0.0):,.2f} left in today's allowance",
                        now, "live", "blocked", pre=pre, dry=False, worker=ctx.worker)
                return "blocked"
            reservation = uuid.uuid4().hex
            con.execute(f"INSERT INTO {SCHEMA}.budget_reservations VALUES (?,?,?,?,?,?,?,?,?)",
                        [reservation, p.get("app"), now.date(), p.get("currency") or "", delta, p["proposal_id"], ctx.worker, now, "held"])
    iid = _intent(con, p, ctx, pre)              # write-ahead: persisted before anything is sent
    try:
        blockers, invalid = preflight(p, ctx, final=True)
    except state.LeaseLost:
        _intent_set(con, iid, "failed", "lease lost before sending; nothing sent")
        _finish(con, p, "failed", "Not sent: this run lost its job lease", now, "live", "skipped", pre=pre, dry=False, intent_id=iid, worker=ctx.worker)
        _release(con, reservation)
        raise
    if blockers:
        _intent_set(con, iid, "failed", "stopped before sending; nothing sent")
        _invalidate(con, p["proposal_id"], blockers[0], now)
        _record(con, p, now, "live", "blocked", pre=pre, error=blockers[0], intent_id=iid, worker=ctx.worker, dry=False)
        _release(con, reservation)
        return "blocked"
    try:
        ad.apply({**p, "_pre": pre})
    except AmbiguousResult as e:
        _intent_set(con, iid, "uncertain", str(e))
        _finish(con, p, "uncertain", "The platform didn't confirm the change; it will be checked (not re-sent)", now, "live",
                "uncertain", pre=pre, error=str(e), dry=False, intent_id=iid, worker=ctx.worker)
        return "uncertain"
    except UnsupportedAction as e:
        _intent_set(con, iid, "failed", str(e))
        _finish(con, p, "failed", f"Not supported: {e}", now, "live", "failed", pre=pre, error=str(e), dry=False, intent_id=iid, worker=ctx.worker)
        _release(con, reservation)
        return "failed"
    except Exception as e:  # noqa: BLE001
        _intent_set(con, iid, "failed", f"{type(e).__name__}: {e}")
        _finish(con, p, "failed", f"{p['channel']} refused the change", now, "live", "failed", pre=pre, error=f"{type(e).__name__}: {e}",
                dry=False, intent_id=iid, worker=ctx.worker)
        _release(con, reservation)
        return "failed"
    _intent_set(con, iid, "accepted")
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'applied', outcome = ?, outcome_at = ? WHERE proposal_id = ?",
                [f"Sent: {verb}; the platform accepted it, waiting for confirmation", now, p["proposal_id"]])
    _record(con, p, now, "live", "accepted", pre=pre, dry=False, intent_id=iid, worker=ctx.worker)
    post = None
    try:
        post = ad.read_state(p)
    except Exception:  # noqa: BLE001
        post = None
    if post is not None and desired_reached(p, post):
        _intent_set(con, iid, "verified")
        _finish(con, p, "verified", f"Done: {verb} (confirmed by a re-read)", now, "live", "verified", pre=pre, post=post, dry=False,
                intent_id=iid, worker=ctx.worker)
        _consume(con, reservation)
        return "verified"
    return "applied"   # accepted, not visible yet: reconcile() confirms or fails it later; never re-sent


def _release(con, rid):
    if rid:
        con.execute(f"UPDATE {SCHEMA}.budget_reservations SET state = 'released' WHERE reservation_id = ?", [rid])


def _consume(con, rid):
    if rid:
        con.execute(f"UPDATE {SCHEMA}.budget_reservations SET state = 'consumed' WHERE reservation_id = ?", [rid])


def _consume_for(con, pid, ok: bool):
    con.execute(f"UPDATE {SCHEMA}.budget_reservations SET state = ? WHERE proposal_id = ? AND state = 'held'",
                ["consumed" if ok else "released", pid])


class _Rules:
    approval_ttl_hours = 24

    def __getattr__(self, k):
        return None


def reconcile(con, adapters: dict, now: dt.datetime, ctx=None) -> list[dict]:
    """Settle 'uncertain', 'applied' (accepted, unconfirmed) and orphaned 'executing' proposals by READING
    the remote state. Nothing is ever re-sent from here; a change that didn't happen needs a new proposal."""
    out = []
    worker = ctx.worker if ctx else "reconcile"
    ttl = dt.timedelta(minutes=ctx.lease_ttl if ctx else 55)
    for p in _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE status IN ('uncertain', 'applied', 'executing')")):
        if p["status"] == "executing":
            claimed = _ts(p.get("claimed_at"))
            holder = state.lock_holder(con, "hourly")
            if p.get("claimed_by") == worker or (claimed and now - claimed < ttl and holder and holder["owner"] == p.get("claimed_by")):
                continue    # still being worked on by a live run
            it = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.intents WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1", [p["proposal_id"]]))
            if not it:      # crashed after claiming, before writing the intent: nothing was sent
                _invalidate(con, p["proposal_id"], "the run that took it stopped before sending anything", now)
                _record(con, p, now, "live", "blocked", error="interrupted before sending", worker=worker, dry=False)
                out.append({"proposal_id": p["proposal_id"], "result": "interrupted_not_sent"})
                continue
            if it[0]["state"] == "failed":
                _finish(con, p, "failed", "Interrupted; the intent shows nothing was sent", now, "live", "failed", worker=worker, dry=False)
                continue
        ad = adapters.get(p["channel"])
        if ad is None:
            continue
        try:
            stt = ad.read_state(p)
        except Exception:  # noqa: BLE001
            if p["status"] == "executing":
                con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'uncertain', outcome = ? WHERE proposal_id = ?",
                            ["Interrupted while sending; the platform couldn't be read yet", p["proposal_id"]])
            continue
        it = _dicts(con.execute(f"SELECT intent_id, created_at FROM {SCHEMA}.intents WHERE proposal_id = ? ORDER BY created_at DESC LIMIT 1",
                                [p["proposal_id"]]))
        iid = it[0]["intent_id"] if it else None
        if desired_reached(p, stt):
            _intent_set(con, iid, "verified")
            _finish(con, p, "verified", "Confirmed on a later check", now, "live", "reconciled_verified", post=stt, dry=False,
                    intent_id=iid, worker=worker)
            _consume_for(con, p["proposal_id"], True)
            out.append({"proposal_id": p["proposal_id"], "result": "verified"})
        elif p["status"] == "applied" and it and now - _ts(it[0]["created_at"]) < VERIFY_GRACE:
            continue    # accepted recently; give the platform time to show it
        else:
            _intent_set(con, iid, "not_applied")
            _finish(con, p, "failed", "A later check shows the change did not take effect; not re-sent (a new proposal may follow)",
                    now, "live", "reconciled_not_applied", post=stt, dry=False, intent_id=iid, worker=worker)
            _consume_for(con, p["proposal_id"], False)
            out.append({"proposal_id": p["proposal_id"], "result": "not_applied"})
    return out


# --------------------------------------------------------------------------- #
# Human decisions, stop / resume and emergency controls
# --------------------------------------------------------------------------- #
def decide(con, proposal_id: str, approve: bool, by: str, dry: bool, now=None, expected_hash: Optional[str] = None,
           ttl_hours: float = 24.0) -> dict:
    now = now or _now()
    rows_ = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE proposal_id = ?", [proposal_id]))
    if not rows_:
        raise ValueError("No such proposal")
    r = rows_[0]
    if approve:
        blk = _blk(r)
        reapprovable = r["status"] == "blocked" and r.get("reapproval_required") and all(b.startswith(REAPPROVAL) for b in blk)
        if r["status"] != "proposed" and not reapprovable:
            raise ValueError(f"This proposal is {r['status']}, so it can't be approved")
        if r["kind"] == "optimization" and state.automation_stopped(con):
            raise ValueError("Automation is stopped; resume it before approving changes")
        if not expected_hash or expected_hash != r.get("proposal_hash") or proposal_hash(r) != r.get("proposal_hash"):
            raise ValueError("This proposal changed since it was shown. Reload and review it again.")
        con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'approved', decided_by = ?, decided_at = ?, approved_hash = ?, "
                    "approval_expires_at = ?, reapproval_required = FALSE, blockers = '[]' "
                    "WHERE proposal_id = ? AND status = ? AND proposal_hash = ?",
                    [by, now, r["proposal_hash"], now + dt.timedelta(hours=float(ttl_hours or 24)), proposal_id, r["status"], r["proposal_hash"]])
        new = "approved"
    else:
        if r["status"] not in ("proposed", "blocked", "approved"):
            raise ValueError(f"This proposal is {r['status']}, so it can't be rejected")
        con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'rejected', decided_by = ?, decided_at = ?, approved_hash = NULL "
                    "WHERE proposal_id = ? AND status = ?", [by, now, proposal_id, r["status"]])
        new = "rejected"
    back = con.execute(f"SELECT status FROM {SCHEMA}.proposals WHERE proposal_id = ?", [proposal_id]).fetchone()
    if not back or back[0] != new:
        raise ValueError("It changed while you were deciding. Reload and try again.")
    _audit(con, by, f"proposal.{new}", r["entity_key"], {"proposal_id": proposal_id, "action": r["action"], "hash": r.get("proposal_hash")}, dry)
    return {"proposal_id": proposal_id, "status": new}


def stop(con, by: str, reason: str, dry: bool = True) -> dict:
    """Stop automation. Every approval still waiting is withdrawn: after Resume each needs a new approval."""
    v = state.stop_automation(con, by, reason)
    n = _withdraw_approvals(con, "automation was stopped", _now())
    _audit(con, by, "automation.stopped", None, {**v, "approvals_withdrawn": n}, dry)
    return {**v, "approvals_withdrawn": n}


def resume(con, by: str, dry: bool = True) -> dict:
    """Clear the stop latch. Resumes NO campaign and runs NO old approval: anything approved before is blocked
    until a person approves it again."""
    state.reset_automation_stop(con, by)
    n = _withdraw_approvals(con, "approved before automation was resumed", _now())
    _audit(con, by, "automation.resumed", None, {"approvals_withdrawn": n, "note": "nothing resumed; old approvals need re-approval"}, dry)
    return {"resumed": True, "approvals_withdrawn": n}


def _withdraw_approvals(con, why: str, now) -> int:
    ids = [r[0] for r in con.execute(f"SELECT proposal_id FROM {SCHEMA}.proposals WHERE status = 'approved'").fetchall()]
    for pid in ids:
        _invalidate(con, pid, why, now)
    return len(ids)


def request_emergency_pause(con, rules, by: str, apps: list[str], app: Optional[str] = None, channel: Optional[str] = None,
                            today: Optional[dt.date] = None, dataset="ads", table="ad_performance", dry=True, rule_app_fn=None) -> dict:
    """Record an explicit emergency pause for every active managed campaign / ad set in scope. Nothing is sent from
    here: the hourly worker (which has the platform credentials) carries the pauses out on its next run, and only
    when DRY_RUN is off. Scope is strictly the managed apps' mapped campaigns."""
    if app is not None and app not in apps:
        raise ValueError(f"Unknown app: {app}")
    if channel is not None and channel not in MANAGED:
        raise ValueError(f"Unknown or unmanaged channel: {channel}")
    today = today or _now().date()
    now = _now()
    rid = uuid.uuid4().hex[:12]
    rows = load_rows(con, today - dt.timedelta(days=30), today, dataset, table)
    cmap = ingest.mapping(con)
    agg = aggregate(rows, rules, cmap, today, apps, rule_app_fn or (lambda n: None))
    pv = policy_version(rules)
    made = []
    for be in agg["budget_entities"].values():
        ch = be["entity_key"].split("|")[0]
        if (app and be["app"] != app) or (channel and ch != channel):
            continue
        if str(be.get("status") or "").upper() in ("PAUSED", "REMOVED", "DELETED", "ARCHIVED"):
            continue
        act = pause_action(ch, be["entity_type"])
        if not act:
            continue
        p = _proposal("emergency", {**be, "channel": ch}, act, f"Emergency pause requested by {by}", "emergency_pause",
                      {"spend_7d": round(be["spend_7d"], 2)}, sufficiency="Explicit emergency instruction",
                      expected="Stops delivery of this campaign once the worker sends it; reported spend may still arrive")
        p["fingerprint"] = fingerprint(p) + rid
        _prepare(p, rules, pv)
        row = {**p, "proposal_id": uuid.uuid4().hex, "created_at": now, "run_id": "emergency", "status": "approved",
               "metrics": p["metrics_json"], "blockers": "[]", "decided_by": by, "decided_at": now, "request_id": rid,
               "approved_hash": p["proposal_hash"], "approval_expires_at": now + dt.timedelta(hours=24)}
        _insert(con, row)
        made.append(row["proposal_id"])
    latch = {"active": True, "request_id": rid, "since": now.isoformat(), "requested_at": now.isoformat(), "by": by,
             "scope": {"app": app, "channel": channel}, "count": len(made), "dry_run_at_request": dry,
             "persisted_at": None, "acknowledged_at": None}
    state.put(con, "emergency_pause", latch, by)
    back = state.get(con, "emergency_pause")
    if not (isinstance(back, dict) and back.get("request_id") == rid):
        raise state.StorageError("The emergency request was not confirmed by the database")
    back["persisted_at"] = _now().isoformat()
    state.put(con, "emergency_pause", back, by)
    _audit(con, by, "emergency.pause_requested", app or channel or "all", {"proposals": len(made), "scope": latch["scope"], "request_id": rid}, dry)
    return {"requested": len(made), "proposal_ids": made, "latch": back, "status": emergency_status(con)}


def emergency_active(con) -> Optional[dict]:
    v = state.get(con, "emergency_pause")
    return v if isinstance(v, dict) and v.get("active") else None


def emergency_status(con) -> Optional[dict]:
    """Stages of the latest emergency request: requested → persisted → worker acknowledged → per campaign
    pause requested remotely → verified / failed (or simulated under DRY_RUN)."""
    v = state.get(con, "emergency_pause")
    if not isinstance(v, dict) or not v.get("request_id"):
        return None
    counts = {r[0]: int(r[1]) for r in con.execute(
        f"SELECT status, count(*) FROM {SCHEMA}.proposals WHERE request_id = ? GROUP BY 1", [v["request_id"]]).fetchall()}
    sent = con.execute(f"SELECT count(*) FROM {SCHEMA}.intents i JOIN {SCHEMA}.proposals p USING (proposal_id) WHERE p.request_id = ?",
                       [v["request_id"]]).fetchone()[0]
    return {"request_id": v["request_id"], "active": bool(v.get("active")), "requested_at": v.get("requested_at"),
            "persisted_at": v.get("persisted_at"), "acknowledged_at": v.get("acknowledged_at"), "acknowledged_by": v.get("acknowledged_by"),
            "campaigns": int(v.get("count") or 0), "waiting": counts.get("approved", 0) + counts.get("executing", 0),
            "remote_requests_sent": int(sent or 0), "accepted_unconfirmed": counts.get("applied", 0), "verified": counts.get("verified", 0),
            "already_paused": counts.get("already_done", 0), "failed": counts.get("failed", 0), "uncertain": counts.get("uncertain", 0),
            "simulated_dry_run": counts.get("simulated", 0), "blocked": counts.get("blocked", 0) + counts.get("expired", 0)}


def _ack_emergency(con, worker: str) -> None:
    v = state.get(con, "emergency_pause")
    if isinstance(v, dict) and v.get("active") and not v.get("acknowledged_at"):
        v["acknowledged_at"], v["acknowledged_by"] = _now().isoformat(), worker
        state.put(con, "emergency_pause", v, worker)


def clear_emergency(con, by: str, dry: bool = True) -> None:
    """Clears the latch only. Nothing paused is resumed: that stays a manual step in each platform. Pauses from
    this request that haven't been sent yet are withdrawn."""
    v = state.get(con, "emergency_pause") or {}
    rid = v.get("request_id") if isinstance(v, dict) else None
    n = 0
    if rid:
        ids = [r[0] for r in con.execute(f"SELECT proposal_id FROM {SCHEMA}.proposals WHERE request_id = ? AND status = 'approved'", [rid]).fetchall()]
        for pid in ids:
            _invalidate(con, pid, "the emergency pause was cleared before it was sent", _now())
        n = len(ids)
    state.put(con, "emergency_pause", {**(v if isinstance(v, dict) else {}), "active": False, "cleared_at": _now().isoformat(), "cleared_by": by}, by)
    _audit(con, by, "emergency.cleared", None, {"note": "nothing resumed automatically", "unsent_withdrawn": n}, dry)


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def run_cycle(con, rules, *, apps: list[str], rule_app_fn, adapters: Optional[dict] = None, alerter=None,
              environ=None, now: Optional[dt.datetime] = None, dataset="ads", table="ad_performance",
              lease: Optional[tuple] = None, worker: Optional[str] = None, storage: str = "unknown",
              execute_changes: bool = True) -> dict:
    """One decision cycle. With execute_changes=False (the web service's preview) nothing is executed or
    reconciled, and the stop / emergency acknowledgements and worker heartbeat are left to the real worker."""
    now = now or _now()
    today = now.date()
    run_id = uuid.uuid4().hex[:12]
    worker = worker or (lease[1] if lease else f"cycle:{run_id}")
    adapters = adapters or {}
    dry = state.dry_run(environ)
    mode = state.mode(con)
    if execute_changes:
        stopped = state.acknowledge_stop(con, worker)        # stage 3: the worker has seen the stop
        _ack_emergency(con, worker)
        state.worker_heartbeat(con, worker, storage)
    else:
        stopped = state.automation_stopped(con)
    emergency = emergency_active(con)
    statuses = source_statuses(con, rules, now)
    alerter = alerter or alerts_mod.Alerter(con, alerts_mod.transport_from_env(rules, environ), rules)
    summary = {"run_id": run_id, "mode": mode, "dry_run": dry, "automation_stopped": bool(stopped),
               "emergency_active": bool(emergency), "proposals": {}, "executions": {}, "notes": []}
    cmap = ingest.mapping(con)
    base_ctx = ExecContext(con, rules, adapters=adapters, dry=dry, now=now, worker=worker, lease=lease, apps=apps, cmap=cmap,
                           statuses=statuses, emergency=emergency, rule_app_fn=rule_app_fn, mode=mode)
    summary["reconciled"] = reconcile(con, adapters, now, base_ctx) if execute_changes else []

    max_lag = max([maturity(rules, c) for c in MANAGED] + [1])
    start = min(today.replace(day=1), today - dt.timedelta(days=int(rules.lookback_days) + max_lag + 7))
    rows = load_rows(con, start, today, dataset, table)
    agg = aggregate(rows, rules, cmap, today, apps, rule_app_fn)
    pace = budget_mod.pacing(rules, agg["mtd_app"], agg["mtd_channel"], today, apps, agg["unattributed_mtd"])
    summary["pacing"] = pace
    summary["budget_errors"] = budget_mod.validate(rules, apps, MANAGED)

    cands: list[dict] = []
    current_budgets: dict = {}
    budgets_read_at = _now()
    if mode != "observe":
        perf, notes = performance_proposals(agg, rules, statuses)
        cands += perf
        summary["notes"] += notes
        bprops, bnotes = budget_proposals(agg, rules, pace, adapters, apps, bool(stopped), bool(emergency), current_out=current_budgets)
        cands += bprops
        summary["notes"] += [{"note": n} for n in bnotes]
    else:   # manual overrides are still honoured in Observe
        perf, _ = performance_proposals(agg, rules, statuses)
        cands += [p for p in perf if p["kind"] == "manual"]
    sprops, warns = safeguard_proposals(agg, rules, pace, apps)
    cands += sprops

    hist = _history(con, now - dt.timedelta(hours=max(48, float(rules.cooldown_hours) + 24)))
    open_unc = {r[0] for r in con.execute(f"SELECT entity_key FROM {SCHEMA}.proposals WHERE status IN ('uncertain', 'applied', 'executing')").fetchall()}
    for c in cands:
        validate(c, rules, mode=mode, stopped=bool(stopped), statuses=statuses, history=hist, now=now,
                 open_uncertain=open_unc, emergency=bool(emergency))
    stored = store(con, cands, run_id, now, rules, dry)
    for s in stored:
        summary["proposals"][s["status"]] = summary["proposals"].get(s["status"], 0) + 1

    # bounded-automatic approvals are bound exactly like a person's
    if mode == "bounded_auto" and not stopped and execute_changes:
        ttl = dt.timedelta(hours=float(getattr(rules, "approval_ttl_hours", 24) or 24))
        for s in stored:
            c = s["_cand"]
            if s["status"] == "proposed" and auto_allowed(c, rules) and not s.get("reapproval_required"):
                con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'approved', decided_by = 'policy: bounded automatic', decided_at = ?, "
                            "approved_hash = proposal_hash, approval_expires_at = ? WHERE proposal_id = ? AND status = 'proposed'",
                            [now, now + ttl, s["proposal_id"]])
                _audit(con, "system", "proposal.auto_approved", c["entity_key"], {"action": c["action"]}, dry)

    ctx = ExecContext(con, rules, adapters=adapters, dry=dry, now=now, worker=worker, lease=lease, apps=apps, cmap=cmap,
                      statuses=statuses, pace=pace, current_budgets=current_budgets, budgets_read_at=budgets_read_at,
                      emergency=emergency, live_fps={c["fingerprint"] for c in cands}, rule_app_fn=rule_app_fn, mode=mode)
    to_run = _dicts(con.execute(f"SELECT proposal_id, kind FROM {SCHEMA}.proposals WHERE status = 'approved' "
                                "ORDER BY CASE kind WHEN 'emergency' THEN 0 WHEN 'safeguard' THEN 1 WHEN 'manual' THEN 2 ELSE 3 END, created_at")) \
        if execute_changes else []
    summary["lease_lost"] = False
    for p in to_run:
        try:
            res = execute(con, p, ctx=ctx)
        except state.LeaseLost as e:
            summary["lease_lost"] = True
            summary["notes"].append({"note": str(e)})
            alerter.send("execution_failure", "critical", f"lease:{run_id}", "AdBot run lost its job lock", "No further changes were sent by this run.")
            break
        summary["executions"][res] = summary["executions"].get(res, 0) + 1

    # alerts
    for ch, st in statuses.items():
        sev = status.ATTENTION.get(st["state"])
        if sev:
            alerter.send("import_failure" if st["state"] == "failed" else "stale_data", sev, f"source:{ch}:{st['state']}",
                         f"{ch}: {st['label']}", st.get("detail") or "")
    um = ingest.unmapped(con)
    if um:
        alerter.send("unmapped", "warning", f"unmapped:{len(um)}", f"{len(um)} campaign(s) not mapped to an app",
                     "They're excluded from decisions until you map or ignore them on the Ads tab.")
    for w in warns:
        alerter.send("budget", w["severity"], w["key"] + ":" + pace["month"], "Budget", w["text"])
    if emergency:
        es = emergency_status(con) or {}
        alerter.send("emergency", "critical", f"emergency:{emergency.get('request_id') or emergency.get('since')}:{es.get('verified')}:{es.get('simulated_dry_run')}",
                     "Emergency pause " + ("simulated (dry run)" if dry else "in progress"),
                     f"Requested by {emergency.get('by')}. Verified paused: {es.get('verified', 0)}, failed: {es.get('failed', 0)}, "
                     f"unconfirmed: {es.get('uncertain', 0) + es.get('accepted_unconfirmed', 0)}, simulated: {es.get('simulated_dry_run', 0)}. "
                     "Nothing is resumed automatically.")
    if stopped:
        alerter.send("emergency", "warning", f"stopped:{stopped.get('request_id') or stopped.get('since')}", "Automation is stopped",
                     f"{stopped.get('reason') or ''} Seen by the worker; no advertising writes are sent while stopped. Ads keep running.")
    fails = summary["executions"].get("failed", 0) + summary["executions"].get("uncertain", 0)
    if fails:
        alerter.send("execution_failure", "critical", f"exec:{run_id}", f"{fails} change(s) failed or unconfirmed", "See the Log tab.")
    new_props = [s for s in stored if s["status"] in ("proposed", "approved") and s.get("run_id") == run_id]
    if new_props:
        lines = "\n".join(f"• {s['_cand']['entity_name']} ({s['channel']}): {s['_cand']['reason']}" for s in new_props[:15])
        alerter.send("proposals", "info", f"proposals:{run_id}", f"{len(new_props)} new proposal(s)" + (" · dry run" if dry else ""), lines)
    ex = summary["executions"]
    if ex.get("verified") or ex.get("applied") or ex.get("simulated"):
        alerter.send("applied", "info", f"applied:{run_id}",
                     f"{ex.get('verified', 0)} verified, {ex.get('applied', 0)} sent awaiting confirmation, {ex.get('simulated', 0)} simulated",
                     "See the Log tab.")
    if now.hour < 14:
        tot = pace.get("total") or {}
        alerter.send("daily_summary", "info", f"daily:{today.isoformat()}", f"Daily summary ({mode}{', dry run' if dry else ''})",
                     f"Month to date: {sum(agg['mtd_app'].values()):,.2f} {rules.budget_currency} spend across managed apps"
                     + (f" of {tot.get('monthly'):,.2f}" if tot else " (no monthly budget set)")
                     + f". Open proposals: {sum(v for k, v in summary['proposals'].items() if k in ('proposed', 'blocked', 'approved'))}. "
                     + "Revenue: shown only where a platform or store reported it, never added across platforms.")
    state.put(con, "last_cycle", {"at": now.isoformat(), "run_id": run_id, "mode": mode, "dry_run": dry, "worker": worker,
                                  "proposals": summary["proposals"], "executions": summary["executions"]})
    _audit(con, "scheduler", "engine.cycle", None, {k: summary[k] for k in ("run_id", "mode", "dry_run", "proposals", "executions")}, dry)
    return summary
