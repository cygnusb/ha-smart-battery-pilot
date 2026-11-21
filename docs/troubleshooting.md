# Troubleshooting & FAQ

## Where to look first

* **Download diagnostics** (device page → ⋮). It holds the inputs of the last
  plan, the first day of the plan and the executor's last 50 decisions with
  their reasons — usually enough to see why a slot was charged, blocked or left
  alone. Attach it to any issue report.
* **Debug logging** (device page → *Enable debug logging*) adds every planning
  run's inputs, how the price adapter parsed the entity, each charge/discharge
  pairing the optimizer made or rejected (e.g. *spread not reached*, with the
  arithmetic), the reserve refill pass and every executor decision.
* **Settings → Repairs** shows where the battery does not do what the plan
  expects ([consistency checks](configuration.md#consistency-checks)).

## Plan status

`sensor.…_plan_status` says whether a valid plan exists. Its `error_detail`
attribute carries the reason in more detail.

| Status | Meaning | What to check |
|---|---|---|
| `ok` | A valid plan exists. | – |
| `no_price_data` | The price entity is unavailable, or none of its prices lie in the future. | That the price integration works, and that its curve starts today. A source that serves yesterday's curve as today's ends here once that curve has run out ([stale prices](configuration.md#price-sources-without-forecast-attributes)). |
| `no_soc` | The SOC entity has no numeric value. | The battery integration. |
| `no_price_adapter` | The price entity's attributes match none of the [supported formats](configuration.md#1-price-source). | Use a [price bridge](price-bridges.md), or a template sensor with `today` / `tomorrow` lists. |
| `implausible_prices` | A price is far beyond any real tariff (more than 10 EUR/kWh, scaled to the currency), most likely a unit error such as cents read as euros. | The unit of the price entity. |
| `error` | Anything else, e.g. prices that could not be parsed. | `error_detail`, the log. |

Without a valid plan the pilot calls the *auto* script once, logs one warning
and leaves the battery alone until planning works again. After 12 hours the
*No valid plan* repair appears.

## Plan warnings

The plan sensor's `warnings` attribute lists what the planner noticed while it
still produced a plan:

| Warning | Meaning |
|---|---|
| `plan_worse_than_baseline` | The plan came out worse than doing nothing, so the all-`auto` plan is used instead ([step 8](optimizer.md#algorithm)). |
| `reserve_refill_incomplete` | The refill deadline is too short for the charge power to bring the SOC back up to the [backup reserve](configuration.md#backup-reserve). |
| `export_spread_unreachable` | Export mode is on, but no slot sells for more than the minimum spread, so no export can be planned. |

## Common questions

### Why doesn't the plan charge from the grid?

Grid charging is planned only when a later slot is worth more than the charge
after losses plus the [minimum price spread](optimizer.md#why-a-spread-threshold):

```
charge price / efficiency + spread  <  discharge price
```

With the default spread of 0.20, a curve from 0.22 to 0.34 at 90 % efficiency
needs 0.22 / 0.90 + 0.20 ≈ 0.44 — more than the most expensive slot, so nothing
is charged. Lower the spread (*Configure → Optimizer tuning*) if you want the
pilot to work with smaller price swings. Every cycle wears the battery a
little, which is what the spread is there to cover. With debug logging on, the
log shows every rejected pairing with this arithmetic.

### Why does the SOC forecast end at the minimum SOC?

The plan ends with the last known price, usually at midnight before tomorrow's
prices are out, at the end of tomorrow after that. Energy still in the battery
then is worth nothing to the plan, so it covers the house with it before. When
the next day's prices arrive, the pilot plans again and may charge overnight if
the spread pays.

### Why does the battery discharge at a low price, or hold back at a high one?

Stored energy goes to the most expensive slots of the horizon first. A slot is
blocked (`idle`) only when the energy would otherwise run short for a pricier
slot later. So "cheap" and "expensive" are always relative to the rest of the
known curve, and there is no absolute price threshold: holding energy back
through a cheap hour that nothing pricier follows usually means the next PV
surplus no longer fits and is exported at the feed-in tariff. In slots whose
price (including the offset) is zero or negative, the battery never covers the
house.

### Why does it show savings although the pilot did nothing?

*Battery benefit* is what your battery is worth in self-consumption, pilot or
not. What the pilot added is *Pilot savings*, which stays at 0.00 while every
slot is `auto`. See [savings estimate](optimizer.md#savings-estimate).

### Why did the `idle` slots disappear when I raised the backup reserve?

A SOC below the reserve is refilled first, and until then the battery spends
nothing, so there is nothing to block: the slots before the refill show `auto`.
The inverter is expected to hold the reserve itself, or the *block discharging
at the reserve* fallback does it. See [backup reserve](configuration.md#backup-reserve).

### Can I tell the pilot about a load it can't know (guests, a sauna evening)?

No, there is no manual adjustment of the forecast. The plan starts again from
the real SOC every 30 minutes, so an unexpected load shows up in the next plan.
The large planned loads are usually cars, and those have their own handling:
the wallbox is taken out of the learned consumption, and with evcc the planned
sessions are part of the plan ([EV charging](ev-charging.md)).
