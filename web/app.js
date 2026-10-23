/* AdBot phone app */
(() => {
  "use strict";
  const APPS = ["Rapid Nursing Review", "CCRN Adult", "NurseBound"];
  const SHORT = {"Rapid Nursing Review": "RNR", "CCRN Adult": "CCRN", "NurseBound": "NurseBound", "Unassigned": "Other"};
  const COLOR = {"Rapid Nursing Review": "var(--rnr)", "CCRN Adult": "var(--ccrn)", "NurseBound": "var(--nb)", "Unassigned": "var(--ink-4)"};
  const HEX = {"Rapid Nursing Review": "#ff6a2b", "CCRN Adult": "#2dd4bf", "NurseBound": "#8b5cf6", "Unassigned": "#5f5770"};
  const TITLES = {ads: "Ads", revenue: "Revenue", keywords: "Keywords", rules: "Bot rules", trading: "Trading", log: "Bot log"};

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const money = (n, d = 2) => n == null || !isFinite(n) ? "—" : (n < 0 ? "-" : "") + "$" + Math.abs(+n).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
  const smoney = n => n == null || !isFinite(n) ? "—" : (n > 0 ? "+" : "") + money(n);
  const num = n => n == null || !isFinite(n) ? "—" : Math.round(+n).toLocaleString("en-US");
  const pct = (n, d = 1) => n == null || !isFinite(n) ? "—" : (n * 100).toFixed(d) + "%";
  const store = {get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
                 set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
  const S = Object.assign({tab: "ads", app: "all", channel: "all", days: 14, kwFilter: "all", q: ""}, store.get("adbot-ui", {}));
  const save = () => store.set("adbot-ui", {tab: S.tab, app: S.app, channel: S.channel, days: S.days, kwFilter: S.kwFilter});

  function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 3000); }

  async function api(path, opts = {}) {
    const r = await fetch(path, {credentials: "same-origin", headers: {"Content-Type": "application/json"}, ...opts});
    let body = null; try { body = await r.json(); } catch (e) {}
    if (r.status === 401 && body && body.error === "signin") { showLogin(); throw new Error("signin"); }
    if (!r.ok) throw new Error((body && body.error) || `Server error ${r.status}`);
    return body;
  }
  const qs = () => `days=${S.days}&app=${encodeURIComponent(S.app)}&channel=${encodeURIComponent(S.channel)}`;

  /* ---------- login ---------- */
  function showLogin() { $("boot").hidden = true; $("app").hidden = true; $("login").hidden = false; setTimeout(() => $("pw").focus(), 50); }
  $("loginForm").addEventListener("submit", async e => {
    e.preventDefault(); const btn = $("loginBtn"); btn.disabled = true; $("loginErr").textContent = "";
    try { await api("/api/login", {method: "POST", body: JSON.stringify({password: $("pw").value})}); $("pw").value = ""; start(); }
    catch (err) { if (err.message !== "signin") $("loginErr").textContent = err.message; }
    finally { btn.disabled = false; }
  });

  /* ---------- shell ---------- */
  let ME = {dry_run: true};
  async function start() {
    try { ME = await api("/api/me"); } catch (e) { if (e.message !== "signin") { $("boot").hidden = true; showLogin(); $("loginErr").textContent = e.message; } return; }
    $("boot").hidden = true; $("login").hidden = true; $("app").hidden = false;
    $("modePill").className = "pill " + (ME.dry_run ? "dry" : "live"); $("modePill").textContent = ME.dry_run ? "DRY RUN" : "LIVE";
    $("modePill").title = ME.dry_run ? "The bot only reports what it would do" : "The bot pauses keywords and changes budgets";
    renderChips(); $("channel").value = S.channel; $("days").value = String(S.days);
    go(S.tab);
  }
  function renderChips() {
    $("appChips").innerHTML = [["all", "All apps"], ...APPS.map(a => [a, SHORT[a]])].map(([v, l]) =>
      `<button class="chip" data-v="${esc(v)}" aria-pressed="${S.app === v}">${v !== "all" ? `<i style="background:${COLOR[v]}"></i>` : ""}${esc(l)}</button>`).join("");
    $("appChips").querySelectorAll("button").forEach(b => b.onclick = () => { S.app = b.dataset.v; save(); renderChips(); load(); });
  }
  $("channel").onchange = () => { S.channel = $("channel").value; save(); load(); };
  $("days").onchange = () => { S.days = +$("days").value; save(); load(); };
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => go(b.dataset.tab));
  $("refresh").onclick = () => load(true);

  let tradingTimer = null;
  function go(tab) {
    S.tab = TITLES[tab] ? tab : "ads"; save();
    document.querySelectorAll(".tabs button").forEach(b => { if (b.dataset.tab === S.tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    $("title").textContent = TITLES[S.tab];
    $("filters").hidden = !(S.tab === "ads" || S.tab === "keywords");
    window.scrollTo({top: 0});
    clearInterval(tradingTimer);
    if (S.tab === "trading") tradingTimer = setInterval(() => { if (document.visibilityState === "visible") load(false, true); }, 60000);
    load();
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !$("app").hidden) load(false, true); });

  let seq = 0;
  async function load(manual, quiet) {
    const my = ++seq; const v = $("view");
    if (!quiet) v.innerHTML = `<div class="skel"></div><div class="skel" style="margin-top:10px"></div><div class="skel" style="margin-top:10px;height:200px"></div>`;
    if (manual) $("refresh").classList.add("spin");
    try {
      const fn = {ads: viewAds, revenue: viewRevenue, keywords: viewKeywords, rules: viewRules, trading: viewTrading, log: viewLog}[S.tab];
      await fn(my);
    } catch (e) {
      if (e.message !== "signin" && my === seq) v.innerHTML = `<div class="banner bad"><b>Couldn't load.</b> ${esc(e.message)}</div>`;
    } finally { $("refresh").classList.remove("spin"); }
  }

  /* ---------- shared bits ---------- */
  function delta(cur, prev, invert) {
    if (cur == null || prev == null || !isFinite(cur) || !isFinite(prev) || prev === 0) return "";
    const v = (cur - prev) / Math.abs(prev) * 100; const good = invert ? v < 0 : v > 0;
    return `<span class="delta ${Math.abs(v) < .05 ? "flat" : good ? "" : "down"}">${v > 0 ? "▲" : "▼"} ${Math.abs(v).toFixed(Math.abs(v) >= 100 ? 0 : 1)}%</span>`;
  }
  const compact = v => Math.abs(v) >= 1e4 ? (v / 1e3).toFixed(Math.abs(v) >= 1e5 ? 0 : 1) + "K" : Math.abs(v) >= 10 ? Math.round(v).toLocaleString("en-US") : (+v).toFixed(v % 1 ? 1 : 0);
  let chartSeq = 0;
  function lineChart(series, keys, colors, rightKey, opts = {}) {
    const W = 340, H = 190, pl = 44, pr = rightKey ? 40 : 8, pt = 10, pb = 22, n = series.length;
    if (!n || !series.some(s => keys.some(k => s[k]))) return `<div class="empty">Nothing to chart yet.</div>`;
    const left = keys.filter(k => k !== rightKey);
    const vals = series.flatMap(s => left.map(k => +s[k] || 0));
    let lo = opts.fit ? Math.min(...vals) : 0, hi = Math.max(1, ...vals);
    if (opts.fit) { const padv = (hi - lo) * .15 || Math.abs(hi) * .01 || 1; lo -= padv; hi += padv; }
    const mL = hi;
    const mR = rightKey ? Math.max(1, ...series.map(s => +s[rightKey] || 0)) : 1;
    const x = i => pl + (n < 2 ? 0 : i / (n - 1)) * (W - pl - pr);
    const y = (v, m) => m === mL ? pt + (H - pt - pb) * (1 - (v - lo) / (hi - lo)) : pt + (H - pt - pb) * (1 - v / m);
    let g = "";
    for (let k = 0; k <= 3; k++) { const yy = pt + (H - pt - pb) * k / 3; g += `<line x1="${pl}" x2="${W - pr}" y1="${yy}" y2="${yy}" stroke="rgba(0,194,255,.09)" stroke-dasharray="2 4"/><text class="axis" x="${pl - 5}" y="${yy + 3.5}" text-anchor="end">${(opts.money ? "$" : "") + compact(hi - (hi - lo) * k / 3)}</text>${rightKey ? `<text class="axis" x="${W - pr + 5}" y="${yy + 3.5}">${money(mR * (1 - k / 3), 0)}</text>` : ""}`; }
    const every = Math.ceil(n / 5); let xl = "";
    for (let i = 0; i < n; i += every) { const d = String(series[i].date || series[i].ts || ""); xl += `<text class="axis" x="${x(i)}" y="${H - 6}" text-anchor="middle">${+d.slice(5, 7)}/${+d.slice(8, 10)}</text>`; }
    const path = k => series.map((s, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(+s[k] || 0, k === rightKey ? mR : mL).toFixed(1)}`).join(" ");
    const id = "c" + (++chartSeq), base = (H - pb).toFixed(1);
    const defs = `<defs><filter id="${id}g" x="-10%" y="-30%" width="120%" height="160%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>${keys.map((k, i) =>
      `<linearGradient id="${id}a${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${colors[i]}" stop-opacity=".28"/><stop offset="1" stop-color="${colors[i]}" stop-opacity="0"/></linearGradient>`).join("")}</defs>`;
    const area = (k, i) => k === rightKey || i > 0 ? "" : `<path d="${path(k)} L${x(n - 1).toFixed(1)},${base} L${x(0).toFixed(1)},${base} Z" fill="url(#${id}a${i})"/>`;
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Chart">${defs}${g}${xl}${keys.map(area).join("")}${keys.map((k, i) => `<path d="${path(k)}" fill="none" stroke="${colors[i]}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" ${k === rightKey ? 'stroke-dasharray="5 4"' : `filter="url(#${id}g)"`} vector-effect="non-scaling-stroke"/>`).join("")}</svg>`;
  }
  function donut(parts) {
    const tot = parts.reduce((s, p) => s + p.v, 0) || 1; let a0 = -Math.PI / 2; const R = 50, r = 34, c = 55;
    const arcs = parts.map(p => { const a1 = a0 + p.v / tot * Math.PI * 2 - .03; const P = (a, rad) => [c + rad * Math.cos(a), c + rad * Math.sin(a)]; const lg = a1 - a0 > Math.PI ? 1 : 0;
      const [x0, y0] = P(a0, R), [x1, y1] = P(a1, R), [x2, y2] = P(a1, r), [x3, y3] = P(a0, r); const d = `M${x0},${y0}A${R},${R} 0 ${lg} 1 ${x1},${y1}L${x2},${y2}A${r},${r} 0 ${lg} 0 ${x3},${y3}Z`; a0 = a1 + .03; return `<path d="${d}" fill="${p.c}"/>`; }).join("");
    return `<svg viewBox="0 0 110 110" aria-hidden="true">${arcs}</svg>`;
  }
  const flag = a2 => /^[A-Z]{2}$/.test(a2) ? String.fromCodePoint(...[...a2].map(ch => 0x1F1E6 + ch.charCodeAt(0) - 65)) : "";
  let regionName = c => c; try { const dn = new Intl.DisplayNames(["en"], {type: "region"}); regionName = c => { try { return dn.of(c) || c; } catch (e) { return c; } }; } catch (e) {}

  /* ---------- Ads ---------- */
  async function viewAds(my) {
    const d = await api("/api/ads?" + qs()); if (my !== seq) return;
    const T = d.kpis, P = d.prev;
    const k = (label, v, foot, cls) => `<div class="kpi ${cls || ""}"><div class="k">${label}</div><div class="v">${v}</div><div class="d">${foot || "&nbsp;"}</div></div>`;
    const acosBadge = T.acos == null ? "needs revenue" : `<span class="badge ${T.acos <= d.target_acos ? "good" : T.acos <= d.max_acos ? "warn" : "bad"}">target &lt; ${pct(d.target_acos, 0)}</span>`;
    const planParts = Object.entries(d.plan).map(([a, v]) => ({a, v: v.daily, c: HEX[a] || "#666", share: v.share}));
    const planTot = planParts.reduce((s, p) => s + p.v, 0);
    const geoShow = (S.app === "all" || S.app === "NurseBound") && d.geo.length;
    const geoMax = Math.max(0.01, ...d.geo.map(g => g.plan || 0));
    document.getElementById("view").innerHTML = `
      ${!d.has_data ? `<div class="banner"><b>No ad data yet.</b> Once Meta, Google or Apple Search Ads keys are added on Render, the hourly job pulls your campaigns in and this fills up.</div>` : ""}
      <div class="kpis">
        ${k("Spend", money(T.spend), delta(T.spend, P.spend, true) + " vs prior")}
        ${k("Taps / clicks", num(T.taps), delta(T.taps, P.taps))}
        ${k("Installs", num(T.installs), T.taps ? pct(T.installs / T.taps) + " tap→install" : "")}
        ${k("Revenue", money(T.revenue), T.roas ? T.roas.toFixed(2) + "× ROAS" + (T.revenue_estimated ? " · est." : "") : "")}
        ${k("ACoS", T.acos == null ? "—" : pct(T.acos), acosBadge)}
        ${k("Avg CPT", money(T.cpt), delta(T.cpt, P.cpt, true))}
      </div>
      <div class="h">Bot budget plan <small>${money(d.global_cap)}/day cap · ${d.auto_rebalance ? "auto" : "manual"}</small></div>
      <div class="card"><div class="plan"><div class="donut">${donut(planParts)}<div class="mid"><div>${money(planTot)}<small>per day</small></div></div></div>
        <div>${planParts.map(p => `<div class="lrow"><span class="dot" style="background:${p.c}"></span>${esc(SHORT[p.a] || p.a)}<b>${money(p.v)}<small> · cap ${money(d.caps[p.a])}</small></b></div>`).join("")}</div></div></div>
      <div class="h">Taps, installs &amp; spend <small>last ${d.days} days</small></div>
      <div class="card"><div class="legend"><span><i style="background:#00C2FF"></i>Taps</span><span><i style="background:#00E5A3"></i>Installs</span><span><i style="background:#FFB020"></i>Spend (right)</span></div>
        <div class="chart">${lineChart(d.series, ["taps", "installs", "spend"], ["#00C2FF", "#00E5A3", "#FFB020"], "spend")}</div></div>
      ${d.by_app.length ? `<div class="h">By app &amp; channel</div><div class="card list">${d.by_app.map(b => `<div class="item"><span class="dot" style="background:${COLOR[b.app] || "#666"};margin-top:6px"></span><div class="main"><div class="name">${esc(SHORT[b.app] || b.app)} · ${esc(b.channel)}</div><div class="meta">${num(b.taps)} taps · ${num(b.installs)} installs · CPT ${money(b.taps ? b.spend / b.taps : null)}</div></div><div class="right">${money(b.spend)}<small>${b.revenue ? "ACoS " + pct(b.spend / b.revenue, 0) : ""}</small></div></div>`).join("")}</div>` : ""}
      ${geoShow ? `<div class="h">NurseBound countries <small>${money(d.plan.NurseBound ? d.plan.NurseBound.daily : 0)}/day</small></div>
        <div class="card list">${d.geo.filter(g => g.enabled !== false).map(g => `<div class="item"><span class="flag">${flag(g.code)}</span><div class="main"><div class="name">${esc(regionName(g.code))} ${g.flag === "who_safeguards" ? '<span class="badge warn">WHO list</span>' : ""}${g.flag === "china_reach" ? '<span class="badge mute">limited reach</span>' : ""}</div>
          <div class="bar" style="margin-top:6px"><i style="width:${(g.plan || 0) / geoMax * 100}%"></i></div><div class="meta">${g.spend ? money(g.spend) + " spent · " + num(g.installs) + " installs" : "no spend yet"} · going to ${(g.dest || []).map(flag).join(" ")}</div></div><div class="right">${money(g.plan)}<small>${g.plan && g.plan < 1 ? "under $1" : "per day"}</small></div></div>`).join("")}</div>
        ${d.geo_untagged_spend ? `<p class="sub" style="margin:8px 2px">${money(d.geo_untagged_spend)} of NurseBound spend has no country code in the campaign name.</p>` : ""}` : ""}`;
  }

  /* ---------- Keywords ---------- */
  async function viewKeywords(my) {
    const d = await api("/api/keywords?" + qs() + "&q=" + encodeURIComponent(S.q || "")); if (my !== seq) return;
    const counts = {all: d.items.length, pause: d.items.filter(i => i.bot === "pause" && i.override === "auto").length, paused: d.items.filter(i => i.override === "paused").length, protected: d.items.filter(i => i.override === "protected").length};
    const items = d.items.filter(i => S.kwFilter === "all" || (S.kwFilter === "pause" ? i.bot === "pause" && i.override === "auto" : i.override === S.kwFilter));
    const status = i => i.override === "protected" ? `<span class="badge info">Protected</span>` : i.override === "paused" ? `<span class="badge bad">Paused by you</span>` : i.bot === "pause" ? `<span class="badge warn">Bot would pause</span>` : `<span class="badge good">Running</span>`;
    const acos = i => i.acos == null ? "—" : pct(i.acos, 0);
    $("view").innerHTML = `
      <label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span class="sr">Search</span><input id="kwq" type="search" placeholder="Search keywords or campaigns" value="${esc(S.q || "")}"></label>
      <div class="chips static">${[["all", "All"], ["pause", "Bot would pause"], ["paused", "Paused"], ["protected", "Protected"]].map(([v, l]) => `<button class="chip" data-f="${v}" aria-pressed="${S.kwFilter === v}">${l} · ${counts[v]}</button>`).join("")}</div>
      ${items.length ? items.map(i => `<div class="card" data-key="${esc(i.key)}">
        <div style="display:flex;gap:10px;align-items:flex-start"><div style="flex:1;min-width:0"><div class="name" style="font-weight:700">${esc(i.keyword || "—")}</div>
          <div class="sub"><span class="dot" style="display:inline-block;background:${COLOR[i.app] || "#666"};width:7px;height:7px;margin-right:4px"></span>${esc(SHORT[i.app] || i.app || "Other")} · ${esc(i.channel)}${i.country ? " · " + flag(i.country) : ""}</div></div>${status(i)}</div>
        ${i.bot === "pause" && i.override === "auto" ? `<div class="sub" style="margin-top:6px;color:var(--amber)">${esc(i.reason)}</div>` : ""}
        <div class="stats"><div><small>Taps</small><b>${num(i.taps)}</b></div><div><small>Installs</small><b>${num(i.installs)}</b></div><div><small>CPT</small><b style="${i.cpt != null && i.cpt > d.max_cpt ? "color:var(--red)" : ""}">${money(i.cpt)}</b></div><div><small>ACoS</small><b>${acos(i)}</b></div></div>
        <div class="seg" role="group" aria-label="Override">${[["auto", "Auto", ""], ["paused", "Pause", "p"], ["protected", "Protect", "s"]].map(([v, l, c]) => `<button class="${c}" data-o="${v}" aria-pressed="${i.override === v}">${l}</button>`).join("")}</div></div>`).join("")
      : `<div class="empty">${d.items.length ? "Nothing matches." : "No keywords or campaigns yet. They appear once an ad channel is connected."}</div>`}`;
    let t; $("kwq").oninput = e => { clearTimeout(t); t = setTimeout(() => { S.q = e.target.value; load(false, true); }, 350); };
    $("view").querySelectorAll("[data-f]").forEach(b => b.onclick = () => { S.kwFilter = b.dataset.f; save(); load(false, true); });
    $("view").querySelectorAll("[data-o]").forEach(b => b.onclick = async () => {
      const key = b.closest("[data-key]").dataset.key; const o = b.dataset.o;
      try { await api("/api/override", {method: "POST", body: JSON.stringify({key, status: o})}); toast(o === "paused" ? "Will be paused on the bot's next run" : o === "protected" ? "Protected: the bot won't pause it" : "Back to automatic"); load(false, true); }
      catch (e) { if (e.message !== "signin") toast(e.message); }
    });
  }

  /* ---------- Rules ---------- */
  async function viewRules(my) {
    const R = await api("/api/rules"); if (my !== seq) return;
    const n = (id, v, step = "any", label, hint, unit) => `<div class="field"><label for="${id}">${label}${hint ? `<small>${hint}</small>` : ""}</label><input id="${id}" type="number" inputmode="decimal" step="${step}" min="0" value="${v ?? ""}">${unit ? `<span class="unit">${unit}</span>` : ""}</div>`;
    const sw = (id, on, label, hint) => `<div class="field"><label for="${id}">${label}${hint ? `<small>${hint}</small>` : ""}</label><span class="switch"><input id="${id}" type="checkbox" ${on ? "checked" : ""}><i></i></span></div>`;
    const geo = (R.geo_targets && R.geo_targets.NurseBound) || [];
    $("view").innerHTML = `
      <div class="group"><h3>Daily spend limits</h3><p class="hint">The bot never sets budgets above these. ${money(R.global_daily_cap * 30.4, 0)} a month at the current cap.</p>
        ${n("global_daily_cap", R.global_daily_cap, "0.01", "All apps", "per day", "$")}
        ${APPS.map((a, i) => n("cap" + i, R.app_caps[a], "0.01", esc(a), "per day", "$")).join("")}</div>
      <div class="group"><h3>Priority</h3><p class="hint">Multiplies each app's score when the budget is split. Higher gets more.</p>
        ${APPS.map((a, i) => n("pri" + i, (R.app_priority || {})[a] ?? 1, "0.05", esc(a))).join("")}</div>
      <div class="group"><h3>Kill switch</h3><p class="hint">Checked against the last ${R.lookback_days} days on every run.</p>
        ${n("max_cpt", R.max_cpt, "0.01", "Pause above this cost per tap", `after ${R.min_taps_for_cpt} taps`, "$")}
        ${n("min_taps_for_cpt", R.min_taps_for_cpt, "1", "Taps before CPT rule applies")}
        ${n("zero_install_taps", R.zero_install_taps, "1", "Pause after taps with 0 installs")}
        ${n("max_acos", Math.round(R.max_acos * 100), "1", "ACoS ceiling", `once spend reaches ${money(R.min_spend_for_acos, 0)}`, "%")}
        ${n("min_spend_for_acos", R.min_spend_for_acos, "1", "Spend before ACoS rule applies", "", "$")}
        ${n("target_acos", Math.round(R.target_acos * 100), "1", "Target ACoS", "green at or below", "%")}</div>
      <div class="group"><h3>Rebalancing</h3>
        ${sw("auto_rebalance", R.auto_rebalance, "Auto-rebalance budget", "move money toward the apps that earn the most per dollar")}
        ${n("min_share", Math.round(R.min_share * 100), "1", "Minimum share per app", "", "%")}
        ${n("max_share", Math.round(R.max_share * 100), "1", "Maximum share per app", "", "%")}
        ${n("max_budget_step", Math.round(R.max_budget_step * 100), "1", "Biggest change per run", "", "%")}</div>
      <div class="group"><h3>Value per install</h3><p class="hint">Used to estimate revenue when an ad platform reports none.</p>
        ${APPS.map((a, i) => n("vpi" + i, (R.value_per_install || {})[a] ?? 0, "0.01", esc(a), "", "$")).join("")}</div>
      <div class="group"><h3>NurseBound countries</h3><p class="hint">Weight is each country's share of NurseBound's budget. Put the code in campaign names, e.g. “NurseBound | PH | Prospecting”.</p>
        ${geo.map((g, i) => `<div class="field"><label for="gw${i}"><span class="flag">${flag(g.code)}</span> ${esc(regionName(g.code))}${g.flag === "who_safeguards" ? "<small>On WHO's 2023 safeguards list: advertise the study app, not recruitment</small>" : g.flag === "china_reach" ? "<small>Meta and Google ads don't serve in mainland China</small>" : ""}</label><input id="gw${i}" type="number" inputmode="decimal" step="1" min="0" value="${g.weight}" style="width:70px"><span class="switch"><input id="ge${i}" type="checkbox" ${g.enabled !== false ? "checked" : ""} aria-label="${esc(regionName(g.code))} on"><i></i></span></div>`).join("")}</div>
      <div class="group"><h3>Telegram alerts</h3><p class="hint">The bot token stays on Render (TELEGRAM_BOT_TOKEN), never in the app.</p>
        <div class="field"><label for="telegram_chat_id">Chat ID</label><input id="telegram_chat_id" type="text" inputmode="numeric" value="${esc(R.telegram_chat_id || "")}" placeholder="123456789"></div>
        ${sw("al_daily_summary", R.alerts.daily_summary, "Daily summary")}${sw("al_kill_switch", R.alerts.kill_switch, "Kill-switch alerts")}${sw("al_rebalance", R.alerts.rebalance, "Rebalance logs")}${sw("al_high_cpt", R.alerts.high_cpt, "High CPT warnings")}</div>
      <div class="actions">
        <button class="btn primary block" id="saveRules">Save rules</button>
        <button class="btn" id="tgTest">Send test notification</button>
        <button class="btn orange" id="runBot">Run bot now ${ME.dry_run ? "(dry run)" : ""}</button>
        <label class="btn" for="importFile">Import rules.json from the dashboard</label><input id="importFile" type="file" accept=".json,application/json" hidden>
        <button class="btn" id="signOut">Sign out</button>
      </div>`;
    const v = id => +$(id).value;
    $("saveRules").onclick = async () => {
      const body = {global_daily_cap: v("global_daily_cap"), max_cpt: v("max_cpt"), min_taps_for_cpt: v("min_taps_for_cpt"), zero_install_taps: v("zero_install_taps"),
        max_acos: v("max_acos") / 100, min_spend_for_acos: v("min_spend_for_acos"), target_acos: v("target_acos") / 100, auto_rebalance: $("auto_rebalance").checked,
        min_share: v("min_share") / 100, max_share: v("max_share") / 100, max_budget_step: v("max_budget_step") / 100, telegram_chat_id: $("telegram_chat_id").value.trim(),
        app_caps: Object.fromEntries(APPS.map((a, i) => [a, v("cap" + i)])), app_priority: Object.fromEntries(APPS.map((a, i) => [a, v("pri" + i)])),
        value_per_install: Object.fromEntries(APPS.map((a, i) => [a, v("vpi" + i)])),
        alerts: {daily_summary: $("al_daily_summary").checked, kill_switch: $("al_kill_switch").checked, rebalance: $("al_rebalance").checked, high_cpt: $("al_high_cpt").checked},
        geo_targets: {NurseBound: geo.map((g, i) => ({code: g.code, weight: v("gw" + i), enabled: $("ge" + i).checked}))}};
      $("saveRules").disabled = true;
      try { await api("/api/rules", {method: "POST", body: JSON.stringify(body)}); toast("Rules saved. The bot uses them on its next run."); }
      catch (e) { if (e.message !== "signin") toast(e.message); } finally { $("saveRules").disabled = false; }
    };
    $("tgTest").onclick = async () => { try { const r = await api("/api/telegram-test", {method: "POST"}); toast(r.ok ? "Sent. Check Telegram." : r.error); } catch (e) { if (e.message !== "signin") toast(e.message); } };
    $("runBot").onclick = async () => { $("runBot").disabled = true; try { const r = await api("/api/run-bot", {method: "POST"}); toast(`${r.dry_run ? "Dry run: would pause" : "Paused"} ${r.paused} · plan ${Object.entries(r.plan).map(([a, d]) => (SHORT[a] || a) + " " + money(d)).join(", ")}`); } catch (e) { if (e.message !== "signin") toast(e.message); } finally { $("runBot").disabled = false; } };
    $("importFile").onchange = async () => { const f = $("importFile").files[0]; if (!f) return; try { const data = JSON.parse(await f.text()); await api("/api/rules", {method: "POST", body: JSON.stringify({import: data})}); toast("Imported."); load(false, true); } catch (e) { if (e.message !== "signin") toast("Couldn't import: " + e.message); } };
    $("signOut").onclick = async () => { try { await api("/api/logout", {method: "POST"}); } catch (e) {} showLogin(); };
  }

  /* ---------- Trading ---------- */
  async function viewTrading(my) {
    const d = await api("/api/trading"); if (my !== seq) return;
    if (!d.connected) {
      $("view").innerHTML = `<div class="banner"><b>Trading isn't reporting yet.</b> On the trading PC, open the <b>adbot-sidecar</b> folder, run <b>setup-sidecar.bat</b> once, then <b>start-sidecar.bat</b>. This tab updates every minute after that.</div>`;
      return;
    }
    const hb = d.heartbeat || {}, a = d.account || {}, r = d.risk || null;
    const age = hb.ts ? (Date.parse(d.now + "Z") - Date.parse(hb.ts + "Z")) / 60000 : null;
    const st = String(hb.status || "unknown").toLowerCase(); const ok = st === "running" && age != null && age < 10;
    const dayChange = a.equity != null && d.day_start_equity != null ? a.equity - d.day_start_equity : null;
    const limitUsed = r && r.daily_loss_limit ? Math.min(1, (r.daily_loss_used || 0) / r.daily_loss_limit) : null;
    $("view").innerHTML = `
      <div class="card" style="display:flex;align-items:center;gap:12px"><span class="dot" style="width:12px;height:12px;background:${ok ? "var(--green)" : st === "stopped" ? "var(--red)" : "var(--amber)"};box-shadow:0 0 0 4px ${ok ? "rgba(52,211,153,.18)" : "rgba(251,191,36,.15)"}"></span>
        <div style="flex:1;min-width:0"><div style="font-weight:700">${esc(d.bot_id)} · ${esc(st)}</div><div class="sub">${esc(hb.message || "")}${age != null ? ` · ${age < 1 ? "just now" : Math.round(age) + " min ago"}` : ""}</div></div>
        <span class="pill ${hb.mode === "live" ? "live" : "dry"}">${hb.mode === "live" ? "LIVE" : "DEMO"}</span></div>
      ${r && r.kill_switch ? `<div class="banner bad" style="margin-top:10px"><b>Trading is halted.</b> ${esc(r.notes || "")}</div>` : ""}
      ${age != null && age > 15 ? `<div class="banner warn" style="margin-top:10px">No report for ${Math.round(age)} minutes. Check that start-sidecar.bat is running on the trading PC.</div>` : ""}
      <div class="kpis" style="margin-top:10px">
        <div class="kpi"><div class="k">Equity</div><div class="v">${money(a.equity)}</div><div class="d">${dayChange != null ? smoney(dayChange) + " today" : "&nbsp;"}</div></div>
        <div class="kpi"><div class="k">Today's P&amp;L</div><div class="v" style="color:${(a.day_pnl || 0) < 0 ? "var(--red)" : (a.day_pnl || 0) > 0 ? "var(--green)" : "inherit"}">${smoney(a.day_pnl)}</div><div class="d">realised</div></div>
        <div class="kpi"><div class="k">Total P&amp;L</div><div class="v">${smoney(a.total_pnl)}</div><div class="d">realised, all time</div></div>
        <div class="kpi"><div class="k">Margin used</div><div class="v">${money(a.margin_used)}</div><div class="d">balance ${money(a.balance)}</div></div>
      </div>
      ${r ? `<div class="card" style="margin-top:10px">${limitUsed != null ? `<div class="lrow" style="padding-top:0">Daily loss limit<b>${money(r.daily_loss_used)} of ${money(r.daily_loss_limit)}</b></div><div class="bar"><i style="width:${limitUsed * 100}%;background:${limitUsed > .8 ? "var(--red)" : limitUsed > .5 ? "var(--amber)" : "var(--green)"}"></i></div>` : ""}<div class="sub" style="margin-top:8px">${esc(r.notes || "")}</div></div>` : ""}
      ${d.equity.length > 1 ? `<div class="h">Equity <small>30 days</small></div><div class="card"><div class="chart">${lineChart(d.equity.map(e => ({date: e.ts, equity: e.equity})), ["equity"], ["#00E5A3"], null, {fit: true, money: true})}</div></div>` : ""}
      <div class="h">Open positions <small>${d.positions.length}</small></div>
      <div class="card list">${d.positions.length ? d.positions.map(p => `<div class="item"><div class="main"><div class="name">${esc(p.symbol)} <span class="badge ${p.side === "buy" || p.side === "long" ? "good" : "bad"}">${esc(String(p.side || "").toUpperCase())}</span> ${p.strategy ? `<span class="badge warn">${esc(p.strategy)}</span>` : ""}</div><div class="meta">${p.qty} lots · in ${p.entry} · now ${p.price ?? "—"} · SL ${p.stop ?? "—"} · TP ${p.target ?? "—"}</div></div><div class="right" style="color:${(p.unrealized_pnl || 0) < 0 ? "var(--red)" : "var(--green)"}">${smoney(p.unrealized_pnl)}</div></div>`).join("") : `<div class="empty" style="padding:12px">Flat: no open positions.</div>`}</div>
      <div class="h">Closed trades <small>7 days</small></div>
      <div class="card list">${d.trades.length ? d.trades.map(t => `<div class="item"><div class="main"><div class="name">${esc(t.symbol)} ${esc(String(t.side || "").toUpperCase())}</div><div class="meta">${t.closed_at ? new Date(t.closed_at + "Z").toLocaleString("en-US", {weekday: "short", hour: "numeric", minute: "2-digit"}) : "open"} · ${esc(t.reason || t.strategy || "")}</div></div><div class="right" style="color:${(t.pnl || 0) < 0 ? "var(--red)" : "var(--green)"}">${smoney(t.pnl)}</div></div>`).join("") : `<div class="empty" style="padding:12px">No closed trades this week.</div>`}</div>
      <div class="h">Signals <small>24 hours</small></div>
      <div class="card list">${d.signals.length ? d.signals.map(s => `<div class="item"><div class="main"><div class="name">${esc(s.symbol)} ${s.direction ? esc(String(s.direction).toUpperCase()) : ""} <span class="badge ${s.decision === "taken" ? "good" : s.decision === "blocked" ? "bad" : "mute"}">${esc(s.decision)}</span></div><div class="meta">${esc(s.reason || "")}</div></div><div class="right">${s.confidence != null ? pct(s.confidence, 0) : ""}<small>${new Date(s.ts + "Z").toLocaleTimeString("en-US", {hour: "numeric", minute: "2-digit"})}</small></div></div>`).join("") : `<div class="empty" style="padding:12px">No signals in the last 24 hours.</div>`}</div>
      <p class="sub" style="text-align:center;margin-top:14px">Read-only. AdBot never places, changes or closes trades.</p>`;
  }

  /* ---------- Revenue: futurist command deck (Stage B: registry + data sources; numbers arrive once store reports are imported) ---------- */
  const PLAT = {apple_app_store: "App Store", google_play: "Google Play", apple_ads: "Apple Ads", google_ads: "Google Ads", meta_ads: "Meta Ads", tiktok_ads: "TikTok Ads"};
  const PLAT_HUE = {apple_app_store: "#00C2FF", google_play: "#00E5A3", apple_ads: "#C7D2FE", google_ads: "#4F8CFF", meta_ads: "#7C6CFF", tiktok_ads: "#FF2E88"};
  const PLAN = {monthly: "Monthly", three_month: "3-month pass", annual: "Annual", lifetime: "Lifetime", other: "Other"};
  const TYPE = {unverified: "type not verified", auto_renewable_subscription: "auto-renewing", non_renewing_subscription: "non-renewing",
                non_consumable: "one-time", consumable: "consumable", one_time_product: "one-time"};
  const APP_HUE = {rnr_pro: ["#FF6A2B", "#FF2E63"], ccrn_adult: ["#2DD4BF", "#00C2FF"], nursebound_pro: ["#8B5CF6", "#E024C3"]};
  const when = t => t ? new Date(String(t).replace(" ", "T") + (String(t).endsWith("Z") ? "" : "Z")).toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "NEVER";
  const srcState = s => s.status === "verified" ? "on" : (s.status === "error" || s.last_status === "failed") ? "err" : s.status === "configured" ? "keys" : "off";
  const STATE_TXT = {on: "LINKED", err: "ERROR", keys: "KEYS SET", off: "OFFLINE"};

  function readinessRing(sources) {
    // one arc per data source: lit = linked, amber = keys set, red = error, dim = offline (real status only)
    const n = sources.length, R = 54, C = 2 * Math.PI * R, gap = 7, seg = C / n - gap;
    const col = {on: "#00E5A3", keys: "#FFB020", err: "#FF4B4B", off: "rgba(148,163,184,.18)"};
    const linked = sources.filter(s => srcState(s) === "on").length;
    return `<svg class="ring" viewBox="0 0 140 140" role="img" aria-label="${linked} of ${n} data sources linked">
      <defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
      <circle cx="70" cy="70" r="66" fill="none" stroke="rgba(0,194,255,.12)" stroke-dasharray="2 5"/>
      <g class="spin-slow"><circle cx="70" cy="70" r="40" fill="none" stroke="rgba(224,36,195,.25)" stroke-width="1" stroke-dasharray="18 10 4 10"/></g>
      ${sources.map((s, i) => { const st = srcState(s); return `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${col[st]}" stroke-width="9" stroke-linecap="round"
        stroke-dasharray="${seg.toFixed(2)} ${(C - seg).toFixed(2)}" stroke-dashoffset="${(-i * (C / n)).toFixed(2)}" transform="rotate(-90 70 70)" ${st !== "off" ? 'filter="url(#glow)"' : ""}><title>${esc(s.name)}: ${STATE_TXT[st]}</title></circle>`; }).join("")}
      <text x="70" y="68" text-anchor="middle" class="ring-n">${linked}/${n}</text>
      <text x="70" y="86" text-anchor="middle" class="ring-l">LINKED</text></svg>`;
  }

  async function viewRevenue(my) {
    const d = await api("/api/revenue"); if (my !== seq) return;
    const isStore = s => s.platform === "apple_app_store" || s.platform === "google_play";
    const stores = d.sources.filter(isStore), ads = d.sources.filter(s => !isStore(s));
    const anyStore = stores.some(s => srcState(s) === "on");
    const lastSync = stores.map(s => s.last_success).filter(Boolean).sort().pop();
    const tile = (k, hue, sub) => `<div class="hx-tile" style="--hue:${hue}"><div class="hx-k">${k}</div><div class="hx-v">UNAVAILABLE</div>
        <svg class="hx-spark" viewBox="0 0 120 26" preserveAspectRatio="none" aria-hidden="true"><path d="M0 20H120" stroke-dasharray="3 4"/></svg><div class="hx-d">${esc(sub)}</div><i class="scan"></i></div>`;
    const node = (label, st, sub) => `<div class="pn ${st}"><b>${label}</b><small>${sub}</small></div>`;
    const wire = st => `<div class="pw ${st}"><i></i></div>`;
    const srcCard = s => { const st = srcState(s); return `<div class="src ${st}" style="--hue:${PLAT_HUE[s.platform] || "#00C2FF"}">
        <div class="src-top"><span class="led"></span><b>${esc(PLAT[s.platform] || s.name)}</b><span class="chip ${st}">${STATE_TXT[st]}</span></div>
        <div class="src-meta">LAST SYNC <b>${when(s.last_success)}</b>${s.period_end ? ` · DATA TO ${esc(String(s.period_end).slice(0, 10))}` : ""}</div>
        ${s.last_status === "failed" && s.last_message ? `<div class="src-err">${esc(s.last_message)}</div>` : s.status === "error" && s.last_error ? `<div class="src-err">${esc(s.last_error)}</div>` : ""}
        ${s.last_status === "partial" && s.last_message ? `<div class="src-warn">${esc(String(s.last_message).split(" | ")[0])}</div>` : ""}
        ${s.credentials.missing.length ? `<details><summary>Setup · ${s.credentials.missing.length} Render setting${s.credentials.missing.length > 1 ? "s" : ""} needed</summary>${s.credentials.missing.map(v => `<code>${esc(v)}</code>`).join("")}</details>` : ""}</div>`; };
    const idRow = (label, val, empty = "NOT SET") => `<div class="idr"><span>${label}</span><b class="${val ? "" : "nil"}">${val ? esc(val) : empty}</b></div>`;

    $("view").innerHTML = `<div class="rev">
      <section class="hero">
        <div class="hero-txt">
          <div class="eyebrow"><span class="pulse ${anyStore ? "on" : ""}"></span>REVENUE COMMAND</div>
          <h2>${anyStore ? "Store feeds online" : "Awaiting store feeds"}</h2>
          <p>${d.has_revenue_data ? "Figures below come from imported store reports." : "Every figure reads <b>Unavailable</b> until App Store Connect and Google Play reports are imported. Never a fake $0."}</p>
          <div class="hero-meta"><span>LAST SYNC <b>${when(lastSync)}</b></span><span>MODE <b>${d.dry_run ? "READ-ONLY" : "LIVE"}</b></span></div>
        </div>
        ${readinessRing(d.sources)}
      </section>

      <div class="hx-grid">
        ${tile("YESTERDAY", "#00C2FF", "App Store + Google Play")}
        ${tile("LAST 30 DAYS", "#00E5A3", "gross sales")}
        ${tile("MONTH TO DATE", "#E024C3", "estimated proceeds")}
        ${tile("AD SPEND · MONTH", "#FFB020", "ad accounts not synced")}
      </div>

      <div class="hx-h"><span>DATA PIPELINE</span><small>live status</small></div>
      <div class="pipe">
        <div class="pcol">${stores.map(s => node(esc(PLAT[s.platform]), srcState(s), STATE_TXT[srcState(s)])).join("")}</div>
        ${wire(anyStore ? "on" : "off")}
        ${node("Revenue DB", "on", "19 tables ready")}
        ${wire(d.has_revenue_data ? "on" : "off")}
        ${node("Dashboard", d.has_revenue_data ? "on" : "keys", d.has_revenue_data ? "live" : "waiting")}
      </div>

      <div class="hx-h"><span>STORE FEEDS</span><small>read-only</small></div>
      <div class="src-grid">${stores.map(srcCard).join("")}</div>

      <div class="hx-h"><span>APPS</span><small>${d.apps.length} registered</small></div>
      ${d.apps.map(a => {
        const ios = a.listings.apple_app_store || {}, and = a.listings.google_play || {};
        const [h1, h2] = APP_HUE[a.app_id] || ["#00C2FF", "#E024C3"];
        const live = new Set(a.products.filter(p => p.verified).map(p => p.platform + ":" + p.plan_code));
        const prods = [...a.products].filter(p => p.verified || !live.has(p.platform + ":" + p.plan_code)).sort((x, y) => Object.keys(PLAN).indexOf(x.plan_code) - Object.keys(PLAN).indexOf(y.plan_code));
        const price = p => { const pr = (p.prices || []).find(x => x.currency === "USD" && x.territory === "US") || (p.prices || []).find(x => x.currency === "USD"); return pr ? +pr.amount : null; };
        const max = Math.max(1, ...prods.map(price).filter(v => v != null));
        return `<article class="appx" style="--h1:${h1};--h2:${h2}">
          <header><div><div class="appx-name">${esc(a.name)}</div><div class="appx-sub">AD BOT · ${esc(a.bot_app_name || "—")}</div></div>
            <button class="hx-btn" data-edit="${esc(a.app_id)}">Edit IDs</button></header>
          <div class="ids">
            <div class="idcol"><div class="idh">App Store</div>${idRow("Apple ID", ios.apple_app_id)}${idRow("Bundle", ios.bundle_id)}${idRow("Status", ios.publication_status === "unknown" ? null : ios.publication_status, "NOT CHECKED")}</div>
            <div class="idcol"><div class="idh">Google Play</div>${idRow("Package", and.package_name)}${idRow("Status", and.publication_status === "unknown" ? null : and.publication_status, "NOT CHECKED")}</div>
          </div>
          <form class="idform" data-app="${esc(a.app_id)}" hidden>
            <div class="field"><label>Apple ID<small>App Store Connect → App Information</small></label><input type="text" name="apple_app_id" inputmode="numeric" value="${esc(ios.apple_app_id || "")}" placeholder="1234567890"></div>
            <div class="field"><label>Bundle ID</label><input type="text" name="bundle_id" autocapitalize="off" value="${esc(ios.bundle_id || "")}" placeholder="com.example.app"></div>
            <div class="field"><label>Play package</label><input type="text" name="package_name" autocapitalize="off" value="${esc(and.package_name || "")}" placeholder="com.example.app"></div>
            <button class="btn primary block" type="submit">Save IDs</button>
          </form>
          <div class="ladder-h">PRICE LADDER <small>${prods.some(p => p.verified) ? "✓ = confirmed by the store · US prices" : "iOS reference list · not sales"}</small></div>
          <div class="ladder">${prods.map(p => { const v = price(p); return `<div class="rung">
              <div class="rung-l"><b>${esc(PLAN[p.plan_code] || p.plan_code)}${p.verified ? ' <i class="vchk" title="Confirmed by App Store Connect">✓</i>' : ""}</b><small>${p.platform === "google_play" ? "Android · " : "iOS · "}${esc(TYPE[p.product_type] || p.product_type)}</small></div>
              <div class="rung-bar"><i style="width:${v != null ? Math.max(6, v / max * 100) : 0}%"></i></div>
              <div class="rung-v">${v != null ? money(v) : "—"}</div></div>`; }).join("")}
            ${prods.some(p => p.platform === "google_play") ? "" : `<div class="rung nil"><div class="rung-l"><b>Android</b><small>read from Google Play</small></div><div class="rung-bar"><i style="width:0"></i></div><div class="rung-v">UNAVAILABLE</div></div>`}
          </div></article>`; }).join("")}

      <div class="hx-h"><span>AD PLATFORMS</span><small>spend + return on ad spend</small></div>
      <div class="src-grid two">${ads.map(srcCard).join("")}</div>
      <p class="hx-foot">READ-ONLY · NOTHING HERE CHANGES STORE PRODUCTS, PRICES OR PURCHASES</p>
    </div>`;
    document.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => { const f = document.querySelector(`.idform[data-app="${b.dataset.edit}"]`); f.hidden = !f.hidden; b.textContent = f.hidden ? "Edit IDs" : "Close"; });
    document.querySelectorAll(".idform").forEach(f => f.onsubmit = async e => {
      e.preventDefault(); const btn = f.querySelector("button"); btn.disabled = true; const val = n => f.elements[n].value.trim();
      try {
        await api("/api/revenue/listing", {method: "POST", body: JSON.stringify({app_id: f.dataset.app, platform: "apple_app_store", fields: {apple_app_id: val("apple_app_id"), bundle_id: val("bundle_id")}})});
        await api("/api/revenue/listing", {method: "POST", body: JSON.stringify({app_id: f.dataset.app, platform: "google_play", fields: {package_name: val("package_name")}})});
        toast("Saved."); load(false, true);
      } catch (err) { if (err.message !== "signin") toast(err.message); } finally { btn.disabled = false; }
    });
  }

  /* ---------- Log ---------- */
  async function viewLog(my) {
    const d = await api("/api/log"); if (my !== seq) return;
    $("view").innerHTML = `
      ${d.dry_run ? `<div class="banner warn"><b>Dry run.</b> These are decisions the bot <i>would</i> make. Set DRY_RUN to false on the adbot-hourly job in Render to let it act.</div>` : ""}
      <div class="card list">${d.actions.length ? d.actions.map(x => `<div class="item"><span class="badge ${x.kind === "pause" ? "warn" : "info"}" style="margin-top:2px">${esc(x.kind)}</span><div class="main"><div class="name">${esc(x.keyword || x.entity_key || "")}</div><div class="meta">${esc(SHORT[x.app] || x.app || "")} · ${esc(x.channel || "")} · ${esc(x.detail || "")}${x.error ? ` · <span style="color:var(--red)">${esc(x.error)}</span>` : ""}</div></div><div class="right"><small>${new Date(x.ts + "Z").toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})}</small>${x.applied ? "done" : x.dry_run ? "would" : "—"}</div></div>`).join("") : `<div class="empty">The bot hasn't acted yet.</div>`}</div>`;
  }

  /* ---------- go ---------- */
  if ("serviceWorker" in navigator) window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  start();
})();
