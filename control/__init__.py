"""
AdBot control plane: data states, import history, campaign mapping, budget policy,
the decision engine (DATA → QUALITY → RECOMMENDATION → POLICY → EXECUTION → VERIFICATION → AUDIT),
emergency controls and alerts.

Nothing in this package writes to an ad platform unless ALL of these hold:
  * DRY_RUN=false on the process,
  * the operating mode allows it (approval with an approval, or bounded_auto for an allowed action),
  * the stop-automation latch is off (emergency pauses excepted),
  * every policy check passes.
"""
from __future__ import annotations

from . import schema


def ensure(con) -> list[int]:
    return schema.ensure_schema(con)
