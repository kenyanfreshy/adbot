"""
Launch (phase 3): complete campaign drafts you can review. DRAFTS ONLY.

  Test Budget     per app, the daily amount a new test could use: the app's daily allowance (Rules tab budget) ×
                  the exploration share, capped by the per-campaign maximum. No monthly allocation → no test budget.
  Launch Drafter  for each approved ad copy: an Apple Ads keyword draft (new keywords from the copy and from search
                  terms that already bring installs, plus negatives), a Meta app-install ad draft and a Google App
                  campaign draft, each checked against the platform's limits and your budget.

Nothing here is created on any platform. Approving a draft records your decision only; launching from AdBot is
switched off (a later change would have to add it, behind DRY_RUN and the existing safeguards).
"""
from __future__ import annotations

from . import studio

LAUNCH_NOTE = "Draft only: nothing is created on the platform. Create it yourself in the platform's own tool, or wait for launch support."


def test_budget(ctx) -> dict:
    import dlt_pipeline as bot
    from control import budget
    from .money import _engine_view
    rules = ctx.rules
    agg = _engine_view(ctx)["agg"]
    pace = budget.pacing(rules, agg["mtd_app"], agg["mtd_channel"], ctx.today, bot.APPS, agg["unattributed_mtd"])
    out = {}
    for app in bot.APPS:
        p = (pace.get("apps") or {}).get(app) or {}
        allowance = p.get("daily_allowance")
        if allowance is None:
            out[app] = {"daily": None, "why": p.get("blocker") or "Set a monthly budget and app allocation on the Rules tab"}
            continue
        t = float(allowance) * float(rules.exploration_share)
        if rules.campaign_daily_max:
            t = min(t, float(rules.campaign_daily_max))
        t = round(t, 2)
        out[app] = {"daily": t if t >= 1 else None, "allowance": allowance, "share": rules.exploration_share,
                    "why": None if t >= 1 else "Under $1/day: platforms reject budgets this small"}
    return {"currency": rules.budget_currency, "apps": out}


def _countries(rules, app: str) -> list[str]:
    geo = (rules.geo_targets or {}).get(app) or []
    return [g["code"] for g in geo if g.get("code")] or ["US"]


def _search_campaign(con, app: str, rules) -> dict | None:
    import dlt_pipeline as bot
    rows = con.execute("SELECT campaign_id, campaign_name FROM control.ad_entities WHERE channel = 'Apple Search Ads' "
                       "AND entity_type = 'campaign' ORDER BY last_seen DESC").fetchall()
    for cid, name in rows:
        if bot.app_for(name or "", rules) == app:
            return {"campaign_id": cid, "campaign": name}
    return None


def launch_drafter(ctx) -> dict:
    con, rules = ctx.con, ctx.rules
    budgets = (ctx.out.get("test_budget") or {}).get("apps") or {}
    watch = ctx.out.get("search_ad_watch") or {}
    todo = [c for c in studio.items(con, kind="copy", status=("approved",))
            if not studio.items(con, kind="draft", parent_id=c["item_id"], status=("draft", "approved", "rejected"))]
    made = 0
    for c in todo[:6]:
        app, copy = c["app"], c["content"]
        tb = (budgets.get(app) or {}).get("daily")
        errors = [] if tb else [f"No test budget for {app}: " + ((budgets.get(app) or {}).get("why") or "unknown")]
        existing = {r[0].lower() for r in con.execute(
            "SELECT name FROM control.ad_entities WHERE entity_type = 'keyword' AND channel = 'Apple Search Ads'").fetchall() if r[0]}
        kws = []
        for k in ((copy.get("apple_ads") or {}).get("keywords") or []) + [t["term"] for t in watch.get("add_as_keyword") or [] if t["app"] == app]:
            k = " ".join(str(k).lower().split())
            if k and k not in existing and k not in [x["text"] for x in kws]:
                kws.append({"text": k, "match_type": "EXACT", "suggested_max_cpt": round(float(rules.max_cpt), 2)})
        negatives = [t["term"] for t in watch.get("negative_candidates") or [] if t["app"] == app]
        meta, gapp = copy.get("meta") or {}, copy.get("google_app") or {}
        if not meta.get("primary_text") or not meta.get("headline"):
            errors.append("Meta draft needs primary text and a headline")
        if len(gapp.get("headline") or []) < 2 or not gapp.get("description"):
            errors.append("Google App campaign needs at least 2 headlines and 1 description")
        countries = _countries(rules, app)
        draft = {
            "launch": LAUNCH_NOTE, "test_budget_daily": tb, "currency": rules.budget_currency, "errors": errors,
            "apple_ads": {"campaign": _search_campaign(con, app, rules), "add_keywords": kws[:15], "add_negative_keywords": negatives[:10],
                          "note": "Keywords go into the app's existing Search Results campaign at or below your max cost per tap."},
            "meta": {"objective": "App promotion (installs)", "countries": countries, "daily_budget": tb,
                     "primary_text": (meta.get("primary_text") or [None])[0], "headline": (meta.get("headline") or [None])[0],
                     "description": (meta.get("description") or [None])[0], "alternatives": meta,
                     "creative": "Use a creative from the matching brief (Creative Studio)"},
            "google_app": {"campaign_type": "App campaign for installs", "countries": countries, "daily_budget": tb,
                           "headlines": gapp.get("headline") or [], "descriptions": gapp.get("description") or []},
        }
        studio.add(con, "draft", app, "launch_drafter", f"Launch draft · {c['title'].removeprefix('Copy · ')}", draft,
                   parent_id=c["item_id"], flags=list(c.get("flags") or []) + errors, now=ctx.now)
        made += 1
    return {"drafted": made, "waiting_copy": max(0, len(todo) - made), "launch": "off (drafts only)"}
