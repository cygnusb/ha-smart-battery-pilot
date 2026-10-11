"""Coordinator side of planned EV sessions from evcc."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from test_coordinator import _coordinator, _FakeHass, _plan_slot
from test_pilot_savings import T0, _meters, _plan, _step

from smart_battery_pilot import coordinator as coordinator_module
from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_IDLE,
    CONF_EV_BATTERY_POLICY,
    CONF_EV_SOURCE,
    CONF_EVCC_URL,
    CONF_EXTERNAL_CONTROL_ENTITY,
    EV_POLICY_ALLOW,
    EV_POLICY_BLOCK,
    EV_POLICY_EXTERNAL,
    EV_SOURCE_EVCC,
)
from smart_battery_pilot.evcc import EvccError
from smart_battery_pilot.sensor import ChargePlanSensor, ConfigSensor


def _run(coro):
    return asyncio.run(coro)


def _hass():
    hass = _FakeHass()
    hass.states.set("sensor.price", 0.30, {"today": [0.30] * 24, "tomorrow": [0.30] * 24})
    hass.states.set("sensor.soc", 50.0)
    return hass


def _evcc_state(**lp) -> dict:
    loadpoint = {
        "connected": True,
        "charging": True,
        "chargePower": 11000,
        "mode": "now",
        "vehicleName": "car",
        "vehicleSoc": 40,
        "effectiveLimitSoc": 80,
        "smartCostLimit": None,
        "plan": None,
        **lp,
    }
    return {
        "version": "0.300.0",
        "loadpoints": [loadpoint],
        "vehicles": {"car": {"capacity": 50, "title": "Private name"}},
        "forecast": {"planner": []},
        "batteryDischargeControl": True,
    }


class _Evcc:
    """Replaces the HTTP fetches; counts calls, fails on demand."""

    def __init__(self, monkeypatch, state: dict | None = None) -> None:
        self.state = state if state is not None else _evcc_state()
        self.calls = 0
        self.fail = False
        monkeypatch.setattr(coordinator_module, "async_fetch_state", self._state)
        monkeypatch.setattr(coordinator_module, "async_fetch_plans", self._plans)

    async def _state(self, hass, url):
        self.calls += 1
        self.url = url
        if self.fail:
            raise EvccError("timeout reading evcc")
        return self.state

    async def _plans(self, hass, url, state):
        return {}


def _coord(hass=None, *, policy=EV_POLICY_EXTERNAL, **extra):
    return _coordinator(
        hass or _hass(),
        **{
            CONF_EV_SOURCE: EV_SOURCE_EVCC,
            CONF_EVCC_URL: "evcc.local:7070/",
            CONF_EV_BATTERY_POLICY: policy,
            CONF_EXTERNAL_CONTROL_ENTITY: "sensor.evcc_battery_mode",
            **extra,
        },
    )


def test_without_an_ev_source_nothing_is_fetched(monkeypatch):
    evcc = _Evcc(monkeypatch)
    data = _run(_coordinator(_hass())._async_update_data())
    assert evcc.calls == 0
    assert data.ev_source is None
    assert all(s.ev_kwh == 0 for s in data.plan.slots)
    assert "evcc" not in data.inputs["ev"]


def test_a_charging_car_lands_in_the_plan_as_blocked_slots(monkeypatch):
    evcc = _Evcc(monkeypatch)
    data = _run(_coord()._async_update_data())
    assert evcc.url == "http://evcc.local:7070"
    assert data.ev_source == EV_SOURCE_EVCC
    # 40 -> 80 % of 50 kWh at 90 % efficiency
    assert data.ev_kwh_total == pytest.approx(0.4 * 50 / 0.9, abs=0.05)
    (session,) = data.ev_sessions
    assert session["source"] == "now"
    blocked = [s for s in data.plan.slots if s.battery_blocked]
    assert blocked
    assert all(s.action == ACTION_AUTO and s.discharge_kwh == 0 for s in blocked)
    assert data.inputs["config"]["ev_no_grid_charge"] is True
    info = data.inputs["ev"]["evcc"]
    assert info["status"] == "ok"
    assert info["version"] == "0.300.0"
    assert "Private name" not in repr(data.inputs)


def test_allow_keeps_the_battery_in_the_session(monkeypatch):
    _Evcc(monkeypatch)
    data = _run(_coord(policy=EV_POLICY_ALLOW)._async_update_data())
    assert data.ev_kwh_total > 0
    assert not any(s.battery_blocked for s in data.plan.slots)
    assert data.inputs["config"]["ev_no_grid_charge"] is False


def test_block_holds_only_above_the_wallbox_threshold(monkeypatch):
    evcc = _Evcc(monkeypatch)
    data = _run(_coord(policy=EV_POLICY_BLOCK)._async_update_data())
    assert any(s.battery_blocked for s in data.plan.slots)

    evcc.state = _evcc_state(chargePower=600)
    data = _run(_coord(policy=EV_POLICY_BLOCK)._async_update_data())
    assert data.ev_kwh_total > 0
    assert not any(s.battery_blocked for s in data.plan.slots)


def test_evcc_without_discharge_control_never_holds(monkeypatch):
    evcc = _Evcc(monkeypatch)
    evcc.state["batteryDischargeControl"] = False
    data = _run(_coord()._async_update_data())
    assert data.ev_kwh_total > 0
    assert not any(s.battery_blocked for s in data.plan.slots)


def test_an_unreachable_evcc_plans_without_sessions(monkeypatch, caplog):
    evcc = _Evcc(monkeypatch)
    evcc.fail = True
    coord = _coord()
    data = _run(coord._async_update_data())
    assert data.valid
    assert data.ev_source == EV_SOURCE_EVCC
    assert data.ev_sessions == []
    assert data.ev_kwh_total == 0
    assert data.inputs["ev"]["evcc"]["status"] == "offline"
    assert "timeout" in data.inputs["ev"]["evcc"]["error"]
    # A hold the plan cannot see must not stop the pilot from grid charging.
    assert data.inputs["config"]["ev_no_grid_charge"] is False

    _run(coord._async_update_data())
    warnings = [r for r in caplog.records if "evcc is not readable" in r.getMessage()]
    assert len(warnings) == 1


def test_a_recent_fetch_is_reused_by_the_next_refresh(monkeypatch):
    evcc = _Evcc(monkeypatch)
    coord = _coord()
    _run(coord._async_update_data())
    _run(coord._async_update_data())
    assert evcc.calls == 1


class _Refreshes:
    def __init__(self, coord) -> None:
        self.count = 0
        coord.hass.async_create_task = self._task

    def _task(self, coro):
        self.count += 1
        coro.close()


def test_the_poll_replans_only_when_the_sessions_change(monkeypatch):
    evcc = _Evcc(monkeypatch)
    coord = _coord()
    coord.data = _run(coord._async_update_data())
    refreshes = _Refreshes(coord)

    _run(coord._async_poll_evcc())
    assert refreshes.count == 0

    evcc.state = _evcc_state(connected=False, charging=False)
    _run(coord._async_poll_evcc())
    assert refreshes.count == 1


def test_the_poll_replans_once_when_evcc_goes_away_with_sessions_planned(monkeypatch):
    evcc = _Evcc(monkeypatch)
    coord = _coord()
    coord.data = _run(coord._async_update_data())
    refreshes = _Refreshes(coord)
    evcc.fail = True
    _run(coord._async_poll_evcc())
    assert refreshes.count == 1
    # The re-plan it asked for, while evcc stays away.
    coord.data = _run(coord._async_update_data())
    _run(coord._async_poll_evcc())
    _run(coord._async_poll_evcc())
    assert refreshes.count == 1

    evcc.fail = False
    _run(coord._async_poll_evcc())
    assert refreshes.count == 2


def test_the_sensors_carry_the_ev_plan(monkeypatch):
    _Evcc(monkeypatch)
    coord = _coord()
    coord.data = _run(coord._async_update_data())

    sensor = object.__new__(ChargePlanSensor)
    sensor.coordinator = coord
    attrs = sensor.extra_state_attributes
    assert attrs["ev_source"] == EV_SOURCE_EVCC
    assert attrs["ev_kwh_total"] == coord.data.ev_kwh_total
    assert attrs["ev_sessions"] == coord.data.ev_sessions
    charging = [s for s in attrs["slots"] if s["ev_kwh"] > 0]
    assert charging
    assert charging[0]["ev_source"] == "now"
    assert charging[0]["battery_blocked"] is True

    config = object.__new__(ConfigSensor)
    config.coordinator = coord
    assert config.extra_state_attributes["ev_source"] == EV_SOURCE_EVCC


def test_the_slots_carry_no_ev_keys_without_an_ev_source():
    coord = _coordinator(_hass())
    coord.data = _run(coord._async_update_data())
    sensor = object.__new__(ChargePlanSensor)
    sensor.coordinator = coord
    attrs = sensor.extra_state_attributes
    assert "ev_source" not in attrs
    assert "ev_kwh" not in attrs["slots"][0]


def test_holding_for_a_car_books_no_pilot_savings():
    """An idle the executor sets for a car is the EV policy, not arbitrage."""
    hass = _FakeHass()
    coord = _coordinator(hass)
    plan = _plan(_plan_slot("auto", 0.20, T0), _plan_slot("auto", 0.40, T0 + timedelta(hours=1)))
    _meters(hass, 0.0, 0.0)
    coord._ev_hold_active = True
    _step(coord, plan, ACTION_IDLE, T0)
    coord._ev_hold_active = False
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=1))
    _meters(hass, 0.0, 1.0)
    _step(coord, plan, ACTION_AUTO, T0 + timedelta(hours=2))
    assert coord.pilot_ledger.savings_eur == 0.0
