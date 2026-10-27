"""
Measurement model. Every number says where it came from and what it covers, and incompatible
data is never combined:

  store_sales        store-reported gross sales (App Store / Google Play sales reports), estimated
  store_proceeds     estimated developer proceeds from the same reports, in the proceeds currency
  finalized          finalized proceeds / payouts from finance reports (Apple finance, Google earnings)
  refunds            refunds as reported (negative lines in sales reports)
  ad_attributed      conversions and revenue an ad platform attributes to its own ads
  organic            store activity not attributed to any ad: NOT derivable today (no install attribution feed)
  modeled            estimates such as value-per-install x installs: always labeled, never used for decisions

Total store revenue is never assigned to ad campaigns. Money in different currencies is reported
side by side, not added up (no FX conversion without a dated rate).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .channels import CHANNELS

DEFINITIONS = [
    {"id": "spend", "name": "Ad spend", "formula": "sum of spend reported by each ad platform",
     "attribution": "n/a", "period": "selected days by each platform's reporting day", "currency": "each account's currency; never mixed"},
    {"id": "cpt", "name": "Cost per tap / click", "formula": "spend ÷ taps or clicks", "denominator": "taps (Apple), link clicks (Meta), clicks (Google)",
     "attribution": "n/a", "period": "selected days", "currency": "account currency"},
    {"id": "cpi", "name": "Cost per install", "formula": "spend ÷ installs", "denominator": "installs the platform attributes to its ads (Apple tap-through installs; Meta app installs)",
     "attribution": "each platform's own attribution window", "period": "selected days, mature days only for decisions",
     "note": "Google Ads 'conversions' are whatever the account counts, so Google shows cost per conversion instead"},
    {"id": "attributed_revenue", "name": "Ad-attributed revenue", "formula": "purchase value the platform attributes to its ads",
     "attribution": "Meta: account's attribution setting; Google: conversion action settings; Apple Ads: not reported",
     "note": "Shown only over spend where it was reported; missing is shown as unavailable, never $0"},
    {"id": "roas", "name": "Return on ad spend", "formula": "ad-attributed revenue ÷ spend, over items that report revenue",
     "denominator": "spend of items with reported revenue only"},
    {"id": "store_proceeds", "name": "Store proceeds (estimated)", "formula": "proceeds in store sales reports",
     "attribution": "none: all store sales, organic and paid", "currency": "proceeds currency, per currency"},
    {"id": "finalized", "name": "Finalized proceeds", "formula": "finance / earnings report lines", "period": "fiscal month"},
    {"id": "mrr", "name": "Recurring revenue", "formula": "not computed",
     "note": "An annual payment is never counted as monthly recurring revenue; normalization would be labeled if added"},
    {"id": "ltv", "name": "Lifetime value", "formula": "not computed",
     "note": "Needs cohort data with age, sample size and uncertainty; no renewal rate is assumed"},
]

UNAVAILABLE = {
    "cost_per_trial": "No trial events are imported yet (needs subscription event reports or a purchase webhook)",
    "cost_per_paying_customer": "No link between ad clicks and store purchases is imported (needs install attribution)",
    "trial_to_paid": "No trial or conversion events are imported yet",
    "retention": "No retention / cohort data is imported",
    "renewals": "No subscription renewal events are imported yet",
    "cohort_proceeds": "No cohort data is imported",
    "ltv": "Needs cohort data; not estimated without it",
    "organic": "Organic vs paid installs can't be separated without an attribution feed",
}


def ad_summary(rows: list[dict], rules) -> dict:
    """Ad metrics for a set of performance rows (already filtered). Grouped by currency."""
    by_cur: dict = {}
    for r in rows:
        cur = r.get("currency") or "unknown"
        # grouped by currency AND platform: platform-attributed revenue overlaps, so it's never added across platforms
        g = by_cur.setdefault((cur, r.get("channel")), {"currency": cur, "channel": r.get("channel"), "spend": 0.0, "taps": 0.0, "impressions": 0.0, "installs": 0.0,
                                    "install_spend": 0.0, "conversions_other": 0.0, "revenue": 0.0, "revenue_spend": 0.0,
                                    "rows": 0, "revenue_rows": 0})
        ch = CHANNELS.get(r.get("channel"), {})
        g["rows"] += 1
        for f in ("spend", "taps", "impressions"):
            g[f] += float(r.get(f) or 0)
        if ch.get("conversion_is_install"):
            g["installs"] += float(r.get("installs") or 0)
            g["install_spend"] += float(r.get("spend") or 0)
        else:
            g["conversions_other"] += float(r.get("installs") or 0)
        if r.get("revenue") is not None and ch.get("revenue") != "none":
            g["revenue"] += float(r["revenue"])
            g["revenue_spend"] += float(r.get("spend") or 0)
            g["revenue_rows"] += 1
    out = []
    for g in by_cur.values():
        out.append({**g,
                    "cpt": g["spend"] / g["taps"] if g["taps"] else None,
                    "cpi": g["install_spend"] / g["installs"] if g["installs"] else None,
                    "revenue": g["revenue"] if g["revenue_rows"] else None,
                    "revenue_coverage": (g["revenue_spend"] / g["spend"]) if g["spend"] else None,
                    "roas": (g["revenue"] / g["revenue_spend"]) if g["revenue_rows"] and g["revenue_spend"] else None})
    return {"by_currency": sorted(out, key=lambda x: -x["spend"]), "mixed_currency": len({g["currency"] for g in out}) > 1}


def store_money(con, start: dt.date, end: dt.date) -> dict:
    """Store-reported money between two dates, per platform and currency. Nothing is converted."""
    out = {"estimated": [], "refunds": [], "finalized": [], "available": False}
    try:
        for plat, cur, gross, proc, units in con.execute(
                "SELECT source_platform, coalesce(proceeds_currency, currency), sum(gross_amount), sum(proceeds_amount), sum(units) "
                "FROM revenue.sales_reports WHERE period_start BETWEEN ? AND ? AND transaction_type <> 'refund' GROUP BY 1, 2",
                [start, end]).fetchall():
            out["estimated"].append({"platform": plat, "currency": cur, "gross": _s(gross), "proceeds": _s(proc), "units": int(units or 0)})
        for plat, cur, gross, proc in con.execute(
                "SELECT source_platform, coalesce(proceeds_currency, currency), sum(gross_amount), sum(proceeds_amount) "
                "FROM revenue.sales_reports WHERE period_start BETWEEN ? AND ? AND transaction_type = 'refund' GROUP BY 1, 2",
                [start, end]).fetchall():
            out["refunds"].append({"platform": plat, "currency": cur, "gross": _s(gross), "proceeds": _s(proc)})
        for plat, cur, proc, period in con.execute(
                "SELECT source_platform, currency, sum(proceeds_amount), fiscal_period FROM revenue.financial_reports "
                "WHERE period_end >= ? AND period_start <= ? GROUP BY 1, 2, 4", [start, end]).fetchall():
            out["finalized"].append({"platform": plat, "currency": cur, "proceeds": _s(proc), "fiscal_period": period})
    except Exception:  # noqa: BLE001 - revenue schema not there yet
        return out
    out["available"] = bool(out["estimated"] or out["finalized"] or out["refunds"])
    return out


def _s(v):
    if v is None:
        return None
    return str(Decimal(str(v)).quantize(Decimal("0.01")))
