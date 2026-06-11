"""Base classes and auto-detection for price source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """A time slot with a market price in EUR/kWh."""

    start: datetime
    end: datetime
    price: float

    @property
    def hours(self) -> float:
        """Slot length in hours."""
        return (self.end - self.start).total_seconds() / 3600.0


@dataclass(frozen=True, slots=True)
class PriceAdapter:
    """A named parser converting entity attributes to price slots."""

    name: str
    matches: Callable[[dict[str, Any]], bool]
    parse: Callable[[dict[str, Any], datetime], list[PriceSlot]]


def _parse_dt(value: Any) -> datetime:
    """Parse an ISO timestamp (or datetime passthrough)."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def slots_from_entries(
    entries: list[dict[str, Any]],
    start_key: str,
    end_key: str | None,
    price_key: str,
    price_factor: float = 1.0,
) -> list[PriceSlot]:
    """Build slots from a list of {start, end, price} style dicts.

    If end_key is None the end of each slot is the start of the next one;
    the last slot gets the median slot length of the series.
    """
    parsed: list[tuple[datetime, datetime | None, float]] = []
    for entry in entries:
        price = entry.get(price_key)
        if price is None:
            continue
        start = _parse_dt(entry[start_key])
        end = _parse_dt(entry[end_key]) if end_key and entry.get(end_key) else None
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
    """Drop past slots (keep the one containing `now`), dedupe and sort."""
    seen: dict[datetime, PriceSlot] = {}
    for slot in slots:
        if slot.end <= now:
            continue
        seen[slot.start] = slot
    return sorted(seen.values(), key=lambda s: s.start)


# Imported at the bottom to avoid circular imports.
from .nordpool import NORDPOOL_ADAPTER  # noqa: E402
from .epex import EPEX_ADAPTER  # noqa: E402
from .entsoe import ENTSOE_ADAPTER  # noqa: E402
from .awattar import AWATTAR_ADAPTER  # noqa: E402
from .tibber import TIBBER_ADAPTER  # noqa: E402

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
