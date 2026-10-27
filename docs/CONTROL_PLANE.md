# AdBot control plane: semantics

## Who can write to ad platforms

Only the **hourly worker** (`adbot-hourly`, `python dlt_pipeline.py`). The web service never sends an
advertising change: "Run bot now" is a decision preview (forced dry run, no adapters, no execution step),
and Stop, Resume, emergency pause and approvals are *records* in the shared database that the worker acts
on at its next run (hourly at :07 UTC). Nothing on the phone is "immediate".

A live write needs all of: `DRY_RUN=false` on the worker, a mode that allows the action, a valid bound
approval, no stop, a held job lease, passing pre-write checks, and the platform credentials. Live
adapters also refuse every write themselves while `DRY_RUN` is not `false` (defence in depth).

## Shared storage

The web service and the worker must use the same MotherDuck database (`MOTHERDUCK_TOKEN`, same
`MOTHERDUCK_DATABASE`). On Render (`RENDER` is set) or with `ADBOT_ENV=production`, both refuse to run on
a local DuckDB file (`SharedStorageMissing`; the web API answers 503). The worker writes a heartbeat; the
Ads tab raises "Hourly job not seen in this database" when it's missing or older than 3 hours, which is how
a split between the two services shows up.

## Stop automation

| Stage | Recorded as |
|---|---|
| Stop requested | `automation_stop.requested_at` |
| Persisted | written, read back from the shared DB, then `persisted_at` set; otherwise the request fails (503) |
| Worker acknowledged | `acknowledged_at` / `acknowledged_by`, set at the start of the worker's next run |

Effects: every approval still waiting is withdrawn (blocked, needs a new approval). The worker re-reads the
flag **before every advertising write**, including queued and previously approved actions, and once more
immediately before sending; a stop that lands between two writes stops the second. Emergency pauses
requested **after** the stop may still run (they are a later, explicit instruction). Stopping never pauses
ads.

**Resume** (password required) clears the latch, un-pauses nothing, and blocks every earlier approval
until a person approves it again. Old approvals never run by themselves after a resume.

## Emergency pause

| Stage | Where |
|---|---|
| Requested + persisted | `emergency_pause` latch (`requested_at`, `persisted_at`), one approved pause proposal per campaign in scope |
| Worker acknowledged | latch `acknowledged_at` |
| Remote pause requested | a write-ahead intent per campaign (`control.intents`) |
| Verified / failed / unconfirmed | proposal `verified`, `failed`, `applied` (accepted, not yet visible) or `uncertain` |
| Dry run | proposal `simulated`; no adapter call at all |

Scope is the managed apps' mapped campaigns only: an unknown app or channel is rejected (never widened to
"all"); a campaign re-mapped or marked "not ours" after the request is not paused. Emergency pauses bypass
optimization modes, cooldowns and daily limits, never DRY_RUN, authorization or scope. Clearing the latch
withdraws unsent pauses and resumes nothing.

## Approvals are bound

`proposal_hash` covers the exact action and values, currency, account, campaign, entity, rule and rule
version, policy version (hash of the rule settings) and the evidence (metrics and data window). Approving
requires the hash the person saw (`expected_hash`); a proposal that changed in between is refused. An
approval expires after `approval_ttl_hours` (default 24 h).

Before each write the worker re-validates: the row still hashes to what was approved; not expired; same
policy version; still in scope; mode allows it; source data usable; the rule still fires on this run's data;
no emergency (for increases); the current remote value still equals the approved "from" value (a manual
change in the platform invalidates it); the platform's budget shape is supported; budget headroom exists.
A failed binding check blocks the proposal and requires a new approval; transient ones (e.g. mode) leave
it waiting until it expires.

## Execution lifecycle

`proposed → approved → executing → applied → verified`, or `blocked`, `expired`, `failed`, `uncertain`,
`superseded`, `rejected`, `simulated` (dry run), `already_done`.

* An intent row (`pending`) is persisted **before** the request is sent, with the exact values.
* Accepted by the platform → `applied` (intent `accepted`); a re-read confirming it → `verified`.
* Timeout / 5xx / dropped connection → `uncertain`. Never re-sent: `reconcile()` reads the remote state
  next run → `verified`, or `failed` ("did not take effect; a new proposal may follow").
* `applied` but not visible: re-checked for up to 3 h, then `failed`.
* Worker crash: an `executing` row whose worker no longer holds the lease is recovered from its intent:
  no intent → nothing was sent → blocked, needs re-approval; intent present → reconciled by reading.
* An entity with an `uncertain`/`applied`/`executing` change gets no new change until it settles.

## Locks and budget reservations

One lease (`hourly`, 55 min) per run, shared by the worker and the web preview. The lease is renewed before
every write; if it expired or another worker took it, the run raises `LeaseLost` and sends nothing more.
Budget increases reserve their delta (`control.budget_reservations`) against the app's daily allowance
minus current budgets; a second change (or a second job) can't spend the same headroom. Holds from a
crashed run are released after the lease time.

## Report revisions

Each imported row carries `_fetched_at`. Within an import and against what is stored, the newest revision
per (channel, entity, date) wins; an older revision arriving late is dropped and counted, not merged over a
newer one. The engine reads the newest `_fetched_at` first, then the newest load.

## Web security

Signed HttpOnly SameSite=Lax session cookie (30 days; changing `APP_PASSWORD` or `SESSION_SECRET` signs
everyone out). Every write also needs the `X-AdBot-Request` header and, when sent, a same-host `Origin`
(CSRF). Rate limits per client: control actions 12/min, "Run bot now" 3 per 5 min, others 60/min; logins
10 failures per client and 50 overall per 15 min, keyed on the proxy's last `X-Forwarded-For` hop.
Switching to Approval / Bounded automatic, Resume, and changing automatic actions need the password again.
In production a demo password (`test-pass` etc.) is refused, and `tests/local_server.py` refuses to start
where `RENDER`, `ADBOT_ENV=production` or `MOTHERDUCK_TOKEN` is set.

Limitations: one shared password (no per-user accounts or roles); sessions can't be revoked individually
(rotate `SESSION_SECRET`); rate-limit counters are per process and reset on restart.

## Verification status (2026-10-27)

| Check | How | Evidence so far |
|---|---|---|
| Unit / failure-path tests | `python -m unittest discover -s tests` | 136 pass in Claude's sandbox through the DuckDB **CLI shim** (real DuckDB 1.5.6 engine, not the Python driver) |
| Real `duckdb` + `dlt` runtime | `python scripts/staging_check.py` | **Not run yet.** The sandbox can't install packages from PyPI. Must run on a machine with the real packages |
| Isolated MotherDuck staging | `python scripts/staging_check.py --motherduck adbot_staging` and `python scripts/process_check.py --motherduck adbot_staging` | **Not run yet** (needs `MOTHERDUCK_TOKEN`; both scripts refuse the production database name) |
| Separate processes (lease race, crashed holder + expiry, reservations, web→worker→web shared state, crash after remote success) | `python scripts/process_check.py` | All pass on a local file via the shim: real separate processes, **serialised by the file lock**, not true concurrency |
| Auth audit (cookie flags, every `/api` route needs a session, header not a substitute, expiry, tampering, origin) | `tests/test_revenue_web.py` `AuthAuditTests` | Pass in-process. The live site's cookie flags and its Host/Origin behaviour behind Render's proxy are **not** verified |

Known findings, not fixed (feature scope frozen):
* **Budget reservations are only safe under the job lease.** Without it, three processes reserved 18 of 10 headroom (check-then-insert race, reproduced by `process_check.py`). Only the lease-holding worker executes today, but `run_cycle()` will execute without a lease if a caller passes none. Fix: a conditional ledger `UPDATE`, and refuse live execution without a lease.
* **Logout doesn't revoke the session token server-side.** A copied cookie works until it expires (30 days) or the password / `SESSION_SECRET` changes.
* **Apple Ads Campaign Management API v5 is deprecated and will be sunset on January 26, 2027** (Apple: "Apple Ads Campaign Management API 5" page and the Apple Ads Platform API changelog). Its replacement, the Apple Ads Platform API 1.0 (August 2026), uses `/v1/campaigns/{id}` scoped to an ad account, a campaign `dailyBudget` field, `sharedBudgets` from budget orders, and the same OAuth client-credentials flow with scope `searchadsorg`. The Apple adapter (reporting and writes) must be migrated before then. Whether existing v5 API credentials work unchanged on the new API isn't stated in the pages checked.
* The v5 Apple budget check doesn't yet block campaigns attached to budget orders.
