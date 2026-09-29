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
* Charge factor (cold-weather charging, off by default): the share of the max
  charge power the battery is expected to accept at its current temperature.
  It scales the per-slot charge cap, so a cold battery is charged over more
  slots. A slot planned up to its cold limit asks the charge script for the
  maximum power, so the pilot never throttles the battery itself — the BMS
  caps it. A slot that needs less asks for its planned power: a cold BMS caps
  the current rather than taking a share of the request, so asking for more
  would overshoot the plan and max SOC.
* Backup reserve (off by default): the planner counts stored energy above the
  reserve instead of above the minimum SOC, so no discharge or export slot ever
  takes the SOC below it. A SOC under the reserve starts as a deficit. Before
  any arbitrage, a refill pass covers it by the deadline: forecast PV surplus
  first, the rest in the cheapest grid slots up to the deadline, regardless of
  the spread. The refill is mandatory, so its cost is excluded from the savings
  estimate, and the do-nothing baseline gets the same refill — both sides
  compare arbitrage against arbitrage.

## Algorithm

Greedy pairing with a stored-energy timeline simulation. Stored energy is
counted above a floor — the minimum SOC, or the backup reserve when one is
set — and each slot's charge capacity is the max charge power scaled by the
cold-weather charge factor.

0. **Refill the backup reserve** (only if the SOC is below it): forecast PV
   first, then the cheapest grid slots up to the refill deadline, regardless of
   the spread. These become ordinary `charge` slots.
1. **Sort** all slots by price, most expensive first. These are the discharge
   candidates — the hours where battery energy is worth the most.
2. For each candidate, the energy needed to cover its net demand is sourced:
   1. from **energy already in the battery** (PV surplus, initial SOC) — free
      energy is always used at the most expensive hours first;
   2. from **grid charging in cheaper, earlier slots** — only if
      `charge price / efficiency + spread < discharge price`, and never while
      the battery would still be below an unfilled reserve at that slot.
3. Every assignment is validated against the battery **timeline**: SOC never
   leaves the window between the floor and max SOC at any point, and per-slot charge/discharge
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
   the configured import offset is not counted as export revenue). An export
   script is required for this mode; the options flow refuses to select it
   without one. What the cost model assumes of that script is spelled out
   under [Export slots](#what-an-export-slot-assumes) below.
7. **Never worse than doing nothing.** If the finished plan's estimated
   savings still come out negative, it is discarded and the all-`auto` plan
   is returned instead, carrying a `plan_worse_than_baseline` warning in the
   plan sensor's attributes. Leaving the inverter alone is always available
   and is the very baseline the figure is measured against.

The plan is recomputed every 30 minutes, whenever the price entity or the
backup-reserve entity updates (e.g. tomorrow's prices arriving around 14:00),
on option changes, and via the `smart_battery_pilot.replan` service. Only the *current* slot's action is
ever executed, so plan revisions take effect immediately.

## What an export slot assumes

An export slot is the one action where the plan asks the inverter to do two
things at once. The cost model still credits the household load as covered
from the battery and charges only the *remainder* to the grid at the feed-in
price — and the `power_w` handed to the export script is the **sum** of both,
the load share plus the exported share.

Your export script must therefore keep serving the house from the battery
while it discharges into the grid. A script that switches the inverter to a
pure "sell at `power_w`" mode and leaves the house on the grid meter still
runs safely, but the household demand of those slots is then bought at the
import price without the model knowing, and the realised saving comes out
below `estimated_savings`. Export slots are rare and short by construction
(only genuine price peaks qualify), so the gap stays small — but if your
inverter cannot do both, prefer self-consumption mode.

## What curtailed PV is worth

Where the timeline pins the battery at max SOC, the surplus that no longer
fits is treated as worth **nothing** — step 4 above spends energy freely
ahead of such a slot because the PV refills it at no cost. In reality that
surplus is exported and earns the feed-in tariff, so those withdrawals do
have a price.

It does not change any decision. The withdrawal happens to avoid an *import*,
and as long as the import price exceeds the feed-in tariff — which it does in
every residential tariff this integration targets — spending stored energy
before a curtailment window is the better trade regardless. It makes
`estimated_savings` a touch optimistic on sunny days with a fixed feed-in
tariff, by at most the feed-in value of the energy moved.

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
Three running totals come from the battery's energy meters. They answer three
different questions:

| Sensor | Question | How it counts |
|---|---|---|
| **Battery benefit (net)** (`sensor.…_actual_savings_eur`, id kept from before the rename) | What is the battery worth? | Discharge credited at what it displaced (the import price; in an `export` slot the feed-in tariff, or the raw market price when that is `0`), minus what the charge cost: grid charge at the import price, PV charge in auto/idle at the feed-in tariff it displaced. A charge in a pilot mode not yet on record is priced at the import price. |
| **Battery benefit (gross)** (`sensor.…_battery_gross_eur`) | What did the battery displace? | Discharge only, valued the same way. Charge costs, including the PV opportunity cost, are not deducted. |
| **Pilot savings** (`sensor.…_pilot_savings_eur`) | What did the *pilot* add? | Only energy the pilot moved differently from the inverter: grid charge in a `charge` slot, and demand an `idle` slot kept the battery from covering (estimated from the plan's demand forecast). Each goes into a lot with its price. Later discharge uses the lots oldest first and is credited with the value then, minus that price, after losses. Discharge beyond the lots is plain self-consumption and earns the pilot nothing. A pilot that only ever sends `auto` therefore shows 0.00. A trade that did not pay off lowers the figure. |

**Net and gross count whether or not the pilot steers.** With the pilot off or
in dry-run, the inverter runs in its own (auto) mode, and the energy is priced
that way. The pilot savings are booked segment by segment, at every slot
boundary and mode change. The lots never exceed the energy the battery
actually holds above the minimum SOC, and they appear in the diagnostics dump.
Wh meters are converted to kWh. All totals keep their last value when an
update fails, so a blinking price entity does not tear a hole in their
long-term statistics.

None of these is a counterfactual against the inverter's own behaviour. That
cannot be measured. `estimated_savings` is the one with a simulated
do-nothing baseline, over the plan horizon.

## Worked example (Dunkelflaute)

Prices: night 0.10, morning peak 0.45, day 0.20, evening peak 0.50 EUR/kWh.
Demand 1.5 kWh/h, battery 12.8 kWh / 6 kW, SOC 10 %, spread 0.10:

* The evening peak (0.50) is paired first → charged in the cheapest night
  slots (0.10 / 0.9 + 0.10 = 0.21 < 0.50 ✓).
* The morning peak (0.45) is paired next, also from night slots.
* Day slots at 0.20 stay `idle` — discharging there would waste energy worth
  0.50 in the evening.
* Result: `charge` at night, `auto` during both peaks, `idle` in between.
