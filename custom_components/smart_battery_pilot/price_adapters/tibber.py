"""Adapter for hourly price arrays (Tibber-style `today`/`tomorrow` lists).

Format: attributes contain `today` (and optionally `tomorrow`) as plain
lists of float prices in EUR/kWh. The arrays are anchored at local
midnight of the current day; the slot length is 24h / len(today)
(96 entries = 15-minute slots, 24 entries = hourly).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots


def _matches(attrs: dict[str, Any]) -> bool:
    today = attrs.get("today")
    return (
        isinstance(today, list)
        and len(today) >= 24
        and all(isinstance(v, (int, float)) or v is None for v in today)
    )


def _civil_datetime(midnight: datetime, minutes: int) -> datetime:
    """Wall-clock offset from local midnight, so DST does not shift the grid."""
    day = midnight.date() + timedelta(days=minutes // (24 * 60))
    rem = minutes % (24 * 60)
    hour, minute = divmod(rem, 60)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=midnight.tzinfo)


def _day_slots(values: list[Any], midnight: datetime) -> list[PriceSlot]:
    if not values:
        return []
    slot_minutes = int(round(24 * 60 / len(values)))
    slots = []
    seen: set[float] = set()
    for i, value in enumerate(values):
        if value is None:
            continue
        start = _civil_datetime(midnight, i * slot_minutes)
        end = _civil_datetime(midnight, (i + 1) * slot_minutes)
        try:
            start_ts = start.timestamp()
            end_ts = end.timestamp()
        except (OverflowError, OSError, ValueError):
            continue
        if start_ts in seen or end_ts <= start_ts:
            continue
        seen.add(start_ts)
        slots.append(PriceSlot(start=start, end=end, price=float(value)))
    return slots


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slots = _day_slots(list(attrs.get("today") or []), midnight)
    tomorrow = attrs.get("tomorrow")
    tomorrow_valid = attrs.get("tomorrow_valid", bool(tomorrow))
    if tomorrow_valid and isinstance(tomorrow, list):
        next_midnight = _civil_datetime(midnight, 24 * 60)
        slots += _day_slots(tomorrow, next_midnight)
    return merge_future_slots(slots, now)


TIBBER_ADAPTER = PriceAdapter(name="hourly_arrays", matches=_matches, parse=_parse)
