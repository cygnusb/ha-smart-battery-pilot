"""Price source adapters.

Each adapter converts the attributes of a price forecast entity into a
normalized list of PriceSlot objects. Adapters are pure functions over
plain dicts so they can be unit-tested without Home Assistant.
"""

from .base import PriceSlot, PriceAdapter, detect_adapter, ADAPTERS

__all__ = ["PriceSlot", "PriceAdapter", "detect_adapter", "ADAPTERS"]
