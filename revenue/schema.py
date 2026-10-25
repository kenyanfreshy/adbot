"""
Revenue database schema (DuckDB / MotherDuck, schema `revenue`).

Lives in the same `adbot` database the web app and the hourly job already use,
so nothing new has to be hosted. Each change is a numbered migration recorded
in revenue.schema_migrations; `ensure_schema()` is safe to call on every start.

Design rules (spec Phase 4):
  * Money columns are DECIMAL(18,6); exchange rates DECIMAL(20,10). No DOUBLE.
  * Every financial row keeps: source platform, the store's own app reference,
    product id, original currency + amount, reporting period, reporting
    timezone, source report id, import time and data_status
    ('estimated' = preliminary sales / 'finalized' = Apple/Google financial
    reports).
  * Natural primary keys make every import idempotent: re-loading the same
    report inserts nothing new (ON CONFLICT DO NOTHING).
  * No secrets in the database. store_accounts records only the NAMES of the
    environment variables that hold credentials. Purchase tokens and original
    transaction ids are stored as SHA-256 hashes.
  * No foreign keys: DuckDB can't update/replace a row that another table
    references, which would block re-imports. Referential checks are done in
    code and by tests instead.
"""
from __future__ import annotations

import datetime as dt

SCHEMA = "revenue"

PLATFORMS = ("apple_app_store", "google_play", "apple_ads", "google_ads", "meta_ads", "tiktok_ads")
STORES = ("apple_app_store", "google_play")
DATA_STATUS = ("estimated", "finalized")

_P = ", ".join(f"'{p}'" for p in PLATFORMS)
_S = ", ".join(f"'{p}'" for p in STORES)
_DS = ", ".join(f"'{s}'" for s in DATA_STATUS)

# Columns every financial fact table carries (spec: "Every financial record must preserve")
FIN = f"""
    source_platform     VARCHAR NOT NULL CHECK (source_platform IN ({_S})),
    app_id              VARCHAR,                 -- our registry id; NULL until the store ref is matched
    source_app_ref      VARCHAR NOT NULL,        -- Apple Apple ID / SKU or Google package name, as reported
    store_product_id    VARCHAR,                 -- as reported (NULL for app-level rows such as downloads)
    currency            VARCHAR NOT NULL CHECK (length(currency) = 3),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    reporting_timezone  VARCHAR NOT NULL,        -- e.g. 'America/Los_Angeles' (Apple, Google earnings) or 'UTC' (Google sales)
    source_report_id    VARCHAR NOT NULL,        -- revenue.report_files.report_id
    imported_at         TIMESTAMP NOT NULL,
    data_status         VARCHAR NOT NULL CHECK (data_status IN ({_DS})),
    CHECK (period_end >= period_start)
"""

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "application registry, revenue facts, ads, sync, audit, risk", [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",

        # ---- registry ---------------------------------------------------- #
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.applications (
            app_id        VARCHAR PRIMARY KEY CHECK (regexp_full_match(app_id, '[a-z][a-z0-9_]{{1,40}}')),
            name          VARCHAR NOT NULL UNIQUE,
            bot_app_name  VARCHAR,                    -- name the ad bot uses ('Rapid Nursing Review', ...)
            active        BOOLEAN NOT NULL DEFAULT TRUE,
            notes         VARCHAR,
            created_at    TIMESTAMP NOT NULL,
            updated_at    TIMESTAMP NOT NULL)""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.store_listings (
            app_id              VARCHAR NOT NULL,
            platform            VARCHAR NOT NULL CHECK (platform IN ({_S})),
            apple_app_id        VARCHAR CHECK (apple_app_id IS NULL OR regexp_full_match(apple_app_id, '[0-9]{{6,12}}')),
            bundle_id           VARCHAR,
            package_name        VARCHAR,
            publication_status  VARCHAR NOT NULL DEFAULT 'unknown',  -- as reported by the store; 'unknown' until verified
            status_source       VARCHAR NOT NULL DEFAULT 'none' CHECK (status_source IN ('none', 'admin', 'store_api')),
            status_checked_at   TIMESTAMP,
            updated_at          TIMESTAMP NOT NULL,
            PRIMARY KEY (app_id, platform),
            CHECK (platform = 'apple_app_store' OR (apple_app_id IS NULL AND bundle_id IS NULL)),
            CHECK (platform = 'google_play' OR package_name IS NULL))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.store_accounts (
            account_id           VARCHAR PRIMARY KEY,
            platform             VARCHAR NOT NULL CHECK (platform IN ({_P})),
            display_name         VARCHAR NOT NULL,
            purpose              VARCHAR NOT NULL CHECK (purpose IN ('reporting', 'ad_execution')),
            credential_env_vars  VARCHAR NOT NULL,    -- comma-separated env var NAMES; never values
            reporting_timezone   VARCHAR,
            status               VARCHAR NOT NULL DEFAULT 'not_configured'
                                 CHECK (status IN ('not_configured', 'configured', 'verified', 'error')),
            last_verified_at     TIMESTAMP,
            last_error           VARCHAR,
            updated_at           TIMESTAMP NOT NULL)""",

        # one row per product as the store defines it. Reference rows (from the
        # spec, not yet seen in a store) use product_key 'ref:<app>:<platform>:<plan>'.
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.store_products (
            product_key        VARCHAR PRIMARY KEY,
            app_id             VARCHAR NOT NULL,
            platform           VARCHAR NOT NULL CHECK (platform IN ({_S})),
            store_product_id   VARCHAR,               -- Apple product id / Google productId; NULL for reference rows
            plan_code          VARCHAR NOT NULL CHECK (plan_code IN ('monthly', 'three_month', 'annual', 'lifetime', 'other')),
            display_name       VARCHAR NOT NULL,
            product_type       VARCHAR NOT NULL DEFAULT 'unverified' CHECK (product_type IN (
                                 'unverified', 'auto_renewable_subscription', 'non_renewing_subscription',
                                 'non_consumable', 'consumable', 'one_time_product')),
            billing_period     VARCHAR,               -- ISO 8601 (P1M, P3M, P1Y) only once the store confirms it
            base_plan_id       VARCHAR,               -- Google Play subscriptions
            offer_id           VARCHAR,
            availability       VARCHAR NOT NULL DEFAULT 'unknown',
            source             VARCHAR NOT NULL CHECK (source IN ('reference', 'admin', 'store_api')),
            verified           BOOLEAN NOT NULL DEFAULT FALSE,
            verified_at        TIMESTAMP,
            created_at         TIMESTAMP NOT NULL,
            updated_at         TIMESTAMP NOT NULL,
            CHECK (source = 'reference' OR store_product_id IS NOT NULL),
            CHECK (NOT verified OR (source = 'store_api' AND verified_at IS NOT NULL AND product_type <> 'unverified')),
            UNIQUE (platform, store_product_id, base_plan_id, offer_id))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.product_prices (
            product_key     VARCHAR NOT NULL,
            territory       VARCHAR NOT NULL CHECK (regexp_full_match(territory, '[A-Z]{{2}}')),  -- ISO 3166-1 alpha-2
            currency        VARCHAR NOT NULL CHECK (length(currency) = 3),
            amount          DECIMAL(18,6) NOT NULL CHECK (amount >= 0),
            source          VARCHAR NOT NULL CHECK (source IN ('reference', 'admin', 'store_api')),
            effective_from  DATE NOT NULL,
            effective_to    DATE,
            source_ref      VARCHAR,
            imported_at     TIMESTAMP NOT NULL,
            PRIMARY KEY (product_key, territory, currency, source, effective_from),
            CHECK (effective_to IS NULL OR effective_to >= effective_from))""",

        # ---- raw report tracking (availability, versions, duplicates) ---- #
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.report_files (
            report_id          VARCHAR PRIMARY KEY,   -- '<platform>:<type>:<subtype>:<freq>:<period>:<version>'
            source_platform    VARCHAR NOT NULL CHECK (source_platform IN ({_S})),
            report_type        VARCHAR NOT NULL,
            report_subtype     VARCHAR,
            frequency          VARCHAR,
            report_version     VARCHAR,
            period_start       DATE NOT NULL,
            period_end         DATE NOT NULL,
            reporting_timezone VARCHAR NOT NULL,
            data_status        VARCHAR NOT NULL CHECK (data_status IN ({_DS})),
            content_sha256     VARCHAR NOT NULL,
            row_count          INTEGER NOT NULL,
            fetched_at         TIMESTAMP NOT NULL,
            superseded_by      VARCHAR,
            CHECK (period_end >= period_start))""",

        # ---- revenue facts ----------------------------------------------- #
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.sales_reports (
            line_key          VARCHAR PRIMARY KEY,    -- sha256 of the identifying fields; same line in two reports = one row
            {FIN},
            territory         VARCHAR,
            transaction_type  VARCHAR NOT NULL,       -- as normalised: download, redownload, update, iap, subscription, refund, other
            product_type      VARCHAR,
            units             INTEGER NOT NULL,
            customer_price    DECIMAL(18,6),          -- per unit, original currency
            gross_amount      DECIMAL(18,6),          -- units * customer price (NULL when the report doesn't give it)
            proceeds_amount   DECIMAL(18,6))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.purchase_events (
            event_key           VARCHAR PRIMARY KEY,  -- '<platform>:<transaction or order id>'
            {FIN},
            event_type          VARCHAR NOT NULL CHECK (event_type IN ('purchase', 'renewal', 'refund', 'revocation', 'chargeback')),
            event_time          TIMESTAMP NOT NULL,
            territory           VARCHAR,
            amount              DECIMAL(18,6),
            purchase_token_sha  VARCHAR,              -- SHA-256 only; the token itself is never stored
            is_subscription     BOOLEAN NOT NULL)""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.subscription_events (
            event_key              VARCHAR PRIMARY KEY,  -- one renewal = one key, however many reports mention it
            {FIN},
            event_type             VARCHAR NOT NULL CHECK (event_type IN (
                                     'new', 'renewal', 'cancel', 'expire', 'billing_issue', 'grace_period',
                                     'refund', 'reactivate', 'upgrade', 'downgrade', 'crossgrade', 'pause', 'other')),
            event_time             TIMESTAMP,
            subscriber_ref_sha     VARCHAR,           -- hashed original transaction id / purchase token
            territory              VARCHAR,
            quantity               INTEGER NOT NULL DEFAULT 1,
            amount                 DECIMAL(18,6))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.subscription_snapshots (
            snapshot_date        DATE NOT NULL,
            source_platform      VARCHAR NOT NULL CHECK (source_platform IN ({_S})),
            source_app_ref       VARCHAR NOT NULL,
            store_product_id     VARCHAR NOT NULL,
            territory            VARCHAR NOT NULL DEFAULT 'ZZ',   -- 'ZZ' = all territories
            app_id               VARCHAR,
            active_subscriptions INTEGER,
            active_free_trials   INTEGER,
            billing_retry        INTEGER,
            grace_period         INTEGER,
            reporting_timezone   VARCHAR NOT NULL,
            source_report_id     VARCHAR NOT NULL,
            imported_at          TIMESTAMP NOT NULL,
            data_status          VARCHAR NOT NULL CHECK (data_status IN ({_DS})),
            PRIMARY KEY (snapshot_date, source_platform, source_app_ref, store_product_id, territory))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.refund_events (
            refund_key     VARCHAR PRIMARY KEY,
            {FIN},
            refund_time    TIMESTAMP,
            territory      VARCHAR,
            units          INTEGER NOT NULL DEFAULT 1,
            amount         DECIMAL(18,6) NOT NULL,       -- positive number = money returned to the customer
            reason         VARCHAR)""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.financial_reports (
            financial_key      VARCHAR PRIMARY KEY,
            {FIN},
            fiscal_period      VARCHAR NOT NULL,         -- e.g. Apple fiscal month '2026-10' or Google earnings month
            region             VARCHAR,
            territory          VARCHAR,
            units              INTEGER,
            gross_amount       DECIMAL(18,6),
            proceeds_amount    DECIMAL(18,6),
            tax_amount         DECIMAL(18,6),
            adjustment_amount  DECIMAL(18,6),
            commission_amount  DECIMAL(18,6),            -- only when the report lets us derive it
            payout_currency    VARCHAR,
            payout_rate        DECIMAL(20,10),
            payout_amount      DECIMAL(18,6),
            CHECK (data_status = 'finalized'))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.exchange_rates (
            rate_date       DATE NOT NULL,
            base_currency   VARCHAR NOT NULL CHECK (length(base_currency) = 3),
            quote_currency  VARCHAR NOT NULL CHECK (length(quote_currency) = 3),
            rate            DECIMAL(20,10) NOT NULL CHECK (rate > 0),
            source          VARCHAR NOT NULL,           -- e.g. 'apple_financial_report', 'ecb'
            imported_at     TIMESTAMP NOT NULL,
            PRIMARY KEY (rate_date, base_currency, quote_currency, source))""",

        # ---- advertising --------------------------------------------------- #
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.advertising_campaigns (
            platform        VARCHAR NOT NULL CHECK (platform IN ({_P})),
            campaign_id     VARCHAR NOT NULL,
            campaign_name   VARCHAR,
            app_id          VARCHAR,
            status          VARCHAR,                    -- as the ad platform reports it
            managed         BOOLEAN NOT NULL DEFAULT FALSE,  -- may the kill switch act on it
            daily_budget    DECIMAL(18,6),
            currency        VARCHAR,
            updated_at      TIMESTAMP NOT NULL,
            PRIMARY KEY (platform, campaign_id))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.advertising_spend (
            platform              VARCHAR NOT NULL CHECK (platform IN ({_P})),
            campaign_id           VARCHAR NOT NULL,
            spend_date            DATE NOT NULL,
            app_id                VARCHAR,
            currency              VARCHAR NOT NULL CHECK (length(currency) = 3),
            spend                 DECIMAL(18,6) NOT NULL,
            impressions           BIGINT,
            clicks                BIGINT,
            installs              BIGINT,             -- NULL = platform doesn't report it
            attributed_purchases  BIGINT,
            conversion_value      DECIMAL(18,6),
            attribution_source    VARCHAR,            -- whose attribution model produced the two columns above
            reporting_timezone    VARCHAR NOT NULL,
            imported_at           TIMESTAMP NOT NULL,
            PRIMARY KEY (platform, campaign_id, spend_date))""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.attribution_records (
            record_key          VARCHAR PRIMARY KEY,
            platform            VARCHAR NOT NULL CHECK (platform IN ({_P})),
            attribution_system  VARCHAR NOT NULL,     -- 'apple_ads_attribution', 'skadnetwork', 'google_ads', 'meta', 'tiktok'
            campaign_id         VARCHAR,
            app_id              VARCHAR,
            event_date          DATE NOT NULL,
            metric              VARCHAR NOT NULL,     -- 'installs', 'purchases', 'conversion_value'
            value               DECIMAL(18,6) NOT NULL,
            currency            VARCHAR,
            dedupe_group        VARCHAR,              -- rows sharing a group describe the same conversions; count once
            imported_at         TIMESTAMP NOT NULL)""",

        # ---- operations ---------------------------------------------------- #
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.synchronization_jobs (
            job_id         VARCHAR PRIMARY KEY,
            source         VARCHAR NOT NULL,
            job_type       VARCHAR NOT NULL,
            period_start   DATE,
            period_end     DATE,
            status         VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'skipped')),
            attempt        INTEGER NOT NULL DEFAULT 1,
            started_at     TIMESTAMP NOT NULL,
            finished_at    TIMESTAMP,
            rows_seen      INTEGER,
            rows_inserted  INTEGER,
            message        VARCHAR)""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.audit_logs (
            audit_id   VARCHAR PRIMARY KEY,
            ts         TIMESTAMP NOT NULL,
            actor      VARCHAR NOT NULL CHECK (actor IN ('system', 'admin', 'ai_agent', 'scheduler')),
            action     VARCHAR NOT NULL,
            target     VARCHAR,
            detail     VARCHAR,                       -- JSON text, already scrubbed of secrets
            dry_run    BOOLEAN NOT NULL DEFAULT TRUE)""",

        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.risk_decisions (
            decision_id      VARCHAR PRIMARY KEY,
            ts               TIMESTAMP NOT NULL,
            proposed_by      VARCHAR NOT NULL CHECK (proposed_by IN ('ai_agent', 'rules', 'admin')),
            action           VARCHAR NOT NULL,        -- 'pause_campaign', 'set_budget', 'kill_switch', ...
            platform         VARCHAR,
            app_id           VARCHAR,
            campaign_id      VARCHAR,
            amount           DECIMAL(18,6),
            currency         VARCHAR,
            verdict          VARCHAR NOT NULL CHECK (verdict IN ('approved', 'rejected', 'needs_human')),
            reasons          VARCHAR,                 -- JSON list
            approved_by      VARCHAR,
            approved_at      TIMESTAMP,
            executed_at      TIMESTAMP,
            execution_status VARCHAR CHECK (execution_status IS NULL OR execution_status IN (
                               'not_executed', 'executed', 'pause_unconfirmed', 'failed')))""",
    ]),
]

MIGRATIONS.append((2, "Apple sales detail columns + raw report archive", [
    # Apple pays proceeds in 'Currency of Proceeds', which can differ from the customer's currency,
    # so proceeds get their own currency column (gross stays in `currency`).
    f"ALTER TABLE {SCHEMA}.sales_reports ADD COLUMN IF NOT EXISTS proceeds_currency VARCHAR",
    f"ALTER TABLE {SCHEMA}.sales_reports ADD COLUMN IF NOT EXISTS sku VARCHAR",          # Apple SKU column
    f"ALTER TABLE {SCHEMA}.sales_reports ADD COLUMN IF NOT EXISTS parent_ref VARCHAR",   # Apple 'Parent Identifier' (app SKU of an IAP)
    f"ALTER TABLE {SCHEMA}.sales_reports ADD COLUMN IF NOT EXISTS subscription_kind VARCHAR",  # Apple 'Subscription': New / Renewal
    f"ALTER TABLE {SCHEMA}.sales_reports ADD COLUMN IF NOT EXISTS sub_period VARCHAR",        # Apple 'Period': 1 Month, 3 Months, 1 Year, ...
    # every downloaded report file kept as received (gzip, base64) so it can be re-parsed later
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.report_blobs (
        report_id     VARCHAR PRIMARY KEY,
        encoding      VARCHAR NOT NULL,
        content_b64   VARCHAR NOT NULL,
        stored_at     TIMESTAMP NOT NULL)""",
]))

MIGRATIONS.append((3, "Google Play: transaction type on financial lines", [
    # Google earnings files list each money movement on its own line (Charge, Google fee, Charge refund, ...)
    f"ALTER TABLE {SCHEMA}.financial_reports ADD COLUMN IF NOT EXISTS transaction_type VARCHAR",
]))

MIGRATIONS.append((4, "Google Ads no longer needs a developer token (Google change, 2026-09-09)", [
    f"UPDATE {SCHEMA}.store_accounts SET credential_env_vars = "
    "'GOOGLE_ADS_CLIENT_ID,GOOGLE_ADS_CLIENT_SECRET,GOOGLE_ADS_REFRESH_TOKEN,GOOGLE_ADS_CUSTOMER_IDS' "
    "WHERE account_id = 'google_ads_reporting'",
]))

MIGRATIONS.append((5, "Store ID check: result of the last check and pending requests", [
    # check_result: verified | mismatch | not_found | no_access | no_keys | error (NULL = never checked)
    f"ALTER TABLE {SCHEMA}.store_listings ADD COLUMN IF NOT EXISTS check_result VARCHAR",
    f"ALTER TABLE {SCHEMA}.store_listings ADD COLUMN IF NOT EXISTS check_detail VARCHAR",
    f"ALTER TABLE {SCHEMA}.store_listings ADD COLUMN IF NOT EXISTS check_requested_at TIMESTAMP",
]))

LATEST = max(v for v, _, _ in MIGRATIONS)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def applied_versions(con) -> set[int]:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    con.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations "
                "(version INTEGER PRIMARY KEY, description VARCHAR NOT NULL, applied_at TIMESTAMP NOT NULL)")
    return {int(r[0]) for r in con.execute(f"SELECT version FROM {SCHEMA}.schema_migrations").fetchall()}


def ensure_schema(con) -> list[int]:
    """Apply any migrations not yet recorded. Returns the versions applied now."""
    done = applied_versions(con)
    applied = []
    for version, desc, statements in MIGRATIONS:
        if version in done:
            continue
        for sql in statements:
            con.execute(sql)
        con.execute(f"INSERT INTO {SCHEMA}.schema_migrations VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                    [version, desc, _now()])
        applied.append(version)
    return applied


TABLES = [
    "applications", "store_listings", "store_accounts", "store_products", "product_prices", "report_files",
    "sales_reports", "purchase_events", "subscription_events", "subscription_snapshots", "refund_events",
    "financial_reports", "exchange_rates", "advertising_campaigns", "advertising_spend", "attribution_records",
    "synchronization_jobs", "audit_logs", "risk_decisions", "report_blobs",
]
