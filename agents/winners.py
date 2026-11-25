"""
Winner Spotter: over the decision engine's mature window (the same days its rules use), the keywords / ad sets
that bring installs cheapest, and the ones spending with no installs yet.

Informational only. Pausing stays with the decision engine's own rules and your sign-off; this agent never
proposes or changes anything.
"""
from __future__ import annotations

from control.channels import CHANNELS


def winner_spotter(ctx) -> dict:
    from .money import _engine_view
    rules = ctx.rules
    ents = list(_engine_view(ctx)["agg"]["entities"].values())
    min_taps = int(rules.min_taps_for_cpt)
    cands, watch = [], []
    for a in ents:
        if a.get("entity_type") == "campaign" and a.get("channel") == "Apple Search Ads":
            continue      # campaign-level Apple rows are monitor-only
        install_based = CHANNELS.get(a["channel"], {}).get("conversion_is_install", False)
        item = {"name": a.get("name"), "channel": a["channel"], "app": a.get("app"), "campaign": a.get("campaign"),
                "spend": round(a["spend"], 2), "taps": int(a["taps"]), "installs": round(a["installs"], 1),
                "cpt": round(a["cpt"], 2) if a.get("cpt") is not None else None,
                "metric": "installs" if install_based else "conversions"}
        if a["installs"] > 0 and a["spend"] > 0 and a["taps"] >= min_taps:
            item["cost_per"] = round(a["spend"] / a["installs"], 2)
            cands.append(item)
        elif a["installs"] == 0 and a["spend"] > 0 and a["taps"] >= max(min_taps, int(rules.zero_install_taps) // 2):
            watch.append(item)
    cands.sort(key=lambda x: (x["cost_per"], -x["installs"]))
    watch.sort(key=lambda x: -x["spend"])
    with_data = [a for a in ents if a["taps"] >= min_taps]
    window = None
    if ents:
        w = next(iter(ents))
        window = {"start": w["window_start"].isoformat(), "end": w["window_end"].isoformat()}
    return {"window": window, "entities": len(ents), "with_enough_data": len(with_data), "min_taps": min_taps,
            "winners": cands[:5], "watch": watch[:5],
            "note": None if with_data else f"Not enough data yet: nothing has {min_taps}+ taps in the window"}
