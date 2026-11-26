"""
Apply checklist (phase 8): step-by-step instructions to put an approved launch draft (or books draft) live
YOURSELF, in each platform's own tool. AdBot sends nothing; DRY_RUN and drafts-only stay as they are.

build(item) turns a studio item into [{platform, step, detail, copy}] where `copy` is text to paste (keyword lists,
headlines). The Outcome Tracker later notices keywords you added and measures them.
"""
from __future__ import annotations


def _money(v, cur):
    return "—" if v is None else (f"${v:,.2f}" if cur == "USD" else f"{v:,.2f} {cur}")


def build(item: dict) -> list[dict]:
    c = item.get("content") or {}
    if item.get("kind") == "book":
        return _books(c)
    if item.get("kind") != "draft":
        return []
    cur = c.get("currency") or "USD"
    steps: list[dict] = []
    flags = item.get("flags") or []
    if flags:
        steps.append({"platform": "First", "step": f"Check the {len(flags)} flagged line(s) on this card are true",
                      "detail": "Numbers and claims (“best”, “pass”, “free”) must be things you can back up.", "copy": None})
    if c.get("errors"):
        steps.append({"platform": "First", "step": "Fix what the draft says is missing",
                      "detail": "; ".join(map(str, c["errors"])), "copy": None})

    aa = c.get("apple_ads") or {}
    camp = (aa.get("campaign") or {}).get("campaign")
    kws = aa.get("add_keywords") or []
    if kws:
        cpt = max((k.get("suggested_max_cpt") or 0) for k in kws) or None
        steps += [
            {"platform": "Apple Ads", "step": "Open ads.apple.com → Campaigns" + (f" → “{camp}”" if camp else " → the app's Search Results campaign"),
             "detail": "Then the ad group you use for exact-match keywords.", "copy": None},
            {"platform": "Apple Ads", "step": f"Keywords → Add keywords → paste these {len(kws)}",
             "detail": f"Match type: Exact. Max cost per tap: {_money(cpt, cur)} or less (your Rules tab limit).",
             "copy": "\n".join(k["text"] for k in kws)},
        ]
    if aa.get("add_negative_keywords"):
        steps.append({"platform": "Apple Ads", "step": f"Negative keywords → Add → paste these {len(aa['add_negative_keywords'])}",
                      "detail": "Match type: Exact. These search terms cost money with no installs.",
                      "copy": "\n".join(aa["add_negative_keywords"])})

    m = c.get("meta") or {}
    if m.get("primary_text"):
        steps += [
            {"platform": "Meta", "step": "Ads Manager → Create → App promotion → the app",
             "detail": f"Countries: {', '.join(m.get('countries') or ['US'])}. Daily budget: {_money(m.get('daily_budget'), cur)}.", "copy": None},
            {"platform": "Meta", "step": "Ad: paste the primary text, headline and description",
             "detail": "Image: one from the matching creative brief.",
             "copy": "\n".join(x for x in (m.get("primary_text"), m.get("headline"), m.get("description")) if x)},
        ]
    g = c.get("google_app") or {}
    if g.get("headlines"):
        steps += [
            {"platform": "Google Ads", "step": "Campaigns → New → App promotion → App installs",
             "detail": f"Countries: {', '.join(g.get('countries') or ['US'])}. Daily budget: {_money(g.get('daily_budget'), cur)}.", "copy": None},
            {"platform": "Google Ads", "step": "Ad group: paste the headlines, then the descriptions",
             "detail": "One per line. Add images from the creative brief.",
             "copy": "\n".join(g.get("headlines") or []) + "\n\n" + "\n".join(g.get("descriptions") or [])},
        ]
    if steps:
        steps.append({"platform": "After", "step": "Nothing else to do here",
                      "detail": "AdBot sees the new keywords in its next import and measures them 7 and 14 days after you approved.", "copy": None})
    return steps


def _books(c: dict) -> list[dict]:
    steps = []
    for camp, terms in (c.get("harvest") or {}).items():
        steps.append({"platform": "Amazon Ads", "step": f"Campaign “{camp}” → Keywords → Add keywords → paste these {len(terms)}",
                      "detail": "Match type: Exact. These search terms already sold a book. Start the bid at your current default.",
                      "copy": "\n".join(terms)})
    for camp, terms in (c.get("negatives") or {}).items():
        steps.append({"platform": "Amazon Ads", "step": f"Campaign “{camp}” → Negative keywords → Add → paste these {len(terms)}",
                      "detail": "Negative exact. Many clicks, no sales.", "copy": "\n".join(terms)})
    return steps
