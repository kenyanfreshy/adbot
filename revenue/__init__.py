"""
Unified App Store revenue intelligence for NurseBound Pro, Rapid Nursing Review
Pro and CCRN Adult. Shares the AdBot database (MotherDuck `adbot`, schema
`revenue`) and the AdBot phone web app.

Stage B (this package today): application registry, normalized revenue
schema, Decimal money handling, audit + sync-job bookkeeping.
Store connectors (Stage C/D) and calculations (Stage E) build on these tables.
"""
from __future__ import annotations

import logging

from . import registry, schema
from .schema import SCHEMA

log = logging.getLogger("revenue")


def ensure(con) -> dict:
    """Create/upgrade the revenue schema and seed the registry when needed. Safe to call on every start."""
    applied = schema.ensure_schema(con)
    have = con.execute(f"SELECT count(*) FROM {SCHEMA}.applications").fetchone()[0]
    accounts = con.execute(f"SELECT count(*) FROM {SCHEMA}.store_accounts").fetchone()[0]
    seeded = {}
    if applied or int(have) < len(registry.APPS) or int(accounts) < len(registry.ACCOUNTS):
        seeded = registry.seed(con)
    if applied:
        log.info("revenue schema migrations applied: %s", applied)
    return {"migrations": applied, "seeded": seeded}
