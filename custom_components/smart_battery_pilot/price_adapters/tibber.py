"""Adapter for hourly price arrays (Tibber-style `today`/`tomorrow` lists).

Format: attributes contain `today` (and optionally `tomorrow`) as plain
lists of float prices in EUR/kWh. The arrays are anchored at local
midnight of the current day; the slot length is 24h / len(today)
(96 entries = 15-minute slots, 24 entries = hourly).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots, price_factor_from_attrs

# A local day has 23, 24 or 25 hours. Sources that ship one entry per real
# hour therefore emit 23 or 25 values on the DST transition days.
MIN_ENTRIES_PER_DAY = 23


def _matches(attrs: dict[str, Any]) -> bool:
    today = attrs.get("today")
    return (
        isinstance(today, list)
        and len(today) >= MIN_ENTRIES_PER_DAY
        and all(isinstance(v, (int, float)) or v is None for v in today)
    )


def _civil_datetime(midnight: datetime, minutes: int) -> datetime:
    """Wall-clock offset from local midnight, so DST does not shift the grid."""
    day = midnight.date() + timedelta(days=minutes // (24 * 60))
    rem = minutes % (24 * 60)
    hour, minute = divmod(rem, 60)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=midnight.tzinfo)


def _boundaries(count: int, midnight: datetime) -> list[datetime]:
    """The `count + 1` slot boundaries of the local day starting at `midnight`.

    Two grids, picked from how many values the source actually sent:

    * **Elapsed time** when the values divide the *real* length of the local
      day evenly (23/24/25 hourly values, 92/96/100 quarter-hourly ones).
      That is what Tibber, Nordpool and friends emit, and it puts the skipped
      or repeated DST hour exactly where the clock puts it.
    * **Wall clock** otherwise, i.e. a source with a fixed 24-slot grid. Then
      entry `i` stays at `i` o'clock even on a transition day, and the grid
      does not shift.
    """
    next_midnight = _civil_datetime(midnight, 24 * 60)
    # Both datetimes share one tzinfo object, and subtracting those is
    # documented to ignore the timezone - which would report 24 h on every
    # day of the year. Convert to UTC first to get the real 23/24/25.
    base = midnight.astimezone(UTC)
    next_base = next_midnight.astimezone(UTC)
    day_hours = (next_base - base).total_seconds() / 3600.0
    per_hour = count / day_hours if day_hours else 0.0
    if per_hour >= 1 and abs(per_hour - round(per_hour)) < 1e-9:
        step = (next_base - base) / count
        return [(base + step * i).astimezone(midnight.tzinfo) for i in range(count + 1)]
    slot_minutes = 24 * 60 / count
    return [_civil_datetime(midnight, round(i * slot_minutes)) for i in range(count + 1)]


def _day_slots(
    values: list[Any], midnight: datetime, price_factor: float = 1.0
) -> list[PriceSlot]:
    if not values:
        return []
    bounds = _boundaries(len(values), midnight)
    slots = []
    seen: set[float] = set()
    for i, value in enumerate(values):
        if value is None:
            continue
        start, end = bounds[i], bounds[i + 1]
        try:
            start_ts = start.timestamp()
            end_ts = end.timestamp()
        except (OverflowError, OSError, ValueError):
            continue
        if start_ts in seen or end_ts <= start_ts:
            continue
        seen.add(start_ts)
        slots.append(
            PriceSlot(start=start, end=end, price=float(value) * price_factor)
        )
    return slots


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # This adapter is also the catch-all that picks up a Nordpool or EPEX
    # sensor whose own structured attributes are momentarily empty, so it has
    # to respect the same cents/öre units those sensors can be configured for.
    factor = price_factor_from_attrs(attrs)
    slots = _day_slots(list(attrs.get("today") or []), midnight, factor)
    tomorrow = attrs.get("tomorrow")
    tomorrow_valid = attrs.get("tomorrow_valid", bool(tomorrow))
    if tomorrow_valid and isinstance(tomorrow, list):
        next_midnight = _civil_datetime(midnight, 24 * 60)
        slots += _day_slots(tomorrow, next_midnight, factor)
    return merge_future_slots(slots, now)


TIBBER_ADAPTER = PriceAdapter(name="hourly_arrays", matches=_matches, parse=_parse)
