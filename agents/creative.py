"""
Creative Studio (phase 4): briefs for image ads, a short video, its voiceover, and the sizes each platform needs.

  Creative Briefs  for each approved ad copy, Claude writes 3 image ad briefs (scene + overlay text), a 15-second
                   video script in scenes and a voiceover script. Code checks lengths and flags claims.
  Resizer          the exact sizes / aspect ratios each placement needs (no AI; fixed platform specs)

Generating the actual images, video and voice needs an outside tool. CREATIVE_PROVIDER names one when it is set up;
until then (the default, "none") the briefs are what you hand to a designer or tool, and the Studio tab says so.
"""
from __future__ import annotations

import json

from . import studio

SIZES = [   # placement -> pixels, aspect ratio
    {"platform": "Meta", "placement": "Feed (square)", "size": "1080×1080", "ratio": "1:1"},
    {"platform": "Meta", "placement": "Feed (portrait)", "size": "1080×1350", "ratio": "4:5"},
    {"platform": "Meta", "placement": "Stories / Reels", "size": "1080×1920", "ratio": "9:16"},
    {"platform": "Google App campaign", "placement": "Landscape image", "size": "1200×628", "ratio": "1.91:1"},
    {"platform": "Google App campaign", "placement": "Square image", "size": "1200×1200", "ratio": "1:1"},
    {"platform": "Google App campaign", "placement": "Portrait image", "size": "1200×1500", "ratio": "4:5"},
    {"platform": "Google App campaign", "placement": "Video (portrait)", "size": "1080×1920", "ratio": "9:16"},
    {"platform": "Apple Ads", "placement": "Custom product page screenshots (iPhone 6.9\")", "size": "1290×2796", "ratio": "9:19.5"},
]
OVERLAY_MAX = 40
VOICEOVER_MAX_WORDS = 45

BRIEF_SYSTEM = ("You are a creative director for small nursing exam-prep apps. From the approved ad copy, write: "
                "{\"images\": [3 x {\"scene\": what the picture shows (real nurses/students, phone with the app, study "
                "setting), \"overlay\": short on-image text}], \"video\": {\"length_seconds\": 15, \"scenes\": [{\"seconds\": "
                "\"0-3\", \"shows\": ..., \"on_screen_text\": ...}]}, \"voiceover\": script under 45 words}. "
                "No statistics, pass rates, awards or guarantees unless they are in the input. No real people's names, no brands.")


def resizer(ctx) -> dict:
    return {"sizes": SIZES}


def provider(env) -> str:
    return (str(env.get("CREATIVE_PROVIDER") or "none")).strip().lower() or "none"


def creative_briefs(ctx) -> dict:
    con = ctx.con
    todo = [c for c in studio.items(con, kind="copy", status=("approved",))
            if not studio.items(con, kind="brief", parent_id=c["item_id"], status=("draft", "approved", "rejected"))]
    prov = provider(ctx.environ)
    if not todo:
        return {"status": "nothing to brief", "provider": prov}
    if not ctx.llm:
        return {"status": "no Claude key", "waiting_copy": len(todo), "provider": prov}
    made = 0
    for c in todo[:4]:
        out = ctx.llm.complete_json("creative_briefs", BRIEF_SYSTEM, json.dumps({"app": c["app"], "copy": c["content"]}), max_tokens=1400)
        if not isinstance(out, dict):
            continue
        notes, texts = [], []
        images = []
        for im in (out.get("images") or [])[:3]:
            if not isinstance(im, dict) or not im.get("scene"):
                continue
            ov = " ".join(str(im.get("overlay") or "").split())
            if len(ov) > OVERLAY_MAX:
                notes.append(f"Overlay over {OVERLAY_MAX} characters, shortened: “{ov[:60]}”")
                ov = ov[:OVERLAY_MAX].rsplit(" ", 1)[0]
            images.append({"scene": str(im["scene"])[:400], "overlay": ov})
            texts.append(ov)
        video = out.get("video") if isinstance(out.get("video"), dict) else {}
        scenes = [s for s in (video.get("scenes") or []) if isinstance(s, dict)][:6]
        texts += [str(s.get("on_screen_text") or "") for s in scenes]
        vo = " ".join(str(out.get("voiceover") or "").split())
        if len(vo.split()) > VOICEOVER_MAX_WORDS:
            notes.append(f"Voiceover over {VOICEOVER_MAX_WORDS} words; trim before recording")
        texts.append(vo)
        if not images and not scenes:
            continue
        brief = {"images": images, "video": {"length_seconds": 15, "scenes": scenes}, "voiceover": vo,
                 "sizes": SIZES, "provider": prov, "notes": notes,
                 "generation": ("Not generated: set CREATIVE_PROVIDER once an image/video/voice tool is connected. "
                                "Until then, hand this brief to a designer or your own tool.") if prov == "none"
                 else f"CREATIVE_PROVIDER is '{prov}', but AdBot has no integration for it yet; nothing was generated."}
        studio.add(con, "brief", c["app"], "creative_briefs", f"Creative brief · {c['title'].removeprefix('Copy · ')}", brief,
                   parent_id=c["item_id"], flags=studio.claim_flags(*texts) + notes, now=ctx.now)
        made += 1
    return {"status": "written", "briefs": made, "provider": prov}
