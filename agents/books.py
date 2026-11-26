"""
Book Ads (phase 10): Amazon Ads (Sponsored Products) for your KDP books. READ-ONLY, and off until you add keys.

  Setup         AMAZON_ADS_CLIENT_ID, AMAZON_ADS_CLIENT_SECRET, AMAZON_ADS_REFRESH_TOKEN (Login with Amazon, from an
                approved Amazon Ads API app), optional AMAZON_ADS_PROFILE_ID and AMAZON_ADS_REGION (na | eu | fe; na).
  Reports       Amazon makes reports asynchronously (minutes to hours). Once a day this agent asks for two daily
                reports for the last 14 days: campaigns, and search terms. A later run downloads them when ready.
  Storage       control.book_ads and control.book_search_terms, apart from the app ads on purpose: book spend never
                enters the app budgets, the Rules tab limits or the decision engine.
  Analysis      spend, sales and ACoS (spend ÷ sales) per campaign; search terms that sold a book but aren't keywords
                (add as exact) and ones with many clicks and no sale (add as negatives). Weekly, one "book" draft in
                the Studio with both lists and a checklist. Nothing is ever sent to Amazon: drafts only.

Amazon's "sales" are what the shopper paid, not your royalty. KDP royalties are a share of that, so a book can lose
money at an ACoS that looks fine. KENP page reads (Kindle Unlimited) are reported separately when Amazon includes them.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging

from . import studio

log = logging.getLogger("adbot.agents.books")

HOSTS = {"na": "https://advertising-api.amazon.com", "eu": "https://advertising-api-eu.amazon.com",
         "fe": "https://advertising-api-fe.amazon.com"}
TOKEN_URLS = {"na": "https://api.amazon.com/auth/o2/token", "eu": "https://api.amazon.co.uk/auth/o2/token",
              "fe": "https://api.amazon.co.jp/auth/o2/token"}
DAYS = 14
NEG_CLICKS = 15
STATE = "books.pending"

CAMPAIGN_COLS = ["date", "campaignId", "campaignName", "impressions", "clicks", "cost", "purchases7d", "sales7d", "unitsSoldClicks7d"]
KENP_COLS = ["kindleEditionNormalizedPagesRead14d"]
TERM_COLS = ["date", "campaignId", "campaignName", "adGroupId", "keyword", "searchTerm", "impressions", "clicks", "cost",
             "purchases7d", "sales7d"]


def configured(env) -> bool:
    return all((env.get(k) or "").strip() for k in ("AMAZON_ADS_CLIENT_ID", "AMAZON_ADS_CLIENT_SECRET", "AMAZON_ADS_REFRESH_TOKEN"))


class AmazonAds:
    """A read-only client: profiles, report requests, report status and download. No create/update/delete calls."""

    def __init__(self, env, http=None):
        import requests
        self.http = http or requests
        self.region = (env.get("AMAZON_ADS_REGION") or "na").strip().lower()
        self.host = HOSTS.get(self.region, HOSTS["na"])
        self.client_id = env["AMAZON_ADS_CLIENT_ID"].strip()
        self.secret = env["AMAZON_ADS_CLIENT_SECRET"].strip()
        self.refresh = env["AMAZON_ADS_REFRESH_TOKEN"].strip()
        self.profile_id = (env.get("AMAZON_ADS_PROFILE_ID") or "").strip() or None
        self._token = None

    def token(self) -> str:
        if not self._token:
            r = self.http.post(TOKEN_URLS.get(self.region, TOKEN_URLS["na"]), data={
                "grant_type": "refresh_token", "refresh_token": self.refresh, "client_id": self.client_id,
                "client_secret": self.secret}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"Login with Amazon refused the refresh token (HTTP {r.status_code})")
            self._token = r.json()["access_token"]
        return self._token

    def _headers(self, extra=None, scope=True):
        h = {"Amazon-Advertising-API-ClientId": self.client_id, "Authorization": f"Bearer {self.token()}"}
        if scope and self.profile_id:
            h["Amazon-Advertising-API-Scope"] = str(self.profile_id)
        return {**h, **(extra or {})}

    def profiles(self) -> list[dict]:
        r = self.http.get(f"{self.host}/v2/profiles", headers=self._headers(scope=False), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Amazon Ads profiles: HTTP {r.status_code}")
        return r.json()

    def request_report(self, name: str, start: dt.date, end: dt.date, report_type: str, group_by: list, columns: list) -> str:
        body = {"name": name, "startDate": start.isoformat(), "endDate": end.isoformat(),
                "configuration": {"adProduct": "SPONSORED_PRODUCTS", "groupBy": group_by, "columns": columns,
                                  "reportTypeId": report_type, "timeUnit": "DAILY", "format": "GZIP_JSON"}}
        r = self.http.post(f"{self.host}/reporting/reports", json=body, timeout=60, headers=self._headers(
            {"Content-Type": "application/vnd.createasyncreportrequest.v3+json", "Accept": "application/vnd.createasyncreportresponse.v3+json"}))
        if r.status_code == 425:                     # the same report was asked for already: Amazon names its id
            try:
                detail = str((r.json() or {}).get("detail", ""))
            except ValueError:
                detail = ""
            if ":" in detail:
                return detail.rsplit(":", 1)[1].strip()
        if r.status_code not in (200, 202):
            raise RuntimeError(f"Amazon Ads report request: HTTP {r.status_code} {r.text[:200]}")
        return r.json()["reportId"]

    def report(self, report_id: str) -> dict:
        r = self.http.get(f"{self.host}/reporting/reports/{report_id}", headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Amazon Ads report status: HTTP {r.status_code}")
        return r.json()

    def download(self, url: str) -> list[dict]:
        r = self.http.get(url, timeout=120)          # a pre-signed link: no Amazon Ads headers
        if r.status_code != 200:
            raise RuntimeError(f"Amazon Ads report download: HTTP {r.status_code}")
        return json.loads(gzip.decompress(r.content))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def store(con, kind: str, rows: list[dict], profile_id: str, currency: str | None, now: dt.datetime) -> int:
    n = 0
    for r in rows:
        if kind == "campaigns":
            con.execute("INSERT OR REPLACE INTO control.book_ads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [r.get("date"), profile_id, str(r.get("campaignId")), r.get("campaignName"), _f(r.get("impressions")),
                         _f(r.get("clicks")), _f(r.get("cost")), _f(r.get("purchases7d")), _f(r.get("sales7d")),
                         _f(r.get("unitsSoldClicks7d")), _f(r.get("kindleEditionNormalizedPagesRead14d")), currency, now])
        else:
            con.execute("INSERT OR REPLACE INTO control.book_search_terms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [r.get("date"), profile_id, str(r.get("campaignId")), str(r.get("adGroupId") or ""),
                         str(r.get("keyword") or ""), str(r.get("searchTerm") or "").lower(), r.get("campaignName"),
                         _f(r.get("impressions")), _f(r.get("clicks")), _f(r.get("cost")), _f(r.get("purchases7d")),
                         _f(r.get("sales7d")), now])
        n += 1
    return n


def fetch(ctx, client) -> dict:
    """Download finished reports; ask for new ones once a day. Returns what happened."""
    from control import state as cstate
    con = ctx.con
    if not client.profile_id:
        profs = client.profiles()
        if not profs:
            return {"status": "no Amazon Ads profile on this login"}
        client.profile_id = str(profs[0]["profileId"])
    pending = cstate.get(con, STATE) or {}
    got, notes = {}, []
    for kind, p in list(pending.items()):
        try:
            st = client.report(p["id"])
        except Exception as e:  # noqa: BLE001 - try again next run
            notes.append(f"{kind}: {e}")
            continue
        if st.get("status") == "COMPLETED" and st.get("url"):
            got[kind] = store(con, kind, client.download(st["url"]), p["profile"], p.get("currency"), ctx.now)
            pending.pop(kind)
        elif st.get("status") == "FAILED":
            notes.append(f"{kind}: Amazon couldn't make the report ({st.get('failureReason') or 'no reason given'})")
            pending.pop(kind)
    asked = []
    if ctx.send and not pending and (studio.due(ctx, "book_ads_request", 1) or studio.forced(ctx)):
        end = ctx.today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=DAYS - 1)
        currency = (ctx.environ.get("AMAZON_ADS_CURRENCY") or "USD").upper()
        try:
            rid = client.request_report(f"AdBot campaigns {end}", start, end, "spCampaigns", ["campaign"], CAMPAIGN_COLS + KENP_COLS)
        except RuntimeError as e:
            if "400" not in str(e):
                raise
            rid = client.request_report(f"AdBot campaigns {end}", start, end, "spCampaigns", ["campaign"], CAMPAIGN_COLS)
        pending["campaigns"] = {"id": rid, "profile": client.profile_id, "currency": currency, "asked": ctx.now.isoformat()}
        pending["terms"] = {"id": client.request_report(f"AdBot search terms {end}", start, end, "spSearchTerm", ["searchTerm"], TERM_COLS),
                            "profile": client.profile_id, "currency": currency, "asked": ctx.now.isoformat()}
        asked = ["campaigns", "terms"]
        studio.mark_done(ctx, "book_ads_request")
    if ctx.send:
        cstate.put(con, STATE, pending, by="agents")
    return {"status": "ok", "profile": client.profile_id, "downloaded": got, "requested": asked,
            "waiting": sorted(pending), "notes": notes}


def analyse(con, today: dt.date) -> dict:
    """Spend, sales and ACoS per campaign; search terms to add and to exclude. Pure SQL on the stored rows."""
    end = today - dt.timedelta(days=1)
    out = {"window_end": end.isoformat()}
    for label, days in (("yesterday", 1), ("7d", 7), ("30d", 30)):
        s = end - dt.timedelta(days=days - 1)
        cost, sales, orders, kenp, ccy = con.execute(
            "SELECT sum(cost), sum(sales), sum(purchases), sum(kenp_pages), any_value(currency) FROM control.book_ads "
            "WHERE date BETWEEN ? AND ?", [s, end]).fetchone()
        out[label] = {"spend": round(cost or 0, 2), "sales": round(sales or 0, 2), "orders": int(orders or 0),
                      "kenp_pages": int(kenp) if kenp is not None else None, "currency": ccy,
                      "acos": round(cost / sales, 3) if cost and sales else None}
    s30 = end - dt.timedelta(days=29)
    out["campaigns"] = [{"campaign": n, "spend": round(c or 0, 2), "sales": round(sa or 0, 2), "orders": int(o or 0),
                         "acos": round(c / sa, 3) if c and sa else None}
                        for n, c, sa, o in con.execute(
                            "SELECT any_value(campaign_name), sum(cost), sum(sales), sum(purchases) FROM control.book_ads "
                            "WHERE date BETWEEN ? AND ? GROUP BY campaign_id ORDER BY 2 DESC NULLS LAST LIMIT 12", [s30, end]).fetchall()]
    terms = con.execute(
        "SELECT any_value(campaign_name), search_term, list(DISTINCT lower(keyword)), sum(clicks), sum(cost), sum(purchases) "
        "FROM control.book_search_terms WHERE date BETWEEN ? AND ? GROUP BY campaign_id, search_term", [s30, end]).fetchall()
    harvest, neg = {}, {}
    for camp, term, kws, clicks, cost, buys in terms:
        if (buys or 0) >= 1 and term not in (kws or []) and not term.startswith("b0"):      # b0…: an ASIN, not a phrase
            harvest.setdefault(camp, []).append(term)
        elif (buys or 0) == 0 and (clicks or 0) >= NEG_CLICKS:
            neg.setdefault(camp, []).append(term)
    out["harvest"] = {k: v[:20] for k, v in harvest.items()}
    out["negatives"] = {k: v[:20] for k, v in neg.items()}
    return out


def book_ads(ctx) -> dict:
    env = ctx.environ
    if not configured(env):
        return {"status": "off", "setup": "Add AMAZON_ADS_CLIENT_ID, AMAZON_ADS_CLIENT_SECRET and AMAZON_ADS_REFRESH_TOKEN "
                                          "on the adbot-hourly job (Amazon Ads API access needs Amazon's approval first)."}
    client = ctx.client("amazon_ads")
    fetched = fetch(ctx, client) if client else {"status": "client not built"}
    a = analyse(ctx.con, ctx.today)
    drafted = None
    if ctx.send and (a["harvest"] or a["negatives"]) and (studio.due(ctx, "book_ads_draft", 7) or studio.forced(ctx)):
        studio.supersede(ctx.con, "book", None)
        n_add, n_neg = sum(map(len, a["harvest"].values())), sum(map(len, a["negatives"].values()))
        drafted = studio.add(ctx.con, "book", None, "book_ads", f"Books · {n_add} keyword(s) to add, {n_neg} to exclude",
                             {"harvest": a["harvest"], "negatives": a["negatives"], "window_end": a["window_end"],
                              "launch": "Draft only: nothing is sent to Amazon. Use the checklist to add them yourself."}, now=ctx.now)
        studio.mark_done(ctx, "book_ads_draft")
    return {"status": "on", "fetch": fetched, "drafted": drafted, **a}

