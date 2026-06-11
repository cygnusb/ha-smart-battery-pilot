# Smart Battery Pilot

<img src="assets/logo.svg" alt="Smart Battery Pilot" width="160" align="right"/>

**Charge your home battery when electricity is cheap — use it when it's expensive.**

Smart Battery Pilot is a Home Assistant integration for households with a home
battery and a dynamic electricity tariff (Nordpool, EPEX Spot, Tibber, aWATTar,
ENTSO-E, …). It learns your household's consumption profile, combines it with
the price forecast for the next 24–36 hours and your PV production forecast,
and computes an optimal charge/discharge plan — especially valuable during
dark winter weeks ("Dunkelflaute") when prices swing heavily within a day.

The integration is **vendor neutral**: it controls your battery through Home
Assistant scripts that *you* define, so it works with any inverter/battery
combination that can be controlled from Home Assistant (Fronius, Victron, SMA,
Sungrow, E3DC, …). See [docs/examples](docs/examples/) for ready-to-use
vendor configurations.

## How it works

1. **Price forecast** — the price entity of your existing tariff integration is
   parsed automatically (15-minute or hourly slots, today + tomorrow).
2. **Consumption forecast** — a lightweight model (pure Python, no heavy ML
   dependencies) is trained daily from your Home Assistant history: hour of
   day, weekday/weekend and optionally outdoor temperature (heat pump aware).
3. **PV forecast** — optional; expected solar production is subtracted so the
   plan only covers the *net* grid demand, and forecasted PV surplus is
   modeled as battery charging in the SOC simulation — so on sunny days the
   planner doesn't lock the battery needlessly.
4. **Optimization** — a deterministic planner pairs the cheapest charge slots
   with the most expensive consumption slots, respecting battery capacity,
   power limits, SOC limits and roundtrip efficiency. Charging only happens if
   the price spread exceeds your configured threshold (default 0.20 EUR/kWh).
5. **Execution** — at every slot boundary the integration calls your scripts:
   *force charge*, *block discharge (idle)*, *auto mode* or optionally
   *export to grid*.

### Actions

| Action   | Meaning                                                        |
|----------|----------------------------------------------------------------|
| `charge` | Force-charge from the grid at the planned power                |
| `auto`   | Inverter auto mode — battery covers household consumption      |
| `idle`   | Discharging blocked — preserve energy for more expensive hours |
| `export` | Force-discharge into the grid (optional arbitrage mode)        |

## Installation (HACS)

1. HACS → Integrations → ⋮ → *Custom repositories* →
   `https://github.com/cygnusb/ha-smart-battery-pilot` (category: Integration)
2. Install **Smart Battery Pilot** and restart Home Assistant.
3. Settings → Devices & Services → *Add Integration* → **Smart Battery Pilot**.

## Configuration

The config flow guides you through five steps: price source, battery
parameters, control scripts, consumption sensor and (optional) PV forecast.
Details and the full option reference: [docs/configuration.md](docs/configuration.md).

> **Safety first:** the integration starts **disabled** and in **dry-run**
> mode. Watch the planned actions in the log and the plan sensor for a day or
> two, then turn off dry-run and flip the master switch.

## Entities

| Entity                                   | Description                                  |
|------------------------------------------|----------------------------------------------|
| `sensor.…_current_action`                | Action prescribed right now                  |
| `sensor.…_next_action`                   | Next action change (+ time, price)           |
| `sensor.…_charge_plan`                   | Full plan as `slots` attribute               |
| `sensor.…_estimated_savings`             | Estimated savings over the plan horizon      |
| `sensor.…_consumption_forecast_24h`      | Learned consumption forecast                 |
| `switch.…_enabled`                       | Master switch                                |
| `switch.…_dry_run`                       | Plan only, don't call scripts                |
| `binary_sensor.…_plan_problem`           | On when no valid plan exists                 |

> Entity IDs are generated from the **localized** entity names — on a German
> installation the plan sensor is `sensor.smart_battery_pilot_ladeplan`, on
> an English one `sensor.smart_battery_pilot_charge_plan`. The UI languages
> shipped are English and German.

Service: `smart_battery_pilot.replan` — recompute the plan immediately.

## Dashboard card

The integration ships its own Lovelace card (registered automatically as a
frontend module and lovelace resource):

```yaml
type: custom:smart-battery-pilot-card
entity: sensor.smart_battery_pilot_charge_plan   # optional - auto-discovered
```

It shows the price curve with a labeled price grid, the planned actions as
colored bands, the PV forecast, the projected SOC, day separators and a
hover tooltip with price/SOC/action/PV per slot. If `entity` is omitted or
wrong, the card finds the plan sensor automatically.
See [docs/configuration.md](docs/configuration.md#dashboard).

## Vendor examples

* [BYD Battery-Box Premium HVM + Fronius Gen24 (Modbus TCP)](docs/examples/byd-fronius-gen24.md)
* [Add your setup](docs/examples/TEMPLATE.md) — PRs welcome!

## How the optimizer decides

See [docs/optimizer.md](docs/optimizer.md) for the algorithm, the savings
estimate and worked examples.

## License

MIT
