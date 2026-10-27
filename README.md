# AdBot setup (Render)

1. Put these files (including web/ and static/) in a private GitHub repo.
2. Create a free MotherDuck account and copy a service token. The web app and the hourly job share this database.
3. In Render: New > Blueprint > pick the repo. Render reads render.yaml and creates
   "adbot-control-panel" (web) and "adbot-hourly" (cron).
4. Fill in the secret env vars Render asks for. Leave a channel's keys empty to skip it.
   - APP_PASSWORD: the password for the control panel.
   - Apple Search Ads: Settings > API > create an API user, upload the public key; copy client ID, team ID, key ID; org ID is in the account menu. Paste the private key PEM into ASA_PRIVATE_KEY.
   - Meta Ads: Business Settings > System users > generate a token with ads_read and ads_management; META_AD_ACCOUNT_IDS are the numbers after "act_".
   - Google Ads: a developer token (Tools > API Center), an OAuth client (Google Cloud console) and a refresh token for the ads account; customer IDs without dashes.
   - Telegram: talk to @BotFather to create a bot (TELEGRAM_BOT_TOKEN), send it a message, then read your chat ID from https://api.telegram.org/bot<TOKEN>/getUpdates.
5. Name campaigns so the bot can tell the apps apart: include RNR, CCRN or NurseBound in each campaign name (editable under app_match in the rules). For NurseBound, also add the target country code, e.g. "NurseBound | PH | Prospecting".
6. In the app's Rules tab, press "Send test notification", then "Run bot now". Check the Log tab.
7. On your phone, open the address Render gives you (https://adbot-control-panel.onrender.com), sign in, then Share > Add to Home Screen (iPhone) or ⋮ > Add to Home screen / Install app (Android). It opens full-screen like an app, with tabs for Ads, Keywords, Rules, Trading and Log, and stays signed in for 30 days.
8. DRY_RUN stays "true" and the bot starts in **Recommend** mode: it imports data and proposes changes but never writes to an ad account. Going live is a separate, deliberate step (see "Ad control plane" below).

Rules edited in the dashboard: Export rules.json there, then "Import rules.json" in the control panel (or paste it into a RULES_JSON env var).

Local run: `pip install -r requirements.txt`, set APP_PASSWORD, then `python dlt_pipeline.py --days 14` and `uvicorn web:app --reload`. To try the screens without a database: `python tests/local_server.py` (password test-pass).

## Ad control plane

Every hourly run goes DATA → QUALITY → RECOMMENDATION → POLICY → EXECUTION → VERIFICATION → AUDIT (`control/engine.py`). State lives in the `control` schema (import runs, source status, campaign map, proposals, executions, alerts, locks).

**Modes** (Ads tab): Observe (import and report only) · Recommend (default; proposals, no writes) · Approval (you approve each proposal) · Bounded automatic (only action types listed in `auto_actions`, inside every limit). DRY_RUN=true overrides all modes: approved or automatic changes are recorded as *simulated*.

**Stop automation** latches immediately: nothing automatic runs until you press Resume. **Emergency pause** (type PAUSE) queues pauses for every managed campaign of the chosen app/channel; it runs in any mode and never resumes anything when cleared. Because the web service holds no ad credentials, approved changes and emergency pauses are carried out by the next hourly job.

**Data states** per source: not configured, unsupported, connected/not imported, imported (zero activity), imported (activity), partial, stale, failed. Missing revenue, installs or values are shown as unavailable, never 0. Apple Ads reports no revenue; Google conversions are not installs; TikTok is not integrated.

**Budget policy** (Rules tab): no budget proposals until you set `monthly_budget`, `app_monthly` allocations and `campaign_daily_max`. Daily allowance = (allocation × (1 − reserve) − month-to-date spend) ÷ days left. Rebalancing is blocked without reported revenue, enough spend and conversions.

**Safeguards**: per-change limit (`max_daily_change_pct`), cooldown, per-campaign and per-account daily action caps, deadband, data-maturity days, one job at a time (lease lock), dedup of proposals, read-before-write and verify-after-write, ambiguous results held as *uncertain* until reconciled.

**Dry-run checks**

```
python -m unittest discover -s tests -v          # set DUCKDB_CLI=/path/to/duckdb if the duckdb package is missing
DRY_RUN=true ALERT_TRANSPORT=log python dlt_pipeline.py --no-sync
DRY_RUN=true ALERT_TRANSPORT=log python dlt_pipeline.py --days 14
python tests/local_server.py                     # demo UI, password test-pass, http://localhost:8765
```

`ALERT_TRANSPORT=fake` or `log` keeps alerts off Telegram.

**Before going live** (each needs your decision): set DRY_RUN=false on adbot-hourly; choose Approval or Bounded automatic; list `auto_actions` if automatic; set budgets; run one supervised live smoke test per channel (the live adapters have only been tested against fakes).

## Trading bot monitoring (read-only)

The phone app's **Trading** tab shows what the trading bot reports: equity and P&L, open positions, today's trades and signals (taken / skipped / blocked), the daily loss limit, exposure and the kill switch. It never places, changes or closes trades and never sees broker keys.

1. Copy `trading_status.py` into the trading bot and `pip install duckdb` there.
2. On the trading machine set `MOTHERDUCK_TOKEN` (ideally its own MotherDuck token) and run `python trading_status.py` once; the Trading tab should show a test report.
3. In the Audit/Monitoring service, create `StatusReporter(bot_id="...", mode="paper" or "live")` and call `heartbeat()`, `account()`, `positions()`, `trade()`, `signal()` and `risk()` (examples at the top of the file). Calls return instantly and never raise, so trading can't be slowed or stopped by reporting.
4. The hourly job sends a Telegram alert if the bot goes quiet for 20+ minutes while the FX market is open, or if its kill switch turns on.

### Trading bridge for fx_trading_system (no changes to the trading code)

`trading_sidecar.py` runs next to the trading system, reads its Postgres in a read-only session plus `.runtime/runtime_status.json`, and feeds the Trading tab. On the trading PC: copy the `sidecar/` folder anywhere, double-click `setup-sidecar.bat` (makes its own Python environment, asks for the MotherDuck token, runs one test pass), then `start-sidecar.bat` to keep it running. Use Task Scheduler (At log on → start-sidecar.bat) to start it automatically. Optional: create a read-only Postgres role and set `TRADING_PG_DSN` to use it instead of the trading system's own login.

## Revenue intelligence (Stage B)

App Store and Google Play revenue for NurseBound Pro, Rapid Nursing Review Pro and CCRN Adult
lives in the `revenue` schema of the same database and on the **Revenue** tab of the web app.
See `docs/REVENUE_PLAN.md` for sources, stages and what each stage needs.

**Check store IDs** (Revenue tab, above the app cards) confirms each app's Apple ID, bundle ID and Play
package with the stores, read-only (`revenue/store_check.py`). App Store Connect must return the same bundle ID
for the Apple ID (an empty bundle ID is filled in from the store); Google Play must answer for the package in
your Play Console. Each row then shows VERIFIED, MISMATCH, NOT FOUND, NO ACCESS or NEEDS KEY, plus the store
state (Live, In review, ...). The store keys live on `adbot-hourly`, so a press is answered by the next hourly
run at :07; if you also add the keys to `adbot-control-panel` the check runs instantly. The hourly job re-checks
every listing once a day.

**Google Play reports** are tracked apart from Google Play products (`revenue/google_play.py`): products sync once a
day; the reports bucket has its own status (Not configured, Never checked, Bucket readable but empty, Reports imported,
Access denied, Bucket not found, Failed) and its own "last success", which a failed attempt never moves. A 403/404 is
retried once a day, other failures back off 1 h → 2 h → 4 h → 8 h → 24 h. To retry now without re-syncing products:
press **Retry reports** on the Google Play card (runs at the next :07), or run `python -m revenue.google_play retry-reports`
where the key is available. To test bucket access alone (lists at most one object, writes nothing, prints only the
service account email, bucket, result and a sanitized error): `python -m revenue.google_play diagnose-reports`.

Tests (synthetic data, no network): `python -m unittest discover -s tests -v`
