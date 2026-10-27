"""
What each ad platform integration can actually report and do. The decision engine reads this
instead of applying one platform's keyword logic everywhere.

Facts behind it (official docs; re-check before enabling live writes):
  * Apple Ads Campaign Management API v5 reports taps and tap-through installs per keyword or campaign,
    in the campaign's currency (localSpend). It reports no revenue.
  * Meta Marketing API insights report spend, link clicks and 'actions'/'action_values' per ad set,
    in the ad account's currency (account_currency). Purchase values appear only when an app purchase
    event is set up; installs only when app install events are tracked.
  * Google Ads API reports metrics.conversions for whatever conversion actions the account counts
    (not necessarily installs) and metrics.conversions_value as configured, in customer.currency_code.
  * TikTok Ads: no AdBot integration exists yet.
"""
from __future__ import annotations

CHANNELS = {
    "Apple Search Ads": {
        "platform": "apple_ads", "supported": True,
        "click_label": "taps", "conversion_label": "installs", "conversion_is_install": True,
        "revenue": "none",                       # never reports revenue
        "maturity_days": 1,                      # most tap-through installs land within a day
        "actions": {"keyword": ["pause_keyword"], "campaign": ["pause_campaign", "set_budget"]},
        "budget_entity": "campaign",
        "rules": {"keyword": ["cpt_ceiling", "no_conversions_after_clicks", "no_conversions_after_spend"],
                  "campaign": ["no_conversions_after_spend"]},
        "creds": ["ASA_CLIENT_ID", "ASA_TEAM_ID", "ASA_KEY_ID", "ASA_ORG_ID", "ASA_PRIVATE_KEY"],
    },
    "Meta Ads": {
        "platform": "meta_ads", "supported": True,
        "click_label": "link clicks", "conversion_label": "app installs (Meta-attributed)", "conversion_is_install": True,
        "revenue": "attributed_if_reported",     # purchase action_values, only when the event is set up
        "maturity_days": 3,
        "actions": {"ad_set": ["pause_ad_set", "set_budget"]},
        "budget_entity": "ad_set",
        "rules": {"ad_set": ["no_conversions_after_spend", "acos_ceiling"]},
        "creds": ["META_ACCESS_TOKEN", "META_AD_ACCOUNT_IDS"],
    },
    "Google Ads": {
        "platform": "google_ads", "supported": True,
        "click_label": "clicks", "conversion_label": "conversions (as counted in Google Ads)", "conversion_is_install": False,
        "revenue": "attributed_if_reported",     # conversions_value as configured in the account
        "maturity_days": 3,
        "actions": {"keyword": ["pause_keyword"], "app_campaign": ["pause_campaign", "set_budget"]},
        "budget_entity": "campaign",
        "rules": {"keyword": ["cpt_ceiling", "no_conversions_after_clicks", "no_conversions_after_spend", "acos_ceiling"],
                  "app_campaign": ["no_conversions_after_spend", "acos_ceiling"]},
        "creds": ["GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_IDS"],
    },
    "TikTok Ads": {
        "platform": "tiktok_ads", "supported": False,
        "click_label": "clicks", "conversion_label": "conversions", "conversion_is_install": False,
        "revenue": "none", "maturity_days": 3, "actions": {}, "budget_entity": None, "rules": {},
        "creds": ["TIKTOK_ACCESS_TOKEN", "TIKTOK_ADVERTISER_IDS"],
    },
}

MANAGED = [c for c, v in CHANNELS.items() if v["supported"]]


def supports(channel: str, entity_type: str, action: str) -> bool:
    return action in CHANNELS.get(channel, {}).get("actions", {}).get(entity_type or "", [])


def rules_for(channel: str, entity_type: str) -> list[str]:
    return CHANNELS.get(channel, {}).get("rules", {}).get(entity_type or "", [])


def pause_action(channel: str, entity_type: str):
    for a in CHANNELS.get(channel, {}).get("actions", {}).get(entity_type or "", []):
        if a.startswith("pause_"):
            return a
    return None
