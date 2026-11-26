"""
Creative mockups (phase 9): every creative brief image rendered as a layout mockup, in the three common shapes.

A mockup is an SVG drawn by code from the brief: the app's colour, the on-image text, and a marked box saying what
the photo should show. No photos, no AI image model, no outside service, no cost. They are drafts to judge layout
and wording; finished artwork still comes from a designer or an image tool (CREATIVE_PROVIDER, when one is added).
"""
from __future__ import annotations

import html
import textwrap

SHAPES = {"1x1": (1080, 1080, "Feed square"), "4x5": (1080, 1350, "Feed portrait"), "9x16": (1080, 1920, "Stories / Reels")}
APP_COLOR = {"Rapid Nursing Review": "#FF8A3D", "CCRN Adult": "#00E5A3", "NurseBound": "#7C6CFF"}


def _lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(" ".join(str(text or "").split()), width=width)[:4] or [""]


def render(app: str | None, overlay: str, scene: str, shape: str = "1x1") -> str:
    w, h, _label = SHAPES.get(shape, SHAPES["1x1"])
    c = APP_COLOR.get(app or "", "#00C2FF")
    e = html.escape
    pad = 72
    photo_top, photo_h = int(h * 0.30), int(h * 0.42)
    head = _lines(overlay, 18)
    size = 92 if len(head) <= 2 else 74
    head_svg = "".join(f'<text x="{pad}" y="{150 + i * (size + 14)}" font-size="{size}" font-weight="800" fill="#fff">{e(t)}</text>'
                       for i, t in enumerate(head))
    scene_svg = "".join(f'<text x="{w // 2}" y="{photo_top + photo_h // 2 - 30 + i * 46}" font-size="34" text-anchor="middle" fill="#cbd5e1">{e(t)}</text>'
                        for i, t in enumerate(_lines(scene, 40)[:3]))
    cta_y = h - 190
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="Helvetica, Arial, sans-serif">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c}" stop-opacity=".55"/>'
            f'<stop offset=".55" stop-color="#0b1220"/><stop offset="1" stop-color="#05070d"/></linearGradient></defs>'
            f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
            f'<rect x="0" y="0" width="{w}" height="10" fill="{c}"/>'
            f'{head_svg}'
            f'<rect x="{pad}" y="{photo_top}" width="{w - 2 * pad}" height="{photo_h}" rx="28" fill="#0f172a" fill-opacity=".7" '
            f'stroke="{c}" stroke-width="4" stroke-dasharray="18 12"/>'
            f'<text x="{w // 2}" y="{photo_top + 70}" font-size="30" font-weight="700" letter-spacing="6" text-anchor="middle" fill="{c}">PHOTO</text>'
            f'{scene_svg}'
            f'<rect x="{pad}" y="{cta_y}" width="420" height="104" rx="52" fill="{c}"/>'
            f'<text x="{pad + 210}" y="{cta_y + 66}" font-size="40" font-weight="800" text-anchor="middle" fill="#05070d">Get the app</text>'
            f'<text x="{w - pad}" y="{cta_y + 66}" font-size="34" text-anchor="end" fill="#e2e8f0">{e(app or "")}</text>'
            f'<text x="{w - pad}" y="60" font-size="26" font-weight="700" letter-spacing="4" text-anchor="end" fill="#fbbf24">MOCKUP</text>'
            f'</svg>')


def for_brief(brief: dict) -> list[dict]:
    """The mockups a brief has: one per image × shape (the web app renders them on request)."""
    return [{"image": i, "shape": s, "label": SHAPES[s][2]} for i, _ in enumerate(brief.get("images") or []) for s in SHAPES]
