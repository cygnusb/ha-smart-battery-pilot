"""Coordinator: hourly PV forecasts from Energy-dashboard integrations, local hours."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import logging

import pytest
from test_coordinator import _coordinator, _FakeHass, _run

from smart_battery_pilot import coordinator as coordinator_module
from smart_battery_pilot.const import (
    CONF_PV_FORECAST_ENTRIES,
    CONF_PV_FORECAST_TODAY,
    CONF_PV_FORECAST_TOMORROW,
)
from smart_battery_pilot.forecast.pv import HourlyPv, intervals_from_wh_hours
from smart_battery_pilot.pv_sources import HourlyFetch

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 6, 11, 5, 30, tzinfo=TZ)
FIRST = datetime(2026, 6, 11, 6, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(coordinator_module.dt_util, "now", lambda: NOW)


def _hass(stamp_tz=TZ) -> _FakeHass:
    """24 hourly prices from 06:00 local, stamped in `stamp_tz`."""
    hass = _FakeHass()
    prices = [
        {
            "start_time": (FIRST + timedelta(hours=h)).astimezone(stamp_tz).isoformat(),
            "end_time": (FIRST + timedelta(hours=h + 1)).astimezone(stamp_tz).isoformat(),
            "marketprice": 200.0 + 10 * (h % 5),
        }
        for h in range(24)
    ]
    hass.states.set("sensor.price", 0.2, {"prices": prices})
    hass.states.set("sensor.soc", 50.0)
    hass.states.set("sensor.pv_today", 20.0)
    hass.states.set("sensor.pv_tomorrow", 10.0)
    return hass


PV_SENSORS = {
    CONF_PV_FORECAST_TODAY: "sensor.pv_today",
    CONF_PV_FORECAST_TOMORROW: "sensor.pv_tomorrow",
}


def _flat_curve(hours: int, wh: float = 1000.0) -> HourlyPv:
    data = {(FIRST + timedelta(hours=h)).isoformat(): wh for h in range(hours)}
    return HourlyPv([intervals_from_wh_hours(data, period_end=False)])


def _fake_fetch(monkeypatch, result: HourlyFetch) -> list:
    calls: list = []

    async def fetch(hass, entry_ids):
        calls.append(list(entry_ids))
        return result

    monkeypatch.setattr(coordinator_module, "async_fetch_hourly_pv", fetch)
    return calls


def test_without_forecast_entries_nothing_is_fetched_and_the_sine_stays(monkeypatch):
    calls = _fake_fetch(monkeypatch, HourlyFetch(pv=_flat_curve(24)))
    result = _run(_coordinator(_hass(), **PV_SENSORS)._async_update_data())

    assert calls == []
    assert result.inputs["pv_hourly"] == {"entries": [], "failed": [], "slots": 0}
    pv = [s.pv_kwh for s in result.plan.slots]
    assert sum(pv) == pytest.approx(20.0, rel=0.05)  # today's sine, 06:00 onwards
    assert max(pv) > 1.5  # shaped, not flat


def test_the_hourly_curve_replaces_the_sine(monkeypatch):
    calls = _fake_fetch(monkeypatch, HourlyFetch(pv=_flat_curve(24)))
    coord = _coordinator(_hass(), **PV_SENSORS, **{CONF_PV_FORECAST_ENTRIES: ["east", "west"]})
    result = _run(coord._async_update_data())

    assert calls == [["east", "west"]]
    assert [s.pv_kwh for s in result.plan.slots] == pytest.approx([1.0] * 24)
    assert result.inputs["pv_hourly"]["slots"] == 24
    assert result.pv_forecast_24h_kwh == pytest.approx(24.0)


def test_slots_beyond_the_curve_fall_back_to_the_sine(monkeypatch):
    _fake_fetch(monkeypatch, HourlyFetch(pv=_flat_curve(3, wh=5000.0)))
    coord = _coordinator(_hass(), **PV_SENSORS, **{CONF_PV_FORECAST_ENTRIES: ["east"]})
    result = _run(coord._async_update_data())

    pv = [s.pv_kwh for s in result.plan.slots]
    assert pv[:3] == pytest.approx([5.0] * 3)
    assert result.inputs["pv_hourly"]["slots"] == 3
    # 23:00-24:00 is dark in the sine; noon is not.
    assert pv[6] > 1.0
    assert pv[17] == 0.0


def test_a_missing_curve_falls_back_and_is_reported_once(monkeypatch, caplog):
    _fake_fetch(monkeypatch, HourlyFetch(pv=None, failed=["west"]))
    coord = _coordinator(_hass(), **PV_SENSORS, **{CONF_PV_FORECAST_ENTRIES: ["east", "west"]})

    with caplog.at_level(logging.WARNING):
        first = _run(coord._async_update_data())
        coord.data = first
        second = _run(coord._async_update_data())

    warnings = [r for r in caplog.records if "hourly solar forecast" in r.getMessage()]
    assert len(warnings) == 1
    assert second.inputs["pv_hourly"]["failed"] == ["west"]
    assert second.inputs["pv_hourly"]["slots"] == 0
    assert sum(s.pv_kwh for s in second.plan.slots) == pytest.approx(20.0, rel=0.05)


def test_utc_stamped_prices_plan_like_local_ones(monkeypatch):
    """Hour of day is local: a UTC price feed must not move the PV curve."""
    local = _run(_coordinator(_hass(TZ), **PV_SENSORS)._async_update_data())
    utc = _run(_coordinator(_hass(UTC), **PV_SENSORS)._async_update_data())

    assert [s.pv_kwh for s in utc.plan.slots] == pytest.approx([s.pv_kwh for s in local.plan.slots])
    assert [s.net_demand_kwh for s in utc.plan.slots] == pytest.approx(
        [s.net_demand_kwh for s in local.plan.slots]
    )


def test_a_missing_curve_while_starting_stays_quiet(monkeypatch, caplog):
    """Forecast integrations often load after us; that is no outage."""
    _fake_fetch(monkeypatch, HourlyFetch(pv=None, failed=["west"]))
    hass = _hass()
    hass.is_running = False
    coord = _coordinator(hass, **PV_SENSORS, **{CONF_PV_FORECAST_ENTRIES: ["west"]})

    with caplog.at_level(logging.WARNING):
        result = _run(coord._async_update_data())

    assert not [r for r in caplog.records if "hourly solar forecast" in r.getMessage()]
    assert result.inputs["pv_hourly"]["slots"] == 0
    assert sum(s.pv_kwh for s in result.plan.slots) == pytest.approx(20.0, rel=0.05)


def test_a_start_with_forecast_entries_plans_again_once_started(monkeypatch):
    registered = []
    monkeypatch.setattr(
        coordinator_module,
        "async_at_started",
        lambda hass, cb: registered.append(cb) or (lambda: None),
    )
    hass = _hass()
    hass.is_running = False
    _run(_coordinator(hass, **{CONF_PV_FORECAST_ENTRIES: ["west"]}).async_setup())
    _run(_coordinator(_hass(), **{CONF_PV_FORECAST_ENTRIES: ["west"]}).async_setup())
    starting_without_entries = _hass()
    starting_without_entries.is_running = False
    _run(_coordinator(starting_without_entries).async_setup())

    assert len(registered) == 1
