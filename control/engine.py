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
  bounded-automatic mode may apply on its own. DRY_RUN=true turns every execution into a simulation.
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

RULE_VERSION = "2026-10-26.1"
OPEN = ("proposed", "blocked", "approved", "executing", "uncertain")
CHANGE_RESULTS = ("applied", "simulated", "uncertain", "reconciled_applied")
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


def _audit(con, actor, action, target, detail, dry):
    try:
        from revenue import registry
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
        if r is None:
            out[ch] = {"source": ch, **status.classify(configured=False, supported=info["supported"], now=now)}
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
    order = "_dlt_load_id" if "_dlt_load_id" in cols else ("import_id" if "import_id" in cols else "date")
    sel = ", ".join(c for c in cols if c not in ("_dlt_id",))
    sql = (f"SELECT * FROM (SELECT {sel}, row_number() OVER (PARTITION BY channel, entity_id, CAST(date AS VARCHAR) "
           f"ORDER BY {order} DESC) AS rn FROM {dataset}.{table}) WHERE rn = 1 AND CAST(date AS VARCHAR) BETWEEN ? AND ?")
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


def budget_proposals(agg: dict, rules, pace: dict, adapters: dict, apps: list[str], stopped: bool, emergency: bool) -> tuple[list[dict], list[str]]:
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
        f"SELECT e.attempted_at, e.result, p.status AS pstatus, p.entity_key, p.campaign_id, p.account_id, p.action, p.current_value, p.proposed_value, p.kind "
        f"FROM {SCHEMA}.executions e JOIN {SCHEMA}.proposals p USING (proposal_id) WHERE e.attempted_at > ?", [since]))


def validate(p: dict, rules, *, mode: str, stopped: bool, statuses: dict, history: list[dict], now: dt.datetime,
             open_uncertain: set, emergency: bool) -> None:
    """Attach blockers (plain language) and next_eligible_at. Deterministic; runs on the server only."""
    b = p["blockers"]
    kind = p["kind"]
    if mode == "observe" and kind in ("optimization",):
        b.append("Operating mode is Observe")
    if stopped and kind == "optimization":
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
    history = [h for h in history if h["result"] in CHANGE_RESULTS and h["pstatus"] in ("applied", "simulated", "uncertain", "executing")]
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
# Persistence of proposals (deduplicated)
# --------------------------------------------------------------------------- #
INSERT_COLS = ["proposal_id", "fingerprint", "kind", "created_at", "run_id", "app", "channel", "account_id", "campaign_id", "entity_key",
               "entity_type", "entity_name", "action", "current_value", "proposed_value", "metrics", "window_start", "window_end",
               "freshness", "sufficiency", "rule_id", "rule_version", "reason", "expected_effect", "blockers", "next_eligible_at",
               "status", "decided_by", "decided_at", "outcome", "outcome_at"]


def _insert(con, row: dict) -> None:
    con.execute(f"INSERT INTO {SCHEMA}.proposals ({', '.join(INSERT_COLS)}) VALUES ({', '.join('?' * len(INSERT_COLS))})",
                [row.get(c) for c in INSERT_COLS])


def store(con, cands: list[dict], run_id: str, now: dt.datetime, rules, dry: bool) -> list[dict]:
    """Insert new proposals, refresh identical open ones, supersede changed ones, expire ones no longer produced.
    Respects recent human rejections and recent identical executions (no daily repeats)."""
    open_rows = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE status IN ({', '.join('?' * len(OPEN))})", list(OPEN)))
    by_key = {(r["entity_key"], r["action"]): r for r in open_rows}
    recent = {r["fingerprint"]: r for r in _dicts(con.execute(
        f"SELECT fingerprint, status, outcome_at, decided_at FROM {SCHEMA}.proposals WHERE status IN ('rejected', 'simulated', 'applied', 'already_done') "
        f"AND coalesce(outcome_at, decided_at, created_at) > ?", [now - dt.timedelta(days=7)]))}
    seen, out = set(), []
    for c in cands:
        fp = fingerprint(c)
        c["fingerprint"] = fp
        key = (c["entity_key"], c["action"])
        seen.add(key)
        prev_closed = recent.get(fp)
        if prev_closed and prev_closed["status"] == "rejected":
            continue
        if prev_closed and prev_closed["status"] in ("simulated", "applied", "already_done") and c["kind"] not in ("emergency",):
            t = _ts(prev_closed["outcome_at"] or prev_closed["decided_at"])
            if t and now - t < dt.timedelta(hours=float(rules.cooldown_hours)):
                continue
        old = by_key.get(key)
        if old and old["kind"] in ("emergency", "manual", "safeguard") and c["kind"] == "optimization" and old["status"] in ("approved", "proposed", "executing", "uncertain"):
            continue   # a human / protective request for the same change already exists; don't replace it
        status_ = "blocked" if c["blockers"] else ("approved" if c["kind"] in ("manual", "emergency") else "proposed")
        if old and old["fingerprint"] == fp:
            if old["status"] in ("proposed", "blocked"):
                con.execute(f"UPDATE {SCHEMA}.proposals SET metrics = ?, blockers = ?, next_eligible_at = ?, status = ?, freshness = ?, "
                            "sufficiency = ?, reason = ? WHERE proposal_id = ?",
                            [json.dumps(c["metrics"], default=str), json.dumps(c["blockers"]), c.get("next_eligible_at"),
                             status_, c.get("freshness"), c.get("sufficiency"), c["reason"], old["proposal_id"]])
                old.update(status=status_, blockers=json.dumps(c["blockers"]))
            out.append({**old, "_cand": c})
            continue
        if old:
            if old["status"] in ("executing", "uncertain"):
                continue   # wait for that one to settle
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'superseded', outcome = 'replaced by a newer proposal', outcome_at = ? "
                        "WHERE proposal_id = ?", [now, old["proposal_id"]])
        row = {**c, "proposal_id": uuid.uuid4().hex, "created_at": now, "run_id": run_id, "status": status_,
               "metrics": json.dumps(c["metrics"], default=str), "blockers": json.dumps(c["blockers"]),
               "decided_by": "admin (override)" if c["kind"] == "manual" else None,
               "decided_at": now if c["kind"] == "manual" else None}
        _insert(con, row)
        _audit(con, "system", "proposal.created", c["entity_key"], {"action": c["action"], "kind": c["kind"], "rule": c["rule_id"],
                                                                   "status": status_, "blockers": c["blockers"]}, dry)
        out.append({**row, "_cand": c})
    for key, old in by_key.items():
        if key not in seen and old["status"] in ("proposed", "blocked") and old["kind"] in ("optimization", "safeguard"):
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'expired', outcome = 'conditions no longer met', outcome_at = ? "
                        "WHERE proposal_id = ?", [now, old["proposal_id"]])
        elif key not in seen and old["status"] == "approved" and old["kind"] == "optimization":
            con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'expired', outcome = 'evidence changed after approval; not applied', "
                        "outcome_at = ? WHERE proposal_id = ?", [now, old["proposal_id"]])
    return out


# --------------------------------------------------------------------------- #
# EXECUTION + VERIFICATION
# --------------------------------------------------------------------------- #
def _claim(con, pid: str, from_status: tuple) -> bool:
    token = "claim:" + uuid.uuid4().hex
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'executing', outcome = ? WHERE proposal_id = ? "
                f"AND status IN ({', '.join('?' * len(from_status))})", [token, pid, *from_status])
    r = con.execute(f"SELECT outcome FROM {SCHEMA}.proposals WHERE proposal_id = ?", [pid]).fetchone()
    return bool(r and r[0] == token)


def _finish(con, p: dict, status_: str, outcome: str, now, exec_mode: str, result: str, pre=None, post=None, error=None, dry=True):
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = ?, outcome = ?, outcome_at = ? WHERE proposal_id = ?",
                [status_, outcome[:500], now, p["proposal_id"]])
    con.execute(f"INSERT INTO {SCHEMA}.executions VALUES (?,?,?,?,?,?,?,?)",
                [uuid.uuid4().hex, p["proposal_id"], now, exec_mode, json.dumps(pre, default=str) if pre is not None else None,
                 result, json.dumps(post, default=str) if post is not None else None, (error or None) and str(error)[:500]])
    _audit(con, "system", f"execution.{result}", p["entity_key"], {"action": p["action"], "value": p.get("proposed_value"),
                                                                  "outcome": outcome[:200], "mode": exec_mode}, dry)


def execute(con, p: dict, adapters: dict, dry: bool, now: dt.datetime, from_status=("approved",)) -> str:
    """Carry out one proposal. Returns the final proposal status."""
    if not _claim(con, p["proposal_id"], from_status):
        return "skipped"   # another worker has it, or it changed
    verb = {"pause_keyword": "pause keyword", "pause_ad_set": "pause ad set", "pause_campaign": "pause campaign",
            "set_budget": f"set daily budget to ${_f(p.get('proposed_value')):,.2f}"}.get(p["action"], p["action"])
    if dry:
        _finish(con, p, "simulated", f"Dry run: would {verb}. Nothing was sent to {p['channel']}.", now, "dry_run", "simulated", dry=True)
        return "simulated"
    ad = adapters.get(p["channel"])
    if ad is None:
        con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'approved', outcome = ? WHERE proposal_id = ?",
                    [f"Waiting: no {p['channel']} credentials on this process", p["proposal_id"]])
        return "approved"
    pre = None
    try:
        pre = ad.read_state(p)
    except Exception as e:  # noqa: BLE001
        if not p["action"].startswith("pause_") or p["kind"] not in ("emergency", "safeguard"):
            _finish(con, p, "failed", f"Couldn't read the current state, so nothing was changed: {type(e).__name__}", now, "live",
                    "skipped", error=str(e), dry=False)
            return "failed"
    if pre is not None and desired_reached(p, pre):
        _finish(con, p, "already_done", "Already in the requested state; nothing sent", now, "live", "already_done", pre=pre, dry=False)
        return "already_done"
    if p["action"] == "set_budget" and pre and pre.get("daily_budget") is not None and p.get("current_value") is not None \
            and abs(_f(pre["daily_budget"]) - _f(p["current_value"])) > 0.01:
        _finish(con, p, "expired", f"The budget changed to ${_f(pre['daily_budget']):,.2f} since this was proposed; not applied",
                now, "live", "skipped", pre=pre, dry=False)
        return "expired"
    try:
        ad.apply(p)
    except AmbiguousResult as e:
        _finish(con, p, "uncertain", "The platform didn't confirm the change; it will be checked before any retry", now, "live",
                "uncertain", pre=pre, error=str(e), dry=False)
        return "uncertain"
    except UnsupportedAction as e:
        _finish(con, p, "failed", f"Not supported: {e}", now, "live", "failed", pre=pre, error=str(e), dry=False)
        return "failed"
    except Exception as e:  # noqa: BLE001
        _finish(con, p, "failed", f"{p['channel']} refused the change", now, "live", "failed", pre=pre, error=f"{type(e).__name__}: {e}", dry=False)
        return "failed"
    post = None
    try:
        post = ad.read_state(p)
    except Exception:  # noqa: BLE001
        post = None
    if post is not None and desired_reached(p, post):
        _finish(con, p, "applied", f"Done: {verb} (confirmed)", now, "live", "applied", pre=pre, post=post, dry=False)
        return "applied"
    _finish(con, p, "uncertain", "Sent, but the platform doesn't show the change yet; it will be checked next run", now, "live",
            "uncertain", pre=pre, post=post, dry=False)
    return "uncertain"


def reconcile(con, adapters: dict, now: dt.datetime) -> list[dict]:
    """Resolve uncertain executions by reading the remote state. Never re-sends on its own."""
    out = []
    for p in _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE status = 'uncertain'")):
        ad = adapters.get(p["channel"])
        if ad is None:
            continue
        try:
            stt = ad.read_state(p)
        except Exception:  # noqa: BLE001
            continue
        if desired_reached(p, stt):
            _finish(con, p, "applied", "Confirmed on a later check", now, "live", "reconciled_applied", post=stt, dry=False)
            out.append({"proposal_id": p["proposal_id"], "result": "applied"})
        else:
            _finish(con, p, "failed", "A later check shows the change did not take effect; a new proposal may follow", now, "live",
                    "reconciled_not_applied", post=stt, dry=False)
            out.append({"proposal_id": p["proposal_id"], "result": "not_applied"})
    return out


# --------------------------------------------------------------------------- #
# Human decisions and emergency controls
# --------------------------------------------------------------------------- #
def decide(con, proposal_id: str, approve: bool, by: str, dry: bool, now=None) -> dict:
    now = now or _now()
    r = con.execute(f"SELECT status, entity_key, action FROM {SCHEMA}.proposals WHERE proposal_id = ?", [proposal_id]).fetchone()
    if not r:
        raise ValueError("No such proposal")
    if r[0] != "proposed":
        raise ValueError(f"This proposal is {r[0]}, so it can't be {'approved' if approve else 'rejected'}")
    new = "approved" if approve else "rejected"
    con.execute(f"UPDATE {SCHEMA}.proposals SET status = ?, decided_by = ?, decided_at = ? WHERE proposal_id = ? AND status = 'proposed'",
                [new, by, now, proposal_id])
    _audit(con, "admin", f"proposal.{new}", r[1], {"proposal_id": proposal_id, "action": r[2]}, dry)
    return {"proposal_id": proposal_id, "status": new}


def request_emergency_pause(con, rules, by: str, apps: list[str], app: Optional[str] = None, channel: Optional[str] = None,
                            today: Optional[dt.date] = None, dataset="ads", table="ad_performance", dry=True, rule_app_fn=None) -> dict:
    """Separate, explicit operation: create an approved pause for every active managed campaign / ad set in scope.
    The hourly job (or this process, if it has the platform credentials) carries them out and reports per campaign."""
    today = today or _now().date()
    now = _now()
    rows = load_rows(con, today - dt.timedelta(days=30), today, dataset, table)
    agg = aggregate(rows, rules, ingest.mapping(con), today, apps, rule_app_fn or (lambda n: None))
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
                      expected="Stops delivery of this campaign; reported spend may still arrive for a few hours")
        p["fingerprint"] = fingerprint(p) + now.strftime("%Y%m%d%H%M")
        row = {**p, "proposal_id": uuid.uuid4().hex, "created_at": now, "run_id": "emergency", "status": "approved",
               "metrics": json.dumps(p["metrics"]), "blockers": "[]", "decided_by": by, "decided_at": now}
        _insert(con, row)
        made.append(row["proposal_id"])
    latch = {"active": True, "since": now.isoformat(), "by": by, "scope": {"app": app, "channel": channel}, "count": len(made)}
    state.put(con, "emergency_pause", latch, by)
    _audit(con, "admin", "emergency.pause_requested", app or channel or "all", {"proposals": len(made), "scope": latch["scope"]}, dry)
    return {"requested": len(made), "proposal_ids": made, "latch": latch}


def emergency_active(con) -> Optional[dict]:
    v = state.get(con, "emergency_pause")
    return v if isinstance(v, dict) and v.get("active") else None


def clear_emergency(con, by: str, dry: bool = True) -> None:
    """Clears the latch only. Nothing paused is resumed: that stays a manual step in each platform."""
    state.put(con, "emergency_pause", {"active": False, "cleared_at": _now().isoformat(), "by": by}, by)
    _audit(con, "admin", "emergency.cleared", None, {"note": "nothing resumed automatically"}, dry)


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #
def run_cycle(con, rules, *, apps: list[str], rule_app_fn, adapters: Optional[dict] = None, alerter=None,
              environ=None, now: Optional[dt.datetime] = None, dataset="ads", table="ad_performance") -> dict:
    now = now or _now()
    today = now.date()
    run_id = uuid.uuid4().hex[:12]
    adapters = adapters or {}
    dry = state.dry_run(environ)
    mode = state.mode(con)
    stopped = state.automation_stopped(con)
    emergency = emergency_active(con)
    statuses = source_statuses(con, rules, now)
    alerter = alerter or alerts_mod.Alerter(con, alerts_mod.transport_from_env(rules, environ), rules)
    summary = {"run_id": run_id, "mode": mode, "dry_run": dry, "automation_stopped": bool(stopped),
               "emergency_active": bool(emergency), "proposals": {}, "executions": {}, "notes": []}

    reconciled = reconcile(con, adapters, now) if not dry else []
    summary["reconciled"] = reconciled

    max_lag = max([maturity(rules, c) for c in MANAGED] + [1])
    start = min(today.replace(day=1), today - dt.timedelta(days=int(rules.lookback_days) + max_lag + 7))
    rows = load_rows(con, start, today, dataset, table)
    cmap = ingest.mapping(con)
    agg = aggregate(rows, rules, cmap, today, apps, rule_app_fn)
    pace = budget_mod.pacing(rules, agg["mtd_app"], agg["mtd_channel"], today, apps, agg["unattributed_mtd"])
    summary["pacing"] = pace
    summary["budget_errors"] = budget_mod.validate(rules, apps, MANAGED)

    cands: list[dict] = []
    if mode != "observe":
        perf, notes = performance_proposals(agg, rules, statuses)
        cands += perf
        summary["notes"] += notes
        bprops, bnotes = budget_proposals(agg, rules, pace, adapters, apps, bool(stopped), bool(emergency))
        cands += bprops
        summary["notes"] += [{"note": n} for n in bnotes]
    else:   # manual overrides are still honoured in Observe
        perf, _ = performance_proposals(agg, rules, statuses)
        cands += [p for p in perf if p["kind"] == "manual"]
    sprops, warns = safeguard_proposals(agg, rules, pace, apps)
    cands += sprops

    hist = _history(con, now - dt.timedelta(hours=max(48, float(rules.cooldown_hours) + 24)))
    open_unc = {r[0] for r in con.execute(f"SELECT entity_key FROM {SCHEMA}.proposals WHERE status = 'uncertain'").fetchall()}
    for c in cands:
        validate(c, rules, mode=mode, stopped=bool(stopped), statuses=statuses, history=hist, now=now,
                 open_uncertain=open_unc, emergency=bool(emergency))
    stored = store(con, cands, run_id, now, rules, dry)
    for s in stored:
        summary["proposals"][s["status"]] = summary["proposals"].get(s["status"], 0) + 1

    # execution: approved items, plus what bounded-automatic mode may do on its own
    to_run = _dicts(con.execute(f"SELECT * FROM {SCHEMA}.proposals WHERE status = 'approved' ORDER BY created_at"))
    if mode == "bounded_auto":
        for s in stored:
            c = s["_cand"]
            if s["status"] == "proposed" and auto_allowed(c, rules) and (c["kind"] == "safeguard" or not stopped):
                con.execute(f"UPDATE {SCHEMA}.proposals SET status = 'approved', decided_by = 'policy: bounded automatic', decided_at = ? "
                            "WHERE proposal_id = ? AND status = 'proposed'", [now, s["proposal_id"]])
                _audit(con, "system", "proposal.auto_approved", c["entity_key"], {"action": c["action"]}, dry)
                to_run.append({**s, "status": "approved"})
    done_ids = set()
    for p in to_run:
        if p["proposal_id"] in done_ids:
            continue
        done_ids.add(p["proposal_id"])
        if stopped and p["kind"] == "optimization":
            continue   # approved earlier, but automation is stopped: optimization writes wait
        if mode in ("observe", "recommend") and p["kind"] == "optimization":
            continue   # an approval given in an earlier mode doesn't apply until the mode allows it
        res = execute(con, p, adapters, dry, now)
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
        alerter.send("emergency", "critical", f"emergency:{emergency.get('since')}", "Emergency pause is active",
                     f"Requested by {emergency.get('by')}. Paused campaigns stay paused until you resume them in each platform.")
    if stopped:
        alerter.send("emergency", "warning", f"stopped:{stopped.get('since')}", "Automation is stopped",
                     f"{stopped.get('reason') or ''} Ads keep running until paused; only AdBot's optimization writes are stopped.")
    fails = summary["executions"].get("failed", 0) + summary["executions"].get("uncertain", 0)
    if fails:
        alerter.send("execution_failure", "critical", f"exec:{run_id}", f"{fails} change(s) failed or unconfirmed", "See the Log tab.")
    new_props = [s for s in stored if s["status"] in ("proposed", "approved") and s.get("run_id") == run_id]
    if new_props:
        lines = "\n".join(f"• {s['_cand']['entity_name']} ({s['channel']}): {s['_cand']['reason']}" for s in new_props[:15])
        alerter.send("proposals", "info", f"proposals:{run_id}", f"{len(new_props)} new proposal(s)" + (" · dry run" if dry else ""), lines)
    if summary["executions"].get("applied") or summary["executions"].get("simulated"):
        alerter.send("applied", "info", f"applied:{run_id}",
                     f"{summary['executions'].get('applied', 0)} applied, {summary['executions'].get('simulated', 0)} simulated", "See the Log tab.")
    if now.hour < 14:
        tot = pace.get("total") or {}
        alerter.send("daily_summary", "info", f"daily:{today.isoformat()}", f"Daily summary ({mode}{', dry run' if dry else ''})",
                     f"Month to date: ${sum(agg['mtd_app'].values()):,.2f} spend across managed apps"
                     + (f" of ${tot.get('monthly'):,.2f}" if tot else " (no monthly budget set)")
                     + f". Open proposals: {sum(v for k, v in summary['proposals'].items() if k in ('proposed', 'blocked', 'approved'))}. "
                     + "Revenue: shown only where a platform or store reported it.")
    state.put(con, "last_cycle", {"at": now.isoformat(), "run_id": run_id, "mode": mode, "dry_run": dry,
                                  "proposals": summary["proposals"], "executions": summary["executions"]})
    _audit(con, "scheduler", "engine.cycle", None, {k: summary[k] for k in ("run_id", "mode", "dry_run", "proposals", "executions")}, dry)
    return summary
