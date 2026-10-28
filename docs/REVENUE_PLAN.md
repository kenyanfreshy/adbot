# Revenue Intelligence — architecture and stage plan

The goal: one place, the AdBot phone web app, that shows what NurseBound Pro, Rapid Nursing Review Pro and CCRN Adult earn on the App Store and Google Play, what the advertising costs, and whether the business is profitable.

## Stage A: what's already here (checked 2026-10-22)

| Area | Finding |
|---|---|
| Repo | `kenyanfreshy/adbot`, branch `main`, clean before this work (last commit `bb48fd9`). |
| Backend | Python. `dlt_pipeline.py` (ad sync and bot rules, run hourly as a Render cron job) and `web.py` (Starlette JSON API with password and signed-cookie sign-in). |
| Front end | Plain-JS installable web app (`web/`) with bottom tabs. Works on iPhone, Android, Windows and macOS browsers. |
| Database | DuckDB on MotherDuck (database `adbot`), pinned to DuckDB ≤ 1.5.6 because MotherDuck doesn't support 2.0 yet. Schemas `ads` (ad performance), `trading` (sidecar) and the bot tables. |
| Hosting | Render: web service `adbot-control-panel` and cron job `adbot-hourly`. The Windows 11 mini PC runs only the read-only trading sidecar. |
| Existing integrations | Apple Search Ads (v5, ES256 JWT), Meta Marketing API v21.0, and Google Ads (GAQL) connectors exist for **ads only**. None have credentials yet. There is no App Store Connect or Google Play integration. |
| Tests | `tests/local_server.py`, a fake-data UI server. There were no automated tests before this work. |

**Decision:** extend AdBot rather than build a second system. Revenue lives in a new `revenue` schema in the same MotherDuck database, shown on a new **Revenue** tab of the same web app, and synced by the same Render cron job. Because the hosting is Render, the "recover after a mini PC restart" requirement is met by Render's scheduler: every run is idempotent, so a missed run is simply caught up by the next one.

## Sources and what each one can actually provide

These come from official documentation. Exact field mappings are confirmed against real report files in Stages C and D.

| Data | Official source | Notes |
|---|---|---|
| Apple daily sales, units, IAP, subscriptions, refunds (estimated) | App Store Connect API `GET /v1/salesReports`: `SALES/SUMMARY` (daily), `SUBSCRIPTION`, `SUBSCRIPTION_EVENT`, `SUBSCRIBER` (v1_3) | Needs a **Team** API key (individual keys can't use Sales and Finance). Files arrive gzip-compressed, and there's a reporting delay. |
| Apple finalized proceeds, taxes, adjustments | `GET /v1/financeReports` (monthly, by Apple fiscal period) | Stored as `finalized`, kept separate from estimated sales. |
| Apple impressions, page views, conversion, sessions, crashes | App Store Connect Analytics Reports API | Apple applies privacy thresholds, so missing rows are shown as Unavailable, not 0. |
| Google Play estimated sales, earnings | Play Console reports in the developer's Cloud Storage bucket (`pubsite_prod_rev_…`): `sales/salesreport_YYYYMM.zip`, `earnings/earnings_YYYYMM.zip` | Service account invited in Play Console with **View financial data** (Global) and the `devstorage.read_only` scope. Files are UTF-16 CSV. Sales are dated in UTC, earnings in Pacific time. Data arrives within 3–7 days, and earnings by about the 5th of the next month. |
| Google Play installs, subscriptions stats | Same bucket: `stats/installs/…`, `financial-stats/subscriptions/…` | Monthly CSVs. |
| Google Play products, base plans, offers, prices | Android Publisher API `monetization.subscriptions.list`, `…basePlans.offers.list`, `monetization.onetimeproducts.list` | This is how Android prices and product types get verified. |
| Individual purchase verification | `purchases.subscriptionsv2.get`, `purchases.products.get` plus Real-time Developer Notifications (Pub/Sub) | Read-only: the system never acknowledges, cancels or refunds. |
| Crashes and ANRs | Play Developer **Reporting** API | This API covers vitals only, **not** money. |
| Ads | Apple Ads Campaign Management API v5, Google Ads API, Meta Marketing API (TikTok was dropped on 2026-10-28) | Each has its own attribution model. Results are never summed across platforms without deduplication. |

Official references:
[Download sales and trends reports](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-salesreports) ·
[Sales and Finance (Team key requirement)](https://developer.apple.com/documentation/appstoreconnectapi/sales-and-finance) ·
[Google Play: download and export monthly reports](https://support.google.com/googleplay/android-developer/answer/6135870) ·
[Android Publisher monetization API](https://developers.google.com/android-publisher/api-ref/rest/v3/monetization)

## Stages

| Stage | Scope | Status |
|---|---|---|
| A | Inspection and this plan | Done |
| B | Application registry, revenue database schema, Decimal money handling, audit and sync-job logs, Revenue tab (registry plus source status) | **Done, tested offline, not deployed** |
| C | App Store Connect connector: JWT, product verification, daily sales, monthly finance detail, backfill plus incremental sync, gzip parsing, retries, raw-file archive | **Built and tested offline; first live run on the next hourly job.** Subscription-event and analytics reports come next, once real files can be inspected |
| D | Google Play connector: monetization API for products, types and regional prices (dated history); bucket reports for estimated sales and earnings | **Built and tested offline; first live run on the next hourly job** |
| E | Normalization and reconciliation (estimated vs finalized, refunds, FX with documented rates) and the metric definitions | Needs C or D data |
| F | Dashboard: executive overview, app and store comparison, subscriptions, lifetime purchases, reconciliation | |
| G | Ad spend into `revenue.advertising_spend` (reusing the existing ad connectors) | |
| H | $1,000/month budget engine and kill switch (global, per platform, per app); pause verification; "Pause unconfirmed" alerts | Live changes only after your explicit approval |
| I | AI analysis. Recommendations go through the risk engine and human approval; the AI never calculates money or executes anything. | |
| J | Security audit, full test pass, production-readiness review | |

## Stage B report

**What was built**
- `revenue/schema.py`: 19 tables plus migration tracking in schema `revenue`.
  - Money is `DECIMAL(18,6)`; there are no float columns.
  - Every financial row must carry its platform, the store's app reference, currency, period, timezone, source report, import time, and estimated/finalized status. The database enforces this with NOT NULL and CHECK constraints.
  - Natural primary keys make re-imports idempotent: the same report line or the same renewal is stored once.
  - `financial_reports` accepts finalized rows only.
  - A product can be marked verified only when the store API confirmed it.
- `revenue/registry.py`: the three apps, App Store and Google Play listings (IDs empty until you or a connector fill them), your iOS price list stored as **reference** prices, and the data sources by environment-variable *name*.
  - Every product is "type not verified", including the three-month passes.
  - No Android prices are assumed.
  - It also has the audit log, sync-job records with the last successful sync per source, and token scrubbing for logs.
- `revenue/money.py`: Decimal-only amounts.
  - Floats are rejected.
  - Mixing currencies raises an error.
  - Conversion requires a dated rate with a named source.
  - Missing values stay `Unavailable`.
- Web app: a new **Revenue** tab with sign-in required.
  - Revenue tiles read *Unavailable* until store reports exist.
  - Each data source shows its status, last successful sync ("Never") and which Render variables it still needs.
  - Each app shows its store IDs (editable, validated, audit-logged) and its plans with reference prices.
- `dlt_pipeline.ensure_tables` now creates and upgrades the revenue schema on start. A failure there is logged and never stops the ad bot.

**Tests:** 24 automated tests, all passing (`python -m unittest discover -s tests`). They cover the schema, constraints, duplicates, renewal de-duplication, the seed, edits surviving re-seed, identifier validation, credential names never leaking values, sync status, token scrubbing, Decimal rounding, currency mixing and conversion, and the web endpoints (sign-in required, money sent as text). The data is synthetic.

**Limitations**
- Tests ran on the DuckDB 1.5.6 engine through its CLI because this sandbox couldn't install the `duckdb` Python package. On Render they will run with the real package.
- Nothing has run against MotherDuck yet; that happens on first deploy.
- No live revenue is available until Stages C and D.

**Credentials needed for Stage C/D.** These are set in Render only, never in chat:
- `ASC_ISSUER_ID`, `ASC_KEY_ID`, `ASC_PRIVATE_KEY`, `ASC_VENDOR_NUMBER`: an App Store Connect **Team** key with the Sales and Finance roles.
- `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`, `GOOGLE_PLAY_REPORTS_BUCKET`: a service account invited in Play Console with View app information and View financial data.

## Stage C report (App Store Connect)

**What runs each hour** (`revenue/apple.py`, called by `dlt_pipeline.py` after the ad bot):
- **Products, once a day:** reads each app's in-app purchases and subscription groups and marks every product with its real type and period. This settles whether the three-month passes are auto-renewing or non-renewing.
- **Sales, every hour until each day is in:**
  - Each run fetches any of the last 365 days still missing (Apple keeps daily reports for a year), as the daily `SALES/SUMMARY` report.
  - Amounts are units × per-unit price or proceeds. Refunds stay negative, and proceeds keep their own currency.
  - Rows are matched to apps only by the Apple ID, the app SKU learned from its download rows, or a bundle-ID prefix. Titles are never used.
  - Unmatched rows are kept, and the job is marked *partial*.
- **Finance, once a day:** fetches the `FINANCE_DETAIL` report (region Z1) for the last 12 fiscal months. It's parsed when the header row is recognized. Otherwise the file is archived and flagged, and nothing is guessed.
- **Record-keeping:**
  - Every file is archived (`report_blobs`).
  - Every run is logged (`synchronization_jobs`, `audit_logs`).
  - Re-running inserts nothing twice.
  - A bad key marks the source as *Error* with Apple's message, with tokens removed. The ad bot keeps running.

**Tests:** 11 new tests run against a fake App Store Connect with synthetic files and a throwaway key, for 35 tests in total, all passing. They cover:
- JWT claims
- credential handling
- parsing (UTF-8 and UTF-16)
- refunds and currencies
- app matching
- idempotent re-runs
- 401 handling, 429/503 retries, and giving up during an outage
- product verification
- finance parsing and the unknown-layout path

**Not yet verified live:** the first real run confirms the key, the role, the vendor number and the finance file layout. Until the apps are on sale, Apple returns no sales, and the tab will show *Connected* with no data.

## Stage D report (Google Play)

**What runs once a day** (`revenue/google_play.py`, called by `dlt_pipeline.py` after Apple):
- **Sign-in:** the job uses the service account key from the Render Secret File (any single `*.json`, preferably `play-service-account.json`) and signs in to Google with the read-only Android Publisher and Cloud Storage scopes.
- **Products and prices:**
  - For every Play package on the Revenue tab, the job reads its subscriptions (each base plan) and one-time products (each purchase option).
  - Each one is stored with its real type. An auto-renewing plan becomes `auto_renewable_subscription`. A prepaid plan becomes `non_renewing_subscription`, which is how a non-renewing 3-month pass shows up. One-time products become `one_time_product`.
  - The billing period comes from Google (P1M, P3M, P1Y).
  - Regional prices are kept as a dated history: unchanged prices are left alone, and a changed price closes the old row and opens a new one.
- **Reports, when `GOOGLE_PLAY_REPORTS_BUCKET` is set:**
  - `sales/salesreport_YYYYMM.zip` goes into `sales_reports` as estimated sales, one line per order.
  - `earnings/earnings_YYYYMM.zip` goes into `financial_reports` as finalized money, one line per money movement (Charge, Google fee, refunds).
  - A file is re-read only when Google publishes a new version of it. Orders already stored are not counted again.
  - A file with columns the parser doesn't recognize is archived and the job is flagged, rather than guessing at its contents.
- **When something is missing or refused:**
  - A missing bucket, a refused bucket (the invite needs "View financial data", which can take a day), or a refused products request is recorded in plain words.
  - These problems don't stop the other steps or the ad bot.
  - A bad key marks Google Play as *Error*.

**Tests:** 12 new tests run against a fake Google with a throwaway key and synthetic files, for 48 tests in total. They cover:
- loading the key, accepting the bucket as a name, `gs://` path or link
- the sign-in token's contents
- Google's price format (`units` plus `nanos`)
- product types and regional prices, and the price history
- refused products and a bad key
- importing both report files, and adding only new orders when a file is updated
- UTF-16 files, an unknown layout, and bucket problems
- the once-a-day guard

**Not yet verified live:** the first real run confirms the Play Console invite, that the API is enabled, and the column names in real report files. There are no money reports yet because nothing has sold.
