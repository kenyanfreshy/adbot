"""
Control-plane tables (DuckDB / MotherDuck schema `control`).

These sit beside the existing tables and never replace them:
  * ads.ad_performance (dlt)    performance rows, merged on (channel, entity_id, date)
  * bot_actions / app_budgets   the old bot's log, kept for history
  * revenue.*                   store revenue

What lives here:
  source_state     one row per data source: the shared status model's inputs
  import_runs      one row per source x account per sync: rows, window, currency, quality
  campaign_map     every campaign ever seen, its app (by name rule or by hand), or "ignored"
  control_state    operating mode, the stop-automation latch, pending emergency requests
  proposals        every recommended action with its evidence, blockers and outcome
  executions       every attempt to carry out a proposal (simulated or live), with pre/post state
  alerts           every alert considered: delivered, suppressed as a duplicate, or failed
  locks            leases that stop two jobs doing the same work at once

Each change is a numbered migration recorded in control.schema_migrations.
"""
from __future__ import annotations

import datetime as dt

SCHEMA = "control"

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "control plane: sources, imports, campaign map, proposals, executions, alerts, locks", [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.source_state (
            source                VARCHAR PRIMARY KEY,   -- 'Apple Search Ads', 'Meta Ads', 'Google Ads'
            configured            BOOLEAN NOT NULL DEFAULT FALSE,
            supported             BOOLEAN NOT NULL DEFAULT TRUE,
            accounts              VARCHAR,               -- comma-separated account ids (not secrets)
            currency              VARCHAR,               -- comma-separated when accounts differ
            timezone              VARCHAR,
            last_connection_check TIMESTAMP,             -- last time the platform accepted our credentials
            last_attempt          TIMESTAMP,             -- last import attempt, whatever the result
            last_success          TIMESTAMP,             -- last import that finished without error
            latest_date_covered   DATE,                  -- newest reporting date present in a successful import
            last_rows             INTEGER,
            last_activity_rows    INTEGER,               -- rows with impressions, clicks, installs or spend
            last_status           VARCHAR,               -- succeeded | partial | failed | not_configured
            last_import_id        VARCHAR,
            message               VARCHAR,
            updated_at            TIMESTAMP NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.import_runs (
            import_id      VARCHAR PRIMARY KEY,
            source         VARCHAR NOT NULL,
            account_id     VARCHAR,
            started_at     TIMESTAMP NOT NULL,
            finished_at    TIMESTAMP,
            status         VARCHAR NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            rows           INTEGER NOT NULL DEFAULT 0,
            activity_rows  INTEGER NOT NULL DEFAULT 0,
            window_start   DATE,
            window_end     DATE,
            min_date       DATE,
            max_date       DATE,
            currency       VARCHAR,
            timezone       VARCHAR,
            quality        VARCHAR,          -- ok | empty | mixed_currency | unmapped_rows | error
            unmapped_rows  INTEGER NOT NULL DEFAULT 0,
            message        VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.campaign_map (
            channel        VARCHAR NOT NULL,
            campaign_id    VARCHAR NOT NULL,
            account_id     VARCHAR,
            campaign_name  VARCHAR,
            rule_app       VARCHAR,          -- what the name rules say (may be NULL)
            manual_app     VARCHAR,          -- set by a person on the phone app; wins over rule_app
            ignored        BOOLEAN NOT NULL DEFAULT FALSE,   -- belongs to another product: never managed
            first_seen     TIMESTAMP NOT NULL,
            last_seen      TIMESTAMP NOT NULL,
            updated_by     VARCHAR,
            PRIMARY KEY (channel, campaign_id))""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.control_state (
            key         VARCHAR PRIMARY KEY,
            value       VARCHAR,
            updated_at  TIMESTAMP NOT NULL,
            updated_by  VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.proposals (
            proposal_id      VARCHAR PRIMARY KEY,
            fingerprint      VARCHAR NOT NULL,
            kind             VARCHAR NOT NULL CHECK (kind IN ('optimization', 'safeguard', 'emergency', 'manual')),
            created_at       TIMESTAMP NOT NULL,
            run_id           VARCHAR,
            app              VARCHAR,
            channel          VARCHAR,
            account_id       VARCHAR,
            campaign_id      VARCHAR,
            entity_key       VARCHAR NOT NULL,
            entity_type      VARCHAR,
            entity_name      VARCHAR,
            action           VARCHAR NOT NULL,     -- pause_keyword | pause_ad_set | pause_campaign | set_budget
            current_value    VARCHAR,
            proposed_value   VARCHAR,
            metrics          VARCHAR,              -- JSON: the evidence
            window_start     DATE,
            window_end       DATE,
            freshness        VARCHAR,              -- the source's shared status at decision time
            sufficiency      VARCHAR,              -- why the evidence was judged enough (or not)
            rule_id          VARCHAR,
            rule_version     VARCHAR,
            reason           VARCHAR,
            expected_effect  VARCHAR,              -- an estimate, worded as one
            blockers         VARCHAR,              -- JSON list of plain-language blockers
            next_eligible_at TIMESTAMP,
            status           VARCHAR NOT NULL CHECK (status IN ('proposed', 'blocked', 'approved', 'rejected', 'executing',
                                    'applied', 'simulated', 'failed', 'uncertain', 'superseded', 'expired', 'already_done')),
            decided_by       VARCHAR,
            decided_at       TIMESTAMP,
            outcome          VARCHAR,
            outcome_at       TIMESTAMP)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.executions (
            exec_id      VARCHAR PRIMARY KEY,
            proposal_id  VARCHAR NOT NULL,
            attempted_at TIMESTAMP NOT NULL,
            mode         VARCHAR NOT NULL CHECK (mode IN ('dry_run', 'live')),
            pre_state    VARCHAR,
            result       VARCHAR NOT NULL CHECK (result IN ('applied', 'simulated', 'failed', 'uncertain', 'already_done',
                                    'reconciled_applied', 'reconciled_not_applied', 'skipped')),
            post_state   VARCHAR,
            error        VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.alerts (
            alert_id        VARCHAR PRIMARY KEY,
            dedup_key       VARCHAR NOT NULL,
            category        VARCHAR NOT NULL,
            severity        VARCHAR NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            title           VARCHAR NOT NULL,
            body            VARCHAR,
            created_at      TIMESTAMP NOT NULL,
            transport       VARCHAR,
            delivery_status VARCHAR NOT NULL CHECK (delivery_status IN ('delivered', 'suppressed', 'failed', 'logged', 'disabled')),
            error           VARCHAR)""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.locks (
            name        VARCHAR PRIMARY KEY,
            owner       VARCHAR,
            acquired_at TIMESTAMP,
            expires_at  TIMESTAMP)""",
    ]),
]

# --------------------------------------------------------------------------- #
# Migration 2: execution lifecycle, bound approvals, write-ahead intents, budget reservations
# --------------------------------------------------------------------------- #
P_STATUSES = ("proposed", "blocked", "approved", "rejected", "executing", "applied", "verified", "simulated", "failed",
              "uncertain", "superseded", "expired", "already_done")
E_RESULTS = ("simulated", "accepted", "verified", "failed", "uncertain", "already_done", "blocked", "skipped",
             "reconciled_verified", "reconciled_not_applied", "applied", "reconciled_applied")
P_OLD_COLS = ("proposal_id, fingerprint, kind, created_at, run_id, app, channel, account_id, campaign_id, entity_key, entity_type, "
              "entity_name, action, current_value, proposed_value, metrics, window_start, window_end, freshness, sufficiency, rule_id, "
              "rule_version, reason, expected_effect, blockers, next_eligible_at, status, decided_by, decided_at, outcome, outcome_at")


def _in(vals) -> str:
    return ", ".join(f"'{v}'" for v in vals)


MIGRATIONS.append((2, "execution lifecycle: bound approvals, verified status, intents (write-ahead), budget reservations", [
    f"""CREATE TABLE {SCHEMA}.proposals_v2 (
        proposal_id      VARCHAR PRIMARY KEY,
        fingerprint      VARCHAR NOT NULL,
        kind             VARCHAR NOT NULL CHECK (kind IN ('optimization', 'safeguard', 'emergency', 'manual')),
        created_at       TIMESTAMP NOT NULL,
        run_id           VARCHAR,
        app              VARCHAR,
        channel          VARCHAR,
        account_id       VARCHAR,
        campaign_id      VARCHAR,
        entity_key       VARCHAR NOT NULL,
        entity_type      VARCHAR,
        entity_name      VARCHAR,
        action           VARCHAR NOT NULL,
        current_value    VARCHAR,
        proposed_value   VARCHAR,
        metrics          VARCHAR,
        window_start     DATE,
        window_end       DATE,
        freshness        VARCHAR,
        sufficiency      VARCHAR,
        rule_id          VARCHAR,
        rule_version     VARCHAR,
        reason           VARCHAR,
        expected_effect  VARCHAR,
        blockers         VARCHAR,
        next_eligible_at TIMESTAMP,
        status           VARCHAR NOT NULL CHECK (status IN ({_in(P_STATUSES)})),
        decided_by       VARCHAR,
        decided_at       TIMESTAMP,
        outcome          VARCHAR,
        outcome_at       TIMESTAMP,
        currency            VARCHAR,     -- currency of current/proposed values (budgets)
        policy_version      VARCHAR,     -- hash of the rule settings that produced it
        proposal_hash       VARCHAR,     -- hash of exact values + evidence + account/campaign + policy
        approved_hash       VARCHAR,     -- proposal_hash the approver saw; execution needs it unchanged
        approval_expires_at TIMESTAMP,
        reapproval_required BOOLEAN NOT NULL DEFAULT FALSE,
        request_id          VARCHAR,     -- emergency request this pause belongs to
        claimed_by          VARCHAR,     -- worker that took it for execution
        claimed_at          TIMESTAMP)""",
    f"INSERT INTO {SCHEMA}.proposals_v2 ({P_OLD_COLS}) SELECT {P_OLD_COLS} FROM {SCHEMA}.proposals",
    # migration 1 used 'applied' for a change confirmed by a re-read; that is 'verified' now
    f"UPDATE {SCHEMA}.proposals_v2 SET status = 'verified' WHERE status = 'applied'",
    f"ALTER TABLE {SCHEMA}.proposals RENAME TO proposals_pre_m2",
    f"ALTER TABLE {SCHEMA}.proposals_v2 RENAME TO proposals",
    f"""CREATE TABLE {SCHEMA}.executions_v2 (
        exec_id      VARCHAR PRIMARY KEY,
        proposal_id  VARCHAR NOT NULL,
        attempted_at TIMESTAMP NOT NULL,
        mode         VARCHAR NOT NULL CHECK (mode IN ('dry_run', 'live')),
        pre_state    VARCHAR,
        result       VARCHAR NOT NULL CHECK (result IN ({_in(E_RESULTS)})),
        post_state   VARCHAR,
        error        VARCHAR,
        intent_id    VARCHAR,
        worker       VARCHAR)""",
    f"INSERT INTO {SCHEMA}.executions_v2 (exec_id, proposal_id, attempted_at, mode, pre_state, result, post_state, error) "
    f"SELECT exec_id, proposal_id, attempted_at, mode, pre_state, result, post_state, error FROM {SCHEMA}.executions",
    f"ALTER TABLE {SCHEMA}.executions RENAME TO executions_pre_m2",
    f"ALTER TABLE {SCHEMA}.executions_v2 RENAME TO executions",
    # write-ahead record of every live remote write: persisted BEFORE the request is sent
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.intents (
        intent_id    VARCHAR PRIMARY KEY,
        proposal_id  VARCHAR NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        worker       VARCHAR NOT NULL,
        channel      VARCHAR NOT NULL,
        entity_key   VARCHAR NOT NULL,
        action       VARCHAR NOT NULL,
        request      VARCHAR NOT NULL,      -- exact values sent (no credentials)
        pre_state    VARCHAR,
        state        VARCHAR NOT NULL CHECK (state IN ('pending', 'accepted', 'uncertain', 'verified', 'not_applied', 'failed')),
        updated_at   TIMESTAMP NOT NULL,
        error        VARCHAR)""",
    # budget headroom held by increases, so two changes (or two jobs) can't spend the same allowance
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.budget_reservations (
        reservation_id VARCHAR PRIMARY KEY,
        app            VARCHAR NOT NULL,
        day            DATE NOT NULL,
        currency       VARCHAR NOT NULL,
        amount         DOUBLE NOT NULL,
        proposal_id    VARCHAR NOT NULL,
        worker         VARCHAR NOT NULL,
        created_at     TIMESTAMP NOT NULL,
        state          VARCHAR NOT NULL CHECK (state IN ('held', 'consumed', 'released')))""",
]))

MIGRATIONS.append((3, "TikTok Ads removed from AdBot", [
    f"DELETE FROM {SCHEMA}.source_state WHERE source = 'TikTok Ads'",
]))

LATEST = max(v for v, _, _ in MIGRATIONS)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ensure_schema(con) -> list[int]:
    """Apply missing migrations. Each migration runs as ONE script inside a transaction together with its
    schema_migrations row, so a crash leaves either the old schema or the new one, never half of it."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    con.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations "
                "(version INTEGER PRIMARY KEY, description VARCHAR NOT NULL, applied_at TIMESTAMP NOT NULL)")
    done = {int(r[0]) for r in con.execute(f"SELECT version FROM {SCHEMA}.schema_migrations").fetchall()}
    applied = []
    for version, desc, statements in MIGRATIONS:
        if version in done:
            continue
        script = ";\n".join(["BEGIN TRANSACTION", *statements,
                             f"INSERT INTO {SCHEMA}.schema_migrations VALUES ({version}, {_q(desc)}, "
                             f"TIMESTAMP '{_now().isoformat(sep=' ')}')", "COMMIT"])
        try:
            con.execute(script)
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - no transaction left open
                pass
            raise
        applied.append(version)
    return applied
