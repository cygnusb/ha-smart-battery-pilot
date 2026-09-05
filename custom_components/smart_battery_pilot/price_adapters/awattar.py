"""Adapter for aWATTar style integrations.

Format: attributes contain `prices` (or `forecast`), a list of
{"start_time": iso, "end_time": iso, "marketprice": float} with the
market price in EUR/MWh (aWATTar API convention).

Unlike the Nordpool and ENTSO-E adapters this one does *not* consult the
entity's `unit_of_measurement`. There, the attribute holds the same number the
state shows, so the declared unit describes it. Here `marketprice` is raw API
data whose unit is fixed by aWATTar, while the entity's own unit describes the
state - often ct/kWh. Scaling by it would make a correct sensor ten times too
expensive, which is the direction that force-charges a battery.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots, slots_from_entries


def _entries(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("prices", "forecast", "data"):
        value = attrs.get(key)
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "marketprice" in value[0]
        ):
            return list(value)
    return []


def _matches(attrs: dict[str, Any]) -> bool:
    return len(_entries(attrs)) > 0


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    entries = _entries(attrs)
    start_key = "start_time" if "start_time" in entries[0] else "start"
    end_key = "end_time" if "end_time" in entries[0] else "end"
    slots = slots_from_entries(
        entries, start_key, end_key, "marketprice", 1 / 1000.0, default_tz=now.tzinfo
    )
    return merge_future_slots(slots, now)


AWATTAR_ADAPTER = PriceAdapter(name="awattar", matches=_matches, parse=_parse)
