"""Adapter for the ENTSO-E HACS integration (JaccoR/hass-entso-e).

Format: attributes contain `prices` (and/or `prices_today` /
`prices_tomorrow`), lists of {"time": iso, "price": float} in EUR/kWh.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots, slots_from_entries


def _entries(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(attrs.get("prices"), list) and attrs["prices"]:
        return list(attrs["prices"])
    entries: list[dict[str, Any]] = []
    for key in ("prices_today", "prices_tomorrow"):
        if isinstance(attrs.get(key), list):
            entries += attrs[key]
    return entries


def _matches(attrs: dict[str, Any]) -> bool:
    entries = _entries(attrs)
    return (
        len(entries) > 0
        and isinstance(entries[0], dict)
        and "time" in entries[0]
        and "price" in entries[0]
    )


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    slots = slots_from_entries(_entries(attrs), "time", None, "price")
    return merge_future_slots(slots, now)


ENTSOE_ADAPTER = PriceAdapter(name="entsoe", matches=_matches, parse=_parse)
