"""
Writing (phases 2 and 4): angles, ad copy and store listing text. Claude writes; code checks; you approve.

  Angle Writer     weekly, per app: three reasons to download, each backed by a review or search term that exists
  Ad Copy          for each approved angle: Meta, Google App campaign and Apple Ads text within each platform's limits
  Listing Writer   ("Landing") for each approved angle: App Store promotional text and Google Play short description

Every line is checked: over-length lines are dropped, and numbers or claims ("best", "pass", "guaranteed"...) are
flagged for you to verify before anything runs. Nothing is published anywhere.
"""
from __future__ import annotations

import json

from . import studio

LIMITS = {   # platform -> field -> (max characters, how many)
    "meta": {"primary_text": (125, 3), "headline": (40, 3), "description": (30, 2)},
    "google_app": {"headline": (30, 5), "description": (90, 5)},
    "apple_ads": {"keywords": (100, 10)},           # Search Results ads use the App Store listing; AdBot suggests keywords
}
LISTING_LIMITS = {"app_store_promotional_text": 170, "play_short_description": 80}

ANGLE_SYSTEM = ("You are a performance marketer for small nursing exam-prep apps. From the customer voice, reviews and "
                "search terms given, write exactly 3 ad angles for the app: {\"angles\": [{\"title\": 3-6 words, "
                "\"promise\": one sentence of why to download, \"audience\": who it is for, \"evidence\": [ids]}]}. "
                "Evidence ids must be copied exactly from the input. No made-up statistics, pass rates or awards. "
                "your_past_decisions lists angle titles the owner approved and rejected: lean towards what was approved, "
                "don't repeat a rejected angle.")
COPY_SYSTEM = ("Write ad copy for one angle of a nursing exam-prep app. Return {\"meta\": {\"primary_text\": [3], "
               "\"headline\": [3], \"description\": [2]}, \"google_app\": {\"headline\": [5], \"description\": [5]}, "
               "\"apple_ads\": {\"keywords\": [up to 10 search keywords]}}. Hard limits in characters: Meta primary text 125, "
               "headline 40, description 30; Google headline 30, description 90. No statistics, pass rates, awards or "
               "guarantees unless they are in the input. No emojis.")
LISTING_SYSTEM = ("Write store listing text for a nursing exam-prep app from the approved angle. Return "
                  "{\"app_store_promotional_text\": max 170 characters, \"play_short_description\": max 80 characters}. "
                  "No statistics, pass rates, awards or guarantees unless in the input.")


def _evidence(con, app: str) -> tuple[dict, set]:
    from control.schema import SCHEMA
    voice = studio.items(con, kind="voice", status=studio.OPEN, app=app, limit=1)
    revs = con.execute(f"SELECT review_id, rating, coalesce(title, '') || ' ' || coalesce(body, '') FROM {SCHEMA}.reviews "
                       "WHERE app = ? ORDER BY created_at DESC LIMIT 30", [app]).fetchall()
    terms = con.execute(f"SELECT term, taps, installs FROM {SCHEMA}.search_terms WHERE app = ? AND snapshot_date = "
                        f"(SELECT max(snapshot_date) FROM {SCHEMA}.search_terms) ORDER BY installs DESC, taps DESC LIMIT 30", [app]).fetchall()
    ids = {r[0] for r in revs} | {f"term:{t[0]}" for t in terms}
    return ({"app": app, "customer_voice": voice[0]["content"] if voice else None,
             "reviews": [{"id": a, "rating": b, "text": (c or "").strip()[:400]} for a, b, c in revs],
             "search_terms": [{"id": f"term:{t}", "taps": tp, "installs": ins} for t, tp, ins in terms],
             "your_past_decisions": _past(con, app)}, ids)


def _past(con, app: str) -> dict:
    try:
        from .outcomes import past_decisions
        return past_decisions(con, app)
    except Exception:  # noqa: BLE001 - outcomes table not there yet: nothing learned
        return {"approved": [], "rejected": []}


def angle_writer(ctx) -> dict:
    import dlt_pipeline as bot
    con = ctx.con
    if not (studio.due(ctx, "angle_writer", 7) or studio.forced(ctx)):
        return {"status": "weekly", "drafts": len(studio.items(con, kind="angle", status=("draft",)))}
    if not ctx.llm:
        return {"status": "no Claude key", "drafts": 0}
    made = {}
    for app in bot.APPS:
        facts, ids = _evidence(con, app)
        if not facts["reviews"] and not facts["search_terms"]:
            made[app] = "no reviews or search terms yet"
            continue
        out = ctx.llm.complete_json("angle_writer", ANGLE_SYSTEM, json.dumps(facts), max_tokens=1200)
        angles = [a for a in ((out or {}).get("angles") or []) if isinstance(a, dict) and a.get("title") and a.get("promise")
                  and a.get("evidence") and all(e in ids for e in a["evidence"])][:3]
        if not angles:
            made[app] = "no angle passed the evidence check"
            continue
        studio.supersede(con, "angle", app)
        made[app] = [studio.add(con, "angle", app, "angle_writer", a["title"], a,
                                flags=studio.claim_flags(a["title"], a["promise"]), now=ctx.now) for a in angles]
    studio.mark_done(ctx, "angle_writer")
    return {"status": "written", "made": made}


def check_copy(out: dict) -> tuple[dict, list[str], list[str]]:
    """Keep lines within limits; return (clean copy, dropped notes, flags)."""
    clean, dropped, texts = {}, [], []
    for platform, fields in LIMITS.items():
        for field, (mx, n) in fields.items():
            vals = ((out or {}).get(platform) or {}).get(field) or []
            keep = []
            for v in vals if isinstance(vals, list) else []:
                v = " ".join(str(v).split())
                if not v:
                    continue
                if len(v) > mx:
                    dropped.append(f"{platform} {field} over {mx} characters: “{v[:60]}…”")
                    continue
                keep.append(v)
            if keep:
                clean.setdefault(platform, {})[field] = keep[:n]
                if platform != "apple_ads":
                    texts += keep[:n]
    return clean, dropped, studio.claim_flags(*texts)


def ad_copy(ctx) -> dict:
    con = ctx.con
    todo = [a for a in studio.items(con, kind="angle", status=("approved",))
            if not studio.items(con, kind="copy", parent_id=a["item_id"], status=("draft", "approved", "rejected"))]
    if not todo:
        return {"status": "nothing to write", "written": 0}
    if not ctx.llm:
        return {"status": "no Claude key", "waiting_angles": len(todo)}
    written = 0
    for a in todo[:6]:
        out = ctx.llm.complete_json("ad_copy", COPY_SYSTEM, json.dumps({"app": a["app"], "angle": a["content"]}), max_tokens=1200)
        clean, dropped, flags = check_copy(out or {})
        if not clean:
            continue
        studio.add(con, "copy", a["app"], "ad_copy", f"Copy · {a['title']}", {**clean, "dropped": dropped},
                   parent_id=a["item_id"], flags=flags, now=ctx.now)
        written += 1
    return {"status": "written", "written": written}


def listing_writer(ctx) -> dict:
    con = ctx.con
    todo = [a for a in studio.items(con, kind="angle", status=("approved",))
            if not studio.items(con, kind="listing", parent_id=a["item_id"], status=("draft", "approved", "rejected"))]
    if not todo:
        return {"status": "nothing to write", "written": 0}
    if not ctx.llm:
        return {"status": "no Claude key", "waiting_angles": len(todo)}
    written = 0
    for a in todo[:3]:
        out = ctx.llm.complete_json("listing_writer", LISTING_SYSTEM, json.dumps({"app": a["app"], "angle": a["content"]}), max_tokens=400) or {}
        clean = {k: " ".join(str(out.get(k) or "").split()) for k in LISTING_LIMITS}
        clean = {k: v for k, v in clean.items() if v and len(v) <= LISTING_LIMITS[k]}
        if not clean:
            continue
        studio.add(con, "listing", a["app"], "listing_writer", f"Listing · {a['title']}", clean, parent_id=a["item_id"],
                   flags=studio.claim_flags(*clean.values()), now=ctx.now)
        written += 1
    return {"status": "written", "written": written}
