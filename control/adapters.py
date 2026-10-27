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

from . import money


class AmbiguousResult(Exception):
    """The platform may or may not have applied the change (timeout, dropped connection, 5xx after sending)."""


class UnsupportedAction(Exception):
    pass


class DryRunViolation(RuntimeError):
    """A live write was attempted while DRY_RUN is on. Never caught as a platform error."""


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
        self.budget_blockers: dict = {}
        self.on_write = None
        self.crash_after_write: set = set()

    def read_state(self, p: dict) -> Optional[dict]:
        self.reads.append(p["entity_key"])
        if p["entity_key"] in self.read_fail:
            raise RuntimeError("read failed")
        return dict(self.states.get(p["entity_key"], {})) or None

    def budget_check(self, p: dict, pre: Optional[dict]) -> list[str]:
        r = list(self.budget_blockers.get(p["entity_key"], []))
        if pre is None or pre.get("daily_budget") is None:
            r.append("No readable daily budget")
        return r

    def apply(self, p: dict) -> None:
        if self.on_write:
            self.on_write(p)     # tests: something happens "between" writes (a stop, a lost lease)
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
        if p["entity_key"] in self.crash_after_write:
            raise SystemExit("worker crashed after the platform applied the change")


class LiveAdapter:
    """Base for adapters that talk to a real platform. Every write passes _guard() first: with DRY_RUN not
    explicitly "false" in this process's environment the write is refused here too, whatever the caller did."""
    name = "live"

    def _guard(self):
        import os
        if str(os.environ.get("DRY_RUN", "true")).lower() not in ("false", "0", "no"):
            raise DryRunViolation("DRY_RUN is on: live adapters refuse every write")

    def budget_check(self, p: dict, pre: Optional[dict]) -> list[str]:
        """Reasons a budget write must NOT be sent for this entity (empty = verified as supported)."""
        return ["Budget writes aren't supported for this channel"]


class AppleAdapter(LiveAdapter):
    """Apple Ads Campaign Management API v5 (PUT /api/v5/campaigns/{id}). Apple has announced v5 is sunset on
    2027-01-26 in favour of the Apple Ads Platform API; this adapter must be migrated before then."""
    name = "apple"

    def __init__(self, client):
        self.c = client

    def read_state(self, p):
        from dlt_pipeline import http
        if p["entity_type"] == "keyword":
            m = _m(p)
            j = http("GET", f"{self.c.BASE}/campaigns/{p['campaign_id']}/adgroups/{m.get('adgroup_id')}/targetingkeywords/{p['entity_id']}",
                     headers=self.c.headers(), retries=2).json()
            return {"status": (j.get("data") or {}).get("status")}
        j = http("GET", f"{self.c.BASE}/campaigns/{p['campaign_id']}", headers=self.c.headers(), retries=2).json()
        d = j.get("data") or {}
        dba = d.get("dailyBudgetAmount") or {}
        return {"status": d.get("status"), "daily_budget": float(dba["amount"]) if dba.get("amount") not in (None, "") else None,
                "currency": dba.get("currency"), "budget_level": "campaign", "deleted": d.get("deleted")}

    def budget_check(self, p, pre):
        r = []
        if not pre or pre.get("daily_budget") is None:
            r.append("Apple campaign has no readable daily budget")
        if pre and pre.get("currency") and p.get("currency") and pre["currency"] != p["currency"]:
            r.append(f"Apple campaign budget is in {pre['currency']}, the proposal in {p['currency']}")
        try:
            money.minor_digits((pre or {}).get("currency") or p.get("currency"))
        except money.CurrencyUnsupported as e:
            r.append(str(e))
        return r

    def apply(self, p):
        from dlt_pipeline import http
        self._guard()
        try:
            if p["action"] == "pause_keyword":
                m = _m(p)
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}/adgroups/{m.get('adgroup_id')}/targetingkeywords/bulk",
                     json=[{"id": int(p["entity_id"]), "status": "PAUSED"}], headers=self.c.headers(), retries=1)
            elif p["action"] == "pause_campaign":
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}", json={"campaign": {"status": "PAUSED"}},
                     headers=self.c.headers(), retries=1)
            elif p["action"] == "set_budget":
                cur = p["currency"]
                http("PUT", f"{self.c.BASE}/campaigns/{p['campaign_id']}",
                     json={"campaign": {"dailyBudgetAmount": {"amount": money.apple_amount(p["proposed_value"], cur), "currency": cur}}},
                     headers=self.c.headers(), retries=1)
            else:
                raise UnsupportedAction(p["action"])
        except (UnsupportedAction, DryRunViolation, money.CurrencyUnsupported):
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e)


class MetaAdapter(LiveAdapter):
    """Meta Marketing API. Budgets are integers in the account currency's offset unit (money.META_OFFSET).
    A budget is only written at ad-set level when the ad set has its own DAILY budget and its campaign has no
    campaign budget (Advantage campaign budget) and no ad-set budget sharing."""
    name = "meta"

    def __init__(self, client):
        self.c = client

    def read_state(self, p):
        from dlt_pipeline import http
        j = http("GET", self.c._url(p["entity_id"]), params={
            "fields": "status,effective_status,daily_budget,lifetime_budget,account_id,"
                      "campaign{daily_budget,lifetime_budget,is_adset_budget_sharing_enabled}",
            "access_token": self.c.token}, retries=2).json()
        acct = j.get("account_id")
        cur = None
        if acct:
            a = http("GET", self.c._url(f"act_{str(acct).removeprefix('act_')}"), params={"fields": "currency", "access_token": self.c.token},
                     retries=2).json()
            cur = a.get("currency")
        camp = j.get("campaign") or {}
        out = {"status": j.get("effective_status") or j.get("status"), "currency": cur, "budget_level": "ad_set",
               "raw_daily_budget": j.get("daily_budget"), "lifetime_budget": j.get("lifetime_budget"),
               "campaign_budget": bool(camp.get("daily_budget") or camp.get("lifetime_budget")),
               "budget_sharing": camp.get("is_adset_budget_sharing_enabled")}
        try:
            out["daily_budget"] = money.meta_from_api(j.get("daily_budget"), cur)
        except money.CurrencyUnsupported:
            out["daily_budget"] = None
            out["currency_unverified"] = True
        return out

    def budget_check(self, p, pre):
        r = []
        if not pre:
            return ["Meta ad set state couldn't be read"]
        if pre.get("campaign_budget"):
            r.append("This campaign uses a campaign budget (Advantage campaign budget); ad-set budgets can't be set")
        if pre.get("lifetime_budget") not in (None, "", "0", 0):
            r.append("This ad set has a lifetime budget, not a daily one")
        if pre.get("budget_sharing"):
            r.append("Ad set budget sharing is on (up to 20% moves between ad sets); not verified for AdBot writes")
        if pre.get("currency_unverified") or pre.get("currency") not in money.META_OFFSET:
            r.append(f"Meta currency offset for {pre.get('currency') or 'unknown'} isn't verified")
        if pre.get("daily_budget") is None:
            r.append("Ad set has no readable daily budget")
        if pre.get("currency") and p.get("currency") and pre["currency"] != p["currency"]:
            r.append(f"Account currency is {pre['currency']}, the proposal is in {p['currency']}")
        return r

    def apply(self, p):
        from dlt_pipeline import http
        self._guard()
        try:
            if p["action"] == "pause_ad_set":
                http("POST", self.c._url(p["entity_id"]), data={"status": "PAUSED", "access_token": self.c.token}, retries=1)
            elif p["action"] == "set_budget":
                http("POST", self.c._url(p["entity_id"]), data={"daily_budget": str(money.meta_to_api(p["proposed_value"], p["currency"])),
                                                                 "access_token": self.c.token}, retries=1)
            else:
                raise UnsupportedAction(p["action"])
        except (UnsupportedAction, DryRunViolation, money.CurrencyUnsupported):
            raise
        except Exception as e:  # noqa: BLE001
            raise _classify(e)


class GoogleAdapter(LiveAdapter):
    """Google Ads API. Budgets live on a CampaignBudget (amount_micros). Shared budgets
    (explicitly_shared, or used by more than one campaign) and non-daily periods are never written."""
    name = "google"

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
        r = next(iter(self.c._stream(cid, "SELECT campaign.status, campaign_budget.resource_name, campaign_budget.amount_micros, "
                                          "campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.period, "
                                          f"customer.currency_code FROM campaign WHERE campaign.id = {int(camp)}")), None)
        if not r:
            return None
        b = r.campaign_budget
        return {"status": r.campaign.status.name, "daily_budget": b.amount_micros / 1e6 if b.amount_micros else None,
                "currency": r.customer.currency_code, "budget_level": "campaign_budget", "budget_resource": b.resource_name,
                "shared": bool(b.explicitly_shared) or int(b.reference_count or 0) > 1, "period": b.period.name}

    def budget_check(self, p, pre):
        r = []
        if not pre:
            return ["Google campaign state couldn't be read"]
        if pre.get("shared"):
            r.append("This campaign uses a shared budget; changing it would change other campaigns")
        if pre.get("period") not in ("DAILY",):
            r.append(f"Budget period is {pre.get('period')}, not DAILY")
        if pre.get("daily_budget") is None:
            r.append("No readable daily budget")
        try:
            money.minor_digits(pre.get("currency"))
        except money.CurrencyUnsupported as e:
            r.append(str(e))
        if pre.get("currency") and p.get("currency") and pre["currency"] != p["currency"]:
            r.append(f"Account currency is {pre['currency']}, the proposal is in {p['currency']}")
        return r

    def apply(self, p):
        self._guard()
        try:
            if p["action"] in ("pause_keyword", "pause_campaign"):
                self.c.pause_keyword({"entity_id": p["entity_id"], "entity_type": p["entity_type"]})
            elif p["action"] == "set_budget":
                pre = p.get("_pre") or {}
                if not pre.get("budget_resource"):
                    raise UnsupportedAction("budget resource not read before the write")
                cid = p["entity_id"].split("~")[0]
                c = self.c.client
                op = c.get_type("CampaignBudgetOperation")
                op.update.resource_name = pre["budget_resource"]
                op.update.amount_micros = money.google_to_micros(p["proposed_value"], p["currency"])
                c.copy_from(op.update_mask, c.get_type("FieldMask")(paths=["amount_micros"]))
                c.get_service("CampaignBudgetService").mutate_campaign_budgets(customer_id=cid, operations=[op])
            else:
                raise UnsupportedAction(p["action"])
        except (UnsupportedAction, DryRunViolation, money.CurrencyUnsupported):
            raise
        except Exception as e:  # noqa: BLE001
            if "DEADLINE_EXCEEDED" in str(e) or "UNAVAILABLE" in str(e):
                raise AmbiguousResult(str(e)[:300])
            raise


def _m(p) -> dict:
    m = p.get("metrics") or {}
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = {}
    return m


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
