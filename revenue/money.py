"""
Money arithmetic for the revenue system.

Rules this module enforces (Phase 4 / Phase 5 of the spec):
  * Amounts are Decimal, never float. Floats are rejected on input so a value
    like 0.1 + 0.2 can never creep into a financial total.
  * Every amount carries its ISO 4217 currency. Adding two different
    currencies raises CurrencyMismatch; the only way across currencies is
    convert(), which needs an explicit ExchangeRate with its date and source.
  * Missing data stays missing. `UNAVAILABLE` (None) propagates through the
    helpers so a dashboard shows "Unavailable", never a made-up 0.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional

UNAVAILABLE = None  # the single sentinel for "we don't have this number"

# Storage precision. DECIMAL(18,6) in the database: enough for sub-cent
# per-unit proceeds and for amounts well past any realistic app revenue.
STORE_PLACES = Decimal("0.000001")
RATE_PLACES = Decimal("0.0000000001")
_CCY = re.compile(r"^[A-Z]{3}$")
# ISO 4217 currencies that have no minor unit (used for display rounding only)
ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}


class CurrencyMismatch(ValueError):
    """Two amounts in different currencies were combined without a conversion."""


class MoneyError(ValueError):
    """An amount or currency could not be parsed safely."""


def to_decimal(value) -> Optional[Decimal]:
    """Parse a report value into Decimal. None / '' -> None (unavailable).

    Accepts int, Decimal, and strings such as "1,234.50", "(12.00)" (accounting
    negative) or "-0.70". Floats are refused: by the time a float exists the
    precision is already gone.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise MoneyError("a boolean is not an amount")
    if isinstance(value, float):
        raise MoneyError("refusing a float amount; pass a string or Decimal")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        s = str(value).strip().replace("−", "-")  # unicode minus
        if s == "":
            return None
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        s = s.replace(",", "").replace(" ", "")
        try:
            d = Decimal(s)
        except InvalidOperation as e:
            raise MoneyError(f"not a number: {value!r}") from e
        if neg:
            d = -d
    if not d.is_finite():
        raise MoneyError(f"not a finite number: {value!r}")
    return d


def check_currency(code: str) -> str:
    c = str(code or "").strip().upper()
    if not _CCY.match(c):
        raise MoneyError(f"not an ISO 4217 currency code: {code!r}")
    return c


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self):
        amt = to_decimal(self.amount)
        if amt is None:
            raise MoneyError("Money needs an amount; use UNAVAILABLE (None) for missing values")
        object.__setattr__(self, "amount", amt)
        object.__setattr__(self, "currency", check_currency(self.currency))

    @classmethod
    def of(cls, amount, currency: str) -> "Money":
        return cls(to_decimal(amount), currency)

    def _same(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise TypeError("can only combine Money with Money")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"cannot combine {self.currency} and {other.currency} without an exchange rate")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def times(self, factor) -> "Money":
        f = to_decimal(factor)
        if f is None:
            raise MoneyError("factor is unavailable")
        return Money(self.amount * f, self.currency)

    def stored(self) -> Decimal:
        """Value as written to DECIMAL(18,6) columns (banker's rounding)."""
        return self.amount.quantize(STORE_PLACES, rounding=ROUND_HALF_EVEN)

    def display(self) -> str:
        """Human string, e.g. 'USD 1,234.50'. Rounds half-up to the currency's minor unit."""
        places = Decimal("1") if self.currency in ZERO_DECIMAL else Decimal("0.01")
        q = self.amount.quantize(places, rounding=ROUND_HALF_UP)
        return f"{self.currency} {q:,}"


def total(items: Iterable[Optional[Money]], currency: Optional[str] = None) -> Optional[Money]:
    """Sum amounts of ONE currency.

    Returns UNAVAILABLE when there is nothing to add or any item is unavailable
    (a partial total would look like a real one). Mixed currencies raise.
    """
    out: Optional[Money] = None
    seen = False
    for m in items:
        seen = True
        if m is None:
            return UNAVAILABLE
        out = m if out is None else out + m
    if not seen:
        return UNAVAILABLE
    if currency and out is not None and out.currency != check_currency(currency):
        raise CurrencyMismatch(f"expected {currency}, got {out.currency}")
    return out


def sum_by_currency(items: Iterable[Money]) -> dict[str, Money]:
    """Group amounts by currency without converting anything."""
    out: dict[str, Money] = {}
    for m in items:
        out[m.currency] = out[m.currency] + m if m.currency in out else m
    return out


@dataclass(frozen=True)
class ExchangeRate:
    """1 unit of `base` = `rate` units of `quote`, as published by `source` for `rate_date`."""
    rate_date: dt.date
    base: str
    quote: str
    rate: Decimal
    source: str

    def __post_init__(self):
        r = to_decimal(self.rate)
        if r is None or r <= 0:
            raise MoneyError("exchange rate must be a positive number")
        if not str(self.source or "").strip():
            raise MoneyError("an exchange rate needs a documented source")
        if not isinstance(self.rate_date, dt.date):
            raise MoneyError("an exchange rate needs its conversion date")
        object.__setattr__(self, "rate", r)
        object.__setattr__(self, "base", check_currency(self.base))
        object.__setattr__(self, "quote", check_currency(self.quote))


@dataclass(frozen=True)
class Converted:
    money: Money
    original: Money
    rate: ExchangeRate


def convert(m: Optional[Money], rate: Optional[ExchangeRate], to: str) -> Optional[Converted]:
    """Convert with a documented rate. Unavailable in -> unavailable out.

    Same-currency "conversion" returns the original with rate 1 from 'identity'.
    """
    if m is None:
        return UNAVAILABLE
    to = check_currency(to)
    if m.currency == to:
        return Converted(m, m, ExchangeRate(dt.date.today(), to, to, Decimal(1), "identity"))
    if rate is None:
        raise CurrencyMismatch(f"no exchange rate for {m.currency}->{to}")
    if rate.base == m.currency and rate.quote == to:
        amt = m.amount * rate.rate
    elif rate.base == to and rate.quote == m.currency:
        amt = m.amount / rate.rate
    else:
        raise CurrencyMismatch(f"rate {rate.base}->{rate.quote} can't convert {m.currency}->{to}")
    return Converted(Money(amt, to), m, rate)


def ratio(numerator: Optional[Decimal], denominator: Optional[Decimal]) -> Optional[Decimal]:
    """Safe ratio for rates like ROAS or churn: unavailable or /0 -> UNAVAILABLE."""
    if numerator is None or denominator is None:
        return UNAVAILABLE
    if denominator == 0:
        return UNAVAILABLE
    return Decimal(numerator) / Decimal(denominator)
