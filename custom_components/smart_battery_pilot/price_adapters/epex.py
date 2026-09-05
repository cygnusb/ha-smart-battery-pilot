"""Adapter for the EPEX Spot HACS integration (mampfes/ha_epex_spot).

Format: attributes contain `data`, a list of
{"start_time": iso, "end_time": iso, "price_eur_per_mwh": float}
(newer versions also expose "price_ct_per_kwh").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots, slots_from_entries


def _matches(attrs: dict[str, Any]) -> bool:
    data = attrs.get("data")
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "start_time" in data[0]
        and any(
            key in data[0] for key in ("price_eur_per_mwh", "price_ct_per_kwh", "price_per_kwh")
        )
    )


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    data = list(attrs.get("data") or [])
    if not data:
        return []
    first = data[0]
    if "price_eur_per_mwh" in first:
        key, factor = "price_eur_per_mwh", 1 / 1000.0
    elif "price_ct_per_kwh" in first:
        key, factor = "price_ct_per_kwh", 1 / 100.0
    else:
        key, factor = "price_per_kwh", 1.0
    slots = slots_from_entries(data, "start_time", "end_time", key, factor, default_tz=now.tzinfo)
    return merge_future_slots(slots, now)


EPEX_ADAPTER = PriceAdapter(name="epex_spot", matches=_matches, parse=_parse)
