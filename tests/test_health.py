"""Consistency checks: each fires on its failure and stays quiet on a sane battery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re

import pytest

import smart_battery_pilot
from smart_battery_pilot.const import ACTION_AUTO, ACTION_CHARGE, ACTION_EXPORT, ACTION_IDLE
from smart_battery_pilot.health import (
    ALL_CHECKS,
    CHECK_CAPACITY,
    CHECK_CAPTURE,
    CHECK_CHARGE,
    CHECK_COVER,
    CHECK_EXPORT,
    CHECK_EXTERNAL_STUCK,
    CHECK_IDLE,
    CHECK_PLAN_INVALID,
    CHECK_PV_REMAINING,
    CHECK_SCRIPT_FAILING,
    CHECK_SOC_FROZEN,
    HealthMonitor,
    Observation,
)

TZ = timezone(timedelta(hours=2))
START = datetime(2026, 6, 1, 8, 0, tzinfo=TZ)
STEP = timedelta(minutes=30)
PLACEHOLDER = re.compile(r"\{\w+\}")


def _obs(at: datetime, **kwargs) -> Observation:
    defaults = {
        "day": at.date().isoformat(),
        "steering": True,
        "action": ACTION_AUTO,
        "soc": 50.0,
        "capacity_kwh": 10.0,
        "floor_soc": 10.0,
        "max_soc": 95.0,
        "max_charge_w": 5000.0,
        "max_discharge_w": 5000.0,
    }
    return Observation(at=at, **{**defaults, **kwargs})


def _shown(monitor: HealthMonitor) -> set[str]:
    return set(monitor.issues(enabled=True, dry_run=False))


def _day(monitor, day: int, samples: int = 8, **kwargs) -> None:
    """Half-hourly samples through one day, the meters moving by a fixed step."""
    charge = kwargs.pop("charge_per_step", None)
    discharge = kwargs.pop("discharge_per_step", None)
    charge_start = kwargs.pop("charge_start", 100.0)
    discharge_start = kwargs.pop("discharge_start", 100.0)
    start = START + timedelta(days=day)
    for i in range(samples + 1):
        extra = dict(kwargs)
        if charge is not None:
            extra["charge_kwh"] = charge_start + day * 100 + charge * i
        if discharge is not None:
            extra["discharge_kwh"] = discharge_start + day * 100 + discharge * i
        monitor.observe(_obs(start + STEP * i, **extra))


def _days(monitor, count: int, **kwargs) -> None:
    for day in range(count):
        _day(monitor, day, **kwargs)
    # The first sample of the next day closes the last one.
    monitor.observe(_obs(START + timedelta(days=count), **{"soc": kwargs.get("soc", 50.0)}))


SURPLUS = {"pv_w": 3000.0, "load_w": 500.0}


def test_surplus_that_never_reaches_the_battery_fires_after_three_days():
    monitor = HealthMonitor()
    _days(monitor, 2, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert CHECK_CAPTURE not in _shown(monitor)
    _day(monitor, 2, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    monitor.observe(_obs(START + timedelta(days=3)))
    placeholders = monitor.issues(enabled=True, dry_run=False)[CHECK_CAPTURE]
    assert placeholders == {"expected_kwh": "10.0", "actual_kwh": "0.0"}


def test_a_battery_that_takes_the_surplus_clears_the_repair():
    monitor = HealthMonitor()
    _days(monitor, 3, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert CHECK_CAPTURE in _shown(monitor)
    start = START + timedelta(days=3)
    for i in range(9):
        monitor.observe(
            _obs(start + STEP * i, charge_kwh=500.0 + i, discharge_kwh=500.0, **SURPLUS)
        )
    monitor.observe(_obs(START + timedelta(days=4)))
    assert CHECK_CAPTURE not in _shown(monitor)


def test_a_wrong_pv_forecast_is_not_a_failure():
    """Nothing was measured as surplus, so nothing was missed."""
    monitor = HealthMonitor()
    _days(monitor, 5, charge_per_step=0.0, discharge_per_step=0.05, pv_w=100.0, load_w=500.0)
    assert CHECK_CAPTURE not in _shown(monitor)


def test_a_full_battery_has_no_room_for_surplus():
    monitor = HealthMonitor()
    _days(monitor, 5, soc=92.0, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert CHECK_CAPTURE not in _shown(monitor)


def test_a_car_on_the_surplus_excuses_the_battery():
    monitor = HealthMonitor()
    _days(monitor, 5, ev_active=True, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert CHECK_CAPTURE not in _shown(monitor)


def test_too_little_surplus_leaves_a_series_running():
    monitor = HealthMonitor()
    _days(monitor, 2, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    _day(monitor, 2, charge_per_step=0.0, discharge_per_step=0.0, pv_w=500.0, load_w=500.0)
    _day(monitor, 3, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    monitor.observe(_obs(START + timedelta(days=4)))
    assert CHECK_CAPTURE in _shown(monitor)


def test_a_cold_battery_is_only_expected_to_take_its_limit():
    monitor = HealthMonitor()
    _days(
        monitor,
        5,
        charge_factor=0.1,
        max_charge_w=1000.0,
        charge_per_step=0.05,
        discharge_per_step=0.0,
        **SURPLUS,
    )
    assert CHECK_CAPTURE not in _shown(monitor)


def test_without_a_pv_sensor_there_is_no_capture_check():
    monitor = HealthMonitor()
    _days(monitor, 5, load_w=500.0, charge_per_step=0.0, discharge_per_step=0.0)
    assert monitor.issues(enabled=True, dry_run=False) == {}


def test_a_battery_that_does_not_cover_the_house_fires():
    monitor = HealthMonitor()
    _days(monitor, 3, pv_w=0.0, load_w=2000.0, charge_per_step=0.0, discharge_per_step=0.0)
    assert CHECK_COVER in _shown(monitor)


def test_cover_works_at_night_without_a_pv_sensor():
    monitor = HealthMonitor()
    _days(
        monitor,
        3,
        no_pv_expected=True,
        load_w=2000.0,
        charge_per_step=0.0,
        discharge_per_step=0.0,
    )
    assert CHECK_COVER in _shown(monitor)


def test_a_battery_at_its_floor_rightly_stays_out():
    monitor = HealthMonitor()
    _days(
        monitor, 5, soc=12.0, pv_w=0.0, load_w=2000.0, charge_per_step=0.0, discharge_per_step=0.0
    )
    assert CHECK_COVER not in _shown(monitor)


def test_a_charge_slot_that_barely_charges_fires():
    monitor = HealthMonitor()
    _days(
        monitor,
        3,
        action=ACTION_CHARGE,
        power_w=4000.0,
        charge_per_step=0.1,
        discharge_per_step=0.0,
    )
    placeholders = monitor.issues(enabled=True, dry_run=False)[CHECK_CHARGE]
    assert placeholders == {"expected_kwh": "16.0", "actual_kwh": "0.8"}


def test_a_charge_slot_works_from_the_soc_alone():
    monitor = HealthMonitor()
    for day in range(3):
        start = START + timedelta(days=day)
        for i in range(5):
            monitor.observe(_obs(start + STEP * i, action=ACTION_CHARGE, power_w=4000.0, soc=30.0))
    monitor.observe(_obs(START + timedelta(days=3), soc=30.0))
    assert CHECK_CHARGE in _shown(monitor)


def test_a_cold_factor_lowers_what_a_charge_slot_owes():
    monitor = HealthMonitor()
    _days(
        monitor,
        5,
        action=ACTION_CHARGE,
        power_w=4000.0,
        max_charge_w=4000.0,
        charge_factor=0.1,
        charge_per_step=0.15,
        discharge_per_step=0.0,
    )
    assert CHECK_CHARGE not in _shown(monitor)


def test_an_idle_slot_that_discharges_fires():
    monitor = HealthMonitor()
    _days(monitor, 3, action=ACTION_IDLE, charge_per_step=0.0, discharge_per_step=0.2)
    placeholders = monitor.issues(enabled=True, dry_run=False)[CHECK_IDLE]
    assert placeholders == {"actual_kwh": "1.6"}


def test_an_idle_slot_that_holds_passes():
    monitor = HealthMonitor()
    _days(monitor, 3, action=ACTION_IDLE, charge_per_step=0.0, discharge_per_step=0.2)
    start = START + timedelta(days=3)
    for i in range(5):
        monitor.observe(
            _obs(start + STEP * i, action=ACTION_IDLE, charge_kwh=900.0, discharge_kwh=900.0)
        )
    monitor.observe(_obs(START + timedelta(days=4)))
    assert CHECK_IDLE not in _shown(monitor)


def test_trading_around_a_held_soc_spends_nothing():
    monitor = HealthMonitor()
    _days(monitor, 5, action=ACTION_IDLE, charge_per_step=0.2, discharge_per_step=0.2)
    assert CHECK_IDLE not in _shown(monitor)


def test_an_export_slot_that_exports_nothing_fires():
    monitor = HealthMonitor()
    _days(
        monitor,
        3,
        action=ACTION_EXPORT,
        power_w=3000.0,
        charge_per_step=0.0,
        discharge_per_step=0.0,
    )
    assert CHECK_EXPORT in _shown(monitor)


def test_a_gap_of_hours_settles_nothing():
    monitor = HealthMonitor()
    for day in range(4):
        start = START + timedelta(days=day)
        monitor.observe(_obs(start, charge_kwh=1.0, discharge_kwh=1.0, action=ACTION_IDLE))
        monitor.observe(
            _obs(start + timedelta(hours=3), charge_kwh=1.0, discharge_kwh=9.0, action=ACTION_IDLE)
        )
    assert CHECK_IDLE not in _shown(monitor)


def test_a_meter_that_went_backwards_counts_as_no_reading():
    monitor = HealthMonitor()
    for day in range(4):
        start = START + timedelta(days=day)
        monitor.observe(_obs(start, soc=50.0, discharge_kwh=900.0, action=ACTION_IDLE))
        monitor.observe(_obs(start + STEP, soc=50.0, discharge_kwh=1.0, action=ACTION_IDLE))
    assert CHECK_IDLE not in _shown(monitor)


def test_dry_run_counts_nothing_and_hides_what_needs_steering():
    monitor = HealthMonitor()
    _days(
        monitor,
        5,
        steering=False,
        action=None,
        charge_per_step=0.0,
        discharge_per_step=0.0,
        **SURPLUS,
    )
    assert _shown(monitor) == set()
    monitor = HealthMonitor()
    _days(monitor, 3, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert monitor.issues(enabled=True, dry_run=True) == {}
    assert CHECK_CAPTURE in monitor.issues(enabled=True, dry_run=False)
    assert monitor.issues(enabled=False, dry_run=False) == {}


def test_external_control_is_excused_until_it_looks_stuck():
    monitor = HealthMonitor()
    _day(monitor, 0, external=True, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert CHECK_EXTERNAL_STUCK not in _shown(monitor)
    assert CHECK_CAPTURE not in _shown(monitor)
    monitor.observe(_obs(START + timedelta(hours=23), external=True))
    assert CHECK_EXTERNAL_STUCK not in _shown(monitor)
    monitor.observe(_obs(START + timedelta(hours=24), external=True))
    assert CHECK_EXTERNAL_STUCK in _shown(monitor)
    monitor.observe(_obs(START + timedelta(hours=25)))
    assert CHECK_EXTERNAL_STUCK not in _shown(monitor)


def test_a_failing_script_fires_per_action_even_when_auto_works():
    monitor = HealthMonitor()
    for _ in range(2):
        monitor.script_result(ACTION_CHARGE, "script.charge", False)
        monitor.script_result(ACTION_AUTO, "script.auto", True)
    assert CHECK_SCRIPT_FAILING not in _shown(monitor)
    monitor.script_result(ACTION_CHARGE, "script.charge", False)
    assert monitor.issues(enabled=True, dry_run=False)[CHECK_SCRIPT_FAILING] == {
        "action": ACTION_CHARGE,
        "script": "script.charge",
    }
    monitor.script_result(ACTION_CHARGE, "script.charge", True)
    assert CHECK_SCRIPT_FAILING not in _shown(monitor)


def test_a_plan_invalid_for_half_a_day_fires():
    monitor = HealthMonitor()
    monitor.plan_result(START, "price_unavailable")
    monitor.plan_result(START + timedelta(hours=11), "price_unavailable")
    assert monitor.issues(enabled=True, dry_run=True) == {}
    monitor.plan_result(START + timedelta(hours=12), "no_price_data")
    assert monitor.issues(enabled=True, dry_run=True) == {
        CHECK_PLAN_INVALID: {"error": "no_price_data"}
    }
    monitor.plan_result(START + timedelta(hours=13), None)
    assert monitor.issues(enabled=True, dry_run=True) == {}


def test_a_soc_that_stands_still_while_energy_moves_fires():
    monitor = HealthMonitor()
    for i in range(5):
        monitor.observe(_obs(START + STEP * i, charge_kwh=100.0 + 0.5 * i, discharge_kwh=100.0))
    assert monitor.issues(enabled=True, dry_run=True)[CHECK_SOC_FROZEN] == {
        "soc": "50",
        "moved_kwh": "2.0",
    }
    monitor.observe(_obs(START + STEP * 5, soc=51.0, charge_kwh=103.0, discharge_kwh=100.0))
    assert CHECK_SOC_FROZEN not in _shown(monitor)


def test_a_soc_that_stands_still_on_a_resting_battery_is_fine():
    monitor = HealthMonitor()
    for i in range(48):
        monitor.observe(_obs(START + STEP * i, charge_kwh=100.0, discharge_kwh=100.0))
    assert CHECK_SOC_FROZEN not in _shown(monitor)


def test_a_frozen_soc_shows_without_meters_on_forced_slots():
    monitor = HealthMonitor()
    for i in range(5):
        monitor.observe(_obs(START + STEP * i, action=ACTION_CHARGE, power_w=3000.0))
    assert CHECK_SOC_FROZEN in _shown(monitor)


def _pv_day(monitor, day: int, values: list[float]) -> None:
    start = START + timedelta(days=day)
    for i, value in enumerate(values):
        monitor.observe(_obs(start + timedelta(hours=i), pv_today_kwh=value))


def test_a_today_forecast_that_runs_out_by_sunset_fires():
    monitor = HealthMonitor()
    for day in range(3):
        _pv_day(monitor, day, [20.0, 15.0, 8.0, 2.0, 0.1])
    monitor.observe(_obs(START + timedelta(days=3)))
    assert monitor.issues(enabled=True, dry_run=True)[CHECK_PV_REMAINING] == {
        "high_kwh": "20.0",
        "last_kwh": "0.1",
    }


def test_a_today_forecast_that_holds_passes():
    monitor = HealthMonitor()
    for day in range(5):
        _pv_day(monitor, day, [20.0, 21.0, 19.0, 20.0, 20.0])
    monitor.observe(_obs(START + timedelta(days=5)))
    assert CHECK_PV_REMAINING not in _shown(monitor)


def test_a_dark_day_says_nothing_about_the_forecast_sensor():
    monitor = HealthMonitor()
    for day in range(5):
        _pv_day(monitor, day, [1.0, 0.5, 0.1])
    monitor.observe(_obs(START + timedelta(days=5)))
    assert CHECK_PV_REMAINING not in _shown(monitor)


def _charge_window(monitor, offset_days: int, kwh_per_point: float) -> None:
    start = START + timedelta(days=offset_days)
    for i in range(9):
        monitor.observe(
            _obs(
                start + STEP * i,
                soc=20.0 + 5 * i,
                charge_kwh=100.0 + offset_days * 100 + 5 * i * kwh_per_point,
                discharge_kwh=100.0,
            )
        )


def test_a_wrong_capacity_fires_after_three_windows():
    monitor = HealthMonitor()
    for window in range(3):
        _charge_window(monitor, window, 0.2)  # 20 kWh per 100 points, 10 configured
    assert monitor.issues(enabled=True, dry_run=True)[CHECK_CAPACITY] == {
        "implied_kwh": "20.0",
        "configured_kwh": "10.0",
    }


def test_a_right_capacity_stays_quiet_and_clears():
    monitor = HealthMonitor()
    for window in range(3):
        _charge_window(monitor, window, 0.2)
    _charge_window(monitor, 3, 0.1)
    assert CHECK_CAPACITY not in _shown(monitor)


def test_the_state_survives_a_restart():
    monitor = HealthMonitor()
    _days(monitor, 2, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    monitor.script_result(ACTION_IDLE, "script.idle", False)
    monitor.plan_result(START, "no_price_data")
    restored = HealthMonitor.from_dict(monitor.to_dict())
    assert restored.to_dict() == monitor.to_dict()
    _day(restored, 2, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    restored.observe(_obs(START + timedelta(days=3)))
    assert CHECK_CAPTURE in _shown(restored)


def test_unknown_stored_checks_are_dropped():
    restored = HealthMonitor.from_dict(
        {"active": {"gone": {}, CHECK_IDLE: {"actual_kwh": "1.0"}}, "strikes": {"gone": 2}}
    )
    assert _shown(restored) == {CHECK_IDLE}


def test_a_failing_check_warns_once(caplog):
    caplog.set_level(logging.WARNING, logger="smart_battery_pilot.health")
    monitor = HealthMonitor()
    _days(monitor, 5, charge_per_step=0.0, discharge_per_step=0.0, **SURPLUS)
    assert len([r for r in caplog.records if CHECK_CAPTURE in r.getMessage()]) == 1


@pytest.mark.parametrize("check", ALL_CHECKS)
def test_every_check_has_a_translated_repair(check):
    root = Path(smart_battery_pilot.__file__).parent / "translations"
    english = json.loads((root / "en.json").read_text())["issues"][check]
    assert english["title"]
    assert english["description"]
    for path in root.glob("*.json"):
        translated = json.loads(path.read_text())["issues"][check]
        for name in ("title", "description"):
            assert set(PLACEHOLDER.findall(translated[name])) == set(
                PLACEHOLDER.findall(english[name])
            ), (path, check)
