"""Money in integer minor units (cents), never floats, plus sandbox reference FX rates.

The FX table is a fixed reference for the sandbox: it is used to convert wallet balances into
the customer's base currency and to price cross-currency card purchases. It is not a market
quote and is labelled as such wherever it is shown.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

EXPONENT: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "CHF": 2, "CAD": 2}
SYMBOL: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CHF": "CHF ",
    "CAD": "C$",
}
# Units of USD per one unit of the currency. Sandbox reference values, not market quotes.
REFERENCE_USD_RATE: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0067"),
    "CHF": Decimal("1.13"),
    "CAD": Decimal("0.73"),
}
SUPPORTED_CURRENCIES = frozenset(EXPONENT)


class CurrencyError(ValueError):
    pass


def _check(currency: str) -> str:
    if currency not in SUPPORTED_CURRENCIES:
        raise CurrencyError(f"unsupported currency {currency!r}")
    return currency


def to_minor(amount: Decimal | int | str, currency: str) -> int:
    scale = Decimal(10) ** EXPONENT[_check(currency)]
    return int((Decimal(str(amount)) * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def to_major(minor: int, currency: str) -> Decimal:
    return Decimal(minor) / (Decimal(10) ** EXPONENT[_check(currency)])


def fx_rate(source: str, target: str) -> Decimal:
    return REFERENCE_USD_RATE[_check(source)] / REFERENCE_USD_RATE[_check(target)]


def convert(minor: int, source: str, target: str) -> int:
    if source == target:
        return minor
    return to_minor(to_major(minor, source) * fx_rate(source, target), target)


def format_money(minor: int, currency: str) -> str:
    exp = EXPONENT[_check(currency)]
    sign = "-" if minor < 0 else ""
    return f"{sign}{SYMBOL[currency]}{abs(to_major(minor, currency)):,.{exp}f}"
