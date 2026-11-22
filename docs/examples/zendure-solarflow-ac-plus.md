# Zendure SolarFlow 2400 AC+ (AC-coupled) + separate PV inverter

A plug-in AC battery with **no hybrid inverter**: the Zendure SolarFlow 2400 AC+
sits on a wall socket and is controlled through Home Assistant via a SolarFlow
integration (select/number entities). Rooftop PV runs on a separate Hoymiles
micro-inverter (OpenDTU), and the grid meter is a Dutch P1 smart meter read by
DSMR Reader → MQTT. Prices come from a Nordpool template sensor.

This setup differs from most others in one important way: **nothing in the
hardware captures PV surplus by itself.** A hybrid inverter in auto mode
charges its battery from its own PV; the SolarFlow AC+ in `output` mode never
pulls AC power to charge. The pilot plans `auto` slots assuming exactly that
capture happens, so this example adds a small *assist* automation that provides
bidirectional self-consumption inside auto slots: discharge to cover the house
load, charge from surplus otherwise. In idle slots the same automation stays on
in charge-only mode, so surplus that turns up anyway still lands in the battery
while the plan holds the charge. In the
[what an auto slot assumes](../optimizer.md#what-an-auto-slot-assumes) table,
this is the row where nobody follows the meter unless an automation does.

## Architecture

```
                    ┌─ charge slot ──▶ sbp_force_charge ──▶ AC mode input,  input_limit = power_w, assist OFF
pilot ──▶ scripts ──┼─ idle slot ───▶ sbp_block_discharge ▶ AC mode output, output_limit = 0, assist charge-only
                    └─ auto slot ───▶ sbp_auto_mode ──────▶ AC mode output + assist automation ON
 assist automation (auto slots; in idle slots charge-only):
   house load > 150 W ──────────▶ output mode, output_limit = min(load, max_w)   (held off in idle)
   PV surplus > 250 W (stable) ▶ input mode,  input_limit  = min(surplus, max_w)
   otherwise ──────────────────▶ output mode, output_limit = 0
```

The assist is the pilot's "hands" in auto mode — and in idle slots it stays on
in charge-only mode. The charge script switches it off before it commands the
device; the idle script leaves it running but sets an `input_boolean` that the
assist's discharge branch checks. That branch is the only writer of a positive
`output_limit` anywhere, so gating it is what "block discharging" means here:
the plan holds the battery, and the charge and neutral branches keep working —
capturing surplus and re-asserting the 0 W hold. The pilot stays the single
brain.

**What about the device's own smart-meter mode?** With a compatible meter
paired in the vendor app, the 2400 AC+ can follow the grid meter on its own —
the one case in
[what an auto slot assumes](../optimizer.md#what-an-auto-slot-assumes) where
the loop closes without an automation. It is still the wrong tool here: the
mode lives in the vendor app, is not exposed to Home Assistant by the
integration, and it follows the meter in *every* state — it would keep
discharging through charge and idle slots where the plan wants the battery
held or charging, trading the plan's price optimization and evening protection
for plain load-following. So the battery stays in command mode, and the assist
closes the loop instead — in auto slots, in idle slots charge-only, and only
while the pilot has it enabled.

## Prerequisites

* A SolarFlow integration exposing at least:
  * `select.…_ac_mode` (`input` / `output`)
  * `number.…_input_limit`, `number.…_output_limit` (W)
  * `sensor.…_electric_level` (SOC %), `sensor.…_output_home_power` (W),
    `sensor.…_grid_input_power` (W, AC charge draw)
* Grid power at the meter: `sensor.dsmr_reading_electricity_currently_delivered`
  / `…_currently_returned` (kW) — any whole-house power sensor works
* An `input_boolean.assist_charge_only` helper (Settings → Devices & services →
  Helpers → Toggle) — the idle script sets it, the auto and charge scripts
  clear it
* The pilot's `pv_power_entity` set to the inverter's live AC power (OpenDTU)

Entity IDs below use the author's names; yours may differ slightly.

## Scripts

```yaml
sbp_force_charge:
  alias: "SBP: Force charge"
  fields:
    power_w:
      description: Charge power in W
  sequence:
    - action: automation.turn_off
      target:
        entity_id: automation.assist_discharge_on_import
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.assist_charge_only
    - action: select.select_option
      target:
        entity_id: select.solarflow_2400_ac_plus_ac_mode
      data:
        option: input
    - action: number.set_value
      target:
        entity_id: number.solarflow_2400_ac_plus_input_limit
      data:
        value: "{{ [power_w | float(0), 800] | min | round(0) }}"
    - action: number.set_value
      target:
        entity_id: number.solarflow_2400_ac_plus_output_limit
      data:
        value: 0

# Idle: block discharging, keep charging. The assist stays ON so surplus keeps
# landing in the battery; the flag takes only its discharge branch out.
sbp_block_discharge:
  alias: "SBP: Block discharge (idle)"
  sequence:
    - action: input_boolean.turn_on
      target:
        entity_id: input_boolean.assist_charge_only
    - if:
        - condition: state
          entity_id: automation.assist_discharge_on_import
          state: "off"
      then:
        # coming from a charge or discharge slot: end grid charging / export first
        - action: select.select_option
          target:
            entity_id: select.solarflow_2400_ac_plus_ac_mode
          data:
            option: output
        - action: number.set_value
          target:
            entity_id: number.solarflow_2400_ac_plus_output_limit
          data:
            value: 0
    - action: automation.turn_on
      target:
        entity_id: automation.assist_discharge_on_import
    - action: automation.trigger
      target:
        entity_id: automation.assist_discharge_on_import

sbp_auto_mode:
  alias: "SBP: Auto mode"
  sequence:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.assist_charge_only
    - if:
        - condition: state
          entity_id: automation.assist_discharge_on_import
          state: "off"
      then:
        # coming from a charge or discharge slot: end grid charging / export first
        - action: select.select_option
          target:
            entity_id: select.solarflow_2400_ac_plus_ac_mode
          data:
            option: output
        - action: number.set_value
          target:
            entity_id: number.solarflow_2400_ac_plus_output_limit
          data:
            value: 0
    - action: automation.turn_on
      target:
        entity_id: automation.assist_discharge_on_import
    - action: automation.trigger
      target:
        entity_id: automation.assist_discharge_on_import

# optional, only needed for export mode:
sbp_force_discharge:
  alias: "SBP: Force discharge to grid"
  fields:
    power_w:
      description: Discharge power in W
  sequence:
    - action: automation.turn_off
      target:
        entity_id: automation.assist_discharge_on_import
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.assist_charge_only
    - action: select.select_option
      target:
        entity_id: select.solarflow_2400_ac_plus_ac_mode
      data:
        option: output
    - action: number.set_value
      target:
        entity_id: number.solarflow_2400_ac_plus_output_limit
      data:
        value: "{{ [power_w | float(0), 800] | min | round(0) }}"
```

The 2400 AC+ is capped at 800 W per phase on a domestic socket; raise the cap
together with the pilot's *Battery* power limits once your installation allows
more. `power_w` already includes the slot's forecast PV surplus — the battery
charges at that whole power, with the surplus covering part of it and the grid
the rest.

## The assist automation

This is the piece that makes an AC-coupled battery behave like a hybrid in
auto slots. It is edge-triggered per grid-meter telegram, with a time-pattern
backstop, and it re-derives everything from raw sensor states on every run, so
there is no state to get stale.

Two definitions do all the work:

* `gap` = `delivered − returned + battery output` = **house load − PV**,
  independent of what the battery is currently doing (while discharging).
* `surplus` = PV − house, computed **mode-aware**: in output mode it is simply
  `−gap`; in input mode it is `charge draw − gap`. This matters — see
  [pitfalls](#notes--pitfalls).

The triggers are split **per regime** — see pitfall 2 for why one combined
trigger will go deaf. All four share the same prelude:

```yaml
{% set del = states('sensor.dsmr_reading_electricity_currently_delivered') | float(0) * 1000 %}
{% set ret = states('sensor.dsmr_reading_electricity_currently_returned') | float(0) * 1000 %}
{% set bout = states('sensor.solarflow_2400_ac_plus_output_home_power') | float(0) %}
{% set bch = states('sensor.solarflow_2400_ac_plus_grid_input_power') | float(0) %}
{% set soc = states('sensor.solarflow_2400_ac_plus_electric_level') | float(0) %}
{% set mode = states('select.solarflow_2400_ac_plus_ac_mode') %}
{% set gap = del - ret + bout %}
{% set surplus = (bch - gap) if mode == 'input' else (-gap) %}
{% set out = states('number.solarflow_2400_ac_plus_output_limit') | float(0) %}
{% set inl = states('number.solarflow_2400_ac_plus_input_limit') | float(0) %}
```

```yaml
alias: "Assist: discharge on import, charge on surplus"
mode: restart
triggers:
  # 1. discharge desired (house load > 150 W) and diverges from actual
  - trigger: template
    value_template: >
      {% set del = ... %} {% set ret = ... %} {% set bout = ... %}
      {% set soc = ... %} {% set mode = ... %} {% set gap = del - ret + bout %}
      {% set out = states('number.solarflow_2400_ac_plus_output_limit') | float(0) %}
      {{ gap > 150 and (mode != 'output' or (([gap, 800] | min | round(0)) - out) | abs > 50) }}
  # 2. charge entry: surplus first exceeds 250 W while in output mode
  - trigger: template
    value_template: >
      {% set del = ... %} {% set ret = ... %} {% set bout = ... %}
      {% set soc = ... %} {% set mode = ... %} {% set gap = del - ret + bout %}
      {{ mode != 'input' and soc < 100 and (-gap) > 250 }}
  # 3. charge adjust: while charging, true surplus drifted >50 W from the limit
  - trigger: template
    value_template: >
      {% set del = ... %} {% set ret = ... %} {% set bout = ... %} {% set bch = ... %}
      {% set soc = ... %} {% set mode = ... %} {% set gap = del - ret + bout %}
      {% set surplus = (bch - gap) if mode == 'input' else (-gap) %}
      {% set inl = states('number.solarflow_2400_ac_plus_input_limit') | float(0) %}
      {{ mode == 'input' and soc < 100 and surplus > 100
         and (([surplus, 800] | min | round(0)) - inl) | abs > 50 }}
  # 4. neutral: neither direction warranted but device is not at rest
  - trigger: template
    value_template: >
      {% set del = ... %} {% set ret = ... %} {% set bout = ... %} {% set bch = ... %}
      {% set soc = ... %} {% set mode = ... %} {% set gap = del - ret + bout %}
      {% set surplus = (bch - gap) if mode == 'input' else (-gap) %}
      {% set out = states('number.solarflow_2400_ac_plus_output_limit') | float(0) %}
      {{ (mode != 'output' or out != 0)
         and gap <= 150
         and surplus <= (100 if mode == 'input' else 250) }}
  # backstop after restarts or missed telegrams
  - trigger: time_pattern
    minutes: "/5"
conditions: []
actions:
  - choose:
      # discharge branch - condition re-evaluated at run time, gap > 150;
      # suppressed while the idle script holds the battery (charge-only flag)
      - conditions:
          - >
            {% set del = ... %} {% set ret = ... %} {% set bout = ... %}
            {{ (del - ret + bout) > 150
               and not is_state('input_boolean.assist_charge_only', 'on') }}
        sequence:
          - action: number.set_value
            target:
              entity_id: number.solarflow_2400_ac_plus_output_limit
            data:
              value: >
                {% set gap = states('sensor.dsmr_reading_electricity_currently_delivered') | float(0) * 1000 - states('sensor.dsmr_reading_electricity_currently_returned') | float(0) * 1000 + states('sensor.solarflow_2400_ac_plus_output_home_power') | float(0) %}
                {% set soc = states('sensor.solarflow_2400_ac_plus_electric_level') | float(0) %}
                {{ [gap, 800] | min | round(0) if soc > 5 and gap > 150 else 0 }}
          - action: select.select_option
            target:
              entity_id: select.solarflow_2400_ac_plus_ac_mode
            data:
              option: output
      # charge branch - true surplus (mode-aware) above the hold threshold
      - conditions:
          - >
            {% set del = ... %} {% set ret = ... %} {% set bout = ... %} {% set bch = ... %}
            {% set gap = del - ret + bout %}
            {% set surplus = (bch - gap) if is_state('select.solarflow_2400_ac_plus_ac_mode', 'input') else (-gap) %}
            {{ states('sensor.solarflow_2400_ac_plus_electric_level') | float(0) < 100
               and surplus > (100 if is_state('select.solarflow_2400_ac_plus_ac_mode', 'input') else 250) }}
        sequence:
          - action: number.set_value
            target:
              entity_id: number.solarflow_2400_ac_plus_input_limit
            data:
              value: >
                {% set del = ... %} {% set ret = ... %} {% set bout = ... %} {% set bch = ... %}
                {% set gap = del - ret + bout %}
                {% set surplus = (bch - gap) if is_state('select.solarflow_2400_ac_plus_ac_mode', 'input') else (-gap) %}
                {{ [surplus, 800] | min | round(0) }}
          - action: select.select_option
            target:
              entity_id: select.solarflow_2400_ac_plus_ac_mode
            data:
              option: input
          - action: number.set_value
            target:
              entity_id: number.solarflow_2400_ac_plus_output_limit
            data:
              value: 0
    default:
      # neutral: output mode, 0 W
      - action: number.set_value
        target:
          entity_id: number.solarflow_2400_ac_plus_output_limit
        data:
          value: 0
      - action: select.select_option
        target:
          entity_id: select.solarflow_2400_ac_plus_ac_mode
        data:
          option: output
```

(The `...` marks stand for the full `states(...) | float(0)` expressions shown
in the prelude above — write them out in your actual YAML.)

Tuning: the 150 W discharge floor ignores noise and tiny loads; the 250 W
charge entry / 100 W charge hold pair is the hysteresis that keeps the AC relay
from chattering when surplus hovers near the threshold. The 5% SOC floor
matches a pilot *Min SOC* of 5 % (its default is 10 %); the device's own minimum-SOC setting stays in
place as the hardware backstop. The `choose` conditions re-evaluate the regime
at run time, so when two triggers fire around the same moment the newest run
always acts on the freshest regime. The idle gate sits in the discharge
*branch* condition, so it is re-evaluated on every run. With the flag on, a
discharge-desired moment falls through to the neutral branch, which writes
`output_limit = 0` — re-asserting the idle hold. The scripts also trigger the
assist once after flipping the flag: a template trigger that is already true
does not fire again, so without that kick the first moments of the slot after
an idle hold would wait for the 5-minute backstop before discharging or
charging (see pitfall 8).

Two pilot limits need one line each. **Max SOC** — the assist charges while
`soc < 100`, so set the pilot's *Max SOC* to 100 % (its default is 95 %) or
change the checks to match; otherwise the plan stops at Max SOC while the assist charges
on toward 100 %. **Backup reserve** — the fixed 5 % floor ignores
`reserve_soc`; switch on
[block discharging at the reserve](../configuration.md#backup-reserve) and the
pilot runs the block-discharge script at the reserve, which gates the assist's
discharge branch, so the assist can never drain a configured reserve — while
still charging from surplus.

## Integration configuration

| Config flow field | Entity / value |
|---|---|
| Price forecast entity | Nordpool-based template sensor (market + markup, incl. VAT) |
| SOC entity | `sensor.solarflow_2400_ac_plus_electric_level` |
| Capacity | 5.28 kWh |
| Max charge / discharge power | 800 / 800 W (socket-limited) |
| Min / Max SOC | 5 / 100 % (the assist's floor and ceiling) |
| Scripts | the four `sbp_*` scripts above |
| Consumption sensor | DSMR-based whole-house load template sensor |
| PV power entity | OpenDTU inverter AC power |

## Verification

* Charge slot: `grid_input_power` tracks `power_w`; the meter's delivered
  power rises by the same amount, less any PV surplus (`power_w` already
  counts the forecast surplus, so the battery takes it first).
* Auto + surplus: AC mode flips to `input`, `grid_input_power` ≈ surplus, and
  the meter's returned power drops to ≈ 0.
* Auto + load: `output_home_power` ≈ house load (up to the cap), returned
  power ≈ 0, delivered ≈ 0.
* Idle + surplus: the assist stays on and the flag is `on`; surplus charges
  (`grid_input_power` ≈ surplus, returned ≈ 0) while `output_limit` stays 0.
  When the surplus dies, the neutral branch returns to output mode at 0 W.
  Sunny idle spans now feed the battery instead of exporting at raw price.
* Idle + load: with the house importing, `output_home_power` stays 0 even
  though the discharge trigger wants to fire — the flag gate is doing its job.
* Idle → auto with house load: the battery starts discharging within a
  telegram or two, not after the 5-minute backstop.
* Regime flips: a cloud edge or a big load stepping on must switch the device
  within one or two meter telegrams. If a wrong state persists for minutes,
  your triggers have latched — see pitfall 2.
* Sunny-day check: the plan's SOC forecast should now track reality. If the
  forecast rises but the battery does not charge, your capture piece is not
  working — that is exactly the failure mode this example fixes.
* Per-phase meter data (DSMR v5 meters publish `phase_currently_delivered_l*`)
  tells you which phase a single-phase battery actually lands on.

## Notes / pitfalls

1. **Compute surplus mode-aware — never reuse the gap.** A natural first draft
   adds the charge draw into the gap (`gap = delivered − returned + output +
   charge`). But `delivered − returned` already contains the charge draw, so
   the gap inflates by **2× the charge power**: charging starts, the gap jumps
   over the exit threshold, the automation drops out of charge, the draw decays,
   the gap falls back under the entry threshold, charging restarts — a
   self-sustaining oscillation. Measured on a real installation: 14 AC-mode
   flips in 6 minutes with charge never holding, versus 0 flips and steady
   full-power charging with the mode-aware formula.
2. **Split the triggers per regime — one combined trigger goes deaf.** A
   template trigger only fires on a false→true edge. A combined trigger that
   renders true for "charge adjust wanted" and *stays* true when the regime
   flips to "discharge wanted" never fires again: no false→true edge happens,
   because it was already true. Measured on a real installation: a cloud edge
   plus a washing machine pushed the house from exporting to importing 2.3 kW
   while the battery kept charging — for **4 minutes**, until the 5-minute
   backstop finally rescued it. Four per-regime triggers (discharge divergence,
   charge entry, charge adjust, neutral) guarantee every regime change produces
   a fresh false→true edge, and the backstop stays as a last resort.
3. **Order actions so a cancelled run is harmless.** With `mode: restart` a new
   trigger cancels a running sequence mid-flight on a fast-swinging load. Write
   the power limit *before* the mode switch: the worst case then leaves a fresh
   limit sitting in the mode where the device ignores it — never a blind mode
   switch with a stale limit.
4. **The AC relay will click on swingy loads.** A washing machine's element
   cycling under broken clouds swings the house balance by kilowatts in
   seconds, and the assist follows it. That is the intended bidirectional
   behavior; energy loss per flip is seconds of dead time, and the vendor
   firmware tolerates the switching rate. Widen the hysteresis if it bothers
   you.
5. **Keep the device's minimum SOC as the backstop.** The assist floor (5%)
   mirrors the pilot's planning floor; the Zendure's own setting still stops
   the hardware even if every automation is wrong.
6. **Export stays off on a socket install** (`grid_reverse` forbidden); the
   example assumes discharge mode `self_consumption`.
7. **SOC reports integer percent** (1% ≈ 53 Wh on 5.28 kWh) — small charge
   movements are invisible in SOC; verify via power sensors instead.
8. **Keep the idle gate in the branch — and kick the assist when the flag
   flips.** The gate must suppress only the discharge branch, so charge and
   neutral keep working (surplus capture, re-asserting the 0 W hold). Gating
   the branch does leave the *trigger* latched, though: in an idle slot with
   the house importing, the discharge trigger fires once, the neutral branch
   writes 0 W, and the trigger template stays true — and it will still be true
   when the next auto slot clears the flag, because a template trigger only
   fires on a false→true edge and `automation.turn_on` on an already-on
   automation re-arms nothing. Without a kick the battery sits through up to
   five minutes of import (at the evening peak, typically) until the backstop.
   So every script that flips the flag ends with an explicit
   `automation.trigger` of the assist, making the `choose` evaluate the current
   regime immediately. Putting the flag in the *trigger* template instead looks
   tempting — the template is false while idle and fires on the flag clear, so
   it cannot latch — but it would leave the idle hold without its self-healing
   neutral re-assert while the flag is on. Gate in the branch, kick from the
   scripts.
