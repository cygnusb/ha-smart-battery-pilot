# Consistency checks

## Why

The plan rests on assumptions the pilot never verifies: a script that returned
really changed the battery's mode, an auto slot really charges from surplus, the
SOC sensor is alive. When one fails, nothing errors. The plan and the battery
drift apart, and the first symptom is a bad bill (#58).

## What

Eleven read-only checks that raise a repair under Settings > Repairs. They never
change a plan or a script call.

| Check | Fires when | Needs |
|---|---|---|
| `script_failing` | one script failed 3 times in a row | nothing |
| `plan_invalid` | no valid plan for 12 h | nothing |
| `external_control_stuck` | external control active for 24 h | external control entity |
| `soc_frozen` | SOC unchanged for 2 h while 10 % of capacity moved | meters, or forced slots |
| `pv_today_is_remaining` | "today" forecast falls to under 15 % of its daily high by sunset, 3 days | daily PV sensors, no hourly entries |
| `capacity_mismatch` | charged energy implies a capacity over 25 % off, 3 windows of 40 SOC points | charge and discharge meters |
| `charge_ineffective` | under 50 % of the requested energy arrives, 3 days | SOC or meter |
| `idle_discharges` | over 5 % of capacity net leaves in idle slots, 3 days | SOC or meter |
| `export_ineffective` | under 50 % of the planned energy leaves, 3 days | SOC or meter |
| `auto_not_covering` | under 20 % of the measured deficit is delivered, 3 days | load known; PV measured, or a slot with no forecast PV |
| `auto_not_capturing` | under 20 % of the measured surplus is taken, 3 days | PV power entity, load known |

## Rules

* A day counts only with enough energy at stake (10 % of capacity, 5 % for
  forced slots). Days without do not break a series.
* A good day (over 50 % for the auto checks, over 75 % for charge and export,
  under 2 % of capacity leaked for idle) resets the series and clears the repair.
* Slots only count while the pilot steers: no dry-run, no external control, no
  car drawing, and the SOC far enough from the limit that the battery is free to
  act (5 points).
* Surplus and deficit are measured, never taken from the plan: a wrong PV forecast
  must not fire a check. The load falls back to the forecast only when the
  consumption sensor is an energy meter.
* Surplus is capped at what the battery can take, including the cold factor. A
  learned factor is ignored at 15 °C and above, so a battery that is always weak
  cannot be learned away as "cold".
* Checks about steering are hidden in dry-run, and all are hidden while the pilot
  is disabled.
* Every threshold is a first value, tried against one real installation.

## How

`health.py` holds the logic without Home Assistant imports, like the optimizer.
The coordinator hands it an `Observation` at each slot boundary, mode change and
refresh, and mirrors its active checks into the issue registry. The state survives
restarts in the entry's store. Repairs are only synced after the switches restored
their state, so that a restart does not delete a repair the user chose to ignore.

## Not included

Forecast bias (nothing the user can fix) and an option to disable checks (Home
Assistant can ignore a repair).
