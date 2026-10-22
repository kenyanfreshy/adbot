"""
AdBot control panel (Streamlit) for Rapid Nursing Review, CCRN Adult and NurseBound.

Reads the performance table that dlt_pipeline.py loads (Apple Search Ads, Meta Ads,
Google Ads), shows KPIs / ACoS / budget plan, and lets you edit the bot rules and
pause or protect keywords. Rules and overrides are stored in the same DuckDB /
MotherDuck database, so the scheduled pipeline picks them up on its next run.

Run locally:   streamlit run app.py
Environment:   APP_PASSWORD (required), MOTHERDUCK_TOKEN or DUCKDB_PATH,
               TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN — see render.yaml.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import dlt_pipeline as bot

st.set_page_config(page_title="AdBot", page_icon="static/icon-192.png", layout="wide", initial_sidebar_state="collapsed")

# Phone polish: home-screen icon/name and tighter spacing on small screens
st.markdown("""
<style>
@media (max-width: 640px){
  .block-container{padding:1rem .75rem 4rem}
  h2{font-size:1.4rem}
  [data-testid="stMetricValue"]{font-size:1.35rem}
  [data-testid="stHorizontalBlock"]{gap:.5rem}
}
</style>
""", unsafe_allow_html=True)
# runs in a same-origin iframe, so it can add the home-screen tags to the main page
components.html("""
<script>
(function(){ try{
  const d = window.parent.document, h = d.head;
  const add = (tag, attrs) => { const e = d.createElement(tag); Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k,v)); h.appendChild(e); };
  if(!d.querySelector('link[rel="apple-touch-icon"]')){
    add("link", {rel:"apple-touch-icon", href:"app/static/apple-touch-icon.png"});
    add("meta", {name:"apple-mobile-web-app-title", content:"AdBot"});
    add("meta", {name:"apple-mobile-web-app-capable", content:"yes"});
    add("meta", {name:"mobile-web-app-capable", content:"yes"});
    add("meta", {name:"theme-color", content:"#0c0911"});
  }
} catch(e){} })();
</script>
""", height=0)

APP_COLORS = {"Rapid Nursing Review": "#ff6a2b", "CCRN Adult": "#2dd4bf", "NurseBound": "#8b5cf6", "Unassigned": "#64748b"}


# --------------------------------------------------------------------------- #
# Authentication gate
# --------------------------------------------------------------------------- #
def check_password() -> bool:
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        st.error("Set the APP_PASSWORD environment variable to open the control panel.")
        st.stop()
    ok_until = st.session_state.get("auth_until", 0)
    if ok_until > time.time():
        return True
    st.markdown("### 🔒 AdBot control panel")
    with st.form("login"):
        pw = st.text_input("Password or PIN", type="password")
        submitted = st.form_submit_button("Unlock")
    if submitted:
        # constant-time comparison of hashes, so length and content don't leak through timing
        if hmac.compare_digest(hashlib.sha256(pw.encode()).digest(), hashlib.sha256(expected.encode()).digest()):
            st.session_state["auth_until"] = time.time() + 12 * 3600
            st.rerun()
        time.sleep(1.0)  # slow down guessing
        st.error("That password isn't right.")
    st.stop()


check_password()


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_con():
    return bot.connect()


con = get_con()


@st.cache_data(ttl=300)
def load_perf(days: int) -> pd.DataFrame:
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    try:
        df = con.execute(f"select * from {bot.DATASET}.{bot.TABLE} where cast(date as varchar) >= ?", [since]).fetch_df()
    except Exception:
        return pd.DataFrame(columns=["date", "channel", "entity_id", "entity_type", "keyword", "campaign_id", "campaign", "adgroup_id",
                                     "app", "status", "impressions", "taps", "installs", "spend", "revenue"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("impressions", "taps", "installs", "spend", "revenue"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def save_rules(rules: bot.Rules) -> None:
    d = asdict(rules)
    d.pop("overrides", None)  # overrides live in their own table
    con.execute("insert into adbot_rules values (?, ?)", [dt.datetime.utcnow(), json.dumps(d)])


def set_override(key: str, status: str | None, note: str = "") -> None:
    if status in (None, "", "auto"):
        con.execute("delete from keyword_overrides where entity_key = ?", [key])
    else:
        con.execute("insert or replace into keyword_overrides values (?, ?, ?, ?)", [key, status, note, dt.datetime.utcnow()])


rules = bot.load_rules(con)
dry_run = os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")

# --------------------------------------------------------------------------- #
# Header & filters
# --------------------------------------------------------------------------- #
left, right = st.columns([3, 2])
with left:
    st.markdown("## 🤖 AdBot Manager")
    st.caption("Rapid Nursing Review · CCRN Adult · NurseBound — Apple Search Ads, Meta Ads, Google Ads")
with right:
    st.markdown(f"**Mode:** {'🧪 Dry run (no changes are made)' if dry_run else '🟢 Live (bot can pause and change budgets)'}")
    if st.button("🔓 Lock", help="End this session"):
        st.session_state.pop("auth_until", None)
        st.rerun()

f1, f2, f3 = st.columns([2, 2, 1])
app_sel = f1.multiselect("Apps", bot.APPS + ["Unassigned"], default=bot.APPS)
chan_sel = f2.multiselect("Channels", bot.CHANNELS, default=bot.CHANNELS)
days = f3.selectbox("Period", [7, 14, 30, 90], index=1, format_func=lambda d: f"Last {d} days")

perf = load_perf(days * 2)
cut = dt.date.today() - dt.timedelta(days=days - 1)
sel = perf[perf["app"].isin(app_sel) & perf["channel"].isin(chan_sel)]
cur, prev = sel[sel["date"] >= cut], sel[sel["date"] < cut]

tab_dash, tab_kw, tab_rules, tab_log, tab_trade = st.tabs(["📈 Dashboard", "🔑 Keywords", "🛡️ Bot rules & safety", "📜 Bot log", "💹 Trading"])


def totals(df: pd.DataFrame) -> dict:
    sp, rev, taps, ins = df["spend"].sum(), df["revenue"].sum(), df["taps"].sum(), df["installs"].sum()
    if not rev and ins:
        # estimated revenue from value-per-install where platforms report none
        est = df.groupby("app")["installs"].sum()
        rev = sum(v * float(rules.value_per_install.get(a, 0) or 0) for a, v in est.items())
    return {"spend": sp, "taps": taps, "installs": ins, "revenue": rev,
            "acos": sp / rev if rev else None, "cpt": sp / taps if taps else None, "roas": rev / sp if sp else None}


def delta(a, b, pct=True):
    if a is None or b in (None, 0):
        return None
    return f"{(a - b) / abs(b) * 100:+.1f}%" if pct else f"{(a - b):+.2f}"


# --------------------------------------------------------------------------- #
# Dashboard tab
# --------------------------------------------------------------------------- #
with tab_dash:
    if cur.empty:
        st.info("No ad data yet. Add API credentials for at least one channel (see render.yaml), then run "
                "`python dlt_pipeline.py` or wait for the scheduled job.")
    T, P = totals(cur), totals(prev)
    k = st.columns(6)
    k[0].metric("Total spend", f"${T['spend']:,.2f}", delta(T["spend"], P["spend"]), delta_color="inverse")
    k[1].metric("Taps / clicks", f"{T['taps']:,.0f}", delta(T["taps"], P["taps"]))
    k[2].metric("Installs", f"{T['installs']:,.0f}", f"{(T['installs'] / T['taps'] * 100):.1f}% tap→install" if T["taps"] else None, delta_color="off")
    k[3].metric("Revenue", f"${T['revenue']:,.2f}", f"{T['roas']:.2f}× ROAS" if T["roas"] else None, delta_color="off")
    k[4].metric("ACoS", f"{T['acos'] * 100:.1f}%" if T["acos"] else "—", f"target < {rules.target_acos * 100:.0f}%", delta_color="off")
    k[5].metric("Avg CPT", f"${T['cpt']:.2f}" if T["cpt"] else "—", delta(T["cpt"], P["cpt"]), delta_color="inverse")

    c1, c2 = st.columns([1, 2])
    with c1:
        st.markdown("#### ⚖️ Bot budget allocation")
        per_app = {a: {"spend": g["spend"].sum(), "revenue": g["revenue"].sum(), "installs": g["installs"].sum()} for a, g in cur.groupby("app")}
        plan = bot.rebalance(per_app, rules)
        alloc = pd.DataFrame([{"app": a, "daily": v["daily"], "share": v["share"]} for a, v in plan.items()])
        st.altair_chart(alt.Chart(alloc).mark_arc(innerRadius=55).encode(
            theta="daily:Q", color=alt.Color("app:N", scale=alt.Scale(domain=list(APP_COLORS), range=list(APP_COLORS.values())), legend=None),
            tooltip=["app", alt.Tooltip("daily:Q", format="$.2f"), alt.Tooltip("share:Q", format=".0%")]).properties(height=220), use_container_width=True)
        for _, r in alloc.iterrows():
            st.markdown(f"<span style='color:{APP_COLORS[r.app]}'>●</span> **{r.app}** — {r.share * 100:.0f}% · ${r.daily:.2f}/day", unsafe_allow_html=True)
        st.caption(f"Cap ${rules.global_daily_cap:.2f}/day ≈ ${rules.global_daily_cap * 30.4:,.0f}/month. "
                   + ("Auto-rebalance is on." if rules.auto_rebalance else "Auto-rebalance is off; this is the plan it would use."))
    with c2:
        st.markdown("#### 📊 Taps, installs & spend")
        daily = cur.groupby("date")[["taps", "installs", "spend"]].sum().reset_index()
        if not daily.empty:
            long = daily.melt("date", var_name="metric", value_name="value")
            st.altair_chart(alt.Chart(long).mark_line(point=True).encode(
                x="date:T", y="value:Q", color=alt.Color("metric:N", scale=alt.Scale(domain=["taps", "installs", "spend"], range=["#8b5cf6", "#34d399", "#fbbf24"])),
                tooltip=["date:T", "metric", alt.Tooltip("value:Q", format=",.2f")]).properties(height=280), use_container_width=True)
        by_app = cur.groupby(["app", "channel"])[["spend", "taps", "installs", "revenue"]].sum().reset_index()
        if not by_app.empty:
            by_app["ACoS %"] = (by_app["spend"] / by_app["revenue"].where(by_app["revenue"] > 0) * 100).round(1)
            by_app["CPT"] = (by_app["spend"] / by_app["taps"].where(by_app["taps"] > 0)).round(2)
            st.dataframe(by_app, hide_index=True, use_container_width=True)

    # NurseBound country targeting
    if "NurseBound" in app_sel and rules.geo_targets.get("NurseBound"):
        st.markdown("#### 🌍 NurseBound country targeting")
        st.caption("Countries nurses come from on their way to the US, Canada, Australia and New Zealand. "
                   "Put the country code in each campaign or ad set name (e.g. “NurseBound | PH | Prospecting”) so spend is tracked by country.")
        nb = cur[cur["app"] == "NurseBound"].copy()
        nb["country"] = [bot.country_for(f"{c or ''} {k or ''}", "NurseBound", rules) for c, k in zip(nb["campaign"], nb["keyword"])]
        per_c = {c: {"spend": g["spend"].sum(), "revenue": g["revenue"].sum(), "installs": g["installs"].sum(), "taps": g["taps"].sum()} for c, g in nb.groupby("country")}
        nb_daily = bot.rebalance({a: {"spend": g["spend"].sum(), "revenue": g["revenue"].sum(), "installs": g["installs"].sum()} for a, g in cur.groupby("app")}, rules).get("NurseBound", {}).get("daily", 0)
        split = bot.geo_split("NurseBound", nb_daily, per_c, rules)
        notes = {"who_safeguards": "On WHO's 2023 safeguards list: advertise the study app, not recruitment",
                 "china_reach": "Meta and Google ads don't serve in mainland China"}
        geo_df = pd.DataFrame([{"Country": g["code"], "Tier": g["tier"], "On": g.get("enabled", True), "Weight": g["weight"],
                                "Plan $/day": split.get(g["code"], 0.0), "Spend": per_c.get(g["code"], {}).get("spend", 0.0),
                                "Installs": per_c.get(g["code"], {}).get("installs", 0.0), "Going to": ", ".join(g.get("dest", [])),
                                "Note": notes.get(g.get("flag"), "") + (" · under $1/day: most platforms need more" if 0 < split.get(g["code"], 0) < 1 else "")}
                               for g in rules.geo_targets["NurseBound"]])
        st.dataframe(geo_df, hide_index=True, use_container_width=True,
                     column_config={"Plan $/day": st.column_config.NumberColumn(format="$%.2f"), "Spend": st.column_config.NumberColumn(format="$%.2f")})
        untagged = nb[nb["country"].isna()]["spend"].sum()
        if untagged:
            st.caption(f"${untagged:,.2f} of NurseBound spend is in campaigns without a country code.")

# --------------------------------------------------------------------------- #
# Keywords tab
# --------------------------------------------------------------------------- #
with tab_kw:
    agg = bot.aggregate(cur.to_dict("records"), rules)
    kw = pd.DataFrame(agg.values()) if agg else pd.DataFrame()
    if kw.empty:
        st.info("No keywords or ad sets in this period.")
    else:
        kw["key"] = kw["channel"] + "|" + kw["entity_id"].astype(str)
        decisions = [bot.keyword_decision(r, rules) for r in agg.values()]
        kw["bot"] = [d[0] for d in decisions]
        kw["reason"] = [d[1] for d in decisions]
        kw["override"] = kw["key"].map(rules.overrides).fillna("auto")
        kw["ACoS %"] = (kw["acos"] * 100).round(1)
        q = st.text_input("Search keywords, ad sets or campaigns")
        if q:
            m = kw["keyword"].fillna("").str.contains(q, case=False) | kw["campaign"].fillna("").str.contains(q, case=False)
            kw = kw[m]
        view = kw[["override", "bot", "reason", "app", "channel", "keyword", "campaign", "taps", "installs", "spend", "cpt", "revenue", "ACoS %", "key"]].sort_values("spend", ascending=False)
        edited = st.data_editor(
            view, hide_index=True, use_container_width=True, disabled=[c for c in view.columns if c != "override"],
            column_config={
                "override": st.column_config.SelectboxColumn("Override", options=["auto", "paused", "protected"], help="auto = bot decides; paused = always pause; protected = never auto-pause"),
                "bot": st.column_config.TextColumn("Bot decision"), "cpt": st.column_config.NumberColumn("CPT", format="$%.2f"),
                "spend": st.column_config.NumberColumn("Spend", format="$%.2f"), "revenue": st.column_config.NumberColumn("Revenue", format="$%.2f"),
                "key": None,
            }, key="kw_editor")
        changed = edited[edited["override"] != view["override"]]
        if not changed.empty and st.button(f"Save {len(changed)} override(s)", type="primary"):
            for _, r in changed.iterrows():
                set_override(r["key"], r["override"])
            st.success("Saved. The bot applies them on its next run.")
            st.rerun()

# --------------------------------------------------------------------------- #
# Rules tab
# --------------------------------------------------------------------------- #
with tab_rules:
    with st.form("rules"):
        st.markdown("#### 💵 Daily spend hard limits")
        g = st.number_input("Portfolio cap ($/day, all apps)", 0.0, 10000.0, float(rules.global_daily_cap), 0.5)
        cols = st.columns(3)
        caps = {a: cols[i].number_input(f"{a} cap ($/day)", 0.0, 10000.0, float(rules.app_caps.get(a, 0)), 0.5) for i, a in enumerate(bot.APPS)}
        st.markdown("#### 🛡️ Kill-switch & safeguards")
        c = st.columns(3)
        max_cpt = c[0].number_input("Pause when CPT is above ($)", 0.0, 100.0, float(rules.max_cpt), 0.05)
        min_taps = c[1].number_input("…after at least (taps)", 0, 1000, int(rules.min_taps_for_cpt))
        zero = c[2].number_input("Pause with 0 installs after (taps)", 1, 10000, int(rules.zero_install_taps))
        c = st.columns(3)
        max_acos = c[0].number_input("Pause when ACoS is above (%)", 0.0, 1000.0, float(rules.max_acos * 100), 1.0)
        min_sp = c[1].number_input("…once spend reaches ($)", 0.0, 10000.0, float(rules.min_spend_for_acos), 1.0)
        target = c[2].number_input("Target ACoS (%)", 0.0, 1000.0, float(rules.target_acos * 100), 1.0)
        st.markdown("#### ⚖️ Rebalancing")
        c = st.columns(4)
        auto = c[0].toggle("Auto-rebalance budget", value=bool(rules.auto_rebalance))
        mn = c[1].number_input("Min share per app (%)", 0.0, 100.0, float(rules.min_share * 100), 1.0)
        mx = c[2].number_input("Max share per app (%)", 0.0, 100.0, float(rules.max_share * 100), 1.0)
        step = c[3].number_input("Max change per run (%)", 0.0, 100.0, float(rules.max_budget_step * 100), 1.0)
        cols = st.columns(3)
        prio = {a: cols[i].number_input(f"{a}: priority", 0.0, 10.0, float(rules.app_priority.get(a, 1.0)), 0.25,
                                        help="Multiplies this app's score when splitting the budget; higher gets more") for i, a in enumerate(bot.APPS)}
        cols = st.columns(3)
        vpi = {a: cols[i].number_input(f"{a}: value per install ($)", 0.0, 1000.0, float(rules.value_per_install.get(a, 0) or 0), 0.5,
                                       help="Used to estimate revenue when the ad platform reports none") for i, a in enumerate(bot.APPS)}
        st.markdown("#### 📨 Telegram alerts")
        chat = st.text_input("Chat ID (the bot token stays in the TELEGRAM_BOT_TOKEN env var)", rules.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", ""))
        c = st.columns(4)
        alerts = {k: c[i].checkbox(lbl, value=bool(rules.alerts.get(k, True))) for i, (k, lbl) in enumerate(
            [("daily_summary", "Daily summary"), ("kill_switch", "Kill-switch alerts"), ("rebalance", "Rebalance logs"), ("high_cpt", "High CPT warnings")])}
        if st.form_submit_button("💾 Save rules", type="primary"):
            if mn * len(bot.APPS) > 100 or mx < 100 / len(bot.APPS):
                st.error("Min and max share can't both be met with three apps. Keep min ≤ 33% and max ≥ 34%.")
            else:
                new = bot.Rules.from_dict({**asdict(rules), "global_daily_cap": g, "app_caps": caps, "max_cpt": max_cpt, "min_taps_for_cpt": int(min_taps),
                                           "zero_install_taps": int(zero), "max_acos": max_acos / 100, "min_spend_for_acos": min_sp, "target_acos": target / 100,
                                           "auto_rebalance": auto, "min_share": mn / 100, "max_share": mx / 100, "max_budget_step": step / 100,
                                           "value_per_install": vpi, "app_priority": prio, "telegram_chat_id": chat, "alerts": alerts})
                save_rules(new)
                st.success("Rules saved. The scheduled job uses them on its next run.")
                st.rerun()

    c1, c2, c3 = st.columns(3)
    if c1.button("📨 Send test notification"):
        if bot.telegram("✅ <b>AdBot test</b> from the control panel.", rules):
            st.success("Sent. Check Telegram.")
        else:
            st.error("Not sent. Check TELEGRAM_BOT_TOKEN and the chat ID.")
    if c2.button("▶️ Run bot now (no sync)"):
        res = bot.run_bot(con, rules, dry_run)
        st.json({"paused": res.get("paused", 0), "plan": {a: v["daily"] for a, v in (res.get("plan") or {}).items()}, "dry_run": dry_run})
    up = c3.file_uploader("Import rules.json from the dashboard", type="json")
    if up is not None and st.button("Import these rules"):
        data = json.load(up)
        for key, status in (data.pop("overrides", {}) or {}).items():
            set_override(key, status, "imported")
        save_rules(bot.Rules.from_dict({**asdict(rules), **data}))
        st.success("Imported.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Log tab
# --------------------------------------------------------------------------- #
with tab_log:
    log_df = con.execute("select * from bot_actions order by ts desc limit 500").fetch_df()
    if log_df.empty:
        st.info("The bot hasn't acted yet.")
    else:
        st.dataframe(log_df, hide_index=True, use_container_width=True)
    hist = con.execute("select ts, app, daily from app_budgets order by ts").fetch_df()
    if not hist.empty:
        st.altair_chart(alt.Chart(hist).mark_line(point=True).encode(
            x="ts:T", y=alt.Y("daily:Q", title="$/day"), color=alt.Color("app:N", scale=alt.Scale(domain=list(APP_COLORS), range=list(APP_COLORS.values())))),
            use_container_width=True)


# --------------------------------------------------------------------------- #
# Trading tab (read-only view of what the trading bot reports via trading_status.py)
# --------------------------------------------------------------------------- #
def tq(sql: str, params=None) -> pd.DataFrame:
    try:
        return con.execute(sql, params or []).fetch_df()
    except Exception:
        return pd.DataFrame()


def money(v, sign=False):
    if v is None or pd.isna(v):
        return "—"
    return f"{'+' if sign and v > 0 else ''}{'-' if v < 0 else ''}${abs(v):,.2f}"


def fx_market_open(now_utc: dt.datetime) -> bool:
    """Spot FX trades from Sunday ~22:00 UTC to Friday ~22:00 UTC (21:00 in northern summer)."""
    wd, h = now_utc.weekday(), now_utc.hour
    return not (wd == 5 or (wd == 6 and h < 21) or (wd == 4 and h >= 22))


with tab_trade:
    @st.fragment(run_every="60s")
    def trading_view():
        hb = tq("select * from trading.heartbeat order by ts desc limit 1")
        if hb.empty:
            st.info("No reports from the trading bot yet. Add trading_status.py to the trading bot's monitoring service "
                    "(see the top of that file), set MOTHERDUCK_TOKEN on the trading machine, then run "
                    "`python trading_status.py` there once to test.")
            return
        bots = tq("select distinct bot_id from trading.heartbeat order by 1")["bot_id"].tolist()
        bot = st.selectbox("Bot", bots, index=0) if len(bots) > 1 else bots[0]
        hb = tq("select * from trading.heartbeat where bot_id = ? order by ts desc limit 1", [bot]).iloc[0]
        age = (dt.datetime.utcnow() - pd.Timestamp(hb["ts"]).to_pydatetime()).total_seconds() / 60
        mode = str(hb["mode"]).upper()
        status_ok = age < 10 and str(hb["status"]).lower() in ("running", "ok", "idle", "test")
        st.markdown(f"**{bot}** · {'🧪 PAPER' if mode == 'PAPER' else '🔴 LIVE'} · "
                    + (f"🟢 {hb['status']} · last report {age:.0f} min ago" if status_ok else f"🟠 {hb['status']} · last report {age:.0f} min ago")
                    + (f" · {hb['message']}" if hb["message"] else ""))
        if age > 15 and fx_market_open(dt.datetime.utcnow()):
            st.warning("No report for more than 15 minutes while the FX market is open. Check the trading machine.")

        acct = tq("select * from trading.account where bot_id = ? order by ts desc limit 1", [bot])
        today = dt.datetime.utcnow().date()  # reports are stored in UTC
        start = tq("select * from trading.account where bot_id = ? and cast(ts as date) = ? order by ts limit 1", [bot, today])
        k = st.columns(4)
        if not acct.empty:
            a = acct.iloc[0]
            k[0].metric("Equity", money(a["equity"]), money(a["equity"] - start.iloc[0]["equity"], True) + " today" if not start.empty else None)
            k[1].metric("Today's P&L", money(a["day_pnl"], True))
            k[2].metric("Total P&L", money(a["total_pnl"], True))
            k[3].metric("Margin used", money(a["margin_used"]))
        risk = tq("select * from trading.risk where bot_id = ? order by ts desc limit 1", [bot])
        if not risk.empty:
            r = risk.iloc[0]
            if bool(r["kill_switch"]):
                st.error(f"🛑 Kill switch is ON. {r['notes'] or ''}")
            c = st.columns(2)
            if r["daily_loss_limit"]:
                used = float(r["daily_loss_used"] or 0) / float(r["daily_loss_limit"])
                c[0].markdown(f"**Daily loss limit:** {money(r['daily_loss_used'])} of {money(r['daily_loss_limit'])} used")
                c[0].progress(min(1.0, max(0.0, used)))
            if r["max_exposure"]:
                ex = float(r["exposure"] or 0) / float(r["max_exposure"])
                c[1].markdown(f"**Exposure:** {money(r['exposure'])} of {money(r['max_exposure'])}")
                c[1].progress(min(1.0, max(0.0, ex)))

        eq = tq("select ts, equity from trading.account where bot_id = ? and ts > ? order by ts", [bot, dt.datetime.utcnow() - dt.timedelta(days=30)])
        if len(eq) > 1:
            st.markdown("#### Equity, last 30 days")
            st.altair_chart(alt.Chart(eq).mark_area(line={"color": "#ff6a2b"}, color=alt.Gradient(
                gradient="linear", stops=[alt.GradientStop(color="rgba(255,106,43,.35)", offset=0), alt.GradientStop(color="rgba(255,106,43,0)", offset=1)],
                x1=1, x2=1, y1=0, y2=1)).encode(x="ts:T", y=alt.Y("equity:Q", scale=alt.Scale(zero=False))).properties(height=220), use_container_width=True)

        snap = tq("select max(snapshot_at) as s from trading.positions where bot_id = ?", [bot])
        st.markdown("#### Open positions")
        if snap.empty or pd.isna(snap.iloc[0]["s"]):
            st.caption("No position report yet.")
        else:
            pos = tq("select symbol, side, qty, entry, price, stop, target, unrealized_pnl, opened_at, strategy from trading.positions "
                     "where bot_id = ? and snapshot_at = ? and symbol is not null order by abs(unrealized_pnl) desc", [bot, pd.Timestamp(snap.iloc[0]["s"]).to_pydatetime()])
            if pos.empty:
                st.caption("Flat: no open positions.")
            else:
                st.dataframe(pos, hide_index=True, use_container_width=True,
                             column_config={"unrealized_pnl": st.column_config.NumberColumn("Unrealised P&L", format="$%.2f")})

        st.markdown("#### Today (UTC)")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Closed trades**")
            tr = tq("select closed_at, symbol, side, qty, entry, exit, pnl, strategy, reason from trading.trades "
                    "where bot_id = ? and cast(coalesce(closed_at, opened_at) as date) = ? order by coalesce(closed_at, opened_at) desc", [bot, today])
            if tr.empty:
                st.caption("None yet today.")
            else:
                wins = (tr["pnl"] > 0).sum(); n = tr["pnl"].notna().sum()
                st.caption(f"{n} trades · {wins} winners · {money(tr['pnl'].sum(), True)}")
                st.dataframe(tr, hide_index=True, use_container_width=True, column_config={"pnl": st.column_config.NumberColumn("P&L", format="$%.2f")})
        with c2:
            st.markdown("**Signals and decisions**")
            sg = tq("select ts, symbol, direction, confidence, decision, reason, source from trading.signals "
                    "where bot_id = ? and cast(ts as date) = ? order by ts desc limit 200", [bot, today])
            if sg.empty:
                st.caption("No signals today.")
            else:
                cnt = sg["decision"].value_counts().to_dict()
                st.caption(" · ".join(f"{v} {k}" for k, v in cnt.items()))
                st.dataframe(sg, hide_index=True, use_container_width=True,
                             column_config={"confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1, format="%.2f")})
        st.caption("Read-only. This panel never places, changes or closes trades. Updates every minute.")

    trading_view()
