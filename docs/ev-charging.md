# EV charging, evcc and other controllers

A wallbox changes two things for the pilot:

1. **The consumption forecast.** A whole-house meter (a grid-balance template,
   Fronius `P_Load`, a smart meter …) counts the car too. One evening session
   of 11 kW for two hours outweighs a whole day of household load, and the
   model learns it as if the house drew it every day at that hour. EV sessions
   follow the car, not the clock, so they do not belong in that forecast.
2. **Who controls the battery.** Without a rule the inverter discharges the
   house battery into the car — usually not what you want when the grid is
   cheap. Tools like [evcc](https://evcc.io) solve that by holding the battery
   while the car charges. They write to the same inverter as the pilot, and
   the two must not fight over it.

The section **EV charging & other controllers** in the options menu handles
both. It does not depend on evcc and works with any wallbox Home Assistant can
see. Every field is optional, and the defaults change nothing.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| Wallbox power sensors | – | One or more power sensors (W or kW) with what each wallbox draws. Used for both the forecast and the *Block* policy. |
| House battery while an EV charges | Allow | What the battery does during a session, see below. |
| EV charging threshold | 1000 W | *Block* engages above this total wallbox power and releases below half of it, so ramping and phase switching do not flip the inverter back and forth. |
| External control entity | – | Tells when another controller has the battery, see below. Works with any policy. |
| Planned EV sessions | None | *evcc*: the plan leaves room for sessions evcc will run, see [Planned sessions](#planned-sessions-evcc). |
| evcc address | – | Where evcc runs, e.g. `http://evcc.local:7070`. Pre-filled from evcc's Home Assistant integration. |

### Forecast

As soon as wallbox power sensors are set, each hour the wallboxes drew is
taken off that hour of the consumption history before the model is trained
(never below zero). Power sensors are read from their hourly mean, energy
meters (kWh/Wh) from their hourly change — so they need long-term statistics
(`state_class`), as the consumption sensor does. Takes effect at the next daily
retrain or after a restart.

### Policies

* **Allow** — nothing special. The inverter may discharge into the car. Pick
  this if you only want the cleaner forecast, or if your wallbox controller
  handles the battery some other way.
* **Block** — while the wallboxes draw more than the threshold, the pilot runs
  your *block discharge* script instead of *auto*. It reacts to the wallbox
  sensor right away, not only at the next re-plan. A planned grid charge
  stays a grid charge. Decisions show up as `ev_block` in the diagnostics.
  Use this for a wallbox without its own battery logic (go-e, Easee, Wallbox,
  openWB without battery control …).
* **External** — another controller decides while it is active (see the next
  section). This policy requires the external control entity.

### External control

The entity says when another controller has the battery right now:

* a `binary_sensor`, `input_boolean` or `switch` that is **on**, or
* a sensor that reads one of evcc's battery mode codes `hold`, `charge` or
  `holdcharge` — evcc's `sensor.evcc_battery_mode`.

Every other state counts as inactive, including `unknown` and `unavailable`.
The list names the active states on purpose: integrations translate their
states, and a state nobody anticipated must not pause the pilot for good. For
anything else, a template binary sensor turns the condition into on/off.

While it is active the pilot calls **no** script (decision
`external_control`), not even a planned grid charge. That avoids a tug of war
over the inverter, but it has a price: an evcc session that charges the car in
the cheapest hours runs in exactly the slots the pilot would pick to charge the
house battery from the grid, and those charges are skipped. The plan picks up
again with the next slot after the hold. When it ends, the pilot sends its current action once
more, even if that is the same action it sent before. Whatever the other
controller left in the inverter is not what the pilot last applied, and
without the re-send it would stay there until the plan changes. An
unavailable entity counts as inactive: the pilot would rather steer than
leave the battery to a controller nobody can see.

`sensor.…_current_action` shows `external_control_active` as an attribute; the
diagnostics carry the whole EV block (`inputs.ev`).

## evcc

The usual split: **evcc owns the car, the pilot owns the house battery.**

1. In evcc, keep **battery discharge control** on
   (`batteryDischargeControl`). evcc then holds the battery while the car
   charges in fast mode or from a cheap-price plan.
2. In evcc, leave **grid charging of the home battery** off
   (`batteryGridChargeLimit` empty or 0). Grid charging of the house battery
   is the pilot's job; two planners charging the same battery at different
   price thresholds is the fight to avoid.
3. In the pilot (options → *EV charging & other controllers*):
   * Wallbox power sensors: `sensor.evcc_<loadpoint>_charge_power`, one per
     loadpoint.
   * Policy: **External**.
   * External control entity: `sensor.evcc_battery_mode` — evcc's
     *Battery mode [CODE]* sensor. The evcc integration ships it
     **disabled**; enable it first under *Settings → Entities*.

   * Planned EV sessions: **evcc**, with evcc's address (optional, see
     [Planned sessions](#planned-sessions-evcc)).

   With evcc's Home Assistant integration installed, the section is
   pre-filled with these values while it is still empty (the mode sensor only
   once it is enabled).

   Two look-alikes that do **not** work:
   * `sensor.evcc_battery_mode_value` is the same mode, translated
     ("entladen sperren", "Unbekannt" …). Its states cannot be matched, so it
     never counts as active.
   * `switch.evcc_battery_discharge_control` is the setting that *allows* evcc
     to hold the battery, not a sign that it does. It is on all the time and
     would pause the pilot for good.
4. Give evcc the real battery capacity in its site settings. evcc's buffer
   and priority SOC logic works on it, and a wrong value (some templates
   default to 1 kWh) makes its decisions hard to predict.

`prioritySoc` and `bufferSoc` in evcc decide whether PV surplus goes to the
house battery or the car first. With `prioritySoc` at 100 % the house battery
fills first, which matches what the pilot's plan assumes.

## Other wallboxes

| Setup | Wallbox power sensors | Policy | External control entity |
|---|---|---|---|
| Wallbox without battery logic | the wallbox power | Block | – |
| Wallbox controller that holds the battery itself | the wallbox power | External | a sensor/binary sensor from that controller, or a template |
| Forecast only | the wallbox power | Allow | – |

A template binary sensor works as an external control entity for anything
Home Assistant can see, e.g. *on* while an automation forces the battery for
another purpose.

## Planned sessions (evcc)

Optional, and only for evcc. Without it the pilot sees a session when it
starts: the wallbox sensor rises or evcc's hold begins. With it the plan knows
ahead of time, so the SOC forecast, the savings estimate and the grid charges
already account for the car.

Set **Planned EV sessions** to *evcc* and enter evcc's address. The pilot then
reads evcc's REST API (`/api/state`, and `/api/loadpoints/<id>/plan` for a
loadpoint with a charge plan) once a minute and re-plans when the expected
sessions change. It only reads; it never changes anything in evcc. If evcc
cannot be reached, the pilot plans without sessions and logs one warning.

For every connected loadpoint, the expected session comes from what evcc has
already decided:

| evcc | Expected charging |
|---|---|
| A charge plan (target time) | evcc's plan slots, at the power evcc sized the plan with |
| Fast mode | from now until the car reaches its limit |
| Cheap-price charging (smart cost limit) | every slot whose evcc price is at or below the limit, in time order, until the car reaches its limit |
| Charging for any other reason (e.g. PV mode) | the slot in progress |

The energy is bounded by what the car still needs: the vehicle SOC up to its
limit at the vehicle's capacity (with evcc's 90 % charge efficiency), or the
session's energy limit. A car that reports neither is only counted for evcc's
own plan slots and while it charges; anything more would hold the battery
all day on a guess. A disconnected car or a loadpoint that is off counts
nothing.

How the plan uses it depends on the policy (see
[the optimizer](optimizer.md#ev-sessions-in-the-plan)):

* **Allow** — the car's grid energy is demand; the planner decides whether
  the battery serves it.
* **Block** — slots above the threshold are held: no discharge.
* **External** — the slots are held while evcc's battery discharge control is
  on, and no grid charge is planned there, because the pilot calls no script
  while evcc holds the battery.

In every case the car takes PV surplus before the battery does, so the SOC
forecast no longer runs high on days the car charges from the sun.

Keep the wallbox power sensors set as well. They clean the consumption
history; without them a house meter that includes the wallbox counts the car
twice, once in the learned forecast and once as a planned session.

The plan sensor carries the sessions (`ev_sessions`, `ev_kwh_total`, and
`ev_price_limit`: evcc's price limit moved onto the plan's price basis, since
evcc's prices may leave out fees the pilot's include). Each slot gets
`ev_kwh`, `ev_source` and `battery_blocked`. The card draws the charging
windows, evcc's price limit and the slots where the battery is held. The
diagnostics show what evcc reported per loadpoint, without vehicle names.

Not included: evcc's PV mode as a forecast. Whether PV surplus goes to the
car or the battery depends on evcc's `prioritySoc` and `bufferSoc` and on the
sun minute by minute. A PV session is counted once it runs, not ahead.
