"""Support tooling: debug logs and the decision history in the diagnostics dump.

A user report usually arrives as "it charged at 3 am, why?" with a diagnostics
download attached and debug logging not switched on. The dump therefore has to
say on its own what the executor did and what the planner was fed; the debug
log goes one level deeper into why the planner paired the slots it did.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from test_coordinator import _coordinator
from test_coordinator import _FakeHass as _CoordHass
from test_executor import _FakeCoordinator, _FakeHass, _slot
from test_optimizer import BATTERY, CONFIG, make_slots

from smart_battery_pilot.const import ACTION_CHARGE, ACTION_IDLE
from smart_battery_pilot.coordinator import SBPData
from smart_battery_pilot.diagnostics import async_get_config_entry_diagnostics
from smart_battery_pilot.executor import MAX_DECISIONS, PlanExecutor
from smart_battery_pilot.optimizer import Plan, build_plan


def _run(coro):
    return asyncio.run(coro)


# --- executor decision history -----------------------------------------------


def test_dry_run_decision_is_recorded():
    executor = PlanExecutor(_FakeHass(), _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=True))
    _run(executor.async_apply_current())

    [decision] = executor.decisions
    assert decision["planned"] == ACTION_CHARGE
    assert decision["outcome"] == "dry_run"
    assert decision["power_w"] == 4000
    assert decision["slot_start"]


def test_applied_action_is_recorded():
    executor = PlanExecutor(_FakeHass(), _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=False))
    _run(executor.async_apply_current())
    assert executor.decisions[-1]["outcome"] == "applied"
    assert executor.decisions[-1]["planned"] == ACTION_IDLE


def test_repeated_identical_decisions_collapse_into_one_entry():
    """The coordinator re-applies twice an hour; one idle night must not push
    the interesting part of the history out of the buffer."""
    executor = PlanExecutor(_FakeHass(), _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=False))
    for _ in range(5):
        _run(executor.async_apply_current())

    outcomes = [(d["outcome"], d["repeats"]) for d in executor.decisions]
    assert outcomes == [("applied", 1), ("unchanged", 4)]


def test_disabled_pilot_is_recorded():
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False, enabled=False)
    executor = PlanExecutor(_FakeHass(), coord)
    _run(executor.async_apply_current())
    assert executor.decisions[-1]["outcome"] == "disabled"


def test_invalid_plan_records_the_coordinator_error():
    coord = _FakeCoordinator([], valid=False, dry_run=False)
    coord.data = SBPData(plan=Plan(), valid=False, error="soc_unavailable")
    executor = PlanExecutor(_FakeHass(), coord)
    _run(executor.async_apply_current())

    decision = executor.decisions[-1]
    assert decision["outcome"] == "no_live_plan"
    assert decision["detail"] == "soc_unavailable"


def test_failed_script_and_fallback_are_recorded():
    hass = _FakeHass()
    hass.services.fail = True
    executor = PlanExecutor(hass, _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=False))
    _run(executor.async_apply_current())
    assert executor.decisions[-1]["outcome"] == "failed"
    assert executor.decisions[-1]["planned"] == ACTION_CHARGE


def test_decision_history_is_bounded():
    coord = _FakeCoordinator([_slot(ACTION_CHARGE)], dry_run=True)
    executor = PlanExecutor(_FakeHass(), coord)
    for i in range(MAX_DECISIONS + 10):
        # A changing power makes every decision distinct, so none collapse.
        coord.data = SBPData(plan=Plan(slots=[_slot(ACTION_CHARGE, power=1000.0 + i)]), valid=True)
        _run(executor.async_apply_current())
    assert len(executor.decisions) == MAX_DECISIONS


def test_executor_logs_why_it_skips(caplog):
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.executor")
    executor = PlanExecutor(_FakeHass(), _FakeCoordinator([_slot(ACTION_IDLE)], dry_run=False))
    _run(executor.async_apply_current())
    _run(executor.async_apply_current())
    assert "already applied" in caplog.text


# --- optimizer ---------------------------------------------------------------


def test_optimizer_logs_each_pairing(caplog):
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.optimizer")
    prices = [0.10] * 4 + [0.50] * 4
    plan = build_plan(make_slots(prices, demand_kwh=1.0), BATTERY, CONFIG)
    assert any(s.action == ACTION_CHARGE for s in plan.slots)

    assert "pair charge" in caplog.text
    assert "0.1000" in caplog.text and "0.5000" in caplog.text
    assert "Plan result" in caplog.text


def test_optimizer_logs_when_the_spread_is_not_reached(caplog):
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.optimizer")
    prices = [0.30] * 4 + [0.32] * 4
    plan = build_plan(make_slots(prices, demand_kwh=1.0), BATTERY, CONFIG)
    assert all(s.action != ACTION_CHARGE for s in plan.slots)
    assert "spread not reached" in caplog.text


def test_optimizer_is_silent_without_debug(caplog):
    caplog.set_level(logging.INFO, logger="smart_battery_pilot.optimizer")
    build_plan(make_slots([0.10] * 4 + [0.50] * 4), BATTERY, CONFIG)
    assert caplog.text == ""


# --- coordinator ---------------------------------------------------------------


def test_plan_inputs_are_kept_with_the_result():
    hass = _CoordHass()
    coord = _coordinator(hass)
    hass.states.set("sensor.price", 0.30, {"today": [0.20] * 12 + [0.40] * 12})
    hass.states.set("sensor.soc", 55.0)

    result = _run(coord._async_update_data())
    inputs = result.inputs
    assert inputs["soc"] == 55.0
    assert inputs["slots"] == len(result.plan.slots)
    # Past slots are dropped, so which half of the day remains depends on
    # the clock - compare against the plan built from the same inputs.
    prices = [slot.price for slot in result.plan.slots]
    assert inputs["price_min"] == min(prices)
    assert inputs["price_max"] == max(prices)
    assert inputs["battery"]["capacity_kwh"] > 0
    assert "spread_threshold" in inputs["config"]


def test_invalid_plan_warns_once_per_reason(caplog):
    hass = _CoordHass()
    coord = _coordinator(hass)
    hass.states.set("sensor.price", 0.30, {"today": [0.30] * 24})
    coord.data = _run(coord._async_update_data())

    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.coordinator")
    hass.states.set("sensor.soc", "unavailable")
    coord.data = _run(coord._async_update_data())
    coord.data = _run(coord._async_update_data())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "soc_unavailable" in warnings[0].getMessage()


# --- diagnostics -----------------------------------------------------------------


def test_diagnostics_carry_decisions_and_inputs():
    hass = _CoordHass()
    coord = _coordinator(hass)
    hass.states.set("sensor.price", 0.30, {"today": [0.30] * 24})
    hass.states.set("sensor.soc", 50.0)
    coord.data = _run(coord._async_update_data())
    coord.last_update_success = True

    executor = PlanExecutor(hass, coord)
    coord.enabled, coord.dry_run = True, True
    _run(executor.async_apply_current())

    entry = SimpleNamespace(
        data=dict(coord.entry.data),
        options={},
        runtime_data=SimpleNamespace(coordinator=coord, executor=executor),
    )
    dump = _run(async_get_config_entry_diagnostics(hass, entry))
    assert dump["decisions"][-1]["outcome"] == "dry_run"
    assert dump["state"]["inputs"]["soc"] == 50.0
    assert dump["runtime"]["charge_rate_bands"] == []
