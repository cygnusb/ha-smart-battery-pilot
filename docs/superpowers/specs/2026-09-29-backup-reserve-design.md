# Backup reserve — design

Status: approved in chat · 2026-09-29

## Goal

Keep enough energy in the battery to ride out a grid outage, and keep that
reserve even while the pilot does price arbitrage.

Today `min_soc` is a **planning assumption only**. The optimizer does not
count on energy below it, but nothing on the hardware enforces it. In `auto`
the inverter discharges down to its *own* reserve: the Fronius GEN24 example
scripts write `MinRsvPct` = 5 %. A user who sets `min_soc = 30 %` therefore
still ends up at 5 %. The planner also never charges from the grid unless the
price spread pays for it, so once the SOC has dropped below a wanted reserve,
nothing brings it back.

### Success criteria

1. A configured reserve is never planned away. No discharge and no export
   slot takes the SOC below it.
2. If the SOC is below the reserve, it is brought back up by a deadline:
   first from forecast PV surplus, the rest from the cheapest grid slots
   inside the deadline, whether or not that pays off.
3. The reserve reaches the inverter as a script variable, so an inverter that
   has its own reserve setting keeps it even when Home Assistant is down.
4. For inverters without such a setting, an optional fallback has the pilot
   itself block discharging at the reserve.
5. The reserve can be changed at runtime through an entity, for example by a
   storm-warning automation, and that change reaches the inverter without
   waiting for an action change.
6. With the feature off (reserve at or below `min_soc`), plans and script
   calls are identical to today, apart from the extra `reserve_soc` variable.

### Non-goals

* No built-in weather or storm logic. Automations drive the reserve entity.
* No automatic cold-weather raise of the reserve. That can come later.
* No reserve that varies over the horizon. The current effective reserve
  applies to every slot.
* No change to how `min_soc` behaves.

## Configuration

A new options-menu section **Backup reserve** (`reserve`), next to
cold-weather charging. It is options-only; the initial setup is unchanged.

| Key | Selector | Default | Meaning |
|---|---|---|---|
| `backup_reserve` | slider 0–100 %, step 5 | `0` | Fixed reserve SOC. |
| `backup_reserve_entity` | entity (`input_number`, `number`, `sensor`) | – | Optional. While it reads a number, it replaces the fixed value. |
| `reserve_refill_hours` | number 1–48 h, step 1 | `12` | Deadline for refilling a reserve the SOC is below. |
| `reserve_block_discharge` | boolean | `false` | Fallback: the pilot blocks discharging at the reserve. |

**Effective reserve** = `clamp(entity value if readable else fixed value,
min_soc, max_soc)`. It is *active* only when it is above `min_soc`. The source
is reported as `entity`, `fixed` or `off`. An entity that is unavailable, or
reads something that is not a number, falls back to the fixed value. The
first time that happens, a warning is logged (once-per-reason pattern).

Validation in the flow: none beyond the selector ranges. A fixed value at or
below `min_soc` is allowed and simply means "off".

## Planner (`optimizer.py`)

`BatteryState` gains `reserve_soc: float | None = None` (None or ≤ `min_soc`
means inactive). `OptimizerConfig` gains `reserve_refill_hours: float = 12.0`.

**Coordinates.** The optimizer keeps working in stored energy above a floor.
With an active reserve, that floor is the reserve instead of `min_soc`:

* `e_max = (max_soc − reserve) × capacity`
* `e_init = (soc − reserve) × capacity`, which **may be negative**. The
  negative part is the deficit.
* `soc_forecast = reserve + level / capacity`, so the forecast shows the real
  SOC, including below the reserve.

Arbitrage only ever withdraws positive stored energy. A negative level means
nothing may be withdrawn, and `withdrawable()` is clamped at 0 already.

**Refill pass**, run before arbitrage and only when `e_init < 0`:

1. Deadline slot `k` = the last slot that starts before
   `now + reserve_refill_hours`, clamped to the horizon.
2. Simulate the timeline with PV surplus only. If the level reaches ≥ 0 by
   the end of slot `k`, PV alone refills the reserve and no grid charge is
   added.
3. Otherwise, take the slots `0..k` in ascending price order. In each, add
   grid charge up to its free charge cap (after PV and after cold derating)
   until the simulated level at the end of slot `k` reaches 0. Assignments
   go through `charge_stored`, so the rest of the planner sees them as charge
   slots.
4. If the deficit cannot be covered by slot `k`, charge what fits and add the
   warning `reserve_refill_incomplete`.

The refill's grid energy and cost are recorded on the plan as
`reserve_refill_kwh` and `reserve_refill_cost_eur`. They are **excluded from
`estimated_savings_eur` and from the never-worse-than-baseline check**. The
refill is mandatory, not an arbitrage choice, and counting it would make that
check throw the refill away as "worse than doing nothing". The do-nothing
baseline is simulated on the same reserve floor, so both sides compare
arbitrage against arbitrage.

**Requested power.** Refill slots are ordinary `charge` slots. The
cold-derating rule applies unchanged: a slot filled to its derated cap asks
for full power, otherwise it asks for the planned power.

With the reserve inactive, the planner runs exactly as today (criterion 6).
A test pins identical plans.

## Executor

**Script variable.** Every script call carries `reserve_soc` as a whole
percent: the effective reserve, or `min_soc` when the reserve is inactive.
`charge` and `export` additionally carry `power_w`, as today. HA script
services accept arbitrary variables, so scripts that ignore `reserve_soc`
keep working.

**Reserve change.** The executor remembers the reserve it last sent. If the
effective reserve differs at an apply, the current action's script is called
again even when the action is unchanged. The decision history records this
as outcome `reserve_updated`. The last sent reserve is persisted next to
`last_applied`, so a restart does not re-send needlessly, and neither does it
miss a change made while HA was down.

**Discharge block (optional fallback).** With `reserve_block_discharge` on:

* If the live SOC is at or below the reserve and the planned action is `auto`,
  the executor applies `idle` instead. Outcome: `reserve_block`.
* The block holds until the SOC is at least reserve + 2 % (hysteresis).
* `charge`, `idle` and `export` slots are not touched. Export never plans
  below the reserve anyway.
* The coordinator subscribes to the SOC entity and, when the SOC crosses the
  reserve or the release threshold, asks the executor to re-apply. The block
  must not wait up to 30 minutes for the next refresh.

**Reserve entity changes.** The coordinator also subscribes to the reserve
entity. A change triggers a re-plan (the floor moved), and the executor
re-applies afterwards.

## Visibility

* **Configuration sensor:** `reserve_soc`, `reserve_source`
  (`fixed`/`entity`/`off`), `reserve_block_discharge`.
* **Plan sensor attributes:** `reserve_refill_kwh`, `reserve_refill_cost_eur`.
* **`SBPData.inputs["reserve"]`:** `{soc, source, entity_value,
  refill_hours, block}`, which appears in diagnostics.
* **Debug log:**
  * the "Planning inputs" line carries the reserve and its source,
  * the optimizer logs the refill pass (deficit, deadline slot, each refill
    slot with price and kWh, and whether PV alone suffices),
  * the executor logs `reserve_updated` and `reserve_block`.

## Documentation

* `docs/configuration.md`: the section, the fields, the effective-reserve
  rule. Also a clear note: **for a real outage the reserve must live in the
  inverter**. The pilot can only enforce it while HA runs.
* `docs/examples/byd-fronius-gen24.md`: `sbp_auto_mode` and
  `sbp_block_discharge` write `40350 = reserve_soc × 100` (default 500 when
  the variable is missing). `sbp_charge` keeps 9900 on purpose.
* `docs/optimizer.md`: the reserve floor and the refill pass.

## Error handling

| Situation | Behaviour |
|---|---|
| Reserve inactive | identical plans; `reserve_soc` = `min_soc` in script calls |
| Entity unavailable or non-numeric | fixed value, warn once, debug afterwards |
| Entity value outside `min_soc..max_soc` | clamped; debug log |
| Deficit larger than the deadline allows | charge what fits, warning `reserve_refill_incomplete` |
| SOC unavailable | the plan is invalid as today; the block is not evaluated |
| Script call with the new reserve fails | same failure handling as any script; retried at the next apply because the sent reserve is only remembered on success |

## Testing

* **Optimizer:**
  * reserve inactive → identical plan,
  * no discharge or export below the reserve,
  * refill in the cheapest slots inside the deadline,
  * PV alone refills → no grid charge,
  * deadline too short → partial refill plus warning,
  * refill cost excluded from savings and from the baseline check,
  * `soc_forecast` shows the real SOC below the reserve,
  * interaction with cold derating (refill slots obey the power rule).
* **Coordinator:**
  * effective reserve from the fixed value, the entity, clamping, and
    fallback with a single warning,
  * `inputs["reserve"]`,
  * the SOC-entity and reserve-entity subscriptions trigger what they should.
* **Executor:**
  * `reserve_soc` in every payload,
  * re-call on a reserve change with the action unchanged,
  * the persisted last-sent reserve,
  * block engage/release with hysteresis,
  * the block only replaces `auto`.
* **Config flow:** the section, defaults, menu order; translations in all 10
  languages.
* **tests_ha:** the options step renders, serialises and saves, and setup
  with the reserve on works. A real script receives `reserve_soc`.

## Rollout

One PR, version 0.9.0 (new config keys, a new script variable). The feature
is off by default. The release notes tell users to adapt their `auto`/`idle`
scripts if they want the inverter to hold the reserve.
