"""
Execution adapters: read an entity's current remote state, and apply one change.

Every adapter exposes:
  read_state(p) -> {"status": "ENABLED" | "PAUSED" | ..., "daily_budget": float | None} or raises
  apply(p)      -> None, raises AmbiguousResult when the request may or may not have taken effect

The live adapters wrap the existing clients in dlt_pipeline.py. They have NOT been run against the
real platforms from this codebase's tests: tests use FakeAdapter. Live writes additionally require
DRY_RUN=false and a mode that allows them (see control.engine).
"""
from __future__ import annotations

import json
from typing import Optional


class AmbiguousResult(Exception):
    """The platform may or may not have applied the change (timeout, dropped connection, 5xx after sending)."""


class UnsupportedAction(Exception):
    pass


PAUSED = {"PAUSED", "REMOVED", "DELETED", "ARCHIVED", "CAMPAIGN_PAUSED", "ADSET_PAUSED"}


def desired_reached(p: dict, state: Optional[dict]) -> bool:
    """Does the remote state already match what the proposal asks for?"""
    if not state:
        return False
    if p["action"].startswith("pause_"):
        return str(state.get("status") or "").upper() in PAUSED
    if p["action"] == "set_budget":
        try:
            return state.get("daily_budget") is not None and abs(float(state["daily_budget"]) - float(p["proposed_value"])) < 0.005
        except (TypeError, ValueError):
            return False
    return False


def _classify(e: Exception) -> Exception:
    import requests
    if isinstance(e, (requests.Timeout, requests.ConnectionError)):
        return AmbiguousResult(f"{type(e).__name__}: {e}")
    msg = str(e)
    if any(f"-> {c}" in msg for c in ("500", "502", "503", "504")):
        return AmbiguousResult(msg[:300])
    return e


class FakeAdapter:
    """In-memory platform for tests. Records every write; never touches the network."""

    def __init__(self, states: Optional[dict] = None, fail: Optional[dict] = None, read_fail: Optional[set] = None):
        self.states = {k: dict(v) for k, v in (states or {}).items()}   # entity_key -> state
        self.fail = dict(fail or {})        # entity_key -> "ambiguous" | "error" | "ambiguous_applied"
        self.read_fail = set(read_fail or ())
        self.writes: list[dict] = []
        self.reads: list[str] = []

    def read_state(self, p: dict) -> Optional[dict]:
        self.reads.append(p["entity_key"])
        if p["entity_key"] in self.read_fail:
            raise RuntimeError("read failed")
        return dict(self.states.get(p["entity_key"], {})) or None

    def apply(self, p: dict) -> None:
        mode = self.fail.get(p["entity_key"])
        self.writes.append({"entity_key": p["entity_key"], "action": p["action"], "value": p.get("proposed_value")})
        if mode == "error":
            raise RuntimeError("platform rejected the change")
        st = self.states.setdefault(p["entity_key"], {})
        if mode == "ambiguous":
            raise AmbiguousResult("timed out")
        if p["action"].startswith("pause_"):
            st["status"] = "PAUSED"
        elif p["action"] == "set_budget":
            st["daily_budget"] = float(p["proposed_value"])
        if mode == "ambiguous_applied":
            raise AmbiguousResult("timed out after the change was made")


class AppleAdapter:
    def __init__(self, client):
        self.c = client

    def read_state(self, p):
        from dlt_pipeline import http
        if p["entity_type"] == "keyword":
            m = json.loads(p.get("metrics") or "{}")
            j = http("GET", f"{self.c.BASE}/campaigns/{p['campaign_id']}/adgroups/{m.get('adgroup_id')}/targetingkeywords/{p['entity_id']}",
                     headers=self.c.headers(), retries=2).json()
            return {"status": (j.get("data") or {}).get("status")}
        j = http("GET", f"{self.c.BASE}/campaigns/{p['campaign_id']}", headers=self.c.headers(), retries=2).json()
        d = j.get("data") or {}
        return {"status": d.get("status"), "daily_budget": float((d.get("dailyBudgetAmount") or {}).get("amount") or 0) or None}

    def apply(self, p):
        from dlt_pipeline import http
        try:
            if p["action"] == "pause_keyword":
                m = json.loads(p.get("metrics") or "{}")
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}/adgroups/{m.get('adgroup_id')}/targetingkeywords/bulk",
                     json=[{"id": int(p["entity_id"]), "status": "PAUSED"}], headers=self.c.headers(), retries=1)
            elif p["action"] == "pause_campaign":
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}", json={"campaign": {"status": "PAUSED"}},
                     headers=self.c.headers(), retries=1)
            elif p["action"] == "set_budget":
                m = json.loads(p.get("metrics") or "{}")
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}",
                     json={"campaign": {"dailyBudgetAmount": {"amount": f"{float(p['proposed_value']):.2f}", "currency": m.get("currency") or "USD"}}},
                     headers=self.c.headers(), retries=1)
            else:
                raise UnsupportedAction(p["action"])
        except UnsupportedAction:
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e)


class MetaAdapter:
    def __init__(self, client):
        self.c = client

    def read_state(self, p):
        from dlt_pipeline import http
        j = http("GET", self.c._url(p["entity_id"]), params={"fields": "status,effective_status,daily_budget",
                                                              "access_token": self.c.token}, retries=2).json()
        b = j.get("daily_budget")
        return {"status": j.get("effective_status") or j.get("status"), "daily_budget": (float(b) / 100) if b else None}

    def apply(self, p):
        from dlt_pipeline import http
        try:
            if p["action"] == "pause_ad_set":
                http("POST", self.c._url(p["entity_id"]), data={"status": "PAUSED", "access_token": self.c.token}, retries=1)
            elif p["action"] == "set_budget":
                http("POST", self.c._url(p["entity_id"]), data={"daily_budget": str(int(round(float(p["proposed_value"]) * 100))),
                                                                 "access_token": self.c.token}, retries=1)
            else:
                raise UnsupportedAction(p["action"])
        except UnsupportedAction:
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e)


class GoogleAdapter:
    def __init__(self, client):
        self.c = client

    def read_state(self, p):
        cid, *rest = p["entity_id"].split("~")
        if p["entity_type"] == "keyword":
            q = (f"SELECT ad_group_criterion.status FROM ad_group_criterion WHERE ad_group.id = {int(rest[0])} "
                 f"AND ad_group_criterion.criterion_id = {int(rest[1])}")
            r = next(iter(self.c._stream(cid, q)), None)
            return {"status": r.ad_group_criterion.status.name} if r else None
        camp = p["campaign_id"].split("~")[1]
        r = next(iter(self.c._stream(cid, f"SELECT campaign.status, campaign_budget.amount_micros FROM campaign WHERE campaign.id = {int(camp)}")), None)
        return {"status": r.campaign.status.name, "daily_budget": r.campaign_budget.amount_micros / 1e6} if r else None

    def apply(self, p):
        try:
            if p["action"] in ("pause_keyword", "pause_campaign"):
                self.c.pause_keyword({"entity_id": p["entity_id"], "entity_type": p["entity_type"]})
            elif p["action"] == "set_budget":
                self.c.set_campaign_budget(p["campaign_id"], float(p["proposed_value"]))
            else:
                raise UnsupportedAction(p["action"])
        except UnsupportedAction:
            raise
        except Exception as e:  # noqa: BLE001
            if "DEADLINE_EXCEEDED" in str(e) or "UNAVAILABLE" in str(e):
                raise AmbiguousResult(str(e)[:300])
            raise


def live_adapters() -> dict:
    """Adapters for the platforms whose credentials are present on this process."""
    import dlt_pipeline as bot
    out = {}
    a, m, g = bot.AppleSearchAds(), bot.MetaAds(), bot.GoogleAds()
    if a.configured:
        out["Apple Search Ads"] = AppleAdapter(a)
    if m.configured:
        out["Meta Ads"] = MetaAdapter(m)
    if g.configured:
        out["Google Ads"] = GoogleAdapter(g)
    return out
