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
load, charge from surplus otherwise.

## Architecture

```
                    ┌─ charge slot ──▶ sbp_force_charge ──▶ AC mode input,  input_limit = power_w
pilot ──▶ scripts ──┼─ idle slot ───▶ sbp_block_discharge ▶ AC mode output, output_limit = 0
                    └─ auto slot ───▶ sbp_auto_mode ──────▶ AC mode output + assist automation ON
 assist automation (runs only in auto slots):
   house load > 150 W ──────────▶ output mode, output_limit = min(load, max_w)
   PV surplus > 250 W (stable) ▶ input mode,  input_limit  = min(surplus, max_w)
   otherwise ──────────────────▶ output mode, output_limit = 0
```

The assist is the pilot's "hands" in auto mode only: the charge and idle scripts
switch it off before they command the device, so there is exactly one writer to
the power limits at any moment. The pilot stays the single brain.

## Prerequisites

* A SolarFlow integration exposing at least:
  * `select.…_ac_mode` (`input` / `output`)
  * `number.…_input_limit`, `number.…_output_limit` (W)
  * `sensor.…_electric_level` (SOC %), `sensor.…_output_home_power` (W),
    `sensor.…_grid_input_power` (W, AC charge draw)
* Grid power at the meter: `sensor.dsmr_reading_electricity_currently_delivered`
  / `…_currently_returned` (kW) — any whole-house power sensor works
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

sbp_block_discharge:
  alias: "SBP: Block discharge (idle)"
  sequence:
    - action: automation.turn_off
      target:
        entity_id: automation.assist_discharge_on_import
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

sbp_auto_mode:
  alias: "SBP: Auto mode"
  sequence:
    - action: select.select_option
      target:
        entity_id: select.solarflow_2400_ac_plus_ac_mode
      data:
        option: output
    - action: automation.turn_on
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
more.

## The assist automation

This is the piece that makes an AC-coupled battery behave like a hybrid in
auto slots. It is edge-triggered per grid-meter telegram (a template trigger
that fires when the *desired* device state diverges from the actual one), with
a time-pattern backstop, and it re-derives everything from raw sensor states on
every run, so there is no state to get stale.

Two definitions do all the work:

* `gap` = `delivered − returned + battery output` = **house load − PV**,
  independent of what the battery is currently doing (while discharging).
* `surplus` = PV − house, computed **mode-aware**: in output mode it is simply
  `−gap`; in input mode it is `charge draw − gap`. This matters — see
  [pitfalls](#notes--pitfalls).

```yaml
alias: "Assist: discharge on import, charge on surplus"
mode: restart
triggers:
  - trigger: template
    value_template: >
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
      {% if gap > 150 %}
        {{ mode != 'output' or (([gap, 800] | min | round(0)) - out) | abs > 50 }}
      {% elif soc < 100 and surplus > (100 if mode == 'input' else 250) %}
        {{ mode != 'input' or (([surplus, 800] | min | round(0)) - inl) | abs > 50 }}
      {% else %}
        {{ mode != 'output' or out != 0 }}
      {% endif %}
  - trigger: time_pattern
    minutes: "/5"
conditions: []
actions:
  - choose:
      - conditions:
          - "{{ (states('sensor.dsmr_reading_electricity_currently_delivered') | float(0) * 1000 - states('sensor.dsmr_reading_electricity_currently_returned') | float(0) * 1000 + states('sensor.solarflow_2400_ac_plus_output_home_power') | float(0)) > 150 }}"
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
      - conditions:
          - >
            {% set ret = states('sensor.dsmr_reading_electricity_currently_returned') | float(0) * 1000 %}
            {% set bout = states('sensor.solarflow_2400_ac_plus_output_home_power') | float(0) %}
            {% set bch = states('sensor.solarflow_2400_ac_plus_grid_input_power') | float(0) %}
            {% set gap = states('sensor.dsmr_reading_electricity_currently_delivered') | float(0) * 1000 - ret + bout %}
            {% set surplus = (bch - gap) if is_state('select.solarflow_2400_ac_plus_ac_mode', 'input') else (-gap) %}
            {{ states('sensor.solarflow_2400_ac_plus_electric_level') | float(0) < 100 and surplus > (100 if is_state('select.solarflow_2400_ac_plus_ac_mode', 'input') else 250) }}
        sequence:
          - action: number.set_value
            target:
              entity_id: number.solarflow_2400_ac_plus_input_limit
            data:
              value: >
                {% set ret = states('sensor.dsmr_reading_electricity_currently_returned') | float(0) * 1000 %}
                {% set bout = states('sensor.solarflow_2400_ac_plus_output_home_power') | float(0) %}
                {% set bch = states('sensor.solarflow_2400_ac_plus_grid_input_power') | float(0) %}
                {% set gap = states('sensor.dsmr_reading_electricity_currently_delivered') | float(0) * 1000 - ret + bout %}
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

Tuning: the 150 W discharge floor ignores noise and tiny loads; the 250 W
charge entry / 100 W charge hold pair is the hysteresis that keeps the AC relay
from chattering when surplus hovers near the threshold. The 5% SOC floor
mirrors the pilot's minimum SOC; the device's own minimum-SOC setting stays in
place as the hardware backstop.

## Integration configuration

| Config flow field | Entity / value |
|---|---|
| Price forecast entity | Nordpool-based template sensor (market + markup, incl. VAT) |
| SOC entity | `sensor.solarflow_2400_ac_plus_electric_level` |
| Capacity | 5.28 kWh |
| Max charge / discharge power | 800 / 800 W (socket-limited) |
| Scripts | the four `sbp_*` scripts above |
| Consumption sensor | DSMR-based whole-house load template sensor |
| PV power entity | OpenDTU inverter AC power |

## Verification

* Charge slot: `grid_input_power` tracks `power_w`; the meter's delivered
  power rises by the same amount.
* Auto + surplus: AC mode flips to `input`, `grid_input_power` ≈ surplus, and
  the meter's returned power drops to ≈ 0.
* Auto + load: `output_home_power` ≈ house load (up to the cap), returned
  power ≈ 0, delivered ≈ 0.
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
   800 W charging with the mode-aware formula.
2. **Order actions so a cancelled run is harmless.** With `mode: restart` a new
   trigger cancels a running sequence mid-flight on a fast-swinging load. Write
   the power limit *before* the mode switch: the worst case then leaves a fresh
   limit sitting in the mode where the device ignores it — never a blind mode
   switch with a stale limit.
3. **The AC relay will click on swingy loads.** A washing machine's element
   cycling under broken clouds swings the house balance by kilowatts in
   seconds, and the assist follows it. That is the intended bidirectional
   behavior; energy loss per flip is seconds of dead time, and the vendor
   firmware tolerates the switching rate. Widen the hysteresis if it bothers
   you.
4. **Keep the device's minimum SOC as the backstop.** The assist floor (5%)
   mirrors the pilot's planning floor; the Zendure's own setting still stops
   the hardware even if every automation is wrong.
5. **Export stays off on a socket install** (`grid_reverse` forbidden); the
   example assumes discharge mode `self_consumption`.
6. **SOC reports integer percent** (1% ≈ 53 Wh on 5.28 kWh) — small charge
   movements are invisible in SOC; verify via power sensors instead.
