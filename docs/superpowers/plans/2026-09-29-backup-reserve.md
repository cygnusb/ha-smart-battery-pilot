# Backup Reserve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a configurable backup reserve that arbitrage never spends, that is refilled by a deadline, that reaches the inverter as the script variable `reserve_soc`, and that the pilot can optionally enforce itself by blocking discharge.

**Architecture:**
- The optimizer plans above a raised floor. A negative starting level is a deficit, and a refill pass run before arbitrage covers it. The refill is excluded from the savings figure but included in the do-nothing baseline.
- The coordinator computes the effective reserve (the fixed value or the entity, clamped) and feeds it to the planner. It re-plans when the entity changes and persists the reserve it last sent.
- The executor sends `reserve_soc` with every script call and re-calls the script when the reserve changes. As an option it replaces `auto` with `idle` at the reserve, with hysteresis, reacting to live SOC changes.

**Tech Stack:** Python 3.12+, Home Assistant custom integration, pytest against `tests/stubs`, pytest-homeassistant-custom-component in `.venv-ha` for `tests_ha/`, ruff 0.16.5.

**Spec:** `docs/superpowers/specs/2026-09-29-backup-reserve-design.md`

## Global Constraints

- Off by default (`backup_reserve = 0`). With the reserve inactive, plans must be identical to today's.
- Effective reserve = `clamp(entity value if readable else fixed value, min_soc, max_soc)`. Active only if `> min_soc`.
- Every script call carries `reserve_soc` (int %): the effective reserve, or `min_soc` when inactive. `charge`/`export` also carry `power_w`.
- Refill deadline default 12 h (range 1–48). Refill = PV first, then the cheapest grid slots in slots `0..k`. Warning `reserve_refill_incomplete` if short.
- Refill energy and cost go on `Plan.reserve_refill_kwh` / `Plan.reserve_refill_cost_eur`. Excluded from `estimated_savings_eur`; included in the baseline simulation.
- Block: only replaces `auto`; engage at `soc <= reserve`, release at `soc >= reserve + 2`. Outcome `reserve_block`. Re-call on a reserve change: outcome `reserve_updated`.
- Options menu key `reserve`, placed after `derating`, before `apply`. Translations in all 10 languages.
- The optimizer stays free of Home Assistant imports.
- Version 0.9.0.
- Tests: `.venv/bin/python -m pytest tests -q`; `.venv-ha/bin/pytest tests_ha/ -o asyncio_mode=auto -q -p no:cacheprovider`; lint `.venv/bin/ruff check . && .venv/bin/ruff format --check .`.

## Review Focus

1. The SOC starts **above** the reserve and PV surplus is large: the refill pass must not run, and arbitrage must not discharge into the reserve band. Pinned in Task 1.
2. The reserve entity reads a value **below `min_soc` or above `max_soc`**: it must be clamped, not crash, and not send a nonsense `reserve_soc`. Pinned in Task 3.
3. A **restart** with `last_reserve_sent` persisted and the reserve unchanged must not re-call the script; a changed one must. Pinned in Tasks 3 and 4.
4. **Dry-run** must never send a reserve update. Pinned in Task 4.
5. The **cold-derated** charge cap applies to refill slots, and the full-power rule holds there too. Pinned in Task 1.

---

### Task 1: Optimizer — reserve floor and refill pass

**Files:** modify `custom_components/smart_battery_pilot/optimizer.py`; create `tests/test_optimizer_reserve.py`.

**Interfaces — Produces:**
- `BatteryState.reserve_soc: float | None = None`
- `OptimizerConfig.reserve_refill_hours: float = 12.0`
- `Plan.reserve_refill_kwh: float = 0.0`, `Plan.reserve_refill_cost_eur: float = 0.0`
- warning string `"reserve_refill_incomplete"`

- [ ] **Step 1: failing tests** (`tests/test_optimizer_reserve.py`):

```python
"""Backup reserve in the planner: a floor arbitrage never spends, refilled by a deadline."""

from __future__ import annotations

from dataclasses import replace

from smart_battery_pilot.const import ACTION_CHARGE, ACTION_EXPORT, DISCHARGE_MODE_EXPORT
from smart_battery_pilot.optimizer import build_plan
from test_optimizer import BATTERY, CONFIG, make_slots

PEAKY = [0.10] * 6 + [0.50] * 4 + [0.10] * 6 + [0.50] * 4


def _charges(plan):
    return [i for i, s in enumerate(plan.slots) if s.action == ACTION_CHARGE]


def test_an_inactive_reserve_is_todays_plan():
    slots = make_slots(PEAKY, demand_kwh=1.5)
    today = build_plan(slots, replace(BATTERY, soc=60.0), CONFIG)
    for reserve in (None, 0.0, BATTERY.min_soc):
        assert build_plan(slots, replace(BATTERY, soc=60.0, reserve_soc=reserve), CONFIG) == today


def test_arbitrage_never_plans_below_the_reserve():
    plan = build_plan(
        make_slots(PEAKY, demand_kwh=1.5), replace(BATTERY, soc=60.0, reserve_soc=40.0), CONFIG
    )
    assert min(s.soc_forecast for s in plan.slots) >= 40.0 - 0.05


def test_export_never_plans_below_the_reserve():
    config = replace(CONFIG, discharge_mode=DISCHARGE_MODE_EXPORT, feed_in_tariff=0.0)
    plan = build_plan(
        make_slots(PEAKY, demand_kwh=0.2), replace(BATTERY, soc=90.0, reserve_soc=50.0), config
    )
    assert any(s.action == ACTION_EXPORT for s in plan.slots)
    assert min(s.soc_forecast for s in plan.slots) >= 50.0 - 0.05


def test_a_deficit_is_refilled_in_the_cheapest_slots_before_the_deadline():
    prices = [0.30, 0.12, 0.25, 0.11, 0.40, 0.40] + [0.05] * 6  # cheapest slots after 6 h
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)  # no arbitrage
    plan = build_plan(make_slots(prices, 0.0), replace(BATTERY, soc=20.0, reserve_soc=40.0), config)
    # 20 % of 12.8 kWh fits one slot: the cheapest before the deadline (slot 3 @ 0.11).
    assert _charges(plan) == [3]
    assert plan.slots[5].soc_forecast >= 40.0 - 0.05
    assert plan.reserve_refill_kwh > 0
    assert "reserve_refill_incomplete" not in plan.warnings


def test_the_soc_forecast_shows_the_real_soc_below_the_reserve():
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)
    plan = build_plan(
        make_slots([0.30] * 3 + [0.10] * 9, 0.0), replace(BATTERY, soc=20.0, reserve_soc=40.0), config
    )
    assert plan.slots[0].soc_forecast == 20.0


def test_pv_alone_refills_without_grid_charge():
    slots = make_slots([0.20] * 12, 0.0)
    slots = [replace(s, net_demand_kwh=-3.0, pv_kwh=3.0) for s in slots]
    plan = build_plan(slots, replace(BATTERY, soc=20.0, reserve_soc=40.0), replace(CONFIG, spread_threshold=5.0))
    assert _charges(plan) == []
    assert plan.reserve_refill_kwh == 0.0


def test_a_too_short_deadline_refills_what_fits_and_warns():
    config = replace(CONFIG, reserve_refill_hours=1.0, spread_threshold=5.0)
    battery = replace(BATTERY, soc=10.0, reserve_soc=90.0)  # needs ~10 kWh, 1 h at 6 kW cannot
    plan = build_plan(make_slots([0.20] * 12, 0.0), battery, config)
    assert _charges(plan)[:1] == [0]
    assert "reserve_refill_incomplete" in plan.warnings


def test_refill_cost_is_not_counted_against_the_savings():
    config = replace(CONFIG, reserve_refill_hours=6.0, spread_threshold=5.0)  # no arbitrage
    plan = build_plan(make_slots([0.30] * 12, 0.5), replace(BATTERY, soc=20.0, reserve_soc=40.0), config)
    assert plan.reserve_refill_cost_eur > 0
    assert plan.estimated_savings_eur >= 0.0
    assert "plan_worse_than_baseline" not in plan.warnings
    assert _charges(plan)  # the refill survived the baseline check


def test_refill_slots_obey_the_cold_derating_cap():
    config = replace(CONFIG, reserve_refill_hours=12.0, spread_threshold=5.0)
    battery = replace(BATTERY, soc=10.0, reserve_soc=50.0, charge_factor=0.25)
    plan = build_plan(make_slots([0.20] * 12, 0.0), battery, config)
    cap_kwh = BATTERY.max_charge_power_w / 1000.0 * 0.25 * (BATTERY.efficiency / 100.0) ** 0.5
    previous = battery.soc
    for slot in plan.slots:
        stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
        assert stored <= cap_kwh + 0.013
        if slot.action == ACTION_CHARGE and stored >= cap_kwh - 0.013:
            assert slot.power_w == BATTERY.max_charge_power_w
        previous = slot.soc_forecast
```

- [ ] **Step 2:** run, expect `TypeError ... unexpected keyword argument 'reserve_soc'`.
- [ ] **Step 3: implement.**
  - Add the fields (dataclasses as in Interfaces).
  - In `build_plan`:
    - `floor = reserve if reserve_active else min_soc`, where `reserve_active = reserve_soc is not None and reserve_soc > min_soc`.
    - `e_max = max(0, (max_soc - floor) × cap)`.
    - `e_init = (soc - floor) × cap`, clamped to `≤ e_max` and to `≥ 0` **only when the reserve is inactive**; with it active it may be negative.
  - **Refill pass** after `pv_surplus_stored`, when `e_init < -1e-9`:
    ```python
    horizon_end = slots[0].price_slot.start.timestamp() + config.reserve_refill_hours * 3600
    k = max((i for i in range(n) if slots[i].price_slot.start.timestamp() < horizon_end), default=0)
    levels, _ = timeline()
    missing = max(0.0, -levels[k])
    for i in sorted(range(k + 1), key=lambda i: prices[i]):
        if missing <= 1e-9:
            break
        q = min(missing, charge_cap[i] - pv_surplus_stored[i] - charge_stored[i])
        if q > 1e-9:
            charge_stored[i] += q
            refill_stored[i] += q
            missing -= q
    if missing > 1e-9:
        warnings.append("reserve_refill_incomplete")
    ```
    Debug-log the deficit, `k`, each refill slot (label, price, kWh) and "PV alone refills" when `missing` was 0.
  - Initialise `warnings` before the refill pass (move the list creation up). `plan = Plan(warnings=list(warnings))` must include it.
  - `soc_forecast` uses `floor` instead of `battery.min_soc` (both in `build_plan` and in `_auto_plan`; pass `floor` to `_auto_plan`).
  - `_simulate_self_consumption`:
    - takes `refill_stored` and adds it to the energy each slot,
    - clamps the withdrawal to `max(0.0, energy)`, so a negative level delivers nothing.
  - Accounting:
    - `refill_grid = sum(refill_stored[i] / eta_one_way)`,
    - `refill_cost = sum(refill_stored[i] / eta_one_way * prices[i])`,
    - `cost_plan -= refill_cost` (the charge cost loop still includes it; subtract after the loop),
    - `plan.reserve_refill_kwh = round(refill_grid, 3)`, `plan.reserve_refill_cost_eur = round(refill_cost, 2)`, also on the `_auto_plan` fallback.
- [ ] **Step 4:** full suite green, ruff clean. Rulings for any test expectation that turns out to be wrong go in the ledger.
- [ ] **Step 5:** commit `Optimizer: plan above a backup reserve and refill it by a deadline`.

### Task 2: Options section "Backup reserve"

**Files:** `const.py`, `config_flow.py`, 10 × `translations/*.json`, `tests/test_config_flow.py`, `tests/test_translations.py`.

**Interfaces — Produces:**
- `CONF_BACKUP_RESERVE = "backup_reserve"`, `CONF_BACKUP_RESERVE_ENTITY = "backup_reserve_entity"`, `CONF_RESERVE_REFILL_HOURS = "reserve_refill_hours"`, `CONF_RESERVE_BLOCK_DISCHARGE = "reserve_block_discharge"`
- `DEFAULT_BACKUP_RESERVE = 0`, `DEFAULT_RESERVE_REFILL_HOURS = 12`, `DEFAULT_RESERVE_BLOCK_DISCHARGE = False`
- `schema_reserve(d) -> vol.Schema`, `STEP_FIELDS["reserve"]`, `SBPOptionsFlow.async_step_reserve`

- [ ] **Step 1: failing tests.** Append to `tests/test_config_flow.py`:

```python
# --- backup reserve -------------------------------------------------------------


def test_the_reserve_section_sits_between_derating_and_apply():
    menu = _run(_options_flow(_FakeHass(), _entry()).async_step_init())
    options = menu["menu_options"]
    assert options.index("reserve") == options.index("derating") + 1
    assert options[-1] == "apply"


def test_the_reserve_defaults_are_off():
    defaults = {
        str(m): m.default()
        for m in cf.schema_reserve({}).schema
        if callable(getattr(m, "default", None))
    }
    assert defaults[CONF_BACKUP_RESERVE] == 0
    assert defaults[CONF_RESERVE_REFILL_HOURS] == 12
    assert defaults[CONF_RESERVE_BLOCK_DISCHARGE] is False


def test_a_reserve_section_is_saved_on_apply():
    flow = _options_flow(_FakeHass(), _entry())
    result = _run(
        flow.async_step_reserve(
            {
                CONF_BACKUP_RESERVE: 30,
                CONF_BACKUP_RESERVE_ENTITY: "input_number.reserve",
                CONF_RESERVE_REFILL_HOURS: 8,
                CONF_RESERVE_BLOCK_DISCHARGE: True,
            }
        )
    )
    assert result["type"] == "menu"
    data = _run(flow.async_step_apply())["data"]
    assert data[CONF_BACKUP_RESERVE] == 30
    assert data[CONF_BACKUP_RESERVE_ENTITY] == "input_number.reserve"
    assert data[CONF_RESERVE_BLOCK_DISCHARGE] is True
```

  Add the four constants to the test's const import. Add `"reserve": config_flow.schema_reserve` to `OPTIONS_STEPS` in `tests/test_translations.py`.

- [ ] **Step 2:** run and expect the ImportError.
- [ ] **Step 3: implement.**
  - Add the constants.
  - `schema_reserve`:
    - `Required(CONF_BACKUP_RESERVE)` → `_percent_slider()`,
    - `Optional(CONF_BACKUP_RESERVE_ENTITY, description=_sugg(...))` → `_ENTITY`,
    - `Required(CONF_RESERVE_REFILL_HOURS)` → NumberSelector box, 1–48, step 1, unit `h`,
    - `Required(CONF_RESERVE_BLOCK_DISCHARGE)` → BooleanSelector.
  - `STEP_FIELDS["reserve"]` with the four keys.
  - Add `"reserve"` to the menu after `"derating"`.
  - `async_step_reserve` saves unconditionally, like `pv`.
- [ ] **Step 4: translations.** Add to every language: `options.step.init.menu_options.reserve`, `options.step.reserve` (title, 4 labels, 4 descriptions), inserted before `apply` as in the derating merge. The texts are in the one-off merge script in the task log.
- [ ] **Step 5:** full suite green; commit `Options: backup reserve section`.

### Task 3: Coordinator — effective reserve, inputs, persistence, sensors

**Files:** `coordinator.py`, `sensor.py`, `tests/test_coordinator_reserve.py`.

**Interfaces — Produces:**
- `SBPCoordinator.reserve_state() -> tuple[float | None, str, float | None]`: (active reserve or None, source `fixed`/`entity`/`off`, raw entity value)
- `SBPCoordinator.reserve_for_scripts() -> int`: the effective reserve, or `min_soc`, rounded
- `SBPCoordinator.reserve_block_enabled() -> bool`
- `SBPCoordinator.last_reserve_sent: int | None`, persisted in the store under `last_reserve_sent`
- `SBPData.inputs["reserve"] = {"soc", "source", "entity_value", "refill_hours", "block"}`
- Plan sensor attributes `reserve_refill_kwh`, `reserve_refill_cost_eur`; config sensor attributes `reserve_soc`, `reserve_source`, `reserve_block_discharge`.

- [ ] **Step 1: failing tests** (`tests/test_coordinator_reserve.py`):
  - fixed 30 → `reserve_state() == (30.0, "fixed", None)`, `reserve_for_scripts() == 30`;
  - fixed 0 → `(None, "off", None)`, `reserve_for_scripts() == 10` (min_soc default);
  - entity reads 50 → `(50.0, "entity", 50.0)`;
  - entity reads 5 (below min_soc 10) → `(None, "off", 5.0)`; entity reads 120 → clamped to max_soc 95;
  - entity unavailable with fixed 30 → `(30.0, "fixed", None)`, and one warning across two calls;
  - `_async_update_data` passes `reserve_soc` into `inputs["battery"]` and fills `inputs["reserve"]`; with the reserve inactive, `inputs["battery"]["reserve_soc"] is None`;
  - `last_reserve_sent` survives `async_persist` + `async_setup` on a new coordinator;
  - config sensor attributes (built with `object.__new__(ConfigSensor)` as in `test_coordinator_derating.py`).
- [ ] **Step 2:** run, expect `AttributeError`.
- [ ] **Step 3: implement.**
  - `reserve_state`:
    - read the fixed value from `conf(CONF_BACKUP_RESERVE, 0)` and the entity through `_read_float_state`,
    - once-per-reason warning when the entity is configured but unreadable,
    - clamp to `[min_soc, max_soc]`; active if `> min_soc`.
  - Wire it into `BatteryState(reserve_soc=...)` and `OptimizerConfig(reserve_refill_hours=...)`.
  - Add the reserve to the "Planning inputs" debug line.
  - `inputs["reserve"]`.
  - In `async_setup`, subscribe to the reserve entity (if configured) with a callback that requests a refresh, just like the price entity. Unsubscribe in `async_shutdown`.
  - `last_reserve_sent` as a plain attribute: restored in `async_setup`, written in `_store_payload`.
  - Sensor attributes.
  - Surface the warning `reserve_refill_incomplete` as a log warning once per plan (like `export_spread_unreachable`).
- [ ] **Step 4:** green; commit `Coordinator: effective backup reserve, inputs and persistence`.

### Task 4: Executor — reserve_soc, re-call on change, discharge block

**Files:** `executor.py`, `tests/test_executor.py` (the fake and the payload expectations), `tests/test_executor_restart.py` (the fake), `tests/test_executor_reserve.py`.

**Interfaces — Consumes:** `reserve_for_scripts()`, `reserve_state()`, `reserve_block_enabled()`, `last_reserve_sent` (Task 3).

- [ ] **Step 1: failing tests** (`tests/test_executor_reserve.py`, built on `test_executor`'s fakes; the fake gets `reserve=10`, `block=False`, `soc=None`, `last_reserve_sent=None` and the methods returning them):
  - payload of `auto`/`idle` = `{"reserve_soc": 10}`; `charge` = `{"power_w": 4000, "reserve_soc": 10}`;
  - an idle slot applied, then the reserve 10 → 30 with the slot unchanged: the script is called again with `reserve_soc` 30, outcome `reserve_updated`, and `last_reserve_sent == 30`;
  - the reserve unchanged: no second call (outcome `unchanged`);
  - `last_reserve_sent` already equals the reserve after a "restart" (new executor, `last_applied = idle`): no call;
  - dry-run with a reserve change: no call;
  - a failed script leaves `last_reserve_sent` unchanged;
  - block on, reserve 30, soc 30, an auto slot → the `idle` script is called, outcome `reserve_block`; soc 31 → still idle (hysteresis); soc 32 → `auto` script;
  - block on, a charge slot at soc 20 → charge (not replaced);
  - block off, soc 20, an auto slot → auto.
  - SOC listener: `executor._handle_soc_change(soc)` queues an apply only when the block decision flips (count `hass.async_create_task` calls).
- [ ] **Step 2:** run and expect failures.
- [ ] **Step 3: implement.**
  - `_call_script` adds `"reserve_soc": self.coordinator.reserve_for_scripts()` to every payload. On success, set `coordinator.last_reserve_sent`.
  - In `_apply_locked`, after the dry-run branch:
    ```python
    if action == ACTION_AUTO and self._reserve_block_wanted():
        action = ACTION_IDLE
        blocked = True
    ```
    The unchanged branch: if `action == last_applied` and the action is not charge/export:
    - if `coordinator.reserve_for_scripts() != coordinator.last_reserve_sent`, call the script and record `reserve_updated`,
    - else record `unchanged` (or `reserve_block` when blocked).
    A successful apply while blocked records `reserve_block` instead of `applied`.
  - `_reserve_block_wanted()` holds the hysteresis state in `self._reserve_blocking`. It reads the live SOC via `coordinator.live_soc()` (Task 3 adds a one-line helper wrapping `_read_float_state(conf(CONF_SOC_ENTITY))`; add it there if it is missing).
  - SOC subscription: in `async_start`, `async_track_state_change_event(hass, [soc_entity], self._handle_soc_event)` only when the block is enabled. Unsubscribe in `async_stop`. The callback computes the would-be block state and calls `_queue_apply()` only on a flip.
  - Persist after a reserve change: `await coordinator.async_persist()` inside `_remember_reserve`.
  - Update the existing payload assertions in `tests/test_executor.py` to include `reserve_soc`, and extend both fakes.
- [ ] **Step 4:** green; commit `Executor: send reserve_soc, re-apply on reserve change, optional discharge block`.

### Task 5: Docs, examples, tests_ha, version

- [ ] `tests_ha/test_smoke.py`:
  - the options step `reserve` renders, serialises and saves (like the derating test),
  - setup with `backup_reserve: 30` gives `inputs["reserve"]["soc"] == 30`,
  - a real script receives `reserve_soc`: set up the `script` component with `sbp_auto` firing an event carrying `{{ reserve_soc }}`, call `executor._call_script("auto", 0.0)`, and assert the event data.
- [ ] Docs:
  - `docs/configuration.md`: the section, the effective reserve, and "must live in the inverter for a real outage",
  - `docs/optimizer.md`: the floor and the refill pass,
  - `docs/examples/byd-fronius-gen24.md`: `auto`/`idle` write `{{ ((reserve_soc | default(5)) * 100) | int }}`, `charge` keeps 9900,
  - `README.md`: one line.
- [ ] `manifest.json` 0.9.0.
- [ ] Full unit + tests_ha + lint; commit `Document the backup reserve, smoke-test it, version 0.9.0`.
