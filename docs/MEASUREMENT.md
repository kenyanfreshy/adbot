# AdBot measurement and revenue rules

Every number AdBot shows or acts on has one named basis. Different bases are never treated as
interchangeable, and missing values are shown as unavailable, never as 0.

## Revenue bases (never mixed)

| Basis | Where it comes from | Used for decisions? |
|---|---|---|
| **Platform-attributed purchase value** | What one ad platform says its own ads produced (Meta `action_values` for purchase actions under the account's attribution setting; Google Ads `conversions_value` under its conversion-action settings). Apple Ads reports none. | Yes: **only** the ACoS rule, and only for that platform's own entities |
| **Store gross sales (estimated)** | App Store / Google Play sales reports, all buyers, organic and paid | No (display, Revenue tab) |
| **Store proceeds (estimated)** | Same reports, developer proceeds, in the proceeds currency | No (display) |
| **Finalized proceeds** | Apple finance reports / Google earnings reports, by fiscal period | No (display) |
| **Refunds** | Negative sales lines | No (display) |
| **Modeled value** | Value per install × installs | **Never.** Labeled "estimate", display only |
| **Recurring revenue / lifetime value** | Not computed (needs subscription event and cohort data) | No |

Store revenue is never assigned to ad campaigns: there is no install-attribution feed linking a store
purchase to an ad click.

## Overlapping platform claims

Meta and Google can both claim the same purchase. So:

* Platform-attributed revenue is **never added across platforms**: not in the KPI tiles, not per app,
  not in `metrics.ad_summary` (grouped by currency **and** platform).
* When several platforms report revenue for the current filter, the Ads tab shows it per platform with
  "not added: attribution overlaps", and ROAS/ACoS are shown only when exactly one platform reported.
* Rebalancing across apps compares returns only when each app's revenue comes from a single platform;
  otherwise it is blocked ("revenue is claimed by several platforms; there is no deduplicated figure").
  There is no deduplication method in AdBot today, so none is assumed.

## What each decision rule uses

| Rule | Inputs | Revenue basis |
|---|---|---|
| `cpt_ceiling` (Apple, Google keywords) | spend ÷ taps/clicks, mature days, min taps | none |
| `no_conversions_after_clicks` | clicks, platform-attributed conversions (installs on Apple/Meta; Google's own conversion actions) | none |
| `no_conversions_after_spend` (Meta ad sets) | spend, conversions | none |
| `acos_ceiling` (Meta; Google when reported) | spend ÷ that platform's attributed purchase value, needs ≥ `min_conversions_for_revenue_rules` | platform-attributed, single platform |
| `budget_pacing`, `campaign_daily_max` | monthly allocation × (1 − reserve) − observed month-to-date spend, ÷ calendar days left | none |
| `spend_safeguard` | observed month-to-date spend vs the monthly limits | none |
| rebalance (recommendation only) | shrunk ROAS per app | platform-attributed, single platform per app, same currency |

Spend thresholds are in `budget_currency`; data in another currency blocks money rules (no FX conversion).

## Currency precision

See `control/money.py`. Meta budgets use Meta's per-currency offset (100 or 1, from the Marketing API
currency table; e.g. JPY, KRW, HUF, TWD use 1). Google uses micros rounded to the ISO 4217 minor unit;
Apple uses a decimal string at the ISO minor unit. A currency outside these verified tables blocks
budget writes.
