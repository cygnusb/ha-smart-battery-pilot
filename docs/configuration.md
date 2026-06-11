# Configuration

## Config flow steps

### 1. Price source

| Field | Description |
|---|---|
| Price forecast entity | Entity of your tariff integration holding the price forecast in its attributes. Supported formats are detected automatically: **Nordpool** (`raw_today`/`raw_tomorrow`), **EPEX Spot** (`data` with `price_eur_per_mwh`/`price_ct_per_kwh`), **ENTSO-E** (`prices` with `time`/`price`), **aWATTar** (`marketprice` entries) and plain **hourly arrays** (`today`/`tomorrow` float lists, 24 or 96 values). |
| Price offset | Fixed surcharge added to every market price: grid fees, taxes, levies (e.g. `0.187` EUR/kWh in Germany). The optimizer always works with the *total* price you actually pay. |
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

### 3. Control scripts

The integration never talks to your inverter directly — it calls Home
Assistant scripts that you provide. This is what makes it vendor neutral.

| Script | Called when | Receives |
|---|---|---|
| Force charge | a `charge` slot starts | variable `power_w` (planned grid charge power) |
| Block discharge (idle) | an `idle` slot starts | – |
| Auto mode | an `auto` slot starts, the integration is disabled, unloaded, or the plan becomes invalid | – |
| Force discharge to grid (optional) | an `export` slot starts (export mode only) | – |

Ready-made scripts for specific hardware: see [examples](examples/).

### 4. Household consumption

| Field | Description |
|---|---|
| Consumption sensor | Total house consumption. Either a **power** sensor in W (recommended, e.g. `sensor.solarnet_leistung_verbrauch`) or an **energy** sensor in kWh with long-term statistics. Used to train the forecast from recorder history. |
| Outdoor temperature (optional) | Adds a heating-demand feature to the model — recommended for heat pump households. |
| Heat pump present | Marks the household as temperature sensitive. |

The forecast model:

* **Day 1+**: weighted hourly profile (weekday/weekend, recent days count more).
* **After ~14 days of history**: ridge regression with cyclic hour-of-day,
  weekend and temperature features (pure Python, no extra dependencies).
* Retrained automatically every 24 h; the model is persisted across restarts.

### 5. PV forecast (optional)

Daily production forecast sensors in kWh (e.g. Open-Meteo Solar Forecast's
`sensor.energy_production_today` / `…_tomorrow`). The daily total is
distributed over daylight hours and subtracted from the consumption forecast.
Without PV, simply leave the fields empty.

## Options (Settings → Integrations → Configure)

| Option | Default | Description |
|---|---|---|
| Minimum price spread | 0.20 EUR/kWh | Grid charging only happens if `discharge price > charge price / efficiency + spread`. Raise it to be more conservative (fewer cycles), lower it to arbitrage more aggressively. |
| Discharge mode | Self-consumption | `Self-consumption`: the battery only ever covers the house load. `Export`: additionally force-discharge into the grid during extreme price peaks. **Check your tariff/regulatory situation before enabling export.** |
| Price offset / feed-in tariff | – | Same as in the config flow. |
| Training days | 60 | History window for the consumption model. |

## Going live

1. After setup the integration is **disabled** with **dry-run on**.
2. Watch `sensor.…_charge_plan` and the log (`DRY RUN: would apply …`).
3. Turn on `switch.…_enabled` — still dry-run, nothing is called yet.
4. When the plan looks sensible, turn off `switch.…_dry_run`.

If anything goes wrong (sensors unavailable, no prices), the integration
calls your *auto mode* script once and stops interfering.

## Dashboard

The bundled card (auto-registered, no resource setup needed):

```yaml
type: custom:smart-battery-pilot-card
entity: sensor.smart_battery_pilot_charge_plan
soc_entity: sensor.byd_battery_box_premium_hv_ladezustand
```

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
