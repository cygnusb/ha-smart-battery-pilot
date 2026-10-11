"""Executor side of EV charging: the discharge block and external controllers."""

from __future__ import annotations

import asyncio

from test_executor import _FakeCoordinator, _FakeHass, _slot

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_IDLE,
    EV_POLICY_ALLOW,
    EV_POLICY_BLOCK,
)
from smart_battery_pilot.executor import PlanExecutor


def _run(coro):
    return asyncio.run(coro)


def _services(hass):
    return [service for _, service, _, _ in hass.services.calls]


def _live(action, *, policy=EV_POLICY_BLOCK, power=None):
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(action)], dry_run=False)
    coord.ev_policy_value = policy
    coord.ev_entities = ["sensor.wallbox_power"]
    coord.ev_power = power
    return hass, coord, PlanExecutor(hass, coord)


# --- block policy ------------------------------------------------------------------


def test_a_charging_ev_turns_auto_into_a_discharge_block():
    hass, coord, executor = _live(ACTION_AUTO, power=3000.0)
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_idle"]
    assert executor.decisions[-1]["outcome"] == "ev_block"
    assert coord.last_applied == ACTION_IDLE


def test_below_the_threshold_auto_stays_auto():
    hass, _coord, executor = _live(ACTION_AUTO, power=800.0)
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto"]
    assert executor.decisions[-1]["outcome"] == "applied"


def test_the_block_holds_until_the_power_falls_below_half_the_threshold():
    """evcc ramps and pauses between phases - the inverter must not follow."""
    hass, coord, executor = _live(ACTION_AUTO, power=3000.0)
    _run(executor.async_apply_current())
    coord.ev_power = 600.0
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_idle"]
    assert executor.decisions[-1]["outcome"] == "ev_block"

    coord.ev_power = 400.0
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_idle", "sbp_auto"]


def test_a_planned_grid_charge_is_not_blocked():
    hass, _coord, executor = _live(ACTION_CHARGE, power=11000.0)
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_charge"]


def test_the_allow_policy_ignores_the_wallbox():
    hass, _coord, executor = _live(ACTION_AUTO, policy=EV_POLICY_ALLOW, power=11000.0)
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto"]


def test_no_wallbox_reading_blocks_nothing():
    hass, _coord, executor = _live(ACTION_AUTO, power=None)
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto"]


def test_the_reserve_block_keeps_its_own_label():
    hass, coord, executor = _live(ACTION_AUTO, power=3000.0)
    coord.block = True
    coord.reserve = 30
    coord.soc = 25.0
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_idle"]
    assert executor.decisions[-1]["outcome"] == "reserve_block"


# --- external control --------------------------------------------------------------


def test_an_external_controller_gets_no_competing_script_call():
    hass, coord, executor = _live(ACTION_CHARGE, policy=EV_POLICY_ALLOW)
    coord.external = True
    _run(executor.async_apply_current())
    assert hass.services.calls == []
    assert executor.decisions[-1]["outcome"] == "external_control"


def test_the_plan_is_re_sent_once_the_controller_lets_go():
    """evcc's hold leaves the inverter in its own mode; `last_applied` would
    claim auto and skip the call for good."""
    hass, coord, executor = _live(ACTION_AUTO, policy=EV_POLICY_ALLOW)
    coord.last_applied = ACTION_AUTO
    coord.external = True
    _run(executor.async_apply_current())
    assert hass.services.calls == []

    coord.external = False
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto"]
    assert executor.decisions[-1]["outcome"] == "applied"

    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto"]
    assert executor.decisions[-1]["outcome"] == "unchanged"


def test_a_failed_re_send_is_retried():
    hass, coord, executor = _live(ACTION_AUTO, policy=EV_POLICY_ALLOW)
    coord.last_applied = ACTION_AUTO
    coord.external = True
    _run(executor.async_apply_current())
    coord.external = False
    hass.services.fail = True
    _run(executor.async_apply_current())
    hass.services.fail = False
    _run(executor.async_apply_current())
    assert _services(hass) == ["sbp_auto", "sbp_auto"]
    assert executor.decisions[-1]["outcome"] == "applied"


def test_dry_run_reports_dry_run_even_under_external_control():
    hass = _FakeHass()
    coord = _FakeCoordinator([_slot(ACTION_AUTO)], dry_run=True)
    coord.external = True
    executor = PlanExecutor(hass, coord)
    _run(executor.async_apply_current())
    assert executor.decisions[-1]["outcome"] == "dry_run"


# --- listeners ---------------------------------------------------------------------


def test_a_change_of_external_control_queues_a_run():
    _hass, coord, executor = _live(ACTION_AUTO, policy=EV_POLICY_ALLOW)
    coord.external = True
    executor._handle_ev_event(None)
    assert executor._apply_queued


def test_wallbox_power_crossing_the_threshold_queues_a_run():
    _hass, coord, executor = _live(ACTION_AUTO, power=200.0)
    executor._handle_ev_event(None)
    assert not executor._apply_queued
    coord.ev_power = 4000.0
    executor._handle_ev_event(None)
    assert executor._apply_queued


def test_wallbox_ticks_outside_an_auto_slot_do_nothing():
    _hass, _coord, executor = _live(ACTION_CHARGE, power=4000.0)
    executor._handle_ev_event(None)
    assert not executor._apply_queued
