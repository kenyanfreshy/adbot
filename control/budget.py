"""
One budget policy across every managed channel.

  monthly_budget      explicit monthly total (no 30-day assumption: pacing uses the real calendar month)
  app_monthly         per-app monthly allocation; must add up to no more than the monthly total
  channel_monthly     optional per-channel monthly limits (across apps)
  campaign_daily_max  optional per-campaign / ad-set daily limit
  reserve_pct         held back because platforms report spend late

Daily pacing for an app = (allocation x (1 - reserve) - spend observed so far this month) / days left,
counting today. An app's allowance is never multiplied across platforms: it is ONE amount that all of
that app's campaigns on every channel share.

Limits of any budget control (shown in the app too): platforms report spend hours late, daily budgets
are targets the platforms may overshoot on a given day, and observed spend lags requested budgets.
So these are pacing targets and ceilings on what AdBot asks for, not a guaranteed real-time spend cap.
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Optional

LIMITS_NOTE = ("Platforms report spend late and may overspend a daily budget on a given day, so these are pacing targets "
               "and ceilings on the budgets AdBot sets, not a guaranteed real-time spend cap.")


def days_in_month(d: dt.date) -> int:
    return calendar.monthrange(d.year, d.month)[1]


def days_left(d: dt.date) -> int:
    """Calendar days left in the month, counting today."""
    return days_in_month(d) - d.day + 1


def validate(rules, apps: list[str], channels: list[str]) -> list[str]:
    """Problems that make the policy infeasible or unsafe. Empty list = usable."""
    errs = []
    mb = rules.monthly_budget
    if mb is None:
        return ["No monthly budget is set, so AdBot won't propose budget changes. Set one in Rules."]
    if mb <= 0:
        errs.append("The monthly budget must be more than $0.")
    alloc = {a: float(v) for a, v in (rules.app_monthly or {}).items() if v is not None}
    missing = [a for a in apps if a not in alloc]
    if missing:
        errs.append("Set a monthly allocation for " + ", ".join(missing) + " (use 0 to give an app nothing).")
    if any(v < 0 for v in alloc.values()):
        errs.append("App allocations can't be negative.")
    if alloc and sum(alloc.values()) > mb + 1e-9:
        errs.append(f"App allocations add up to ${sum(alloc.values()):,.2f}, more than the ${mb:,.2f} monthly budget.")
    ch = {c: float(v) for c, v in (rules.channel_monthly or {}).items() if v is not None}
    for c, v in ch.items():
        if c not in channels:
            errs.append(f"Unknown channel in channel limits: {c}.")
        if v < 0:
            errs.append(f"The {c} limit can't be negative.")
    if ch and alloc and sum(ch.get(c, mb) for c in channels) < sum(alloc.values()) - 1e-9 and len(ch) == len(channels):
        errs.append("Channel limits add up to less than the app allocations, so the allocations can't all be spent.")
    if not 0 <= float(rules.reserve_pct) < 0.5:
        errs.append("The reserve must be between 0% and 50%.")
    if rules.campaign_daily_max is not None and rules.campaign_daily_max < 1:
        errs.append("The per-campaign daily maximum must be at least $1 (platforms reject smaller budgets).")
    return errs


def pacing(rules, spend_mtd_by_app: dict, spend_mtd_by_channel: dict, today: dt.date, apps: list[str],
           unattributed_mtd: float = 0.0) -> dict:
    """Pacing for the current calendar month. Spend is OBSERVED spend from imports (it lags)."""
    dim, left = days_in_month(today), days_left(today)
    out = {"month": today.strftime("%Y-%m"), "days_in_month": dim, "days_left": left, "currency": rules.budget_currency,
           "reserve_pct": rules.reserve_pct, "note": LIMITS_NOTE, "apps": {}, "channels": {}, "total": None,
           "unattributed_spend_mtd": round(unattributed_mtd, 2)}
    if rules.monthly_budget is None:
        return out
    reserve = float(rules.reserve_pct)
    spent_total = sum(spend_mtd_by_app.get(a, 0.0) for a in apps) + unattributed_mtd
    total_usable = float(rules.monthly_budget) * (1 - reserve)
    out["total"] = {"monthly": float(rules.monthly_budget), "usable": round(total_usable, 2), "spent_mtd": round(spent_total, 2),
                    "remaining": round(total_usable - spent_total, 2),
                    "daily_allowance": round(max(0.0, total_usable - spent_total) / left, 2),
                    "used_pct": spent_total / float(rules.monthly_budget) if rules.monthly_budget else None}
    for a in apps:
        m = (rules.app_monthly or {}).get(a)
        sp = float(spend_mtd_by_app.get(a, 0.0))
        if m is None:
            out["apps"][a] = {"monthly": None, "spent_mtd": round(sp, 2), "daily_allowance": None,
                              "blocker": "No monthly allocation set"}
            continue
        usable = float(m) * (1 - reserve)
        out["apps"][a] = {"monthly": float(m), "usable": round(usable, 2), "spent_mtd": round(sp, 2),
                          "remaining": round(usable - sp, 2), "daily_allowance": round(max(0.0, usable - sp) / left, 2),
                          "used_pct": sp / float(m) if m else None}
    for c, lim in (rules.channel_monthly or {}).items():
        if lim is None:
            continue
        sp = float(spend_mtd_by_channel.get(c, 0.0))
        usable = float(lim) * (1 - reserve)
        out["channels"][c] = {"monthly": float(lim), "spent_mtd": round(sp, 2), "remaining": round(usable - sp, 2),
                              "daily_allowance": round(max(0.0, usable - sp) / left, 2), "used_pct": sp / float(lim) if lim else None}
    return out


def split_allowance(allowance: float, members: dict, campaign_daily_max: Optional[float]) -> dict:
    """Split one app's daily allowance across its budget entities by recent spend share (equal if none).
    Caps at campaign_daily_max; money a capped entity can't take is NOT pushed onto the others."""
    if not members:
        return {}
    tot = sum(members.values())
    out = {}
    for k, s in members.items():
        v = allowance * (s / tot if tot > 0 else 1 / len(members))
        if campaign_daily_max is not None:
            v = min(v, float(campaign_daily_max))
        out[k] = round(v, 2)
    return out


# --------------------------------------------------------------------------- #
# Rebalancing: only when it can be justified
# --------------------------------------------------------------------------- #
def rebalance(rules, per_app: dict, apps: list[str], source_ok: bool, mode: str, stopped: bool,
              last_eval_at=None, last_applied_at=None) -> dict:
    """Recommend new app allocations from comparable, mature, platform-attributed returns.

    per_app[app] = {spend, revenue (None = not reported), conversions, currency}
    Returns {configured, eligible, blockers, baseline, proposed, method}. Never applies anything itself."""
    out = {"configured": bool(rules.auto_rebalance), "eligible": False, "blockers": [], "baseline": {}, "proposed": {},
           "method": ("Return on ad spend from platform-attributed revenue, shrunk toward the portfolio average "
                      f"by ${float(rules.min_spend_for_rebalance):,.0f} of pseudo-spend; shares kept between "
                      f"{float(rules.exploration_share)*100:.0f}% and {float(rules.max_share)*100:.0f}%, "
                      f"each moving at most {float(rules.max_budget_step)*100:.0f}% per recommendation."),
           "last_evaluation": last_eval_at, "last_applied": last_applied_at}
    b = out["blockers"]
    if not rules.auto_rebalance:
        b.append("Turned off in Rules")
    if mode == "observe":
        b.append("Operating mode is Observe")
    if stopped:
        b.append("Automation is stopped")
    if rules.monthly_budget is None or any((rules.app_monthly or {}).get(a) is None for a in apps):
        b.append("Monthly budget and every app's allocation must be set")
    else:
        out["baseline"] = {a: float(rules.app_monthly[a]) for a in apps}
    if not source_ok:
        b.append("An ad source is stale, failed or not imported")
    currencies = {per_app.get(a, {}).get("currency") for a in apps if per_app.get(a, {}).get("spend")}
    if len({c for c in currencies if c}) > 1:
        b.append("Apps report spend in different currencies")
    for a in apps:
        p = per_app.get(a) or {}
        if p.get("revenue") is None:
            b.append(f"{a}: " + (p.get("revenue_note") or "revenue isn't reported by its ad platforms, so returns can't be compared"))
        elif float(p.get("spend") or 0) < float(rules.min_spend_for_rebalance):
            b.append(f"{a}: only ${float(p.get('spend') or 0):,.2f} of mature spend (needs ${float(rules.min_spend_for_rebalance):,.0f})")
        elif float(p.get("conversions") or 0) < int(rules.min_conversions_for_revenue_rules):
            b.append(f"{a}: too few conversions to judge ({float(p.get('conversions') or 0):.0f})")
    if b:
        return out
    k = float(rules.min_spend_for_rebalance)
    tot_rev = sum(float(per_app[a]["revenue"]) for a in apps)
    tot_sp = sum(float(per_app[a]["spend"]) for a in apps)
    prior = tot_rev / tot_sp if tot_sp else 0.0
    eff = {a: (float(per_app[a]["revenue"]) + prior * k) / (float(per_app[a]["spend"]) + k) for a in apps}
    base_tot = sum(out["baseline"].values()) or 1.0
    w = {a: out["baseline"][a] / base_tot * (eff[a] / prior if prior else 1.0) for a in apps}
    sw = sum(w.values()) or 1.0
    share = {a: w[a] / sw for a in apps}
    for _ in range(10):
        share = {a: min(float(rules.max_share), max(float(rules.exploration_share), s)) for a, s in share.items()}
        t = sum(share.values())
        share = {a: s / t for a, s in share.items()}
    prop = {}
    for a in apps:
        cur = out["baseline"][a]
        target = share[a] * base_tot
        lo, hi = cur * (1 - float(rules.max_budget_step)), cur * (1 + float(rules.max_budget_step))
        prop[a] = min(max(target, lo), hi) if cur else target
    s = sum(prop.values())
    if s > base_tot:          # step limits must never push the total above the allocated budget
        prop = {a: v * base_tot / s for a, v in prop.items()}
    prop = {a: round(v, 2) for a, v in prop.items()}
    out.update(eligible=True, proposed=prop, efficiency={a: round(eff[a], 3) for a in apps})
    return out
