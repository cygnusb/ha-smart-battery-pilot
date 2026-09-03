"""Price grids across the two days a year that are not 24 hours long.

Tibber, Nordpool and friends ship one entry per *real* hour, so `today` has
23 or 25 values on a transition day. Both cases used to fail: the short day
matched no adapter at all, the long day produced 58-minute slots.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from smart_battery_pilot.price_adapters import detect_adapter
from smart_battery_pilot.price_adapters.base import PriceSlot, merge_future_slots

TZ = ZoneInfo("Europe/Berlin")
SPRING = (2026, 3, 29)  # 23 hours, 02:00 does not exist
AUTUMN = (2026, 10, 25)  # 25 hours, 02:00 happens twice


def _parse(values, day):
    attrs = {"today": list(values)}
    adapter = detect_adapter(attrs)
    assert adapter is not None, f"no adapter matched {len(values)} values"
    return adapter.parse(attrs, datetime(*day, 0, 0, tzinfo=TZ))


@pytest.mark.parametrize(
    ("day", "count", "expected_hours"),
    [
        (SPRING, 23, 1.0),
        (SPRING, 92, 0.25),
        (AUTUMN, 25, 1.0),
        (AUTUMN, 100, 0.25),
        ((2026, 6, 1), 24, 1.0),
        ((2026, 6, 1), 96, 0.25),
    ],
)
def test_real_hour_grids_keep_every_slot_at_full_length(day, count, expected_hours):
    slots = _parse([0.10] * count, day)
    assert len(slots) == count
    assert {round(slot.hours, 6) for slot in slots} == {expected_hours}


@pytest.mark.parametrize(("day", "day_hours"), [(SPRING, 23.0), (AUTUMN, 25.0)])
def test_slots_cover_the_real_length_of_the_day(day, day_hours):
    slots = _parse([0.10] * int(day_hours), day)
    assert sum(slot.hours for slot in slots) == pytest.approx(day_hours)


def test_autumn_keeps_both_repeated_hours_apart():
    """The two 02:00 slots are different instants and must both survive."""
    slots = _parse([0.10 + i * 0.01 for i in range(25)], AUTUMN)
    starts = [slot.start.timestamp() for slot in slots]
    assert len(set(starts)) == 25
    assert starts == sorted(starts)
    repeated = [s for s in slots if s.start.hour == 2]
    assert len(repeated) == 2
    assert repeated[0].price != repeated[1].price


def test_spring_skips_the_hour_that_does_not_exist():
    slots = _parse([0.10] * 23, SPRING)
    hours = [slot.start.hour for slot in slots]
    assert 2 not in hours
    assert hours[:4] == [0, 1, 3, 4]


def test_fixed_24_slot_source_keeps_wall_clock_placement():
    """A source with a rigid 24-value grid still maps entry i to i o'clock."""
    slots = _parse([0.10] * 24, AUTUMN)
    assert slots[0].start.hour == 0
    assert [slot.start.hour for slot in slots][:4] == [0, 1, 2, 3]


def test_slot_length_uses_elapsed_time_not_wall_clock():
    """Subtracting same-tzinfo datetimes ignores the DST jump; hours must not."""
    start = datetime(2026, 3, 29, 1, 0, tzinfo=TZ)
    end = datetime(2026, 3, 29, 3, 0, tzinfo=TZ)
    assert PriceSlot(start=start, end=end, price=0.1).hours == pytest.approx(1.0)


def test_merge_keeps_slots_that_share_a_wall_clock_time():
    """Dedup keys on the instant, not on the naive datetime."""
    first = datetime(2026, 10, 25, 2, 0, fold=0, tzinfo=TZ)
    second = datetime(2026, 10, 25, 2, 0, fold=1, tzinfo=TZ)
    slots = [
        PriceSlot(start=first, end=second, price=0.10),
        PriceSlot(start=second, end=second.replace(hour=3), price=0.20),
    ]
    merged = merge_future_slots(slots, datetime(2026, 10, 25, 0, 0, tzinfo=TZ))
    assert [slot.price for slot in merged] == [0.10, 0.20]


def test_slot_lookup_picks_the_right_repeated_hour():
    """The two 02:00 slots carry different prices; `covers` must tell them apart."""
    from smart_battery_pilot.optimizer import PlanSlot

    first = datetime(2026, 10, 25, 2, 0, fold=0, tzinfo=TZ)
    second = datetime(2026, 10, 25, 2, 0, fold=1, tzinfo=TZ)
    third = datetime(2026, 10, 25, 3, 0, tzinfo=TZ)
    slots = [
        PlanSlot(start=first, end=second, action="charge", price=0.10, net_demand_kwh=1.0),
        PlanSlot(start=second, end=third, action="auto", price=0.40, net_demand_kwh=1.0),
    ]

    in_first = datetime(2026, 10, 25, 2, 30, fold=0, tzinfo=TZ)
    in_second = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=TZ)

    assert [s.action for s in slots if s.covers(in_first)] == ["charge"]
    assert [s.action for s in slots if s.covers(in_second)] == ["auto"]
    assert [s.action for s in slots if s.starts_after(in_first)] == ["auto"]
