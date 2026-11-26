"""
Morning Page: one Telegram message a day, from the other agents' facts.

Sent by the first scheduled run at or after AGENTS_MORNING_HOUR (default 8) in AGENTS_TZ (default
America/Los_Angeles), once per local day. Every number is formatted by code from the facts. Claude (when
ANTHROPIC_API_KEY is set) writes only the two- or three-sentence summary at the top, and that summary is dropped
if it contains a number that isn't in the facts. Without Claude, or if the summary is dropped, the page is sent
without it. Switch it off with the "Morning page" toggle on the Rules tab.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re

from control.schema import SCHEMA

SYSTEM = (
    "You write the two or three sentence summary at the top of a small app business's daily ad report. "
    "The business advertises three nursing exam-prep apps (Rapid Nursing Review, CCRN Adult, NurseBound). "
    "Use only the facts given. Do not add any number, percentage or date that is not written in the facts, "
    "and do not calculate new ones. Say what matters most today and what, if anything, needs the owner's "
    "attention. Plain text, no markdown, no greeting, no sign-off.")
NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set:
    out = set()
    for m in NUM.findall(text):
        n = m.replace(",", "")
        out.add(n)
        try:
            f = float(n)
            out.add(f"{f:g}")
            out.add(f"{f:.2f}")
            if f == int(f):
                out.add(str(int(f)))
        except ValueError:
            pass
    return out


def grounded(summary: str, facts_json: str) -> bool:
    """True when every number in the summary also appears in the facts."""
    allowed = _numbers(facts_json)
    return all(n in allowed or n.rstrip("0").rstrip(".") in allowed for n in NUM.findall(summary.replace(",", "")))


def _m(v, cur="USD") -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}" if cur == "USD" else f"{v:,.2f} {cur}"


SHORT = {"Rapid Nursing Review": "RNR", "CCRN Adult": "CCRN", "NurseBound": "NurseBound"}
STORE = {"apple_app_store": "App Store", "google_play": "Google Play"}


FACT_KEYS = ("department_check", "spend_watch", "real_return", "winner_spotter", "change_review",
             "review_miner", "search_ad_watch", "competitor_ad_spy", "test_budget",
             "profit_tracker", "outcome_tracker", "book_ads", "watchdog")


def facts(ctx) -> dict:
    from . import studio
    f = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in ctx.out.items()
         if k in FACT_KEYS and isinstance(v, dict)}
    f["studio_waiting"] = studio.waiting(ctx.con)
    return f


def render(f: dict, local: dt.datetime, dry_run: bool, summary: str | None) -> str:
    e = html.escape
    sw, rr, ws, cr, dc = (f.get(k) or {} for k in ("spend_watch", "real_return", "winner_spotter", "change_review", "department_check"))
    cur = sw.get("currency", "USD")
    L = [f"☀️ <b>AdBot morning page</b> · {local:%a %d %b}"]
    if summary:
        L += ["", f"<i>{e(summary)}</i>"]

    L += ["", "<b>What you spent</b>"]
    yb = sw.get("yesterday_by_channel") or {}
    L.append(f"Yesterday: {_m(sw.get('yesterday_total'), cur)}" +
             (" (" + " · ".join(f"{e(c)} {_m(v, cur)}" for c, v in sorted(yb.items(), key=lambda x: -x[1])) + ")" if yb else ""))
    if sw.get("monthly_budget") is not None:
        flag = "under limit" if sw.get("under_limit") else "⚠️ OVER the usable budget"
        L.append(f"Month to date: {_m(sw.get('mtd_total'), cur)} of {_m(sw.get('usable'), cur)} usable · {flag} · "
                 f"{sw.get('days_left')} days left")
    else:
        L.append(f"Month to date: {_m(sw.get('mtd_total'), cur)} (no monthly budget set on the Rules tab)")
    if sw.get("other_currency_rows"):
        L.append(f"<i>{sw['other_currency_rows']} row(s) in other currencies not included</i>")

    L += ["", "<b>What sold</b> · last 7 days, blended"]
    any_app = False
    for app, w in (rr.get("apps") or {}).items():
        x = w.get("7d") or {}
        if x.get("proceeds") is None and not x.get("spend"):
            continue
        any_app = True
        ret = f" · return {x['return']:.2f}x" if x.get("return") is not None else ""
        paid = f" · {x['paid']} paid" if x.get("paid") is not None else ""
        L.append(f"{e(SHORT.get(app, app))}: {_m(x.get('proceeds'), cur)} proceeds · {_m(x.get('spend'), cur)} ads{ret}{paid}")
    if not any_app:
        L.append("No store proceeds or ad spend in the last 7 days.")
    thru = rr.get("store_data_through") or {}
    if thru:
        names = {"apple_app_store": "App Store", "google_play": "Google Play"}
        L.append("<i>Store data through " + ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(thru.items())) + "</i>")

    pt = f.get("profit_tracker") or {}
    vals = [(a, v) for a, v in (pt.get("apps") or {}).items() if v.get("value_per_download") is not None]
    if vals or pt.get("sync_gaps"):
        L += ["", "<b>Profit</b> · estimates, organic included"]
        if vals:
            L.append("Worth per download (90 days): " + " · ".join(
                f"{e(SHORT.get(a, a))} {_m(v['value_per_download'], cur)}{' (early)' if v.get('confidence') == 'early' else ''}" for a, v in vals))
        t = pt.get("totals_28d") or {}
        if t.get("est_return") is not None:
            L.append(f"Ads, 28 days: {_m(t.get('spend'), cur)} spent → about {_m(t.get('est_value'), cur)} earned ({t['est_return']:.2f}x)")
        for x in (pt.get("losing") or [])[:2]:
            L.append(f"• Losing: {e(str(x['name']))} ({e(SHORT.get(x['app'], x['app'] or '?'))}) {_m(x['spend'], cur)} → {x['est_return']:.2f}x")
        if pt.get("suggest_value_per_install"):
            L.append("Rules tab value per install is 0; measured: " + " · ".join(
                f"{e(SHORT.get(a, a))} {_m(v, cur)}" for a, v in pt["suggest_value_per_install"].items()))
        for g in (pt.get("sync_gaps") or [])[:2]:
            L.append(f"⚠️ {e(g['text'])}")

    L += ["", "<b>Winners</b>" + (f" · {ws['window']['start']} to {ws['window']['end']}" if ws.get("window") else "")]
    if ws.get("winners"):
        for w in ws["winners"][:3]:
            L.append(f"• {e(str(w['name']))} ({e(SHORT.get(w['app'], w['app'] or '?'))}, {e(w['channel'])}): "
                     f"{w['installs']:g} {w['metric']} at {_m(w['cost_per'], cur)} each")
    else:
        L.append(e(ws.get("note") or "No clear winners yet."))
    if ws.get("watch"):
        L.append("<b>Watch</b> (spending, nothing yet)")
        for w in ws["watch"][:3]:
            L.append(f"• {e(str(w['name']))} ({e(SHORT.get(w['app'], w['app'] or '?'))}): {_m(w['spend'], cur)}, {w['taps']} taps, 0 {w['metric']}")

    L += ["", "<b>Needs your OK</b>"]
    if cr.get("needs_your_ok"):
        L.append(f"{cr['needs_your_ok']} proposed change(s) waiting on the Ads tab" +
                 (f" · {cr['blocked']} blocked" if cr.get("blocked") else ""))
        for t in (cr.get("top") or [])[:3]:
            L.append(f"• {e(str(t.get('entity_name')))}: {e(str(t.get('reason') or t.get('action')))}")
    else:
        L.append("Nothing waiting." + (f" {cr['blocked']} blocked change(s) on the Ads tab." if cr.get("blocked") else ""))
    if cr.get("uncertain"):
        L.append(f"⚠️ {cr['uncertain']} change(s) not confirmed by the platform")
    if cr.get("emergency"):
        L.append("🛑 Emergency pause is active")
    elif cr.get("stopped"):
        L.append("⏸ Automation is stopped")

    rm, sa, ca = (f.get(k) or {} for k in ("review_miner", "search_ad_watch", "competitor_ad_spy"))
    research = []
    for app, stores in (rm.get("last_30_days") or {}).items():
        parts = [f"{STORE.get(s, s)} {v['avg_rating']:.1f}★ ({v['reviews_30d']})" for s, v in stores.items() if v.get("avg_rating") is not None]
        if parts:
            research.append(f"{e(SHORT.get(app, app))} reviews, last 30 days: " + ", ".join(parts))
    if (rm.get("fetch") or {}).get("new"):
        research.append(f"{rm['fetch']['new']} new review(s) since yesterday")
    if sa.get("add_as_keyword"):
        research.append(f"{len(sa['add_as_keyword'])} search term(s) bringing installs that aren't keywords yet")
    if sa.get("negative_candidates"):
        research.append(f"{len(sa['negative_candidates'])} search term(s) costing money with no installs")
    if ca.get("status") == "ok" and ca.get("ads_seen"):
        research.append(f"{ca['ads_seen']} competitor ad(s) seen in the last 14 days")
    if research:
        L += ["", "<b>Research</b>"] + [f"• {x}" for x in research]
    waiting = f.get("studio_waiting") or {}
    if any(waiting.values()):
        names = {"angle": "angle", "copy": "ad copy", "listing": "listing text", "draft": "launch draft", "brief": "creative brief",
                 "voice": "customer voice", "book": "books draft"}
        L += ["", "<b>Studio</b> · waiting for your review"]
        L.append(" · ".join(f"{n} {names.get(k, k)}{'s' if n > 1 and not k.endswith('y') else ''}" for k, n in sorted(waiting.items())))

    ot = f.get("outcome_tracker") or {}
    if ot.get("recent"):
        vv = ot.get("verdicts") or {}
        L += ["", "<b>Your decisions, measured</b>"]
        L.append(" · ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(vv.items())))
        for x in ot["recent"][:2]:
            L.append(f"• {e(str(x.get('note') or ''))} ({x['days']} days)")
    bk = f.get("book_ads") or {}
    if bk.get("status") == "on":
        y, w = bk.get("yesterday") or {}, bk.get("7d") or {}
        bc = w.get("currency") or "USD"
        L += ["", "<b>Books</b> · Amazon Ads"]
        L.append(f"7 days: {_m(w.get('spend'), bc)} spent · {_m(w.get('sales'), bc)} sales · {w.get('orders', 0)} orders"
                 + (f" · ACoS {w['acos'] * 100:.0f}%" if w.get("acos") is not None else ""))
        n_add, n_neg = sum(map(len, (bk.get("harvest") or {}).values())), sum(map(len, (bk.get("negatives") or {}).values()))
        if n_add or n_neg:
            L.append(f"{n_add} search term(s) to add as keywords, {n_neg} to exclude (Studio)")
    wd = f.get("watchdog") or {}
    if wd.get("issues"):
        L += ["", f"<b>Watchdog</b> · {len(wd['issues'])} open"]
        for i in wd["issues"][:4]:
            L.append(f"{ {'critical': '🛑', 'warning': '⚠️'}.get(i['severity'], 'ℹ️') } {e(i['text'])}")

    srcs = dc.get("sources") or []
    L += ["", f"<b>Departments</b> · {dc.get('healthy', 0)} of {len(srcs)} sources up to date"]
    icon = {"critical": "🛑", "warning": "⚠️", "info": "ℹ️"}
    for i in (dc.get("issues") or [])[:5]:
        L.append(f"{icon.get(i['severity'], '•')} {e(i['text'])}")
    if not dc.get("issues"):
        L.append("✅ Nothing needs attention")

    L += ["", f"<i>Mode: {e(str(cr.get('mode') or '?'))}{' · dry run (nothing is sent to the ad platforms)' if dry_run else ''}</i>"]
    return "\n".join(L)


def morning_page(ctx) -> dict:
    local = ctx.local_now
    page_date = local.date()
    hour = int(ctx.environ.get("AGENTS_MORNING_HOUR") or 8)
    force = str(ctx.environ.get("AGENTS_FORCE_MORNING") or "").lower() in ("1", "true", "yes")
    if not force:
        if local.hour < hour:
            return {"sent": False, "reason": f"before {hour}:00 {ctx.tz}"}
        done = ctx.con.execute(f"SELECT count(*) FROM {SCHEMA}.morning_pages WHERE page_date = ?", [page_date]).fetchone()[0]
        if done:
            return {"sent": False, "reason": f"already written for {page_date}"}

    f = facts(ctx)
    fj = json.dumps(f, default=str, sort_keys=True)
    summary, used_llm, dropped = None, False, None
    if ctx.llm:
        text = ctx.llm.complete("morning_page", SYSTEM, "Facts (JSON):\n" + fj[:30000], max_tokens=300)
        if text:
            text = " ".join(text.split())[:600]
            if grounded(text, fj):
                summary, used_llm = text, True
            else:
                dropped = "summary mentioned a number not in the facts"
    dry = str(ctx.environ.get("DRY_RUN", "true")).lower() not in ("false", "0", "no")
    page = render(f, local, dry, summary)
    status = "preview"
    if ctx.send:
        from control.alerts import Alerter
        body = page.split("\n", 1)[1].lstrip("\n") if "\n" in page else ""     # the Alerter adds the title line
        status = Alerter(ctx.con, ctx.transport, ctx.rules, clock=lambda: ctx.now).send(
            "morning_page", "info", f"morning:{page_date.isoformat()}", f"AdBot morning page · {local:%a %d %b}", body)
        ctx.con.execute(f"INSERT INTO {SCHEMA}.morning_pages VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (page_date) DO NOTHING",
                        [page_date, ctx.now, status, used_llm, page, fj])
    return {"sent": status in ("delivered", "logged"), "delivery": status, "page_date": page_date.isoformat(),
            "used_llm": used_llm, "summary_dropped": dropped, "text": page}
