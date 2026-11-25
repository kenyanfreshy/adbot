"""
Research (phase 2): what customers say and search for, and what competitors run. Read-only everywhere.

  Review Miner       your own App Store and Google Play reviews (daily)
  Search Ad Watch    the search terms that triggered your Apple Ads Search Results campaigns (daily)
  Competitor Ad Spy  competitors' active ads from the Meta Ad Library, for terms you choose (weekly, optional)
  Customer Voice     Claude reads the reviews and search terms and lists likes, complaints and the words people use
                     (weekly; every point must cite a review or search term that exists)

Sources that aren't set up or refuse access are reported as such, never as "nothing found".
"""
from __future__ import annotations

import datetime as dt

from control.schema import SCHEMA

from . import studio

STORES = {"apple_app_store": "App Store", "google_play": "Google Play"}


def _listings(con) -> list[dict]:
    cur = con.execute("SELECT a.bot_app_name AS app, l.platform, l.apple_app_id, l.package_name FROM revenue.store_listings l "
                      "JOIN revenue.applications a USING (app_id) WHERE a.bot_app_name IS NOT NULL")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _upsert_review(con, r: dict, now) -> bool:
    new = not con.execute(f"SELECT 1 FROM {SCHEMA}.reviews WHERE review_id = ?", [r["review_id"]]).fetchone()
    con.execute(f"INSERT INTO {SCHEMA}.reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (review_id) DO UPDATE SET "
                "rating = excluded.rating, title = excluded.title, body = excluded.body, fetched_at = excluded.fetched_at",
                [r["review_id"], r["app"], r["store"], r.get("rating"), (r.get("title") or "")[:300] or None,
                 (r.get("body") or "")[:4000] or None, r.get("territory"), r.get("created_at"), now])
    return new


def apple_reviews(client, app: str, apple_app_id: str, limit: int = 200) -> list[dict]:
    out = []
    for page in client.pages(f"/v1/apps/{apple_app_id}/customerReviews", {"sort": "-createdDate", "limit": 200}):
        for d in page.get("data") or []:
            a = d.get("attributes") or {}
            out.append({"review_id": f"apple_app_store:{d.get('id')}", "app": app, "store": "apple_app_store",
                        "rating": a.get("rating"), "title": a.get("title"), "body": a.get("body"),
                        "territory": a.get("territory"), "created_at": _ts(a.get("createdDate"))})
        if len(out) >= limit:
            break
    return out


def play_reviews(client, app: str, package: str) -> list[dict]:
    from revenue.google_play import API
    out = []
    for rv in client.pages(f"{API}/applications/{package}/reviews", "reviews", {"maxResults": 100}):
        c = ((rv.get("comments") or [{}])[0].get("userComment") or {})
        secs = (c.get("lastModified") or {}).get("seconds")
        out.append({"review_id": f"google_play:{rv.get('reviewId')}", "app": app, "store": "google_play",
                    "rating": c.get("starRating"), "title": None, "body": (c.get("text") or "").strip(),
                    "territory": c.get("reviewerLanguage"),
                    "created_at": dt.datetime.fromtimestamp(int(secs), dt.timezone.utc).replace(tzinfo=None) if secs else None})
    return out


def review_miner(ctx) -> dict:
    con = ctx.con
    status: dict = {}
    if studio.due(ctx, "review_miner", 1) or studio.forced(ctx):
        new = 0
        for store, fetch, ident in (("apple_app_store", apple_reviews, "apple_app_id"), ("google_play", play_reviews, "package_name")):
            client = ctx.client(store if store == "google_play" else "apple_store")
            if client is None:
                status[store] = "not configured"
                continue
            got, errors = 0, []
            for l in _listings(con):
                if l["platform"] != store or not l.get(ident):
                    continue
                try:
                    for r in fetch(client, l["app"], l[ident]):
                        got += 1
                        new += _upsert_review(con, r, ctx.now)
                except Exception as e:  # noqa: BLE001 - e.g. the key's role can't read reviews
                    errors.append(f"{l['app']}: {type(e).__name__}: {str(e)[:120]}")
            status[store] = ("; ".join(errors) if errors and not got else f"{got} fetched" + (f" ({len(errors)} error(s))" if errors else ""))
        studio.mark_done(ctx, "review_miner")
        status["new"] = new
    else:
        status["skipped"] = "fetched today already"
    since = ctx.now - dt.timedelta(days=30)
    stats = {}
    for app, store, n, avg in con.execute(f"SELECT app, store, count(*), avg(rating) FROM {SCHEMA}.reviews "
                                          "WHERE coalesce(created_at, fetched_at) >= ? GROUP BY 1, 2", [since]).fetchall():
        stats.setdefault(app, {})[store] = {"reviews_30d": int(n), "avg_rating": round(float(avg), 2) if avg is not None else None}
    low = con.execute(f"SELECT review_id, app, rating, coalesce(title, '') || ' ' || coalesce(body, '') FROM {SCHEMA}.reviews "
                      "WHERE rating <= 2 AND coalesce(created_at, fetched_at) >= ? ORDER BY created_at DESC LIMIT 3", [since]).fetchall()
    return {"fetch": status, "last_30_days": stats,
            "recent_low_ratings": [{"review_id": a, "app": b, "rating": c, "text": (d or "").strip()[:200]} for a, b, c, d in low]}


def search_ad_watch(ctx) -> dict:
    """Terms worth adding as keywords (installs at a good cost) and terms costing money with no installs."""
    import dlt_pipeline as bot
    con, rules = ctx.con, ctx.rules
    status = None
    if studio.due(ctx, "search_ad_watch", 1) or studio.forced(ctx):
        asa = ctx.client("apple_ads")
        if asa is None:
            status = "Apple Ads not configured"
        else:
            end = ctx.today - dt.timedelta(days=1)
            start = end - dt.timedelta(days=13)
            n, errors = 0, []
            camps = con.execute("SELECT campaign_id, campaign_name FROM control.ad_entities WHERE channel = 'Apple Search Ads' "
                                "AND entity_type = 'campaign' AND last_seen >= ?", [ctx.now - dt.timedelta(days=7)]).fetchall()
            for cid, name in camps:
                app = bot.app_for(name or "", rules)
                if app not in bot.APPS:
                    continue
                try:
                    for t in asa.search_terms(cid, start, end):
                        con.execute(f"INSERT INTO {SCHEMA}.search_terms VALUES (?, 'Apple Search Ads', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                    "ON CONFLICT (snapshot_date, channel, campaign_id, adgroup_id, term) DO UPDATE SET impressions = excluded.impressions, taps = excluded.taps, "
                                    "installs = excluded.installs, spend = excluded.spend",
                                    [ctx.today, str(cid), t["adgroup_id"] or "-", t["term"][:200], app, t.get("source"), t.get("keyword"),
                                     t["impressions"], t["taps"], t["installs"], t["spend"], start, end])
                        n += 1
                except Exception as e:  # noqa: BLE001 - e.g. not a Search Results campaign
                    errors.append(f"{name}: {str(e)[:120]}")
            status = f"{n} term(s) from {len(camps)} campaign(s)" + (f"; errors: {'; '.join(errors[:2])}" if errors else "")
            studio.mark_done(ctx, "search_ad_watch")
    latest = con.execute(f"SELECT max(snapshot_date) FROM {SCHEMA}.search_terms").fetchone()[0]
    add, negative = [], []
    if latest is not None:
        cur = con.execute(f"SELECT app, term, source, keyword, taps, installs, spend FROM {SCHEMA}.search_terms WHERE snapshot_date = ?", [latest])
        for app, term, source, keyword, taps, installs, spend in cur.fetchall():
            row = {"app": app, "term": term, "taps": int(taps or 0), "installs": round(float(installs or 0), 1),
                   "spend": round(float(spend or 0), 2), "matched_keyword": keyword, "source": source}
            already = keyword and keyword.strip().lower() == term.strip().lower()
            if not already and (installs or 0) >= 1 and (not taps or (spend or 0) / taps <= rules.max_cpt):
                row["cost_per_install"] = round((spend or 0) / installs, 2) if installs else None
                add.append(row)
            elif (installs or 0) == 0 and (taps or 0) >= int(rules.zero_install_taps):
                negative.append(row)
    add.sort(key=lambda r: (-r["installs"], r.get("cost_per_install") or 0))
    negative.sort(key=lambda r: -r["spend"])
    return {"fetch": status or "fetched today already", "snapshot": latest, "add_as_keyword": add[:10], "negative_candidates": negative[:10]}


def competitor_ad_spy(ctx) -> dict:
    terms = [t.strip() for t in str(ctx.environ.get("AGENTS_COMPETITOR_TERMS") or "").split(",") if t.strip()]
    if not terms:
        return {"status": "off", "detail": "Set AGENTS_COMPETITOR_TERMS (e.g. 'nclex,ccrn exam') to watch competitors' ads"}
    get = ctx.client("meta_get")
    if get is None:
        return {"status": "not configured", "detail": "Needs META_ACCESS_TOKEN with Ad Library API access"}
    con = ctx.con
    if studio.due(ctx, "competitor_ad_spy", 7) or studio.forced(ctx):
        countries = [c.strip().upper() for c in str(ctx.environ.get("AGENTS_COMPETITOR_COUNTRIES") or "GB").split(",") if c.strip()]
        n, errors = 0, []
        for term in terms[:5]:
            try:
                body = get("ads_archive", {"search_terms": term, "ad_reached_countries": str(countries).replace("'", '"'),
                                           "ad_active_status": "ACTIVE", "ad_type": "ALL", "limit": 50,
                                           "fields": "id,page_name,ad_creative_bodies,ad_creative_link_titles,ad_delivery_start_time"})
                for ad in body.get("data") or []:
                    con.execute(f"INSERT INTO {SCHEMA}.competitor_ads VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ad_id) DO UPDATE SET "
                                "fetched_at = excluded.fetched_at",
                                [str(ad.get("id")), ad.get("page_name"), ((ad.get("ad_creative_bodies") or [None])[0] or "")[:2000],
                                 ((ad.get("ad_creative_link_titles") or [None])[0] or "")[:300],
                                 str(ad.get("ad_delivery_start_time") or "")[:10] or None, term, ctx.now])
                    n += 1
            except Exception as e:  # noqa: BLE001 - token without Ad Library access, etc.
                errors.append(f"{term}: {str(e)[:150]}")
        studio.mark_done(ctx, "competitor_ad_spy")
        if errors and not n:
            return {"status": "failed", "detail": "; ".join(errors[:2])}
    rows = con.execute(f"SELECT page_name, title, body, search_term FROM {SCHEMA}.competitor_ads WHERE fetched_at >= ? "
                       "ORDER BY fetched_at DESC LIMIT 20", [ctx.now - dt.timedelta(days=14)]).fetchall()
    pages: dict = {}
    for p, *_ in rows:
        pages[p] = pages.get(p, 0) + 1
    return {"status": "ok", "terms": terms, "ads_seen": len(rows), "advertisers": pages,
            "examples": [{"page": p, "title": t, "body": (b or "")[:200], "term": s} for p, t, b, s in rows[:5]]}


VOICE_SYSTEM = ("You analyse app store reviews and search terms for a small business that makes nursing exam-prep apps. "
                "For the app given, return {\"likes\": [...], \"complaints\": [...], \"words_people_use\": [...]}. "
                "Each item is {\"point\": short plain sentence, \"evidence\": [ids]} where ids are review ids or 'term:<search term>' "
                "copied exactly from the input. At most 4 items per list. Never invent evidence, numbers or quotes.")


def customer_voice(ctx) -> dict:
    import dlt_pipeline as bot
    import json
    con = ctx.con
    if not (studio.due(ctx, "customer_voice", 7) or studio.forced(ctx)):
        return {"status": "weekly", "latest": {i["app"]: i["item_id"] for i in studio.items(con, kind="voice", status=studio.OPEN)}}
    if not ctx.llm:
        return {"status": "no Claude key", "latest": {}}
    made = {}
    for app in bot.APPS:
        revs = con.execute(f"SELECT review_id, rating, coalesce(title, '') || ' ' || coalesce(body, '') FROM {SCHEMA}.reviews "
                           "WHERE app = ? ORDER BY created_at DESC LIMIT 60", [app]).fetchall()
        terms = con.execute(f"SELECT term, taps, installs FROM {SCHEMA}.search_terms WHERE app = ? AND snapshot_date = "
                            f"(SELECT max(snapshot_date) FROM {SCHEMA}.search_terms) ORDER BY taps DESC LIMIT 40", [app]).fetchall()
        if not revs and not terms:
            continue
        ids = {r[0] for r in revs} | {f"term:{t[0]}" for t in terms}
        prompt = json.dumps({"app": app, "reviews": [{"id": a, "rating": b, "text": (c or "").strip()[:500]} for a, b, c in revs],
                             "search_terms": [{"id": f"term:{t}", "taps": tp, "installs": ins} for t, tp, ins in terms]})
        out = ctx.llm.complete_json("customer_voice", VOICE_SYSTEM, prompt, max_tokens=1200)
        if not isinstance(out, dict):
            continue
        clean = {k: [p for p in (out.get(k) or []) if isinstance(p, dict) and p.get("point")
                     and p.get("evidence") and all(e in ids for e in p["evidence"])][:4]
                 for k in ("likes", "complaints", "words_people_use")}
        if not any(clean.values()):
            continue
        studio.supersede(con, "voice", app)
        made[app] = studio.add(con, "voice", app, "customer_voice", f"What {app} customers say", clean, now=ctx.now)
    studio.mark_done(ctx, "customer_voice")
    return {"status": "written", "latest": made}
