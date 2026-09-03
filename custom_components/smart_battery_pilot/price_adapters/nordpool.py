"""Adapter for the Nordpool HACS integration (custom_components/nordpool).

Format: attributes contain `raw_today` / `raw_tomorrow` lists of
{"start": iso, "end": iso, "value": float} entries (EUR/kWh) and a
`tomorrow_valid` flag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .base import PriceAdapter, PriceSlot, merge_future_slots, slots_from_entries


def _matches(attrs: dict[str, Any]) -> bool:
    raw = attrs.get("raw_today")
    return (
        isinstance(raw, list)
        and len(raw) > 0
        and isinstance(raw[0], dict)
        and "value" in raw[0]
        and "start" in raw[0]
    )


def _price_factor(attrs: dict[str, Any]) -> float:
    """Nordpool can emit EUR/kWh or cents/öre depending on integration settings."""
    if attrs.get("price_in_cents"):
        return 0.01
    unit = str(attrs.get("unit_of_measurement") or attrs.get("unit") or "").lower()
    unit = unit.replace(" ", "")
    if "öre" in unit or "øre" in unit or "ore/kwh" in unit:
        return 0.01
    if unit.startswith("c") and "eur" in unit:
        return 0.01
    if "ct/" in unit or unit in ("ct", "cent", "cents"):
        return 0.01
    return 1.0


def _parse(attrs: dict[str, Any], now: datetime) -> list[PriceSlot]:
    entries = list(attrs.get("raw_today") or [])
    if attrs.get("tomorrow_valid"):
        entries += list(attrs.get("raw_tomorrow") or [])
    slots = slots_from_entries(
        entries,
        "start",
        "end",
        "value",
        price_factor=_price_factor(attrs),
        default_tz=now.tzinfo,
    )
    return merge_future_slots(slots, now)


NORDPOOL_ADAPTER = PriceAdapter(name="nordpool", matches=_matches, parse=_parse)
