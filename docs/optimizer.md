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
   it during the day and doesn't grid-charge needlessly. PV occupies the
   slot's charge-power headroom first; grid charging in the same slot only
   uses what is left. The SOC projection rises with the sun accordingly.
4. **Curtailed PV makes earlier energy free to spend.** Where the timeline
   pins the battery at max SOC, the surplus that no longer fits is thrown
   away. A kWh discharged *before* such a slot is refilled by that surplus at
   no cost, so it leaves every later SOC level untouched — the timeline check
   in step 3 credits each candidate with the curtailed energy between it and
   the slots it would otherwise starve. Without this, holding energy back for
   a morning peak on a sunny day displaces free midday PV with energy bought
   from the grid, and the plan ends the day having delivered *less* to the
   house than an untouched inverter.
5. Slots that have real demand but whose stored energy the assignment did not
   spend are marked **idle** (discharge blocked): the energy is reserved for
   a later, more expensive slot, so the battery must not be drained early.
   The label follows from the assignment rather than being re-derived beside
   it, which is what keeps the inverter's behaviour and the cost model in
   agreement. Slots with PV surplus stay in **auto** — the inverter charges
   from PV and won't discharge anyway.
6. In **export mode**, remaining peak slots can additionally be paired for
   grid export, valued at the feed-in tariff (or the raw market price if 0 —
   the configured import offset is not counted as export revenue).
7. **Never worse than doing nothing.** If the finished plan's estimated
   savings still come out negative, it is discarded and the all-`auto` plan
   is returned instead, carrying a `plan_worse_than_baseline` warning in the
   plan sensor's attributes. Leaving the inverter alone is always available
   and is the very baseline the figure is measured against.

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

`sensor.…_estimated_savings` compares the plan's grid bill against the bill
you would get from **doing nothing** — plain self-consumption, the inverter's
own behaviour:

```
grid cost = Σ (demand − battery kWh delivered) × slot price
          + Σ grid-charged kWh × slot price
          − Σ exported kWh × sell price

savings   = grid cost (do nothing) − grid cost (plan)
```

The baseline matters. Valuing every discharged kWh against "buy everything
from the grid" would report a fat saving even for an all-`auto` plan that
changes nothing, because a battery covering the house is what the inverter
does anyway. With the do-nothing baseline, a plan that changes nothing
reports `0.00`, and what is left is the part the planner actually earned:
charging cheap, and holding energy back for a pricier hour.

It is an estimate over the current plan horizon, not a billing-grade number.
The sensor is a point-in-time `MEASUREMENT` (it jumps at every replan), not
an accumulating meter — and deliberately carries no `monetary` device class,
which Home Assistant only accepts together with `TOTAL`.
`sensor.…_actual_savings_eur` is the running total from energy-meter deltas,
priced at the time-weighted slot prices of each interval. Grid charge uses
the import price; charge that happens in auto/idle (typically PV) uses the
feed-in tariff; charge in a mode not yet on record — the first interval after
switching on, or a restart before the first script call — is priced at the
import price, because assuming PV there would book grid energy at a fraction
of its cost. Discharge is valued by where the energy went: self-consumption
avoids the full import price, while discharge during an `export` slot only
earns the feed-in tariff (or the raw market price when the tariff is `0`).
Wh meters are converted to kWh. Both totals keep their last value when an
update fails, so a blinking price entity does not tear a hole in their
long-term statistics.

**It only counts while the pilot steers.** Accounting is paused whenever the
master switch is off or dry-run is on. A battery cycling under the inverter's
own control is not the planner's doing, and counting it reported several euros
a day of "savings" from an integration that had not called a single script.
The meter baselines keep advancing while paused, so switching the pilot on
does not settle everything that moved in the meantime as one huge delta.

Note what the figure is: the measured value of the battery's energy flows
while the pilot is in charge — discharge credited at what it displaced, minus
what the charge cost. It is not a counterfactual against the inverter's own
behaviour; a plan whose slots are all `auto` still accrues the value of plain
self-consumption. `estimated_savings` is the one with a do-nothing baseline,
and the two therefore answer different questions.

## Worked example (Dunkelflaute)

Prices: night 0.10, morning peak 0.45, day 0.20, evening peak 0.50 EUR/kWh.
Demand 1.5 kWh/h, battery 12.8 kWh / 6 kW, SOC 10 %, spread 0.10:

* The evening peak (0.50) is paired first → charged in the cheapest night
  slots (0.10 / 0.9 + 0.10 = 0.21 < 0.50 ✓).
* The morning peak (0.45) is paired next, also from night slots.
* Day slots at 0.20 stay `idle` — discharging there would waste energy worth
  0.50 in the evening.
* Result: `charge` at night, `auto` during both peaks, `idle` in between.
