"""Tests for the price source adapters."""

from datetime import datetime, timedelta, timezone

import pytest

from smart_battery_pilot.price_adapters import detect_adapter
from smart_battery_pilot.price_adapters.base import PriceSlot, merge_future_slots

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 6, 11, 10, 5, tzinfo=TZ)


def _iso(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ).isoformat()


# --- Nordpool ---------------------------------------------------------------


def _nordpool_attrs(tomorrow_valid: bool = True) -> dict:
    def day(day_no: int) -> list[dict]:
        return [
            {
                "start": _iso(day_no, h, m),
                "end": _iso(day_no, h, m + 15) if m < 45 else _iso(day_no, h + 1 if h < 23 else h, 0 if h < 23 else 59),
                "value": 0.10 + h * 0.001,
            }
            for h in range(24)
            for m in (0, 15, 30, 45)
        ]

    return {
        "raw_today": day(11),
        "raw_tomorrow": day(12) if tomorrow_valid else [],
        "tomorrow_valid": tomorrow_valid,
        "current_price": 0.167,
    }


def test_nordpool_detect_and_parse():
    attrs = _nordpool_attrs()
    adapter = detect_adapter(attrs)
    assert adapter is not None and adapter.name == "nordpool"

    slots = adapter.parse(attrs, NOW)
    # Past slots dropped: today from 10:00, plus all of tomorrow
    assert slots[0].start.hour == 10
    assert slots[-1].start.day == 12
    assert all(s.end > NOW for s in slots)
    assert slots[0].price == pytest.approx(0.11)


def test_nordpool_without_tomorrow():
    attrs = _nordpool_attrs(tomorrow_valid=False)
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert all(s.start.day == 11 for s in slots)


def test_nordpool_price_in_cents():
    """Nordpool HACS can emit `value` in cents/öre when price_in_cents is set."""
    start = datetime(2026, 6, 11, 12, 0, tzinfo=TZ)
    attrs = {
        "raw_today": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "value": 17.5,
            }
        ],
        "tomorrow_valid": False,
        "price_in_cents": True,
        "unit_of_measurement": "EUR/kWh",
    }
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert slots[0].price == pytest.approx(0.175)


def test_nordpool_cents_unit_without_flag():
    start = datetime(2026, 6, 11, 12, 0, tzinfo=TZ)
    attrs = {
        "raw_today": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "value": 17.5,
            }
        ],
        "tomorrow_valid": False,
        "price_in_cents": False,
        "unit_of_measurement": "cEUR/kWh",
    }
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert slots[0].price == pytest.approx(0.175)


# --- EPEX Spot ---------------------------------------------------------------


def test_epex_eur_per_mwh():
    attrs = {
        "data": [
            {"start_time": _iso(11, h), "end_time": _iso(11, h + 1) if h < 23 else _iso(12, 0), "price_eur_per_mwh": 100.0 + h}
            for h in range(24)
        ]
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "epex_spot"
    slots = adapter.parse(attrs, NOW)
    assert slots[0].price == pytest.approx(0.110)  # 110 EUR/MWh at 10:00
    assert slots[0].hours == pytest.approx(1.0)


def test_epex_ct_per_kwh():
    attrs = {
        "data": [
            {"start_time": _iso(11, 12), "end_time": _iso(11, 13), "price_ct_per_kwh": 25.0}
        ]
    }
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert slots[0].price == pytest.approx(0.25)


# --- ENTSO-E -----------------------------------------------------------------


def test_entsoe():
    attrs = {
        "prices": [{"time": _iso(11, h), "price": 0.05 + h * 0.01} for h in range(24)]
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "entsoe"
    slots = adapter.parse(attrs, NOW)
    # end inferred from next entry start
    assert slots[0].hours == pytest.approx(1.0)
    assert slots[0].start.hour == 10


# --- aWATTar -----------------------------------------------------------------


def test_awattar():
    attrs = {
        "prices": [
            {"start_time": _iso(11, h), "end_time": _iso(11, h + 1) if h < 23 else _iso(12, 0), "marketprice": 90.0}
            for h in range(24)
        ]
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "awattar"
    slots = adapter.parse(attrs, NOW)
    assert slots[0].price == pytest.approx(0.09)


# --- Hourly arrays (Tibber style) -------------------------------------------


def test_hourly_arrays():
    attrs = {
        "today": [0.20] * 24,
        "tomorrow": [0.30] * 24,
        "tomorrow_valid": True,
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "hourly_arrays"
    slots = adapter.parse(attrs, NOW)
    assert slots[0].start.hour == 10
    assert slots[-1].price == pytest.approx(0.30)
    assert len(slots) == 14 + 24


def test_quarter_hour_arrays():
    attrs = {"today": [0.20] * 96, "tomorrow": [], "tomorrow_valid": False}
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert slots[0].hours == pytest.approx(0.25)
    # 10:00 slot kept because it contains NOW (10:05)
    assert slots[0].start.hour == 10 and slots[0].start.minute == 0


def test_hourly_arrays_spring_forward_unique_instants():
    """The skipped DST hour must not produce two slots at the same instant."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 3, 29, 0, 30, tzinfo=tz)
    attrs = {
        "today": [0.10] * 24,
        "tomorrow": [0.20] * 24,
        "tomorrow_valid": True,
    }
    slots = detect_adapter(attrs).parse(attrs, now)
    today = [s for s in slots if s.start.date().isoformat() == "2026-03-29"]
    instants = [s.start.timestamp() for s in today]
    assert len(instants) == len(set(instants))
    assert all(s.end.timestamp() > s.start.timestamp() for s in today)
    tomorrow = [s for s in slots if s.start.date().isoformat() == "2026-03-30"]
    assert tomorrow[0].start == datetime(2026, 3, 30, 0, 0, tzinfo=tz)
    assert today[-1].end <= tomorrow[0].start


def test_hourly_arrays_autumn_tomorrow_starts_at_local_midnight():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 25, 10, 0, tzinfo=tz)
    attrs = {
        "today": [0.10] * 24,
        "tomorrow": [0.20] * 24,
        "tomorrow_valid": True,
    }
    slots = detect_adapter(attrs).parse(attrs, now)
    tomorrow = [s for s in slots if s.start.date().isoformat() == "2026-10-26"]
    assert tomorrow[0].start == datetime(2026, 10, 26, 0, 0, tzinfo=tz)
    today = [s for s in slots if s.start.date().isoformat() == "2026-10-25"]
    assert today[-1].end <= tomorrow[0].start


# --- misc --------------------------------------------------------------------


def test_detect_unknown_format():
    assert detect_adapter({"foo": "bar"}) is None


def test_merge_future_slots_dedupes():
    slot = PriceSlot(start=NOW, end=NOW + timedelta(hours=1), price=0.1)
    dupe = PriceSlot(start=NOW, end=NOW + timedelta(hours=1), price=0.2)
    merged = merge_future_slots([slot, dupe], NOW)
    assert len(merged) == 1 and merged[0].price == 0.2


# --- unit handling across adapters -------------------------------------------


def test_cent_prices_survive_the_fallback_to_the_generic_adapter():
    """A Nordpool sensor whose raw_today is empty must not be read as euros.

    `raw_today` is briefly empty right after a restart and whenever the
    upstream API hiccups. The generic `hourly_arrays` adapter then picks the
    sensor up via its plain `today` list - and used to take 30 cents for 30
    EUR/kWh, which makes every hour look like a giant arbitrage opportunity.
    """
    attrs = {
        "price_in_cents": True,
        "unit_of_measurement": "cent/kWh",
        "today": [30.0] * 24,
        "raw_today": [],
        "raw_tomorrow": [],
        "tomorrow_valid": False,
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "hourly_arrays"
    slots = adapter.parse(attrs, NOW)
    assert slots
    assert all(slot.price == pytest.approx(0.30) for slot in slots)


def test_ore_prices_survive_the_fallback_to_the_generic_adapter():
    attrs = {
        "unit_of_measurement": "öre/kWh",
        "today": [45.0] * 24,
        "raw_today": [],
        "tomorrow_valid": False,
    }
    adapter = detect_adapter(attrs)
    assert adapter.name == "hourly_arrays"
    assert all(slot.price == pytest.approx(0.45) for slot in adapter.parse(attrs, NOW))


def test_euro_arrays_are_left_alone():
    """The common case must not be scaled: a plain EUR/kWh list stays as it is."""
    attrs = {"unit_of_measurement": "EUR/kWh", "today": [0.28] * 24}
    slots = detect_adapter(attrs).parse(attrs, NOW)
    assert all(slot.price == pytest.approx(0.28) for slot in slots)


def test_both_adapters_agree_on_the_same_cent_sensor():
    """Whichever adapter claims a sensor, the price must come out the same."""
    raw = [
        {"start": _iso(11, h), "end": _iso(11, h + 1) if h < 23 else _iso(12, 0), "value": 30.0}
        for h in range(24)
    ]
    structured = {
        "price_in_cents": True,
        "unit_of_measurement": "cent/kWh",
        "today": [30.0] * 24,
        "raw_today": raw,
        "tomorrow_valid": False,
    }
    degraded = {**structured, "raw_today": []}

    via_nordpool = detect_adapter(structured)
    via_fallback = detect_adapter(degraded)
    assert (via_nordpool.name, via_fallback.name) == ("nordpool", "hourly_arrays")
    assert via_nordpool.parse(structured, NOW)[0].price == pytest.approx(
        via_fallback.parse(degraded, NOW)[0].price
    )
