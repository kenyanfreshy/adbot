"""
Currency precision and platform budget units. Nothing here assumes two decimal places.

Sources checked (2026-10-27):
  * Meta Marketing API, "Currencies": every ad-account currency has an OFFSET of 100 or 1; amounts are sent
    as integers in that unit ("minimum denomination level, such as cents for USD", Budgets guide).
    Offset-1 currencies: CLP, COP, CRC, HUF, ISK, IDR, JPY, KRW, PYG, TWD, VND. Note Meta's offset is NOT
    the ISO 4217 minor unit (Meta uses offset 1 for HUF, TWD, IDR, COP and 100 for BHD/JOD).
  * Google Ads API: budgets are CampaignBudget.amount_micros (millionths of the currency unit). The fetched
    Google pages did not state the rounding rule for each currency, so values are rounded to the ISO minor
    unit below and any currency not in that table is refused.
  * Apple Ads Campaign Management API v5, "Update a Campaign": dailyBudgetAmount is Money
    {"amount": "<decimal string>", "currency": "<ISO code>"}. Precision per currency isn't documented;
    the ISO minor unit is used and other currencies are refused.

A budget write in a currency missing from these tables is BLOCKED, not guessed.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional

# Meta Marketing API currency offsets (developers.facebook.com, Marketing API > Currencies)
META_OFFSET = {c: 100 for c in (
    "DZD ARS AUD BHD BDT BOB BGN BRL GBP CAD CNY HRK CZK DKK EGP EUR GTQ HNL HKD INR ILS JOD KES LVL LTL MOP MYR MXN "
    "NZD NIO NGN NOK PKR PEN PHP PLN QAR RON RUB SAR RSD SGD SKK ZAR SEK CHF THB TRY AED UAH USD UYU VEF VES").split()}
META_OFFSET.update({c: 1 for c in "CLP COP CRC HUF ISK IDR JPY KRW PYG TWD VND".split()})

# ISO 4217 minor units for the currencies these accounts can plausibly use. Not exhaustive on purpose.
ISO_MINOR = {c: 2 for c in (
    "USD EUR GBP CAD AUD NZD CHF SEK NOK DKK PLN CZK HUF RON BGN TRY ILS AED SAR QAR INR PKR BDT PHP MYR SGD THB HKD TWD "
    "CNY IDR MXN BRL ARS COP PEN UYU ZAR NGN KES EGP UAH").split()}
ISO_MINOR.update({c: 0 for c in "JPY KRW VND CLP ISK PYG UGX".split()})
ISO_MINOR.update({c: 3 for c in "BHD KWD OMR JOD TND".split()})


class CurrencyUnsupported(ValueError):
    pass


def minor_digits(currency: Optional[str]) -> int:
    c = (currency or "").upper()
    if c not in ISO_MINOR:
        raise CurrencyUnsupported(f"Currency {currency or 'unknown'} isn't in AdBot's verified precision table; budget writes are blocked")
    return ISO_MINOR[c]


def quantize(amount, currency: str) -> Decimal:
    q = Decimal(1).scaleb(-minor_digits(currency))
    return Decimal(str(amount)).quantize(q, rounding=ROUND_HALF_EVEN)


def meta_to_api(amount, currency: str) -> int:
    c = (currency or "").upper()
    if c not in META_OFFSET:
        raise CurrencyUnsupported(f"Meta offset for {currency or 'unknown'} isn't verified; budget writes are blocked")
    return int((Decimal(str(amount)) * META_OFFSET[c]).to_integral_value(rounding=ROUND_HALF_EVEN))


def meta_from_api(value, currency: str) -> Optional[float]:
    if value in (None, "", "0", 0):
        return None
    c = (currency or "").upper()
    if c not in META_OFFSET:
        raise CurrencyUnsupported(f"Meta offset for {currency or 'unknown'} isn't verified")
    return float(Decimal(str(value)) / META_OFFSET[c])


def google_to_micros(amount, currency: str) -> int:
    return int(quantize(amount, currency) * 1_000_000)


def apple_amount(amount, currency: str) -> str:
    return str(quantize(amount, currency))
