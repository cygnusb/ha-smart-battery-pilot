"""Tests for spreading hourly PV forecasts (Energy-dashboard `wh_hours`) over slots."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from smart_battery_pilot.forecast.pv import HourlyPv, intervals_from_wh_hours

CET = timezone(timedelta(hours=1))
BERLIN = ZoneInfo("Europe/Berlin")


def _at(hour: int, minute: int = 0, second: int = 0, day: int = 30) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=CET)


def _iso(when: datetime) -> str:
    return when.isoformat()


# Hour-start keys, as Open-Meteo Solar Forecast and Helios publish them.
START_KEYED = {_iso(_at(h)): wh for h, wh in [(7, 0), (8, 400), (9, 1200), (10, 2000), (11, 0)]}


def test_an_hour_start_key_covers_the_hour_after_it():
    pv = HourlyPv([intervals_from_wh_hours(START_KEYED, period_end=False)])
    assert pv.kwh(_at(9), _at(10)) == pytest.approx(1.2)
    assert pv.kwh(_at(8), _at(11)) == pytest.approx(3.6)


def test_an_hour_is_split_evenly_over_quarter_hour_slots():
    pv = HourlyPv([intervals_from_wh_hours(START_KEYED, period_end=False)])
    quarters = [pv.kwh(_at(10, m), _at(10, m) + timedelta(minutes=15)) for m in (0, 15, 30, 45)]
    assert quarters == pytest.approx([0.5] * 4)


def test_forecast_solar_keys_close_the_period_they_name():
    # Forecast.Solar: the first key is sunrise (0 Wh), "08:00" holds what was
    # produced from sunrise to 08:00, the last key is sunset.
    data = {
        _iso(_at(7, 9, 3)): 0,
        _iso(_at(8)): 510,
        _iso(_at(9)): 1200,
        _iso(_at(17)): 300,
        _iso(_at(17, 9, 27)): 8,
    }
    pv = HourlyPv([intervals_from_wh_hours(data, period_end=True)])
    assert pv.kwh(_at(8), _at(9)) == pytest.approx(1.2)
    assert pv.kwh(_at(9), _at(10)) == pytest.approx(0.0)
    # 07:09:03 - 08:00 holds 510 Wh: the quarter 07:45-08:00 gets 15/51 of it.
    assert pv.kwh(_at(7, 45), _at(8)) == pytest.approx(0.510 * 15 / (50 + 57 / 60), rel=1e-3)
    assert pv.kwh(_at(16), _at(17)) == pytest.approx(0.3)
    assert pv.kwh(_at(17), _at(17, 15)) == pytest.approx(0.008)


def test_several_forecasts_add_up():
    east = intervals_from_wh_hours({_iso(_at(9)): 1500}, period_end=False)
    west = intervals_from_wh_hours({_iso(_at(9)): 500}, period_end=False)
    assert HourlyPv([east, west]).kwh(_at(9), _at(10)) == pytest.approx(2.0)


def test_coverage_ends_with_the_shortest_forecast():
    week = intervals_from_wh_hours(
        {_iso(_at(0) + timedelta(hours=h)): 0 for h in range(24 * 7)}, period_end=False
    )
    two_days = intervals_from_wh_hours(
        {_iso(_at(0) + timedelta(hours=h)): 0 for h in range(48)}, period_end=False
    )
    pv = HourlyPv([week, two_days])
    assert pv.covers(_at(23), _at(23, 15, day=31))
    assert not pv.covers(_at(0, day=31) + timedelta(days=1), _at(0, 15, day=31) + timedelta(days=1))
    assert not pv.covers(_at(0) - timedelta(hours=1), _at(0))


def test_an_empty_forecast_covers_nothing():
    assert not HourlyPv([]).covers(_at(9), _at(10))
    assert not HourlyPv([[]]).covers(_at(9), _at(10))


def test_utc_and_local_keys_describe_the_same_hours():
    utc = {(_at(9).astimezone(UTC)).isoformat(): 1000}
    pv = HourlyPv([intervals_from_wh_hours(utc, period_end=False)])
    assert pv.kwh(_at(9), _at(10)) == pytest.approx(1.0)


def test_the_repeated_autumn_hour_keeps_both_values():
    # 25 Oct 2026: 02:00-03:00 happens twice. Keys carry their offset.
    first = datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN)
    second = datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN, fold=1)
    data = {first.isoformat(): 100, second.isoformat(): 300}
    pv = HourlyPv([intervals_from_wh_hours(data, period_end=False)])
    assert pv.kwh(second, second + timedelta(hours=1)) == pytest.approx(0.3)
    assert pv.kwh(first, first + timedelta(minutes=30)) == pytest.approx(0.05)


def test_unusable_values_are_skipped():
    data = {
        _iso(_at(9)): "n/a",
        _iso(_at(10)): None,
        _iso(_at(11)): -50,
        "garbage": 5,
        _iso(_at(12)): 800,
    }
    pv = HourlyPv([intervals_from_wh_hours(data, period_end=False)])
    assert pv.kwh(_at(9), _at(13)) == pytest.approx(0.8)


def test_naive_keys_are_read_in_the_given_timezone():
    pv = HourlyPv(
        [intervals_from_wh_hours({"2026-10-30T09:00:00": 1000}, period_end=False, default_tz=CET)]
    )
    assert pv.kwh(_at(9), _at(10)) == pytest.approx(1.0)
