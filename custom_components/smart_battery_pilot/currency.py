"""The currency prices are counted in, and how big its numbers run.

Prices are never converted: the planner works in whatever currency the price
sensor delivers, and that is taken to be the one Home Assistant is set to.
An arbitrage decision only compares prices with each other, so the unit
cancels out - except where a fixed number meets a price. Defaults, input
limits and sanity bounds are written in euros, and a Swedish curve runs about
eleven times larger. `price_scale` says by how much, roughly.
"""

from __future__ import annotations

from math import floor, log10
import re
from typing import Any

DEFAULT_CURRENCY = "EUR"

# Units of the currency per euro, to an order of magnitude - NOT exchange
# rates. They size defaults and plausibility bounds, nothing is converted with
# them, so a rate drifting by a third changes nothing that matters. Covers
# the currencies of the European day-ahead markets plus a few common ones.
_PRICE_SCALE: dict[str, float] = {
    "EUR": 1.0,
    "SEK": 11.0,
    "NOK": 11.5,
    "DKK": 7.5,
    "PLN": 4.3,
    "CZK": 25.0,
    "HUF": 400.0,
    "RON": 5.0,
    "CHF": 0.95,
    "GBP": 0.85,
    "USD": 1.1,
    "CAD": 1.5,
    "AUD": 1.7,
    "NZD": 1.9,
}

# A price field's upper bound in euros, and how far past it a field may go for
# a currency without a known scale.
_PRICE_FIELD_MAX_EUR = 1.0
_PRICE_FIELD_MAX_UNKNOWN = 1000.0

_CODE = re.compile(r"[A-Z]{3}")
# Spellings of the euro cent; öre/øre are shared by three currencies and say
# nothing about which one.
_EURO_CENT_PREFIXES = ("ct", "c€", "ceur")


def currency_issue_id(entry_id: str) -> str:
    """Repair issue raised while an entry's price entity counts in another currency."""
    return f"currency_mismatch_{entry_id}"


def hass_currency(hass: Any) -> str:
    """The ISO code Home Assistant is set to, upper case."""
    return str(hass.config.currency or DEFAULT_CURRENCY).upper()


def price_scale(currency: str) -> float | None:
    """Rough size of `currency` against the euro, None when unknown."""
    return _PRICE_SCALE.get(currency.upper())


def scaled(value_eur: float, currency: str) -> float:
    """A euro-sized setting in `currency`, rounded to two significant digits.

    0.20 EUR becomes 2.2 SEK or 80 HUF, not 2.2000000000000006. An unknown
    currency keeps the euro number - better a plain guess than none.
    """
    value = value_eur * (price_scale(currency) or 1.0)
    if value == 0:
        return 0.0
    return round(value, max(1 - floor(log10(abs(value))), 0))


def price_field_max(currency: str) -> float:
    """Upper bound of a per-kWh input field."""
    scale = price_scale(currency)
    if scale is None:
        return _PRICE_FIELD_MAX_UNKNOWN
    return scaled(_PRICE_FIELD_MAX_EUR, currency)


def currency_from_attrs(attrs: dict[str, Any]) -> str | None:
    """The currency a price entity declares, or None when it does not say.

    Read from an explicit `currency` attribute (Nordpool, Tibber), else from
    the numerator of the unit when it is an ISO code, a euro sign or a euro
    cent. Only used to warn about a mismatch - never to guess a scale.
    """
    declared = attrs.get("currency")
    if isinstance(declared, str) and _CODE.fullmatch(declared.strip().upper()):
        return declared.strip().upper()
    unit = str(attrs.get("unit_of_measurement") or attrs.get("unit") or "").replace(" ", "")
    numerator = unit.split("/", 1)[0]
    if _CODE.fullmatch(numerator):
        return numerator
    if numerator.startswith("€") or numerator.lower().startswith(_EURO_CENT_PREFIXES):
        return "EUR"
    return None
