# How the optimizer works

The planner is deliberately **deterministic** — no neural network, no solver
dependency. Every decision can be explained from the price curve, and the
same inputs always produce the same plan.

## Inputs

* Price slots for the next 24–36 h (native resolution of your tariff source,
  usually 15 min or 1 h), including your configured price offset.
* Net demand forecast per slot: learned consumption minus expected PV.
* Battery state: SOC, capacity, min/max SOC, charge/discharge power limits,
  roundtrip efficiency.
* Options: minimum price spread, discharge mode, feed-in tariff.

## Algorithm

Greedy pairing with a stored-energy timeline simulation:

1. **Sort** all slots by price, most expensive first. These are the discharge
   candidates — the hours where battery energy is worth the most.
2. For each candidate, the energy needed to cover its net demand is sourced:
   1. from **energy already in the battery** (PV surplus, initial SOC) — free
      energy is always used at the most expensive hours first;
   2. from **grid charging in cheaper, earlier slots** — only if
      `charge price / efficiency + spread < discharge price`.
3. Every assignment is validated against the battery **timeline**: SOC never
   leaves the min/max window at any point, and per-slot charge/discharge
   power limits are respected. **Forecasted PV surplus charges the battery
   in this timeline** (clamped at max SOC, limited by charge power) — so on
   sunny days the planner knows the battery refills by evening, doesn't lock
   it during the day and doesn't grid-charge needlessly. The SOC projection
   rises with the sun accordingly.
4. Slots that have real demand but whose stored energy is reserved for a
   later, more expensive slot are marked **idle** (discharge blocked) so the
   battery isn't drained early. Slots with PV surplus stay in **auto** —
   the inverter charges from PV and won't discharge anyway.
5. In **export mode**, remaining peak slots can additionally be paired for
   grid export, valued at the feed-in tariff (or market price if 0).

The plan is recomputed every 30 minutes, whenever the price entity updates
(e.g. tomorrow's prices arriving around 14:00), on option changes, and via
the `smart_battery_pilot.replan` service. Only the *current* slot's action is
ever executed, so plan revisions take effect immediately.

## Why a spread threshold?

Every grid-charged kWh loses ~10 % to conversion and costs battery cycle
life. With spread `s` and efficiency `η`, charging at price `p_c` for a
discharge at `p_d` only happens if

```
p_c / η + s  <  p_d
```

The default `s = 0.20 EUR/kWh` means: a night price of 0.10 with 90 %
efficiency requires an evening price above ~0.31 before the battery is
charged from the grid.

## Savings estimate

`sensor.…_estimated_savings` values the plan against buying everything from
the grid at the slot price:

```
savings = Σ discharged kWh × slot price  (+ export kWh × feed-in)
        − Σ grid-charged kWh × slot price
```

It is an estimate over the current plan horizon, not a billing-grade number.

## Worked example (Dunkelflaute)

Prices: night 0.10, morning peak 0.45, day 0.20, evening peak 0.50 EUR/kWh.
Demand 1.5 kWh/h, battery 12.8 kWh / 6 kW, SOC 10 %, spread 0.10:

* The evening peak (0.50) is paired first → charged in the cheapest night
  slots (0.10 / 0.9 + 0.10 = 0.21 < 0.50 ✓).
* The morning peak (0.45) is paired next, also from night slots.
* Day slots at 0.20 stay `idle` — discharging there would waste energy worth
  0.50 in the evening.
* Result: `charge` at night, `auto` during both peaks, `idle` in between.
