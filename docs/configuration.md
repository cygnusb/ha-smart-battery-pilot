# Configuration

## Config flow steps

### 1. Price source

| Field | Description |
|---|---|
| Price forecast entity | Entity of your tariff integration holding the price forecast in its attributes. Supported formats are detected automatically: **Nordpool** (`raw_today`/`raw_tomorrow`; `price_in_cents` or ct/öre/cEUR units are scaled to EUR/kWh), **EPEX Spot** (`data` with `price_eur_per_mwh`/`price_ct_per_kwh`), **ENTSO-E** (`prices` with `time`/`price`), **aWATTar** (`marketprice` entries) and plain **hourly arrays** (`today`/`tomorrow` float lists). Arrays may hold 23, 24 or 25 values (96 / 92 / 100 at quarter-hour resolution) — on the two clock-change days a local day is not 24 hours long, and the grid follows the real day. |
| Price offset | Fixed surcharge added to every market price: grid fees, taxes, levies (e.g. `0.187` EUR/kWh in Germany). Self-consumption decisions use the *total* import price you actually pay. Grid export is valued at the feed-in tariff, or at the raw market price when the tariff is `0` — the import surcharge is not treated as export revenue. |
| Feed-in tariff | What you earn per exported kWh. `0` means the market price is used (dynamic feed-in). Only relevant for the export discharge mode. |

If your integration is not recognized, create a template sensor with `today`/
`tomorrow` attributes as plain price arrays — that format is always accepted.

### 2. Battery parameters

| Field | Description |
|---|---|
| State of charge entity | Live SOC in percent, e.g. `sensor.byd_battery_box_premium_hv_ladezustand`. |
| Usable capacity | Battery capacity in kWh. |
| Max charge / discharge power | Inverter limits in W. |
| Min / Max SOC | The plan never leaves this window (e.g. 10–95 %). |
| Roundtrip efficiency | Grid → battery → load efficiency, typically 88–92 %. Losses are priced into every charge decision. |
| Battery charge / discharge energy (optional) | Cumulative kWh or Wh meters. **Both** are required before either `sensor.…_actual_savings` entity reports a value — each one is a net figure (discharge minus charge), which a single meter cannot produce. Unavailable readings are skipped so a glitch cannot inflate the total. Grid charge is priced at the import slot; PV charge in auto/idle is priced at the feed-in tariff (opportunity cost); discharge into the grid during an `export` slot is credited at the feed-in tariff, not at the import price. |

### 3. Control scripts

The integration never talks to your inverter directly — it calls Home
Assistant scripts that you provide. This is what makes it vendor neutral.

| Script | Called when | Receives |
|---|---|---|
| Force charge | a `charge` slot starts | variable `power_w` (planned grid charge power) |
| Block discharge (idle) | an `idle` slot starts | – |
| Auto mode | an `auto` slot starts; also when the integration is disabled, unloaded, the plan becomes invalid, or dry-run is turned on after a live script was applied | – |
| Force discharge to grid (optional) | an `export` slot starts (export mode only) | variable `power_w` (planned discharge power) |

Ready-made scripts for specific hardware: see [examples](examples/).

### 4. Household consumption

| Field | Description |
|---|---|
| Consumption sensor | Total house consumption. Either a **power** sensor in W (recommended, e.g. `sensor.solarnet_leistung_verbrauch`) or an **energy** sensor in kWh with long-term statistics. Used to train the forecast from recorder history. |
| Outdoor temperature (optional) | Adds a heating-demand feature to the model — recommended for heat pump households. |
| Heat pump present | Forces the heating-demand feature in the consumption model as soon as any outdoor-temperature history exists (instead of waiting until half the samples are tagged). |

The forecast model:

* **Day 1+**: weighted hourly profile (weekday/weekend, recent days count more).
* **After ~14 days of history**: ridge regression with cyclic hour-of-day,
  weekend and temperature features (pure Python, no extra dependencies).
* Retrained automatically every 24 h; the model is persisted across restarts.

### 5. PV forecast (optional)

Daily production forecast sensors in kWh (e.g. Open-Meteo Solar Forecast's
`sensor.energy_production_today` / `…_tomorrow`). The daily total is
distributed over the daylight hours — read from `sun.sun`, so a December day
is modelled as the ~8 hours it really is — and subtracted from the
consumption forecast.
Optional current PV power is shown live on the Lovelace card. Without PV,
simply leave the fields empty.

## Options (Settings → Integrations → Configure)

The configure dialog opens a **menu** in which every input from the initial
setup can be reviewed and changed later — each section shows the currently
configured entities/values pre-filled:

* **Optimizer tuning** — spread, discharge mode, training days
* **Price source & offsets** — price entity, price offset, feed-in tariff
* **Battery parameters** — SOC entity, capacity, power limits, SOC window, efficiency
* **Control scripts** — the four action scripts
* **Consumption & temperature** — consumption sensor, temperature, heat pump
* **PV forecast** — daily forecast entities and optional live PV power

Sections return to the menu after submitting; changes are collected and only
persisted via **“💾 Save & close”** (closing the dialog otherwise discards
them). Saving reloads the integration and recomputes the plan.

| Option | Default | Description |
|---|---|---|
| Minimum price spread | 0.20 EUR/kWh | Grid charging only happens if `discharge price > charge price / efficiency + spread`. Raise it to be more conservative (fewer cycles), lower it to arbitrage more aggressively. |
| Discharge mode | Self-consumption | `Self-consumption`: the battery only ever covers the house load. `Export`: additionally force-discharge into the grid during extreme price peaks. **Check your tariff/regulatory situation before enabling export.** |
| Price offset / feed-in tariff | – | Same as in the config flow. Feed-in `0` uses the market price for export. A fixed tariff below the spread (default 0.20 EUR/kWh) will never schedule export — the plan then carries `export_spread_unreachable`. |
| Training days | 60 | History window for the consumption model. |

## Going live

1. After setup the integration is **disabled** with **dry-run on**.
2. Watch `sensor.…_charge_plan` and the log (`DRY RUN: would apply …`).
3. Turn on `switch.…_enabled` — still dry-run, nothing is called yet.
4. When the plan looks sensible, turn off `switch.…_dry_run`.

If anything goes wrong (price entity unavailable, no prices, SOC missing),
the integration calls your *auto mode* script once and stops interfering.
Turning dry-run **on** after a live script was applied also restores auto.

## Dashboard

The bundled card (auto-registered as frontend module *and* lovelace
resource, no manual resource setup needed):

```yaml
type: custom:smart-battery-pilot-card
entity: sensor.smart_battery_pilot_charge_plan  # optional - auto-discovered
title: Smart Battery Pilot                      # optional
```

Features: price step curve with labeled grid, action bands
(charge/idle/export), PV forecast area, live PV power (if a current-PV
entity is configured), projected SOC, local-midnight day
separators with date, "now" marker and a hover tooltip showing time slot,
action, price, SOC forecast, PV and net demand. `entity` may be omitted —
the card auto-discovers the plan sensor (entity IDs are localized, e.g.
`…_ladeplan` on German installations).

The card's labels, tooltip and date/time formatting follow the Home Assistant
user's language (`hass.locale.language`, falling back to the browser language
and finally to English). English and German ship with the card; to add another
language, extend the `TRANSLATIONS` table at the top of
`custom_components/smart_battery_pilot/frontend/smart-battery-pilot-card.js`
with a new entry keyed by the language tag — any key missing from it falls back
to the English string.

Alternative with [ApexCharts Card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header: { show: true, title: Strompreis & Ladeplan }
graph_span: 36h
span: { start: hour }
series:
  - entity: sensor.smart_battery_pilot_charge_plan
    name: Preis
    data_generator: |
      return entity.attributes.slots.map(s => [new Date(s.start), s.price]);
    type: line
  - entity: sensor.smart_battery_pilot_charge_plan
    name: SOC-Prognose
    data_generator: |
      return entity.attributes.slots.map(s => [new Date(s.start), s.soc_forecast]);
    type: line
    yaxis_id: soc
yaxis:
  - id: price
  - id: soc
    opposite: true
    max: 100
```
