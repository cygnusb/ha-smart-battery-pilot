"""Coordinator: the 24 h forecast totals cover 24 hours, whatever the price horizon.

Day-ahead prices for tomorrow are published in the early afternoon. Until
then the price horizon ends at midnight, and totals summed over price slots
alone covered only what was left of the day: the figures read about half
their real value every morning and jumped when the new prices arrived.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from test_coordinator import _coordinator, _FakeHass, _run

from smart_battery_pilot import coordinator as coordinator_module
from smart_battery_pilot.const import (
    CONF_PV_FORECAST_ENTRIES,
    CONF_PV_FORECAST_TODAY,
    CONF_PV_FORECAST_TOMORROW,
)
from smart_battery_pilot.forecast.consumption import TrainingSample
from smart_battery_pilot.forecast.pv import HourlyPv, intervals_from_wh_hours, pv_kwh_for_slot
from smart_battery_pilot.pv_sources import HourlyFetch

TZ = timezone(timedelta(hours=2))
FIRST = datetime(2026, 6, 11, 14, 0, tzinfo=TZ)
NOW = FIRST + timedelta(minutes=10)

PV_TODAY = 20.0
PV_TOMORROW = 10.0
PV_SENSORS = {
    CONF_PV_FORECAST_TODAY: "sensor.pv_today",
    CONF_PV_FORECAST_TOMORROW: "sensor.pv_tomorrow",
}


def _set_now(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: now)


def _hass(hours: int, first: datetime = FIRST, stamp_tz=None, minutes: int = 60) -> _FakeHass:
    """Prices for `hours` hours from `first`, in slots of `minutes`."""
    hass = _FakeHass()
    step = timedelta(minutes=minutes)
    count = hours * 60 // minutes

    def stamp(when: datetime) -> str:
        return (when.astimezone(stamp_tz) if stamp_tz else when).isoformat()

    prices = [
        {
            "start_time": stamp(first + i * step),
            "end_time": stamp(first + (i + 1) * step),
            "marketprice": 200.0 + 10 * (i % 5),
        }
        for i in range(count)
    ]
    hass.states.set("sensor.price", 0.2, {"prices": prices})
    hass.states.set("sensor.soc", 50.0)
    hass.states.set("sensor.pv_today", PV_TODAY)
    hass.states.set("sensor.pv_tomorrow", PV_TOMORROW)
    return hass


def _trained(coord, tz=TZ):
    """Give the forecaster a different answer for every hour of the day.

    Untrained it predicts one flat value, and any 24 intervals would add up
    to the same total wherever they were placed.
    """
    start = datetime(2026, 6, 1, 0, 0, tzinfo=tz)
    coord.forecaster.train(
        [
            TrainingSample(start + timedelta(days=d, hours=h), 0.2 + 0.05 * h)
            for d in range(7)  # weekdays and a weekend: the DST days are weekend days
            for h in range(24)
        ]
    )
    return coord


def _expected_consumption(coord, first: datetime, local_tz, hours: int = 24) -> float:
    """`hours` real hours from `first`, each predicted at its local clock time."""
    first_utc = first.astimezone(UTC)
    return sum(
        coord.forecaster.predict_kwh((first_utc + timedelta(hours=h)).astimezone(local_tz), 1.0)
        for h in range(hours)
    )


def _expected_sine_pv(coord, now: datetime, first: datetime) -> float:
    sunrise, sunset = coord._daylight_window()
    return sum(
        pv_kwh_for_slot(
            first + timedelta(hours=h),
            1.0,
            PV_TODAY,
            PV_TOMORROW,
            now,
            sunrise_hour=sunrise,
            sunset_hour=sunset,
        )
        for h in range(24)
    )


def test_a_short_price_horizon_still_totals_24_hours(monkeypatch):
    _set_now(monkeypatch, NOW)
    coord = _trained(_coordinator(_hass(hours=10), **PV_SENSORS))

    result = _run(coord._async_update_data())

    assert len(result.plan.slots) == 10  # the planner still sees price slots only
    assert result.consumption_forecast_24h_kwh == pytest.approx(
        _expected_consumption(coord, FIRST, TZ), abs=0.006
    )
    assert result.pv_forecast_24h_kwh == pytest.approx(
        _expected_sine_pv(coord, NOW, FIRST), abs=0.006
    )
    # More than the price slots alone, which is what the totals used to be.
    assert result.consumption_forecast_24h_kwh > 1.5 * sum(
        s.net_demand_kwh + s.pv_kwh for s in result.plan.slots
    )


def test_the_tail_reads_the_hour_of_day_in_local_time(monkeypatch):
    """Prices stamped in UTC must not shift the hours forecast beyond them."""
    _set_now(monkeypatch, NOW)
    local = _run(_trained(_coordinator(_hass(hours=10), **PV_SENSORS))._async_update_data())
    utc = _run(
        _trained(_coordinator(_hass(hours=10, stamp_tz=UTC), **PV_SENSORS))._async_update_data()
    )

    assert utc.consumption_forecast_24h_kwh == pytest.approx(local.consumption_forecast_24h_kwh)
    assert utc.pv_forecast_24h_kwh == pytest.approx(local.pv_forecast_24h_kwh)


def test_quarter_hour_prices_total_the_same_24_hours(monkeypatch):
    _set_now(monkeypatch, NOW)
    hourly = _run(_trained(_coordinator(_hass(hours=10), **PV_SENSORS))._async_update_data())
    quarters = _run(
        _trained(_coordinator(_hass(hours=10, minutes=15), **PV_SENSORS))._async_update_data()
    )

    assert len(quarters.plan.slots) == 40
    # The profile is constant within an hour, so the consumption is exact;
    # the PV sine is integrated in finer steps and differs by rounding only.
    assert quarters.consumption_forecast_24h_kwh == pytest.approx(
        hourly.consumption_forecast_24h_kwh, abs=0.011
    )
    assert quarters.pv_forecast_24h_kwh == pytest.approx(hourly.pv_forecast_24h_kwh, rel=0.02)


def test_the_tail_uses_the_hourly_pv_curve_where_it_reaches(monkeypatch):
    _set_now(monkeypatch, NOW)
    # 1 kWh every hour for the 16 hours from the first slot: 10 price slots,
    # 6 hours of tail. The other 8 hours fall back to the daily sine.
    data = {(FIRST + timedelta(hours=h)).isoformat(): 1000.0 for h in range(16)}
    curve = HourlyPv([intervals_from_wh_hours(data, period_end=False)])

    async def fetch(hass, entry_ids):
        return HourlyFetch(pv=curve)

    monkeypatch.setattr(coordinator_module, "async_fetch_hourly_pv", fetch)
    coord = _coordinator(_hass(hours=10), **PV_SENSORS, **{CONF_PV_FORECAST_ENTRIES: ["east"]})

    result = _run(coord._async_update_data())

    sunrise, sunset = coord._daylight_window()
    sine = sum(
        pv_kwh_for_slot(
            FIRST + timedelta(hours=h),
            1.0,
            PV_TODAY,
            PV_TOMORROW,
            NOW,
            sunrise_hour=sunrise,
            sunset_hour=sunset,
        )
        for h in range(16, 24)
    )
    assert sine > 1.0  # 06:00-14:00 tomorrow: the fallback really contributes
    assert result.pv_forecast_24h_kwh == pytest.approx(16.0 + sine, abs=0.006)
    # Diagnostics count planner slots fed from the curve, not tail intervals.
    assert result.inputs["pv_hourly"]["slots"] == 10


@pytest.mark.parametrize("hours", [24, 36])
def test_a_full_price_horizon_totals_its_first_24_hours(monkeypatch, hours):
    """With prices for the whole window nothing is added to what they give."""
    _set_now(monkeypatch, FIRST)
    coord = _trained(_coordinator(_hass(hours=hours), **PV_SENSORS))

    result = _run(coord._async_update_data())

    assert len(result.plan.slots) == hours
    counted = result.plan.slots[:24]
    assert result.consumption_forecast_24h_kwh == round(
        sum(s.net_demand_kwh + s.pv_kwh for s in counted), 2
    )
    assert result.pv_forecast_24h_kwh == round(sum(s.pv_kwh for s in counted), 2)


def test_the_planner_inputs_do_not_depend_on_the_tail(monkeypatch):
    _set_now(monkeypatch, NOW)
    short = _run(_trained(_coordinator(_hass(hours=10), **PV_SENSORS))._async_update_data())
    full = _run(_trained(_coordinator(_hass(hours=36), **PV_SENSORS))._async_update_data())

    for a, b in zip(short.plan.slots, full.plan.slots[:10], strict=True):
        assert (a.start, a.end, a.price) == (b.start, b.end, b.price)
        assert a.net_demand_kwh == b.net_demand_kwh
        assert a.pv_kwh == b.pv_kwh
    assert short.consumption_forecast_24h_kwh == full.consumption_forecast_24h_kwh
    assert short.pv_forecast_24h_kwh == full.pv_forecast_24h_kwh


def test_the_window_starts_at_the_current_slot_not_at_now(monkeypatch):
    """Mid-slot, 24 hours from the slot start are 24 hourly slots, not 25."""
    _set_now(monkeypatch, FIRST + timedelta(minutes=50))
    coord = _trained(_coordinator(_hass(hours=36), **PV_SENSORS))

    result = _run(coord._async_update_data())

    assert result.consumption_forecast_24h_kwh == pytest.approx(
        _expected_consumption(coord, FIRST, TZ), abs=0.006
    )


BERLIN = ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize(
    ("first", "repeated_hour", "skipped_hour"),
    [
        # Autumn: 02:00 happens twice, so 24 real hours end at 13:00 local.
        (datetime(2026, 10, 24, 14, 0, tzinfo=BERLIN), 2, 13),
        # Spring: 02:00 does not exist, so 24 real hours reach 14:00 again.
        (datetime(2026, 3, 28, 14, 0, tzinfo=BERLIN), 14, 2),
    ],
)
def test_the_tail_counts_real_hours_across_a_dst_change(
    monkeypatch, first, repeated_hour, skipped_hour
):
    now = first + timedelta(minutes=10)
    _set_now(monkeypatch, now)
    coord = _trained(_coordinator(_hass(hours=10, first=first)), tz=BERLIN)

    result = _run(coord._async_update_data())

    assert len(result.plan.slots) == 10
    expected = _expected_consumption(coord, first, BERLIN)
    assert result.consumption_forecast_24h_kwh == pytest.approx(expected, abs=0.006)
    # One of each clock hour, plus the hour seen twice, minus the one not seen.
    day = sum(0.2 + 0.05 * h for h in range(24))
    twice = 0.2 + 0.05 * repeated_hour
    never = 0.2 + 0.05 * skipped_hour
    assert expected == pytest.approx(day + twice - never)
