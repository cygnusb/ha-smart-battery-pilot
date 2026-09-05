"""Base classes and auto-detection for price source adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

_UTC = UTC


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """A time slot with a market price in EUR/kWh."""

    start: datetime
    end: datetime
    price: float

    @property
    def hours(self) -> float:
        """Slot length in hours of real elapsed time.

        Subtracting two aware datetimes that share one tzinfo object is
        documented to ignore the timezone and compare the naive values, so a
        slot spanning a DST change would report its wall-clock length. Going
        through UTC gives the length the battery actually charges for.
        """
        start, end = self.start, self.end
        if start.tzinfo is not None and end.tzinfo is not None:
            start = start.astimezone(_UTC)
            end = end.astimezone(_UTC)
        return (end - start).total_seconds() / 3600.0


@dataclass(frozen=True, slots=True)
class PriceAdapter:
    """A named parser converting entity attributes to price slots."""

    name: str
    matches: Callable[[dict[str, Any]], bool]
    parse: Callable[[dict[str, Any], datetime], list[PriceSlot]]


# Every spelling of "hundredth of a currency unit" this has met in the wild.
# Matched as a prefix of the unit's numerator, not as the whole string: the
# exact-match version this replaced let `Cent/kWh`, `cents/kWh` and `c€/kWh`
# through as euros.
_CENT_PREFIXES = ("ct", "cent", "c€", "ceur", "öre", "øre", "ore")


def price_factor_from_attrs(attrs: dict[str, Any]) -> float:
    """Scale factor turning an entity's price unit into EUR/kWh.

    Lives here rather than in one adapter because every adapter that reads a
    bare number needs it. A Nordpool sensor whose `raw_today` is momentarily
    empty falls through to the generic `hourly_arrays` adapter, and that one
    used to read the very same cents as euros - a plan built on prices a
    hundred times too large charges the battery from the grid at every
    opportunity.

    Only the numerator is inspected, so a currency that merely begins with a
    `c` - CZK, CHF, CAD - is not mistaken for cents.
    """
    if attrs.get("price_in_cents"):
        return 0.01
    unit = str(attrs.get("unit_of_measurement") or attrs.get("unit") or "").lower()
    unit = unit.replace(" ", "")
    # No currency code contains these, so they are safe to find anywhere.
    if "öre" in unit or "øre" in unit:
        return 0.01
    numerator = unit.split("/", 1)[0]
    if numerator.startswith(_CENT_PREFIXES):
        return 0.01
    return 1.0


def _parse_dt(value: Any, default_tz: tzinfo | None = None) -> datetime:
    """Parse an ISO timestamp (or datetime passthrough).

    Timestamps without an offset - template sensors emit those - are read as
    local time. Leaving them naive would blow up later when they meet the
    timezone-aware `now`, and the traceback would say nothing useful.
    """
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None and default_tz is not None:
        return parsed.replace(tzinfo=default_tz)
    return parsed


def slots_from_entries(
    entries: list[dict[str, Any]],
    start_key: str,
    end_key: str | None,
    price_key: str,
    price_factor: float = 1.0,
    default_tz: tzinfo | None = None,
) -> list[PriceSlot]:
    """Build slots from a list of {start, end, price} style dicts.

    If end_key is None the end of each slot is the start of the next one;
    the last slot inherits the length of the one before it.
    """
    parsed: list[tuple[datetime, datetime | None, float]] = []
    for entry in entries:
        price = entry.get(price_key)
        if price is None:
            continue
        start = _parse_dt(entry[start_key], default_tz)
        end = _parse_dt(entry[end_key], default_tz) if end_key and entry.get(end_key) else None
        parsed.append((start, end, float(price) * price_factor))

    parsed.sort(key=lambda item: item[0])
    slots: list[PriceSlot] = []
    for i, (start, end, price) in enumerate(parsed):
        if end is None:
            if i + 1 < len(parsed):
                end = parsed[i + 1][0]
            elif slots:
                end = start + (slots[-1].end - slots[-1].start)
            else:
                end = start + timedelta(hours=1)
        slots.append(PriceSlot(start=start, end=end, price=price))
    return slots


def merge_future_slots(slots: list[PriceSlot], now: datetime) -> list[PriceSlot]:
    """Drop past slots (keep the one containing `now`), dedupe and sort.

    Keyed on the absolute instant, not the datetime object: two aware
    datetimes sharing one tzinfo compare by their naive values, which would
    silently merge the two 02:00 slots of an autumn DST day into one.
    """
    now_ts = now.timestamp()
    seen: dict[float, PriceSlot] = {}
    for slot in slots:
        if slot.end.timestamp() <= now_ts:
            continue
        seen[slot.start.timestamp()] = slot
    return [seen[key] for key in sorted(seen)]


# Imported at the bottom to avoid circular imports.
from .awattar import AWATTAR_ADAPTER
from .entsoe import ENTSOE_ADAPTER
from .epex import EPEX_ADAPTER
from .nordpool import NORDPOOL_ADAPTER
from .tibber import TIBBER_ADAPTER

# Order matters: the first matching adapter wins during auto-detection.
ADAPTERS: list[PriceAdapter] = [
    NORDPOOL_ADAPTER,
    EPEX_ADAPTER,
    ENTSOE_ADAPTER,
    AWATTAR_ADAPTER,
    TIBBER_ADAPTER,
]


def detect_adapter(attributes: dict[str, Any]) -> PriceAdapter | None:
    """Return the first adapter whose format matches the entity attributes."""
    for adapter in ADAPTERS:
        try:
            if adapter.matches(attributes):
                return adapter
        except (TypeError, KeyError, ValueError):
            continue
    return None
