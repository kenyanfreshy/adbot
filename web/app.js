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
    // X-AdBot-Request: a custom header a cross-site page can't send without CORS permission (CSRF defence)
    const r = await fetch(path, {credentials: "same-origin", headers: {"Content-Type": "application/json", "X-AdBot-Request": "1"}, ...opts});
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
    setModePill();
    applyTradingVisibility();
    renderChips(); $("channel").value = S.channel; $("days").value = String(S.days);
    go(S.tab);
  }
  function setModePill() {
    const stopped = ME.automation_stopped, live = !ME.dry_run;
    $("modePill").className = "pill " + (stopped ? "stop" : live ? "live" : "dry");
    $("modePill").textContent = stopped ? "STOPPED" : live ? "LIVE" : "DRY RUN";
    $("modePill").title = stopped ? "Automation is stopped" : live ? "Approved changes are sent to the platforms" : "Every change is simulated; nothing is sent to the ad platforms";
  }
  // the Trading tab appears whenever a trading bot (live or demo) reports in
  function applyTradingVisibility() {
    const visible = !!ME.trading_live || !!ME.trading_demo;
    const btn = document.querySelector('.tabs button[data-tab="trading"]');
    if (btn) btn.hidden = !visible;
    document.querySelector(".tabs").style.setProperty("--tabcount", visible ? 6 : 5);
    if (!visible && S.tab === "trading") S.tab = "ads";
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
    const wide = window.innerWidth >= 1024, W = wide ? 900 : 340, H = wide ? 280 : 190, pl = 44, pr = rightKey ? 40 : 8, pt = 10, pb = 22, n = series.length;
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

  /* ---------- Ads: control overview, action needed, decision queue, pacing, performance ---------- */
  const SEV = {critical: "bad", warning: "warn", info: "info"};
  const PSTAT = {proposed: ["info", "Proposed"], blocked: ["nil", "Blocked"], approved: ["warn", "Approved · waiting to run"], executing: ["warn", "Running"],
                 uncertain: ["bad", "Unconfirmed"], applied: ["ok", "Applied (live)"], simulated: ["sim", "Simulated (dry run)"], failed: ["bad", "Failed"],
                 rejected: ["nil", "Rejected"], superseded: ["nil", "Replaced"], expired: ["nil", "Expired"], already_done: ["ok", "Already done"]};
  const ACT = {pause_keyword: "Pause keyword", pause_ad_set: "Pause ad set", pause_campaign: "Pause campaign", set_budget: "Set daily budget"};
  const KIND = {optimization: "Optimization", safeguard: "Spend safeguard", emergency: "Emergency", manual: "Your override"};
  const pchip = st => { const [c, l] = PSTAT[st] || ["nil", st]; return `<span class="schip ${c}">${esc(l)}</span>`; };
  const ts = t => t ? when(t) : "never";
  const mval = v => v == null || v === "" ? "—" : esc(String(v));

  function qCard(p, mode) {
    const m = p.metrics || {}, bl = Array.isArray(p.blockers) ? p.blockers : [];
    const ev = [["Window", p.window_start ? `${p.window_start} → ${p.window_end} (mature days only)` : null],
      [m.click_label ? m.click_label[0].toUpperCase() + m.click_label.slice(1) : "Clicks", m.taps != null ? num(m.taps) : null],
      [m.conversion_label ? m.conversion_label[0].toUpperCase() + m.conversion_label.slice(1) : "Conversions", m.conversions != null ? num(m.conversions) : null],
      ["Spend", m.spend != null ? money(m.spend) + (m.currency ? " " + m.currency : "") : null],
      ["Revenue", m.revenue_known === false ? "Not reported by the platform" : m.revenue != null ? money(m.revenue) + " (platform-attributed)" : null],
      ["Cost per click", m.cpt != null ? money(m.cpt) : null], ["Recent spend", m.spend_7d != null ? money(m.spend_7d) + " in 7 days" : null],
      ["Pacing allowance", m.allowance != null ? money(m.allowance) + "/day" : null],
      ["Data", p.freshness], ["Evidence", p.sufficiency], ["Rule", p.rule_id ? `${p.rule_id} · v${p.rule_version}` : null],
      ["Expected effect", p.expected_effect], ["Next eligible", p.next_eligible_at ? when(p.next_eligible_at) : null],
      ["Decided", p.decided_by ? `${p.decided_by} · ${when(p.decided_at)}` : null], ["Outcome", p.outcome && !String(p.outcome).startsWith("claim:") ? p.outcome : null]].filter(x => x[1] != null && x[1] !== "");
    const change = p.action === "set_budget" ? `${money(+p.current_value)} → ${money(+p.proposed_value)} /day` : "";
    return `<div class="qcard ${p.kind}" data-pid="${esc(p.proposal_id)}">
      <div class="qtop"><div class="qmain"><div class="qname">${esc(ACT[p.action] || p.action)}: ${esc(p.entity_name || p.entity_key)}</div>
        <div class="qsub"><span class="dot" style="background:${COLOR[p.app] || "#666"}"></span>${esc(SHORT[p.app] || p.app || "—")} · ${esc(p.channel)} · ${esc(KIND[p.kind] || p.kind)}${change ? " · " + change : ""}</div></div>${pchip(p.status)}</div>
      <div class="qreason">${esc(p.reason || "")}</div>
      ${bl.length ? `<ul class="qblock">${bl.map(b => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
      <details class="qev"><summary>Evidence and details</summary><dl>${ev.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></details>
      ${(p.status === "proposed" || (p.status === "blocked" && p.reapproval_required && bl.every(b => String(b).startsWith("Needs a new approval")))) && mode === "approval" ? `<div class="qbtns"><button class="btn" data-dec="0">Reject</button><button class="btn primary" data-dec="1" data-hash="${esc(p.proposal_hash || "")}">${p.status === "blocked" ? "Approve again" : "Approve"}</button></div>` : ""}
      ${p.status === "approved" && p.approval_expires_at ? `<div class="qnote">Approved; expires ${when(p.approval_expires_at)} if not carried out. The scheduled job (next run ${when(C.next_run)}) re-checks everything before sending.</div>` : ""}
      ${p.status === "proposed" && mode !== "approval" ? `<div class="qnote">${mode === "bounded_auto" ? "Not in your list of automatic actions; switch to Approval mode to approve it." : "Recommend mode: nothing is applied. Switch to Approval mode to approve changes."}</div>` : ""}
    </div>`;
  }

  async function viewAds(my) {
    const [d, C] = await Promise.all([api("/api/ads?" + qs()), api("/api/control?" + qs())]); if (my !== seq) return;
    ME.mode = C.mode; setModePill();
    const T = d.kpis, P = d.prev;
    const k = (label, v, foot, cls) => `<div class="kpi ${cls || ""}"><div class="k">${label}</div><div class="v">${v}</div><div class="d">${foot || "&nbsp;"}</div></div>`;
    const cur1 = T.currencies && T.currencies.length === 1 ? " " + T.currencies[0] : "";
    const usable = C.sources.filter(s => ["imported_activity", "imported_zero"].includes(s.state)).length;
    const connected = C.sources.filter(s => !["not_configured", "unsupported"].includes(s.state)).length;
    const sm = C.store_money || {}; const est = (sm.estimated || []);
    const proceeds = est.length ? est.map(e => `${e.proceeds ?? "—"} ${e.currency}`).join(" · ") : null;
    const tot = (C.pacing && C.pacing.total) || null;
    const queueOpen = C.queue.filter(p => ["proposed", "approved", "uncertain", "executing"].includes(p.status));
    const pace = C.pacing || {apps: {}};
    const reb = C.rebalance || {};
    document.getElementById("view").innerHTML = `
      <section class="ctl">
        <div class="ctl-row">
          <div class="ctl-mode"><span class="ctl-k">Automation</span>
            <select id="modeSel" aria-label="Operating mode">${C.modes.map(m => `<option value="${m.id}" ${m.id === C.mode ? "selected" : ""}>${esc(m.label.split(":")[0])}</option>`).join("")}</select>
            <span class="schip ${C.dry_run ? "sim" : "bad"}">${C.dry_run ? "Dry run: changes are simulated" : "Live: approved changes are sent"}</span></div>
          ${C.automation_stopped ? `<button class="btn" id="resumeBtn">Resume automation</button>` : `<button class="btn danger" id="stopBtn">Stop automation</button>`}
        </div>
        <p class="ctl-help">${esc((C.modes.find(m => m.id === C.mode) || {}).label || "")}. Last decision run: ${C.last_cycle ? when(C.last_cycle.at) : "not yet"}.</p>
        ${C.automation_stopped ? `<div class="banner bad"><b>Automation is stopped</b> · requested ${when(C.automation_stopped.requested_at || C.automation_stopped.since)} · ${C.automation_stopped.persisted_at ? "saved" : "NOT confirmed saved"} · ${C.automation_stopped.acknowledged_at ? "seen by the scheduled job " + when(C.automation_stopped.acknowledged_at) : "not yet seen by the scheduled job (next run " + when(C.next_run) + ")"}. No advertising change is sent while stopped. <b>Your ads are still running</b>; use Emergency pause to stop them.</div>` : ""}
        ${C.emergency ? (() => { const E = C.emergency_status || {}; return `<div class="banner bad"><b>Emergency pause requested</b> ${when(C.emergency.requested_at || C.emergency.since)} for ${E.campaigns ?? C.emergency.count} campaign${(E.campaigns ?? C.emergency.count) === 1 ? "" : "s"} · ${E.acknowledged_at ? "seen by the scheduled job " + when(E.acknowledged_at) : "waiting for the scheduled job (next run " + when(C.next_run) + "); nothing has been sent yet"}${C.dry_run ? " · <b>DRY RUN: no pause will be sent</b>" : ""}.<br>Sent ${E.remote_requests_sent || 0} · verified paused ${E.verified || 0} · already paused ${E.already_paused || 0} · failed ${E.failed || 0} · unconfirmed ${(E.uncertain || 0) + (E.accepted_unconfirmed || 0)} · simulated ${E.simulated_dry_run || 0} · waiting ${E.waiting || 0}. Nothing is resumed automatically. <button class="btn sm" id="emClear">Clear latch</button></div>`; })() : ""}
      </section>

      <div class="kpis ov">
        ${k("Spend", money(T.spend), (T.mixed_currency ? "mixed currencies · " : cur1 ? cur1.trim() + " · " : "") + "last " + d.days + " days")}
        ${k("Store proceeds", proceeds ? esc(proceeds) : "Unavailable", proceeds ? "estimated, month to date" : "no store reports imported")}
        ${k("Data coverage", `${usable}/${C.sources.filter(s => s.state !== "unsupported").length}`, `${connected} connected · revenue on ${T.revenue_coverage == null ? "—" : pct(T.revenue_coverage, 0)} of spend`)}
        ${k("Decisions", num(queueOpen.length), `${C.queue.filter(p => p.status === "blocked").length} blocked · ${esc(C.mode)}`)}
      </div>

      ${C.action_needed.length ? `<div class="h">Action needed <small>${C.action_needed.length}</small></div>
        <div class="card list need">${C.action_needed.map(n => `<div class="item sev-${SEV[n.severity]}"><div class="main"><div class="name">${esc(n.title)}</div>${n.text ? `<div class="meta">${esc(n.text)}</div>` : ""}</div></div>`).join("")}</div>` : ""}

      <div class="h">Decision queue <small>${C.queue.length} open</small></div>
      ${C.queue.length ? C.queue.map(p => qCard(p, C.mode)).join("") : `<div class="empty">${C.mode === "observe" ? "Observe mode: AdBot collects and displays data but makes no proposals." : C.sources.some(s => ["imported_activity", "imported_zero"].includes(s.state)) ? "No open proposals. Rules found nothing to change in the mature data." : "No proposals: no ad data has been imported yet."}</div>`}
      ${C.recent.length ? `<details class="fold" id="recentFold"><summary class="h">Recent outcomes <small>${C.recent.length}</small><i class="chev" aria-hidden="true"></i></summary>${C.recent.map(p => qCard(p, C.mode)).join("")}</details>` : ""}

      <div class="h">Data freshness <small>per source</small></div>
      <div class="card list">${C.sources.concat(C.store_sources).map(s => `<details class="srcrow"><summary class="item"><div class="main"><div class="name">${esc(s.source)}</div><div class="meta">${esc(s.detail || (s.last_success ? "Last import " + when(s.last_success) : ""))}</div></div>${stateChip(s)}</summary>
        <dl class="tsl"><dt>Connection check</dt><dd>${ts(s.last_connection_check)}</dd><dt>Last import attempt</dt><dd>${ts(s.last_attempt)}</dd><dt>Last successful import</dt><dd>${ts(s.last_success)}</dd><dt>Latest date covered</dt><dd>${s.latest_date_covered ? esc(String(s.latest_date_covered).slice(0, 10)) : "—"}</dd>${s.currency ? `<dt>Currency</dt><dd>${esc(s.currency)}</dd>` : ""}${s.timezone ? `<dt>Reporting time zone</dt><dd>${esc(s.timezone)}</dd>` : ""}</dl></details>`).join("")}</div>

      <div class="h">Budget pacing <small>${esc(pace.month || "")} · ${pace.days_left || "—"} of ${pace.days_in_month || "—"} days left</small></div>
      <div class="card">
        ${C.budget_errors.length ? `<div class="banner warn" style="margin:0 0 10px"><b>Budget changes are blocked.</b> ${esc(C.budget_errors[0])}</div>` : ""}
        ${tot ? `<div class="lrow" style="padding-top:0">All apps<b>${money(tot.spent_mtd)} of ${money(tot.monthly)}<small> · ${money(tot.daily_allowance)}/day left to pace</small></b></div><div class="bar"><i style="width:${Math.min(100, (tot.used_pct || 0) * 100)}%;${tot.used_pct >= 0.9 ? "background:var(--red)" : ""}"></i></div>` : ""}
        ${APPS.map(a => { const v = (pace.apps || {})[a] || {}; return `<div class="lrow"><span class="dot" style="background:${COLOR[a]}"></span>${esc(SHORT[a])}<b>${v.monthly != null ? `${money(v.spent_mtd)} of ${money(v.monthly)}<small> · ${money(v.daily_allowance)}/day</small>` : `<span class="muted">${money(v.spent_mtd)} spent · no allocation</span>`}</b></div>${v.monthly ? `<div class="bar"><i style="width:${Math.min(100, (v.used_pct || 0) * 100)}%;background:${COLOR[a]}"></i></div>` : ""}`; }).join("")}
        ${pace.unattributed_spend_mtd ? `<p class="sub" style="margin-top:8px">${money(pace.unattributed_spend_mtd)} this month in campaigns not mapped to an app.</p>` : ""}
        <p class="sub" style="margin-top:8px">${esc(pace.note || C.budget_note || "")} Spend shown is what platforms have reported so far; reserve ${pct(pace.reserve_pct || 0, 0)}.</p>
      </div>

      <div class="h">Rebalancing</div>
      <div class="card"><div class="lrow" style="padding-top:0">Configured<b>${reb.configured ? "On" : "Off"}</b></div>
        <div class="lrow">Right now<b>${reb.eligible ? '<span class="schip ok">Eligible</span>' : '<span class="schip nil">Blocked</span>'}</b></div>
        ${!reb.eligible && reb.blockers && reb.blockers.length ? `<ul class="qblock">${reb.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
        ${reb.eligible ? APPS.map(a => `<div class="lrow">${esc(SHORT[a])}<b>${money(reb.baseline[a])} → ${money(reb.proposed[a])}<small>/month (recommendation)</small></b></div>`).join("") : `<p class="sub">Baseline kept: your monthly allocations stay as set.</p>`}
        <p class="sub">${esc(reb.method || "")} Last evaluation: ${reb.last_evaluation ? when(reb.last_evaluation) : "not yet"}.</p></div>

      ${C.unmapped.length ? `<div class="h">Campaigns not mapped to an app <small>${C.unmapped.length}</small></div>
        <div class="card list">${C.unmapped.map(u => `<div class="item umap" data-ch="${esc(u.channel)}" data-cid="${esc(u.campaign_id)}"><div class="main"><div class="name">${esc(u.campaign_name || u.campaign_id)}</div><div class="meta">${esc(u.channel)} · seen ${when(u.last_seen)}</div>
          <div class="row2 umap-ctl"><select aria-label="App for ${esc(u.campaign_name || "")}"><option value="">Choose app…</option>${APPS.map(a => `<option>${esc(a)}</option>`).join("")}</select><button class="btn sm" data-map="1">Map</button><button class="btn sm" data-ign="1">Not ours</button></div></div></div>`).join("")}</div>` : ""}

      <div class="h">Performance <small>live data · last ${d.days} days</small></div>
      ${!d.has_data ? `<div class="banner">No ad performance has been imported. ${C.sources.every(s => ["not_configured", "unsupported"].includes(s.state)) ? "No ad channel has credentials on the adbot-hourly job yet." : "Check Data freshness above for each source's state."}</div>` : ""}
      <div class="kpis">
        ${k("Taps / clicks", num(T.taps), delta(T.taps, P.taps))}
        ${k("Installs", num(T.installs), T.cpi != null ? money(T.cpi) + " per install" : T.conversions_other ? num(T.conversions_other) + " Google conversions" : "")}
        ${k("Attributed revenue", T.revenue == null ? (T.revenue_by_channel && T.revenue_by_channel.length > 1 ? "Per platform" : "Not reported") : money(T.revenue), T.revenue == null ? (T.revenue_note ? T.revenue_by_channel.map(c => esc(c.channel.split(" ")[0]) + " " + money(c.revenue)).join(" · ") + " (not added: attribution overlaps)" : T.revenue_estimate ? "estimate " + money(T.revenue_estimate) + " (value per install)" : "platforms report none") : `${T.roas != null ? T.roas.toFixed(2) + "× ROAS" : ""} on ${pct(T.revenue_coverage, 0)} of spend`)}
        ${k("ACoS", T.acos == null ? "—" : pct(T.acos), T.acos == null ? "needs reported revenue" : "over spend with revenue")}
        ${k("Avg cost per tap", money(T.cpt), delta(T.cpt, P.cpt, true))}
        ${k("Prior period spend", money(P.spend), delta(T.spend, P.spend, true) + " now vs then")}
      </div>
      <div class="card"><div class="legend"><span><i style="background:#00C2FF"></i>Taps</span><span><i style="background:#00E5A3"></i>Installs</span><span><i style="background:#FFB020"></i>Spend (right)</span></div>
        <div class="chart">${lineChart(d.series, ["taps", "installs", "spend"], ["#00C2FF", "#00E5A3", "#FFB020"], "spend")}</div></div>
      ${d.by_app.length ? `<div class="h">By app &amp; channel</div><div class="card list">${d.by_app.map(b => `<div class="item"><span class="dot" style="background:${COLOR[b.app] || "#666"};margin-top:6px"></span><div class="main"><div class="name">${esc(SHORT[b.app] || b.app)} · ${esc(b.channel)}</div><div class="meta">${num(b.taps)} clicks · ${b.channel === "Google Ads" ? num(b.conversions_other) + " conversions" : num(b.installs) + " installs"} · CPT ${money(b.cpt)}</div></div><div class="right">${money(b.spend)}<small>${b.revenue == null ? "revenue not reported" : "ACoS " + (b.acos == null ? "—" : pct(b.acos, 0))}</small></div></div>`).join("")}</div>` : ""}
      <details class="fold emer" id="emFold"><summary class="h">Emergency pause <small>at the next scheduled run</small><i class="chev" aria-hidden="true"></i></summary>
        <div class="card"><p class="sub" style="margin-top:0">Records a pause for every active managed campaign / ad set in scope. Separate from Stop automation. Nothing is paused when you press it: the scheduled job sends the pauses at its next run (${when(C.next_run)}, every ${C.every_hours} h)${C.dry_run ? ", and with DRY RUN on it only simulates them" : ""}, then reports each campaign as verified, failed or unconfirmed. Nothing is resumed automatically.</p>
          <div class="row2"><select id="emApp" aria-label="App"><option value="">All apps</option>${APPS.map(a => `<option>${esc(a)}</option>`).join("")}</select><select id="emCh" aria-label="Channel"><option value="">All channels</option><option>Apple Search Ads</option><option>Meta Ads</option><option>Google Ads</option></select></div>
          <label class="sub" for="emConfirm" style="display:block;margin-top:10px">Type PAUSE to confirm</label><input id="emConfirm" class="txt" autocomplete="off" autocapitalize="characters">
          <button class="btn danger block" id="emGo" style="margin-top:10px">Pause managed campaigns</button></div></details>`;
    // wiring
    const after = msg => { toast(msg); load(false, true); };
    $("modeSel").onchange = async e => { const risky = ["approval", "bounded_auto"].includes(e.target.value); const pw = risky ? prompt("Enter the AdBot password to allow changes to be applied") : null; if (risky && !pw) { e.target.value = C.mode; return; } try { await api("/api/control/mode", {method: "POST", body: JSON.stringify({mode: e.target.value, password: pw})}); after("Mode saved: " + e.target.selectedOptions[0].text); } catch (err) { if (err.message !== "signin") toast(err.message); } };
    if ($("stopBtn")) $("stopBtn").onclick = async () => { try { const r = await api("/api/control/stop", {method: "POST", body: JSON.stringify({reason: "Stopped from the phone app."})}); after(r.note); } catch (err) { if (err.message !== "signin") toast(err.message); } };
    if ($("resumeBtn")) $("resumeBtn").onclick = async () => { const pw = prompt("Enter the AdBot password to resume automation"); if (!pw) return; try { const r = await api("/api/control/resume", {method: "POST", body: JSON.stringify({password: pw})}); after(r.note); } catch (err) { if (err.message !== "signin") toast(err.message); } };
    if ($("emClear")) $("emClear").onclick = async () => { try { const r = await api("/api/control/emergency-clear", {method: "POST"}); after(r.note); } catch (err) { if (err.message !== "signin") toast(err.message); } };
    $("emGo").onclick = async () => { $("emGo").disabled = true; try { const r = await api("/api/control/emergency-pause", {method: "POST", body: JSON.stringify({app: $("emApp").value, channel: $("emCh").value, confirm: $("emConfirm").value.trim().toUpperCase()})}); after(`${r.requested} pause request(s) recorded. ${r.note}`); } catch (err) { if (err.message !== "signin") toast(err.message); } finally { $("emGo").disabled = false; } };
    document.querySelectorAll("[data-dec]").forEach(b => b.onclick = async () => { const pid = b.closest("[data-pid]").dataset.pid; b.disabled = true;
      const hash = (b.closest("[data-pid]").querySelector("[data-hash]") || {dataset: {}}).dataset.hash;
      try { await api("/api/proposals/decide", {method: "POST", body: JSON.stringify({proposal_id: pid, approve: b.dataset.dec === "1", expected_hash: hash})}); after(b.dataset.dec === "1" ? "Approved. The scheduled job (next run " + when(C.next_run) + ") re-checks it and then " + (C.dry_run ? "simulates it: dry run, nothing is sent." : "sends it.") : "Rejected."); } catch (err) { if (err.message !== "signin") toast(err.message); b.disabled = false; } });
    document.querySelectorAll(".umap").forEach(row => {
      const send = async body => { try { await api("/api/campaigns/map", {method: "POST", body: JSON.stringify({channel: row.dataset.ch, campaign_id: row.dataset.cid, ...body})}); after("Saved. Decisions use it from the next run."); } catch (err) { if (err.message !== "signin") toast(err.message); } };
      row.querySelector("[data-map]").onclick = () => { const a = row.querySelector("select").value; if (!a) { toast("Choose an app first"); return; } send({app: a}); };
      row.querySelector("[data-ign]").onclick = () => send({app: null, ignored: true});
    });
  }

  /* ---------- Keywords ---------- */
  async function viewKeywords(my) {
    const d = await api("/api/keywords?" + qs() + "&q=" + encodeURIComponent(S.q || "")); if (my !== seq) return;
    const hasProp = i => i.proposal && i.override === "auto";
    const counts = {all: d.items.length, pause: d.items.filter(hasProp).length, paused: d.items.filter(i => i.override === "paused").length, protected: d.items.filter(i => i.override === "protected").length};
    const items = d.items.filter(i => S.kwFilter === "all" || (S.kwFilter === "pause" ? hasProp(i) : i.override === S.kwFilter));
    const status = i => i.override === "protected" ? `<span class="badge info">Protected</span>` : i.override === "paused" ? `<span class="badge bad">Paused by you</span>`
      : i.proposal ? pchip(i.proposal.status) : String(i.status || "").toUpperCase().includes("PAUSED") ? `<span class="badge mute">Paused</span>` : i.monitor_only ? `<span class="badge mute">Monitor only</span>` : `<span class="badge good">No change proposed</span>`;
    const acos = i => !i.revenue_known ? "n/a" : i.acos == null ? "—" : pct(i.acos, 0);
    $("view").innerHTML = `
      <label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span class="sr">Search</span><input id="kwq" type="search" placeholder="Search keywords or campaigns" value="${esc(S.q || "")}"></label>
      <div class="chips static">${[["all", "All"], ["pause", "Has a proposal"], ["paused", "Paused"], ["protected", "Protected"]].map(([v, l]) => `<button class="chip" data-f="${v}" aria-pressed="${S.kwFilter === v}">${l} · ${counts[v]}</button>`).join("")}</div>
      ${items.length ? items.map(i => `<div class="card" data-key="${esc(i.key)}">
        <div style="display:flex;gap:10px;align-items:flex-start"><div style="flex:1;min-width:0"><div class="name" style="font-weight:700">${esc(i.keyword || "—")}</div>
          <div class="sub"><span class="dot" style="display:inline-block;background:${COLOR[i.app] || "#666"};width:7px;height:7px;margin-right:4px"></span>${esc(SHORT[i.app] || i.app || "Other")} · ${esc(i.channel)}${i.country ? " · " + flag(i.country) : ""}</div></div>${status(i)}</div>
        ${hasProp(i) ? `<div class="sub" style="margin-top:6px;color:var(--amber)">${esc(i.proposal.reason)}${i.proposal.blockers.length ? ` · blocked: ${esc(i.proposal.blockers[0])}` : ""}</div>` : ""}
        <div class="stats"><div><small>${esc(i.click_label)}</small><b>${num(i.taps)}</b></div><div><small>${esc(i.channel === "Google Ads" ? "Conversions" : "Installs")}</small><b>${num(i.installs)}</b></div><div><small>Cost / click</small><b style="${i.cpt != null && i.cpt > d.max_cpt ? "color:var(--red)" : ""}">${money(i.cpt)}</b></div><div><small>ACoS</small><b title="${i.revenue_known ? "" : "Revenue not reported for this item"}">${acos(i)}</b></div></div>
        ${i.monitor_only ? `<div class="sub">AdBot can't change this item on ${esc(i.channel)}; reporting only.</div>` : ""}
        <div class="seg" role="group" aria-label="Override" ${i.can_pause ? "" : "hidden"}>${[["auto", "Auto", ""], ["paused", "Pause", "p"], ["protected", "Protect", "s"]].map(([v, l, c]) => `<button class="${c}" data-o="${v}" aria-pressed="${i.override === v}">${l}</button>`).join("")}</div></div>`).join("")
      : `<div class="empty">${d.items.length ? "Nothing matches." : "No keywords or campaigns yet. They appear once an ad channel is connected."}</div>`}`;
    let t; $("kwq").oninput = e => { clearTimeout(t); t = setTimeout(() => { S.q = e.target.value; load(false, true); }, 350); };
    $("view").querySelectorAll("[data-f]").forEach(b => b.onclick = () => { S.kwFilter = b.dataset.f; save(); load(false, true); });
    $("view").querySelectorAll("[data-o]").forEach(b => b.onclick = async () => {
      const key = b.closest("[data-key]").dataset.key; const o = b.dataset.o;
      try { await api("/api/override", {method: "POST", body: JSON.stringify({key, status: o})}); toast(o === "paused" ? (ME.dry_run ? "Saved. The next run records a simulated pause (dry run): nothing is sent." : "Saved. The next run pauses it.") : o === "protected" ? "Protected: AdBot won't propose changes to it" : "Back to automatic"); load(false, true); }
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
      <div class="group"><h3>Monthly budget</h3><p class="hint">One policy across Apple Ads, Meta and Google Ads. Leave the monthly budget empty to keep budget changes off. Pacing uses the real calendar month. Platforms report spend late and can overspend a daily budget, so this is a pacing target, not a guaranteed cap.</p>
        ${n("monthly_budget", R.monthly_budget ?? "", "0.01", "Monthly budget", "all apps, all channels", "$")}
        ${APPS.map((a, i) => n("am" + i, (R.app_monthly || {})[a] ?? "", "0.01", esc(a), "monthly allocation", "$")).join("")}
        ${["Apple Search Ads", "Meta Ads", "Google Ads"].map((c, i) => n("cm" + i, (R.channel_monthly || {})[c] ?? "", "0.01", esc(c), "monthly limit (optional)", "$")).join("")}
        ${n("campaign_daily_max", R.campaign_daily_max ?? "", "0.01", "Per campaign / ad set", "daily maximum (optional)", "$")}
        ${n("reserve_pct", Math.round((R.reserve_pct ?? 0.1) * 100), "1", "Reserve for late-reported spend", "", "%")}</div>
      <div class="group"><h3>Daily spend limits</h3><p class="hint">Hard daily ceilings per app, kept from the earlier setup. ${money(R.global_daily_cap * 30.4, 0)} a month at the current cap.</p>
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
        ${n("max_budget_step", Math.round(R.max_budget_step * 100), "1", "Biggest change per recommendation", "", "%")}
        ${n("exploration_share", Math.round((R.exploration_share ?? 0.1) * 100), "1", "Exploration share per app", "kept while evidence is thin", "%")}
        ${n("min_spend_for_rebalance", R.min_spend_for_rebalance ?? 50, "1", "Mature spend per app before comparing", "", "$")}</div>
      <div class="group"><h3>Change safeguards</h3><p class="hint">Enforced on the server across runs. Emergency and spend-limit pauses aren't held back by these.</p>
        ${n("max_daily_change_pct", Math.round((R.max_daily_change_pct ?? 0.3) * 100), "1", "Total budget change per campaign", "per 24 hours", "%")}
        ${n("cooldown_hours", R.cooldown_hours ?? 24, "1", "Cooldown after a change", "", "h")}
        ${n("max_actions_per_campaign_day", R.max_actions_per_campaign_day ?? 2, "1", "Changes per campaign", "per 24 hours")}
        ${n("max_actions_per_account_day", R.max_actions_per_account_day ?? 10, "1", "Changes per ad account", "per 24 hours")}
        ${n("deadband_pct", Math.round((R.deadband_pct ?? 0.1) * 100), "1", "Ignore budget changes smaller than", "", "%")}
        ${n("min_days_data", R.min_days_data ?? 3, "1", "Mature days before a rule may act")}
        ${n("min_conversions_for_revenue_rules", R.min_conversions_for_revenue_rules ?? 5, "1", "Conversions before revenue rules apply")}
        ${n("stale_after_hours", R.stale_after_hours ?? 36, "1", "Data counts as stale after", "", "h")}
        ${["Apple Search Ads", "Meta Ads", "Google Ads"].map((c, i) => n("mat" + i, (R.maturity_days || {})[c] ?? "", "1", esc(c) + " conversion delay", "days left out (empty = default)", "d")).join("")}</div>
      <div class="group"><h3>Automatic actions</h3><p class="hint">Only used in Bounded automatic mode. Anything not ticked waits for your approval.</p>
        ${[["pause_keyword", "Pause keywords"], ["pause_ad_set", "Pause Meta ad sets"], ["pause_campaign", "Pause campaigns"], ["set_budget_decrease", "Lower budgets"], ["set_budget_increase", "Raise budgets"], ["safeguard_pause", "Pause when a monthly limit is reached"]]
          .map(([id, l]) => sw("aa_" + id, (R.auto_actions || []).includes(id), l)).join("")}</div>
      <div class="group"><h3>Value per install</h3><p class="hint">A labeled estimate shown next to installs. It never drives a decision: revenue rules need revenue a platform reported. Leave at 0 if you don't have a measured value.</p>
        ${APPS.map((a, i) => n("vpi" + i, (R.value_per_install || {})[a] ?? 0, "0.01", esc(a), "", "$")).join("")}</div>
      <details class="group fold-group" id="rulesGeo" ${store.get("adbot-rules-geo-open", false) ? "open" : ""}><summary><h3>NurseBound countries</h3><span class="fold-sum">${geo.filter(g => g.enabled !== false).length} on · ${geo.filter(g => g.enabled !== false).sort((a, b) => b.weight - a.weight).slice(0, 4).map(g => flag(g.code)).join(" ")}</span><i class="chev" aria-hidden="true"></i></summary><p class="hint">Weight is each country's share of NurseBound's budget. Put the code in campaign names, e.g. “NurseBound | PH | Prospecting”.</p>
        ${geo.map((g, i) => `<div class="field"><label for="gw${i}"><span class="flag">${flag(g.code)}</span> ${esc(regionName(g.code))}${g.flag === "who_safeguards" ? "<small>On WHO's 2023 safeguards list: advertise the study app, not recruitment</small>" : g.flag === "china_reach" ? "<small>Meta and Google ads don't serve in mainland China</small>" : ""}</label><input id="gw${i}" type="number" inputmode="decimal" step="1" min="0" value="${g.weight}" style="width:70px"><span class="switch"><input id="ge${i}" type="checkbox" ${g.enabled !== false ? "checked" : ""} aria-label="${esc(regionName(g.code))} on"><i></i></span></div>`).join("")}</details>
      <div class="group"><h3>Telegram alerts</h3><p class="hint">The bot token stays on Render (TELEGRAM_BOT_TOKEN), never in the app.</p>
        <div class="field"><label for="telegram_chat_id">Chat ID</label><input id="telegram_chat_id" type="text" inputmode="numeric" value="${esc(R.telegram_chat_id || "")}" placeholder="123456789"></div>
        ${sw("al_daily_summary", R.alerts.daily_summary, "Daily summary")}${sw("al_kill_switch", R.alerts.kill_switch !== false, "Proposed and applied changes")}${sw("al_import_failure", R.alerts.import_failure !== false, "Import failures")}${sw("al_stale_data", R.alerts.stale_data !== false, "Stale data")}${sw("al_unmapped", R.alerts.unmapped !== false, "Unmapped campaigns")}${sw("al_budget", R.alerts.budget !== false, "Budget warnings")}
        <p class="hint">Emergency and execution-failure alerts are always on. Repeats are held back (critical 1 h, warning 6 h, info 24 h).</p></div>
      <div class="actions">
        <button class="btn primary block" id="saveRules">Save rules</button>
        <button class="btn" id="tgTest">Send test notification</button>
        <button class="btn orange" id="runBot">Run a decision cycle now ${ME.dry_run ? "(dry run)" : ""}</button>
        <label class="btn" for="importFile">Import rules.json from the dashboard</label><input id="importFile" type="file" accept=".json,application/json" hidden>
        <button class="btn" id="signOut">Sign out</button>
      </div>`;
    const rg = $("rulesGeo"); if (rg) rg.addEventListener("toggle", () => store.set("adbot-rules-geo-open", rg.open));
    // fold every other rules group into a tappable header with a one-line summary
    const shortName = a => SHORT[a] || a;
    const SUMMARY = {
      "Monthly budget": R.monthly_budget == null ? "not set · budget changes off" : `${money(R.monthly_budget, 0)}/month · reserve ${Math.round((R.reserve_pct ?? .1) * 100)}%`,
      "Change safeguards": `${Math.round((R.max_daily_change_pct ?? .3) * 100)}%/day · cooldown ${R.cooldown_hours ?? 24} h`,
      "Automatic actions": (R.auto_actions || []).length ? `${(R.auto_actions || []).length} allowed` : "none: all need approval",
      "Daily spend limits": `${money(R.global_daily_cap)}/day · ${APPS.map(a => shortName(a) + " " + money(R.app_caps[a], 0)).join(" · ")}`,
      "Priority": APPS.map(a => `${shortName(a)} ${(R.app_priority || {})[a] ?? 1}`).join(" · "),
      "Kill switch": `CPT > ${money(R.max_cpt)} · ACoS > ${Math.round(R.max_acos * 100)}%`,
      "Rebalancing": R.auto_rebalance ? `on when eligible · ${Math.round(R.min_share * 100)}–${Math.round(R.max_share * 100)}%` : "off",
      "Value per install": APPS.map(a => `${shortName(a)} ${(R.value_per_install || {})[a] ? money(R.value_per_install[a]) : "not set"}`).join(" · "),
      "Telegram alerts": R.telegram_chat_id ? "chat set" : "no chat ID",
    };
    const openMap = store.get("adbot-rules-open", {});
    $("view").querySelectorAll("div.group").forEach(g => {
      const h = g.querySelector(":scope > h3"); if (!h) return;
      const title = h.textContent.trim();
      const d = document.createElement("details"); d.className = "group fold-group"; d.open = !!openMap[title];
      const sm = document.createElement("summary");
      sm.innerHTML = `<h3>${esc(title)}</h3><span class="fold-sum">${esc(SUMMARY[title] || "")}</span><i class="chev" aria-hidden="true"></i>`;
      h.remove(); d.appendChild(sm);
      while (g.firstChild) d.appendChild(g.firstChild);
      g.replaceWith(d);
      d.addEventListener("toggle", () => { const m = store.get("adbot-rules-open", {}); m[title] = d.open; store.set("adbot-rules-open", m); });
    });
    const v = id => +$(id).value;
    const opt = id => $(id).value.trim() === "" ? null : +$(id).value;
    $("saveRules").onclick = async () => {
      const body = {global_daily_cap: v("global_daily_cap"), max_cpt: v("max_cpt"), min_taps_for_cpt: v("min_taps_for_cpt"), zero_install_taps: v("zero_install_taps"),
        max_acos: v("max_acos") / 100, min_spend_for_acos: v("min_spend_for_acos"), target_acos: v("target_acos") / 100, auto_rebalance: $("auto_rebalance").checked,
        min_share: v("min_share") / 100, max_share: v("max_share") / 100, max_budget_step: v("max_budget_step") / 100, telegram_chat_id: $("telegram_chat_id").value.trim(),
        app_caps: Object.fromEntries(APPS.map((a, i) => [a, v("cap" + i)])), app_priority: Object.fromEntries(APPS.map((a, i) => [a, v("pri" + i)])),
        value_per_install: Object.fromEntries(APPS.map((a, i) => [a, v("vpi" + i)])),
        alerts: {daily_summary: $("al_daily_summary").checked, kill_switch: $("al_kill_switch").checked, import_failure: $("al_import_failure").checked,
          stale_data: $("al_stale_data").checked, unmapped: $("al_unmapped").checked, budget: $("al_budget").checked},
        monthly_budget: opt("monthly_budget"), campaign_daily_max: opt("campaign_daily_max"), reserve_pct: v("reserve_pct") / 100,
        app_monthly: Object.fromEntries(APPS.map((a, i) => [a, opt("am" + i)]).filter(x => x[1] != null)),
        channel_monthly: Object.fromEntries(["Apple Search Ads", "Meta Ads", "Google Ads"].map((c, i) => [c, opt("cm" + i)]).filter(x => x[1] != null)),
        maturity_days: Object.fromEntries(["Apple Search Ads", "Meta Ads", "Google Ads"].map((c, i) => [c, opt("mat" + i)]).filter(x => x[1] != null)),
        max_daily_change_pct: v("max_daily_change_pct") / 100, cooldown_hours: v("cooldown_hours"), max_actions_per_campaign_day: v("max_actions_per_campaign_day"),
        max_actions_per_account_day: v("max_actions_per_account_day"), deadband_pct: v("deadband_pct") / 100, min_days_data: v("min_days_data"),
        min_conversions_for_revenue_rules: v("min_conversions_for_revenue_rules"), stale_after_hours: v("stale_after_hours"),
        exploration_share: v("exploration_share") / 100, min_spend_for_rebalance: v("min_spend_for_rebalance"),
        auto_actions: ["pause_keyword", "pause_ad_set", "pause_campaign", "set_budget_decrease", "set_budget_increase", "safeguard_pause"].filter(id => $("aa_" + id).checked),
        geo_targets: {NurseBound: geo.map((g, i) => ({code: g.code, weight: v("gw" + i), enabled: $("ge" + i).checked}))}};
      $("saveRules").disabled = true;
      try { const r = await api("/api/rules", {method: "POST", body: JSON.stringify(body)}); toast(r.changed && r.changed.length ? `Saved ${r.changed.length} change${r.changed.length > 1 ? "s" : ""}. Used from the next decision run.` : "Saved. Nothing changed."); }
      catch (e) { if (e.message !== "signin") toast(e.message); } finally { $("saveRules").disabled = false; }
    };
    $("tgTest").onclick = async () => { try { const r = await api("/api/telegram-test", {method: "POST"}); toast(r.ok ? "Sent. Check Telegram." : r.error); } catch (e) { if (e.message !== "signin") toast(e.message); } };
    $("runBot").onclick = async () => { $("runBot").disabled = true; try { const r = await api("/api/run-bot", {method: "POST"}); const pr = Object.entries(r.proposals).map(([k, n]) => `${n} ${k}`).join(", ") || "no proposals"; const ex = Object.entries(r.executions).map(([k, n]) => `${n} ${k}`).join(", ") || "nothing executed"; toast(`${r.mode}${r.dry_run ? " · dry run" : ""}: ${pr}; ${ex}`); } catch (e) { if (e.message !== "signin") toast(e.message); } finally { $("runBot").disabled = false; } };
    $("importFile").onchange = async () => { const f = $("importFile").files[0]; if (!f) return; try { const data = JSON.parse(await f.text()); await api("/api/rules", {method: "POST", body: JSON.stringify({import: data})}); toast("Imported."); load(false, true); } catch (e) { if (e.message !== "signin") toast("Couldn't import: " + e.message); } };
    $("signOut").onclick = async () => { try { await api("/api/logout", {method: "POST"}); } catch (e) {} showLogin(); };
  }

  /* ---------- Trading ---------- */
  async function viewTrading(my) {
    const d = await api("/api/trading?demo=1"); if (my !== seq) return;
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
      ${r && r.kill_switch ? `<div class="banner bad" style="margin-top:10px"><b>Trading is halted.</b> ${esc(r.notes || "")}</div>` : ""}
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
  const REP_BAD = ["access_denied", "not_found", "error"];
  // the shared status model (control/status.py): one chip for every source on every tab
  const STATE_CLS = {imported_activity: "ok", imported_zero: "ok", connected_not_imported: "info", partial: "warn", stale: "warn",
                     failed: "bad", not_configured: "nil", unsupported: "nil"};
  const stateChip = sh => `<span class="schip ${STATE_CLS[sh.state] || "nil"}" title="${esc(sh.detail || "")}">${esc(sh.label || sh.state)}</span>`;
  const srcState = s => s.shared && s.shared.state ? ({ok: "on", info: "keys", warn: "part", bad: "err", nil: "off"}[STATE_CLS[s.shared.state]] || "off") : s.status === "verified" ? (s.reports && REP_BAD.includes(s.reports.state) ? "part" : "on")
    : (s.status === "error" || s.last_status === "failed") ? "err" : s.status === "configured" ? "keys" : "off";
  const STATE_TXT = {on: "LINKED", part: "PARTIAL", err: "ERROR", keys: "KEYS SET", off: "OFFLINE"};

  function readinessRing(sources) {
    // one arc per data source: lit = linked, amber = keys set, red = error, dim = offline (real status only)
    const n = sources.length, R = 54, C = 2 * Math.PI * R, gap = 7, seg = C / n - gap;
    const col = {on: "#00E5A3", keys: "#FFB020", part: "#FFB020", err: "#FF4B4B", off: "rgba(148,163,184,.18)"};
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

  // Plan | iOS price | Android price | Product status. Identity, price and availability are separate facts.
  function planRows(a) {
    const order = Object.keys(PLAN);
    const codes = [...new Set(a.products.map(p => p.plan_code))].sort((x, y) => order.indexOf(x) - order.indexOf(y));
    const usd = p => (p.prices || []).find(x => x.currency === "USD" && x.territory === "US") || null;
    const cell = (verified, ref, platform) => {
      if (verified) {
        const pr = usd(verified);
        if (pr && pr.source === "store_api") return `<b>${money(+pr.amount)}</b>`;
        return `<span class="muted">Not retrieved</span>${ref && usd(ref) ? `<small>reference ${money(+usd(ref).amount)}</small>` : ""}`;
      }
      if (ref && usd(ref)) return `<span class="muted">${money(+usd(ref).amount)}</span><small>reference only</small>`;
      return platform === "google_play" ? `<span class="muted">Not retrieved</span>` : `<span class="muted">—</span>`;
    };
    return codes.map(code => {
      const of = (plat, ver) => a.products.find(p => p.plan_code === code && p.platform === plat && !!p.verified === ver);
      const iv = of("apple_app_store", true), ir = of("apple_app_store", false), av = of("google_play", true), ar = of("google_play", false);
      const typed = iv || av;
      let label = PLAN[code] || code, sub = "";
      if (typed && typed.product_type === "auto_renewable_subscription") {
        label = {P1M: "Monthly subscription", P3M: "Quarterly subscription", P1Y: "Annual subscription"}[typed.billing_period] || label;
        sub = "auto-renewing";
      } else if (typed && typed.product_type === "non_renewing_subscription") { sub = "non-renewing pass"; }
      else if (typed && ["non_consumable", "one_time_product"].includes(typed.product_type)) { sub = "one-time purchase"; }
      else if (!typed) { sub = "type not verified"; }
      const st = [];
      st.push(iv ? `<span class="st ok">iOS identity ✓</span>` : `<span class="st nil">iOS not verified</span>`);
      st.push(av ? `<span class="st ok">Android identity ✓</span>` : `<span class="st nil">Android not verified</span>`);
      const avail = [iv, av].filter(Boolean).map(p => p.availability && p.availability !== "unknown" ? `${p.platform === "google_play" ? "Android" : "iOS"}: ${esc(String(p.availability).replace(/_/g, " ").toLowerCase())}` : "").filter(Boolean);
      if (avail.length) st.push(`<span class="st">${avail.join(" · ")}</span>`);
      return {label, sub, ios: cell(iv, ir, "apple_app_store"), and: cell(av, ar, "google_play"), status: st.join("")};
    });
  }

  /* ---------- App race: running totals per app, from imported store reports (revenue/compare.py) ---------- */
  const RACE_HUE = {...APP_HUE, vitalsync: ["#FACC15", "#A3E635"]};
  const RACE_NAME = {rnr_pro: "RNR Pro", ccrn_adult: "CCRN Adult", nursebound_pro: "NurseBound Pro", vitalsync: "VitalSync"};
  const RACE_METRIC = {proceeds: {label: "Proceeds", col: 3, fmt: v => money(v), none: "No sales",
                         axis: (v, step) => "$" + (v >= 1000 ? (v / 1000).toFixed(step >= 1000 && step % 1000 === 0 ? 0 : 1) + "k" : v.toFixed(step % 1 ? 2 : 0))},
                       downloads: {label: "Downloads", col: 1, fmt: v => num(v), axis: v => num(v), none: "No downloads", count: true},
                       paid: {label: "Paid units", col: 2, fmt: v => num(v), axis: v => num(v), none: "No paid units", count: true}};
  const dayAdd = (iso, n) => { const t = new Date(iso + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
  const dayLabel = iso => new Date(iso + "T00:00:00Z").toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"});

  function raceData(cmp, apps, metric, range) {
    const m = RACE_METRIC[metric], end = dayAdd(cmp.today, -1), start = dayAdd(end, -(range - 1)), prevStart = dayAdd(start, -range);
    const days = Array.from({length: range}, (_, i) => dayAdd(start, i));
    const lines = apps.map(a => {
      const rows = cmp.series[a.app_id] || [];
      const byDay = new Map(rows.filter(r => r[0] >= start && r[0] <= end && r[m.col] != null).map(r => [r[0], r[m.col]]));
      const prevRows = rows.filter(r => r[0] >= prevStart && r[0] < start && r[m.col] != null);
      let run = 0; const cum = days.map(d => (run += byDay.get(d) || 0));
      return {...a, cum, total: byDay.size ? run : null, prev: prevRows.length ? prevRows.reduce((s, r) => s + r[m.col], 0) : null,
              hue: RACE_HUE[a.app_id] || ["#00C2FF", "#E024C3"], short: RACE_NAME[a.app_id] || a.name};
    });
    return {days, start, end, lines};
  }

  function raceSvg(rd, metric, animate) {
    const m = RACE_METRIC[metric], W = 360, H = 196, L = 8, R = 46, T = 14, B = 24, iw = W - L - R, ih = H - T - B;
    const n = rd.days.length, max = Math.max(...rd.lines.map(l => l.cum[n - 1]), 0);
    // round axis steps (1, 2, 2.5, 5 × 10^k), four of them, with a little headroom above the leader
    const step = (() => { const raw = max * 1.08 / 4; if (!(raw > 0)) return 0; const p = Math.pow(10, Math.floor(Math.log10(raw)));
      const st = [1, 2, 2.5, 5, 10].map(f => f * p).find(v => v >= raw);
      return m.count ? Math.max(1, Math.ceil(st)) : st; })();   // whole numbers for counts
    const top = max > 0 ? step * 4 : 1, x = i => L + (n === 1 ? iw : iw * i / (n - 1)), y = v => T + ih - ih * v / top;
    const grid = [0, .25, .5, .75, 1].map(f => `<line x1="${L}" x2="${L + iw}" y1="${(T + ih * (1 - f)).toFixed(1)}" y2="${(T + ih * (1 - f)).toFixed(1)}" class="rc-grid${f === 0 ? " base" : ""}"/>`
      + (max > 0 && f > 0 ? `<text x="${W - 2}" y="${(T + ih * (1 - f) + 3).toFixed(1)}" class="rc-ax" text-anchor="end">${esc(m.axis(top * f, step))}</text>` : "")).join("");
    const ticks = [0, Math.floor((n - 1) / 2), n - 1].map(i => `<text x="${x(i).toFixed(1)}" y="${H - 6}" class="rc-ax" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(dayLabel(rd.days[i]))}</text>`).join("");
    // leader last, so its line sits on top
    const order = rd.lines.map((l, i) => i).sort((a, b) => (rd.lines[a].total ?? -1) - (rd.lines[b].total ?? -1));
    const defs = rd.lines.map((l, i) => `<linearGradient id="rcg${i}" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="${l.hue[0]}"/><stop offset="1" stop-color="${l.hue[1]}"/></linearGradient>
      <linearGradient id="rcf${i}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${l.hue[1]}" stop-opacity=".28"/><stop offset="1" stop-color="${l.hue[1]}" stop-opacity="0"/></linearGradient>`).join("");
    const leader = rd.lines.reduce((b, l) => l.total != null && (b == null || l.total > b.total) ? l : b, null);
    const paths = order.map(i => {
      const l = rd.lines[i];
      // a step line: running totals only change on days with activity
      const d = "M" + l.cum.map((v, k) => (k ? `H${x(k).toFixed(1)}V${y(v).toFixed(1)}` : `${x(0).toFixed(1)},${y(v).toFixed(1)}`)).join("");
      if (l.total == null) return `<path d="M${L},${T + ih}H${L + iw}" class="rc-idle" style="stroke:${l.hue[0]}"/>`;
      const area = `${d}V${T + ih}H${x(0).toFixed(1)}Z`, ex = x(n - 1), ey = y(l.cum[n - 1]);
      return `${l === leader ? `<path d="${area}" fill="url(#rcf${i})" class="${animate ? "rc-fade" : ""}"/>` : ""}
        <path d="${d}" class="rc-line${animate ? " rc-draw" : ""}" stroke="url(#rcg${i})" pathLength="1" filter="url(#rcglow)"/>
        <circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.6" fill="${l.hue[1]}" style="color:${l.hue[1]}" class="rc-dot${animate ? " rc-fade" : ""}"/>
        <circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.6" fill="none" stroke="${l.hue[1]}" class="rc-ping"/>`;
    }).join("");
    const empty = !leader ? `<text x="${L + iw / 2}" y="${T + ih / 2 - 4}" text-anchor="middle" class="rc-empty">${esc(m.none)} recorded</text>
      <text x="${L + iw / 2}" y="${T + ih / 2 + 14}" text-anchor="middle" class="rc-empty2">${esc(dayLabel(rd.start))} – ${esc(dayLabel(rd.end))}</text>` : "";
    return `<svg class="rc-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Running ${esc(m.label.toLowerCase())} per app, ${esc(dayLabel(rd.start))} to ${esc(dayLabel(rd.end))}">
      <defs>${defs}<filter id="rcglow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
      ${grid}${ticks}${paths}${empty}
      <g class="rc-cross" hidden><line class="rc-vline" y1="${T}" y2="${T + ih}"/></g>
      <rect class="rc-hit" x="${L}" y="${T}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  }

  function raceStandings(rd, metric) {
    const m = RACE_METRIC[metric];
    const sum = rd.lines.reduce((s, l) => s + (l.total || 0), 0);
    const ranked = rd.lines.filter(l => l.total != null).sort((a, b) => b.total - a.total);
    const idle = rd.lines.filter(l => l.total == null);
    const idleRow = idle.length ? `<li class="rs-idle">${idle.map(l => `<span style="--h1:${l.hue[0]}"><i></i>${esc(l.short)}</span>`).join("")}
      <small>${esc(m.none)} in imported store reports for this period</small></li>` : "";
    return ranked.map((l, i) => {
      const share = sum > 0 ? l.total / sum : 0;
      const delta = l.prev == null ? `<em class="new">none in the previous ${rd.days.length} days</em>`
        : l.prev === 0 ? "" : (() => { const ch = (l.total - l.prev) / Math.abs(l.prev); return `<em class="${ch >= 0 ? "up" : "down"}">${ch >= 0 ? "▲" : "▼"} ${pct(Math.abs(ch), 0)} vs previous ${rd.days.length} days</em>`; })();
      return `<li class="rs" style="--h1:${l.hue[0]};--h2:${l.hue[1]};--w:${(share * 100).toFixed(1)}%">
        <span class="rs-rank">${i + 1}</span>
        <div class="rs-main"><div class="rs-top"><b>${esc(l.short)}</b>${l.extra ? `<small class="rs-tag">tracked, not managed</small>` : ""}<span class="rs-v">${esc(m.fmt(l.total))}</span></div>
          <div class="rs-bar"><i></i></div>
          <div class="rs-sub">${pct(share, 0)} of the total${delta ? " · " : ""}${delta}</div></div></li>`;
    }).join("") + idleRow;
  }

  function mountRace(d, animate) {
    const host = $("race"); if (!host || !d.compare) return;
    const cmp = d.compare, st = {metric: "proceeds", range: 30, ...store.get("adbot-race", {})};
    if (![7, 30, 90].includes(+st.range)) st.range = 30;
    if (!RACE_METRIC[st.metric]) st.metric = "proceeds";
    const apps = d.apps.map(a => ({app_id: a.app_id, name: a.name})).concat((cmp.extra_apps || []).map(a => ({...a, extra: true})));
    const rd = raceData(cmp, apps, st.metric, st.range);
    const hasPlay = d.sources.some(s => s.platform === "google_play" && (s.rows || {}).sales_reports);
    const seg = (key, opts) => `<div class="rc-seg" role="group">${opts.map(([v, t]) => `<button type="button" data-${key}="${v}" aria-pressed="${String(st[key]) === String(v)}">${t}</button>`).join("")}</div>`;
    host.innerHTML = `<div class="rc-ctl">${seg("metric", Object.entries(RACE_METRIC).map(([k, v]) => [k, v.label]))}${seg("range", [[7, "7 days"], [30, "30 days"], [90, "90 days"]])}</div>
      <div class="rc-stage"><div class="rc-read" aria-live="polite" hidden></div>${raceSvg(rd, st.metric, animate)}</div>
      <ol class="rs-list">${raceStandings(rd, st.metric)}</ol>
      <p class="rc-note">Running totals from App Store${hasPlay ? " and Google Play" : ""} sales reports${cmp.estimated ? ", estimated until Apple's finance reports settle" : ""}. ${!hasPlay ? "Google Play isn't included until its reports import. " : ""}${st.metric === "proceeds" ? "Proceeds in USD only" + (cmp.other_currency_rows ? ` (${num(cmp.other_currency_rows)} rows in other currencies left out)` : "") + ". " : ""}${(cmp.extra_apps || []).length ? "VitalSync is compared here but isn't run by the ad bot." : ""}</p>`;
    host.querySelectorAll("[data-metric]").forEach(b => b.onclick = () => { store.set("adbot-race", {...st, metric: b.dataset.metric}); mountRace(d, true); });
    host.querySelectorAll("[data-range]").forEach(b => b.onclick = () => { store.set("adbot-race", {...st, range: +b.dataset.range}); mountRace(d, true); });
    // scrub: drag or hover across the chart to read every app's running total on that day
    const svg = host.querySelector(".rc-svg"), hit = svg.querySelector(".rc-hit"), cross = svg.querySelector(".rc-cross"), vline = svg.querySelector(".rc-vline"), read = host.querySelector(".rc-read");
    const m = RACE_METRIC[st.metric], n = rd.days.length;
    if (!rd.lines.some(l => l.total != null)) return;
    const show = ev => {
      const box = hit.getBoundingClientRect(); if (!box.width) return;
      const f = Math.min(1, Math.max(0, (ev.clientX - box.left) / box.width)), k = Math.round(f * (n - 1));
      const vx = +hit.getAttribute("x") + (+hit.getAttribute("width")) * (n === 1 ? 1 : k / (n - 1));
      vline.setAttribute("x1", vx); vline.setAttribute("x2", vx); cross.hidden = false; read.hidden = false;
      read.innerHTML = `<b>${esc(dayLabel(rd.days[k]))}</b>${[...rd.lines].filter(l => l.total != null).sort((a, b) => b.cum[k] - a.cum[k]).map(l => `<span style="--h:${l.hue[1]}"><i></i>${esc(l.short)} <em>${esc(m.fmt(l.cum[k]))}</em></span>`).join("")}`;
    };
    const hide = () => { cross.hidden = true; read.hidden = true; };
    hit.addEventListener("pointermove", show); hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", hide); hit.addEventListener("pointercancel", hide);
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
    // Google Play: product sync and report sync are shown separately (revenue/google_play.py report_state)
    const REP_CLS = {ok_imported: "ok", ok_empty: "ok", not_configured: "warn", never_checked: "nil", access_denied: "bad", not_found: "bad", error: "bad"};
    const repBlock = s => { const r = s.reports; const bad = REP_BAD.includes(r.state);
      return `<div class="rep">
        <div class="idr"><span>Products</span><b class="${s.products_last_success ? "ok" : "nil"}">${s.products_last_success ? esc(when(s.products_last_success)) : "NEVER"}</b></div>
        <div class="idr"><span>Reports</span><b class="${REP_CLS[r.state] || "warn"}">${esc((r.text || r.state).toUpperCase())}</b></div>
        ${r.last_attempt ? `<div class="idnote">Last attempt ${esc(when(r.last_attempt))} · last success ${esc(when(r.last_success))}${bad && r.next_try ? ` · next automatic try ${esc(when(r.next_try))}` : ""}</div>` : ""}
        ${bad && r.message ? `<div class="src-err">${esc(String(r.message).split(" | ")[0])}</div>` : ""}
        ${r.retry_requested_at ? `<div class="idnote">Retry queued for the next scheduled run</div>`
          : (bad || r.state === "never_checked") ? `<button class="hx-btn" type="button" data-retry-play style="--h1:#00E5A3;margin-top:8px">Retry reports</button>` : ""}
      </div>`; };
    const srcCard = s => { const st = srcState(s); const sh = s.shared || {}; return `<div class="src ${st}" style="--hue:${PLAT_HUE[s.platform] || "#00C2FF"}">
        <div class="src-top"><span class="led"></span><b>${esc(PLAT[s.platform] || s.name)}</b>${sh.state ? stateChip(sh) : `<span class="chip ${st}">${STATE_TXT[st]}</span>`}</div>
        <div class="src-meta">LAST SYNC <b>${when(s.last_success)}</b>${s.period_end ? ` · DATA TO ${esc(String(s.period_end).slice(0, 10))}` : ""}</div>
        ${s.last_status === "failed" && s.last_message ? `<div class="src-err">${esc(s.last_message)}</div>` : s.status === "error" && s.last_error ? `<div class="src-err">${esc(s.last_error)}</div>` : ""}
        ${s.last_status === "partial" && s.last_message ? `<div class="src-warn">${esc(String(s.last_message).split(" | ")[0])}</div>` : ""}
        ${s.reports ? repBlock(s) : ""}
        ${s.credentials.missing.length ? `<details><summary>Setup · ${s.credentials.missing.length} Render setting${s.credentials.missing.length > 1 ? "s" : ""} needed</summary>${s.credentials.missing.map(v => `<code>${esc(v)}</code>`).join("")}</details>` : ""}</div>`; };
    const idRow = (label, val, empty = "NOT SET") => `<div class="idr"><span>${label}</span><b class="${val ? "" : "nil"}">${val ? esc(val) : empty}</b></div>`;
    // result of "Check store IDs" (revenue/store_check.py)
    const CHK = {verified: ["ok", "VERIFIED ✓"], mismatch: ["bad", "MISMATCH"], not_found: ["bad", "NOT FOUND"],
                 no_access: ["warn", "NO ACCESS"], no_keys: ["warn", "NEEDS KEY"], error: ["bad", "ERROR"]};
    const STORE_STATE = {READY_FOR_SALE: "Live", READY_FOR_DISTRIBUTION: "Live", PREPARE_FOR_SUBMISSION: "Not submitted",
                 WAITING_FOR_REVIEW: "Waiting for review", IN_REVIEW: "In review", PENDING_DEVELOPER_RELEASE: "Approved, not released",
                 PENDING_APPLE_RELEASE: "Approved, releasing", PROCESSING_FOR_APP_STORE: "Processing", REJECTED: "Rejected",
                 METADATA_REJECTED: "Metadata rejected", DEVELOPER_REJECTED: "Pulled from review", INVALID_BINARY: "Invalid build",
                 REMOVED_FROM_SALE: "Removed from sale", DEVELOPER_REMOVED_FROM_SALE: "Removed from sale", NO_VERSION: "No version yet",
                 in_play_console: "In Play Console"};
    const hhmm = t => new Date(t + (String(t).endsWith("Z") ? "" : "Z")).toLocaleTimeString("en-US", {hour: "numeric", minute: "2-digit"});
    const checkRows = (l, hasId) => {
      if (!hasId) return idRow("Check", null, "ENTER IDS FIRST");
      if (l.check_requested_at) return `<div class="idr"><span>Check</span><b class="warn">QUEUED</b></div><div class="idnote">Runs on the next scheduled run${l.check_result ? ` · last: ${esc((CHK[l.check_result] || [])[1] || l.check_result)}` : ""}</div>`;
      if (!l.check_result) return idRow("Check", null, "NOT CHECKED");
      const [cls, txt] = CHK[l.check_result] || ["warn", l.check_result.toUpperCase()];
      const st = l.publication_status && l.publication_status !== "unknown" && l.publication_status !== "not_found" ? (STORE_STATE[l.publication_status] || l.publication_status) : null;
      return `<div class="idr"><span>Check</span><b class="${cls}">${txt}</b></div>${st ? idRow("Store", st) : ""}
        <div class="idnote">${esc(l.check_detail || "")}${l.status_checked_at ? ` · ${esc(when(l.status_checked_at))}` : ""}</div>`;
    };

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

      <div class="hx-h"><span>APP RACE</span><small>running totals</small></div>
      <section class="race" id="race"></section>

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

      <div class="hx-h withbtn"><span>APPS</span><small>${d.apps.length} registered</small><button class="hx-btn" type="button" id="checkIds" style="--h1:#00C2FF">Check store IDs</button></div>
      <div class="appgrid">${d.apps.map(a => {
        const ios = a.listings.apple_app_store || {}, and = a.listings.google_play || {};
        const [h1, h2] = APP_HUE[a.app_id] || ["#00C2FF", "#E024C3"];
        const live = new Set(a.products.filter(p => p.verified).map(p => p.platform + ":" + p.plan_code));
        const prods = [...a.products].filter(p => p.verified || !live.has(p.platform + ":" + p.plan_code)).sort((x, y) => Object.keys(PLAN).indexOf(x.plan_code) - Object.keys(PLAN).indexOf(y.plan_code));
        const price = p => { const pr = (p.prices || []).find(x => x.currency === "USD" && x.territory === "US") || (p.prices || []).find(x => x.currency === "USD"); return pr ? +pr.amount : null; };
        const max = Math.max(1, ...prods.map(price).filter(v => v != null));
        const openApps = store.get("adbot-apps-open", {});
        const okIos = !!(ios.apple_app_id && ios.bundle_id), okAnd = !!and.package_name;
        const verified = prods.filter(p => p.verified).length;
        const brief = `${okIos ? "iOS ✓" : "iOS IDs missing"} · ${okAnd ? "Android ✓" : "Android ID missing"} · ${prods.length} plan${prods.length === 1 ? "" : "s"}${verified ? ` (${verified} ✓)` : ""}`;
        return `<article class="appx" style="--h1:${h1};--h2:${h2}"><details class="appfold" data-app="${esc(a.app_id)}" ${openApps[a.app_id] ? "open" : ""}>
          <summary><header><div><div class="appx-name">${esc(a.name)}</div><div class="appx-sub">AD BOT · ${esc(a.bot_app_name || "—")}</div>
              <div class="appx-brief">${esc(brief)}</div></div>
            <span class="appx-right"><button class="hx-btn" type="button" data-edit="${esc(a.app_id)}">Edit IDs</button><i class="chev" aria-hidden="true"></i></span></header></summary>
          <div class="ptable-wrap"><table class="ptable"><caption>Plans and prices <small>US storefront · USD</small></caption>
            <thead><tr><th scope="col">Plan</th><th scope="col">iOS price</th><th scope="col">Android price</th><th scope="col">Product status</th></tr></thead>
            <tbody>${planRows(a).map(r => `<tr><th scope="row">${esc(r.label)}${r.sub ? `<small>${esc(r.sub)}</small>` : ""}</th>
              <td>${r.ios}</td><td>${r.and}</td><td class="pstat">${r.status}</td></tr>`).join("")}</tbody></table></div>
          <p class="pnote">Identity = the store confirmed the product exists · Price = read from the store (reference = your price list, not a store price) · Availability = as the store reports it.</p>
          <details class="diag"><summary>Store IDs and checks</summary>
          <div class="ids">
            <div class="idcol"><div class="idh">App Store</div>${idRow("Apple ID", ios.apple_app_id)}${idRow("Bundle", ios.bundle_id)}${checkRows(ios, !!ios.apple_app_id)}</div>
            <div class="idcol"><div class="idh">Google Play</div>${idRow("Package", and.package_name)}${checkRows(and, !!and.package_name)}</div>
          </div></details>
          <form class="idform" data-app="${esc(a.app_id)}" hidden>
            <div class="field"><label>Apple ID<small>App Store Connect → App Information</small></label><input type="text" name="apple_app_id" inputmode="numeric" value="${esc(ios.apple_app_id || "")}" placeholder="1234567890"></div>
            <div class="field"><label>Bundle ID</label><input type="text" name="bundle_id" autocapitalize="off" value="${esc(ios.bundle_id || "")}" placeholder="com.example.app"></div>
            <div class="field"><label>Play package</label><input type="text" name="package_name" autocapitalize="off" value="${esc(and.package_name || "")}" placeholder="com.example.app"></div>
            <button class="btn primary block" type="submit">Save IDs</button>
          </form>
          </details></article>`; }).join("")}</div>

      <details class="fold hx-fold" id="adsFold" ${store.get("adbot-adplat-open", false) ? "open" : ""}>
        <summary class="hx-h"><span>AD PLATFORMS</span><small>${ads.filter(x => srcState(x) === "on").length}/${ads.length} linked${ads.some(x => srcState(x) === "err") ? " · " + ads.filter(x => srcState(x) === "err").length + " error" : ""}</small><i class="chev" aria-hidden="true"></i></summary>
        <div class="src-grid two">${ads.map(srcCard).join("")}</div>
      </details>
      <p class="hx-foot">READ-ONLY · NOTHING HERE CHANGES STORE PRODUCTS, PRICES OR PURCHASES</p>
    </div>`;
    mountRace(d, true);
    document.querySelectorAll("[data-edit]").forEach(b => b.onclick = e => {
      e.preventDefault(); e.stopPropagation();   // the button sits in the card header: don't also fold/unfold the card
      const f = document.querySelector(`.idform[data-app="${b.dataset.edit}"]`); f.hidden = !f.hidden; b.textContent = f.hidden ? "Edit IDs" : "Close";
      const card = b.closest("details"); if (!f.hidden && card) card.open = true;
    });
    document.querySelectorAll("[data-retry-play]").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { const r = await api("/api/revenue/retry-play-reports", {method: "POST"}); toast(`Report retry queued for the scheduled job (${hhmm(r.next_run)}). Products aren't re-synced.`); load(false, true); }
      catch (err) { if (err.message !== "signin") toast(err.message); b.disabled = false; }
    });
    const cb = $("checkIds"); if (cb) cb.onclick = async () => {
      cb.disabled = true; cb.textContent = "Checking…";
      const NAME = {apple_app_store: "App Store", google_play: "Google Play"};
      try {
        const r = await api("/api/revenue/check-ids", {method: "POST"});
        const parts = Object.entries(r.checked || {}).map(([p, c]) => `${NAME[p]}: ${Object.entries(c).map(([k, n]) => `${n} ${(CHK[k] || [, k])[1].replace(" ✓", "").toLowerCase()}`).join(", ")}`);
        const q = Object.keys(r.queued || {}).map(p => NAME[p]);
        if (q.length) parts.push(`${q.join(" + ")} queued for the scheduled job (${hhmm(r.next_run)})`);
        toast(parts.join(" · ") || "Checked."); load(false, true);
      } catch (err) { if (err.message !== "signin") toast(err.message); cb.disabled = false; cb.textContent = "Check store IDs"; }
    };
    const af = $("adsFold"); if (af) af.addEventListener("toggle", () => store.set("adbot-adplat-open", af.open));
    document.querySelectorAll("details.appfold").forEach(dd => dd.addEventListener("toggle", () => {
      const m = store.get("adbot-apps-open", {}); m[dd.dataset.app] = dd.open; store.set("adbot-apps-open", m);
    }));
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
    const t = x => x ? new Date(String(x).replace(" ", "T") + (String(x).endsWith("Z") ? "" : "Z")).toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : "";
    const DS = {delivered: "ok", logged: "info", suppressed: "nil", failed: "bad", disabled: "nil"};
    const RES = {applied: "ok", reconciled_applied: "ok", simulated: "sim", failed: "bad", uncertain: "bad", reconciled_not_applied: "bad", already_done: "ok", skipped: "nil"};
    const sec = (id, title, n, body) => `<details class="fold" id="${id}" ${store.get("adbot-log-" + id, id === "lgDec") ? "open" : ""}><summary class="h">${title} <small>${n}</small><i class="chev" aria-hidden="true"></i></summary>${body}</details>`;
    $("view").innerHTML = `
      ${d.dry_run ? `<div class="banner warn"><b>Dry run.</b> Changes marked <b>Simulated</b> were never sent to an ad platform. Set DRY_RUN=false on the adbot-hourly job and choose Approval or Bounded automatic mode to allow live changes.</div>` : ""}
      ${sec("lgDec", "Decisions", d.decisions.length, `<div class="card list">${d.decisions.length ? d.decisions.map(x => `<div class="item"><div class="main"><div class="name">${esc(ACT[x.action] || x.action)}: ${esc(x.entity_name || "")}</div><div class="meta">${esc(SHORT[x.app] || x.app || "")} · ${esc(x.channel || "")} · ${esc(KIND[x.kind] || x.kind)} · ${esc(x.reason || "")}${x.outcome && !String(x.outcome).startsWith("claim:") ? " · " + esc(x.outcome) : ""}</div></div><div class="right">${pchip(x.status)}<small>${t(x.outcome_at || x.created_at)}</small></div></div>`).join("") : `<div class="empty">No decisions yet.</div>`}</div>`)}
      ${sec("lgEx", "Executions", d.executions.length, `<div class="card list">${d.executions.length ? d.executions.map(x => `<div class="item"><div class="main"><div class="name">${esc(ACT[x.action] || x.action)}: ${esc(x.entity_name || "")}</div><div class="meta">${esc(x.channel)} · ${x.mode === "live" ? "live" : "simulated"}${x.error ? ` · <span style="color:var(--red)">${esc(x.error)}</span>` : ""}</div></div><div class="right"><span class="schip ${RES[x.result] || "nil"}">${esc(String(x.result).replace(/_/g, " "))}</span><small>${t(x.attempted_at)}</small></div></div>`).join("") : `<div class="empty">Nothing executed yet.</div>`}</div>`)}
      ${sec("lgAl", "Alerts", d.alerts.length, `<div class="card list">${d.alerts.length ? d.alerts.map(a => `<div class="item sev-${SEV[a.severity] || "info"}"><div class="main"><div class="name">${esc(a.title)}</div><div class="meta">${esc(a.category)} · via ${esc(a.transport || "—")}${a.error ? ` · <span style="color:var(--red)">${esc(a.error)}</span>` : ""}</div></div><div class="right"><span class="schip ${DS[a.delivery_status] || "nil"}">${esc(a.delivery_status)}</span><small>${t(a.created_at)}</small></div></div>`).join("") : `<div class="empty">No alerts yet.</div>`}</div>`)}
      ${sec("lgAu", "Audit trail", d.audit.length, `<div class="card list">${d.audit.length ? d.audit.map(a => `<div class="item"><div class="main"><div class="name">${esc(a.action)}</div><div class="meta">${esc(a.actor)}${a.target ? " · " + esc(a.target) : ""}${a.detail ? " · " + esc(String(a.detail).slice(0, 180)) : ""}</div></div><div class="right"><small>${t(a.ts)}</small></div></div>`).join("") : `<div class="empty">No audit entries.</div>`}</div>`)}
      ${d.actions.length ? sec("lgOld", "Earlier bot log", d.actions.length, `<div class="card list">${d.actions.map(x => `<div class="item"><span class="badge ${x.kind === "pause" ? "warn" : "info"}" style="margin-top:2px">${esc(x.kind)}</span><div class="main"><div class="name">${esc(x.keyword || x.entity_key || "")}</div><div class="meta">${esc(SHORT[x.app] || x.app || "")} · ${esc(x.channel || "")} · ${esc(x.detail || "")}${x.error ? ` · <span style="color:var(--red)">${esc(x.error)}</span>` : ""}</div></div><div class="right"><small>${t(x.ts)}</small>${x.applied ? "done" : x.dry_run ? "would" : "—"}</div></div>`).join("")}</div>`) : ""}`;
    document.querySelectorAll("#view details.fold").forEach(f => f.addEventListener("toggle", () => store.set("adbot-log-" + f.id, f.open)));
  }

  /* ---------- go ---------- */
  if ("serviceWorker" in navigator) window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  start();
})();
