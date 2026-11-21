# Energy-system reference: BYD + Fronius Gen24

A worked reference for a typical installation: a BYD Battery-Box Premium HV
behind a Fronius Gen24 hybrid inverter, a dynamic tariff from Nordpool, PV on
two roof faces and an outdoor temperature sensor. It shows which signals the
pilot needs and where they usually come from. All entity IDs are examples;
yours will differ. Ready-to-use scripts for this hardware are in
[examples/byd-fronius-gen24.md](examples/byd-fronius-gen24.md).

---

## 1. Battery control through the Gen24

### Architecture

The BYD battery is **not controlled directly**. All control goes through the
**Fronius Gen24 inverter** over **Modbus TCP** (an HA `modbus` hub, here
named `gen24`, slave 1).

Since HA 2026.9 the core Fronius integration polls the same inverter over
Modbus TCP (port 502) on its own. Keep the YAML scripts as the only writer
and do not also set the core number/switch entities (AC power limit, battery
charge/discharge limits, min reserve, grid charging): they write the same
registers and would override the plan. They are limits, not force-charge or
force-export (the negative OutWRte/InWRte rates are not exposed). Keep an eye
on the number of Modbus sessions on the Gen24 (YAML hub, core integration and
often evcc). Core's `Battery charging/discharging energy total` DC counters
make good energy meters for the pilot's savings figures.

```
Home Assistant
    └─ modbus.write_register (hub: gen24, slave: 1)
          └─ Fronius Gen24 inverter
                └─ BYD Battery-Box Premium HV (via the internal BYD interface)
```

### Registers and scripts

The Modbus registers (StorCtl_Mod, MinRsvPct, OutWRte, InWRte, the revert
timeout), how a power in W becomes a rate, and the four control scripts the
pilot calls are in the
[BYD + Fronius Gen24 example](examples/byd-fronius-gen24.md#relevant-modbus-registers-fronius-gen24-slave-1).
Those scripts are the tested ones; use them rather than writing the registers
from scratch. PV production can be stopped and started through registers
40232 and 40236 (see [negative prices](#negative-prices) below).

### Status sensors

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.battery_soc` | State of charge (SOC) | % |
| `sensor.battery_temperature` | Battery temperature | °C |
| `sensor.battery_storctl_mod` | Armed rate limits (bitfield) | 0 / 1 / 2 / 3 |
| `sensor.battery_min_reserve` | Current min-reserve | % |
| `sensor.battery_outwrte` | Discharge rate | % |
| `sensor.battery_inwrte` | Charge rate | % |
| `sensor.battery_charge_power` | Current charge power | W |
| `sensor.battery_discharge_power` | Current discharge power | W |

### Negative prices

Many installations stop PV production while the spot price is negative
enough that exporting costs money, and start it again once it recovers.
`script.pv_stop` writes `0` to 40232 and then `1` to 40236; `script.pv_start`
writes `0` to 40236:

```yaml
# Stop PV below -0.10 EUR/kWh, start again at or above it
trigger: sensor.electricity_price_total below: -0.1
action: script.pv_stop

trigger: sensor.electricity_price_total above: -0.101
action: script.pv_start
```

---

## 2. Dynamic tariff — Nordpool

**Integration:** `nordpool` HACS custom component, one sensor per region,
currency and price settings, e.g. `sensor.nordpool_kwh_de_eur_3_10_0`.

- Value: the current spot price in EUR/kWh
- Tomorrow's prices arrive around 14:00

**Attributes with price lists:**

```python
{
  "today": [0.175, 0.168, ...],      # 96 values = 15-minute intervals
  "tomorrow": [0.12, 0.13, ...],     # 96 values (available from ~14:00)
  "tomorrow_valid": true/false,      # whether tomorrow's prices are in
  "raw_today": [
    {"start": "2026-06-11T00:00:00+02:00", "end": "2026-06-11T00:15:00+02:00", "value": 0.175},
    ...
  ],
  "raw_tomorrow": [...],             # same structure for tomorrow
  "current_price": 0.167
}
```

> **Note:** `today` / `tomorrow` may contain **96 values** (15-minute
> intervals) even where the market still prices whole hours; the same price
> is then repeated four times. The pilot reads both resolutions.

**What you really pay:** the spot price plus grid fees, taxes and levies.
Enter that markup as the pilot's *price offset* (e.g. 0.20 EUR/kWh), or build
a template sensor for automations:

```yaml
# template.yaml
- name: "Electricity price total"
  unique_id: electricity_price_total
  state: >
    {{ (states('sensor.nordpool_kwh_de_eur_3_10_0') | float(0) + 0.20) | round(4) }}
  unit_of_measurement: "EUR/kWh"
```

aWATTar, EPEX Spot and ENTSO-E integrations work the same way as price
sources; see [price source](configuration.md#1-price-source) for the formats.
The official Tibber and Nord Pool integrations only offer their curve through
an action and need a [price bridge](price-bridges.md).

---

## 3. Household consumption

The pilot learns the household's consumption from the long-term statistics of
one sensor. A **power sensor of the whole house load** is the best choice:

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.house_load_power` | Total house consumption (Fronius computes it from PV, battery and grid) | W |
| `sensor.grid_import_power` | Power currently drawn from the grid | W |
| `sensor.grid_export_power` | Power currently exported to the grid | W |

```
house load = PV power + discharge power + grid import − grid export − charge power
```

A grid-import meter alone (e.g. from the grid operator's portal) is **not** the
house load: it misses everything PV and the battery cover. If the house load
includes a wallbox, set the wallbox power sensor in the pilot's EV section so
the car is taken out of the training data ([EV charging](ev-charging.md)).

The statistics behind it can be checked through the recorder API:

```python
POST /api/recorder/statistics_during_period
{
  "start_time": "2026-06-10T00:00:00Z",
  "end_time": "2026-06-11T00:00:00Z",
  "statistic_ids": ["sensor.house_load_power"],
  "period": "hour"
}
```

---

## 4. PV production

### Power and energy

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.pv_power` | Total PV power (shown live on the card) | W |
| `sensor.pv_energy_total` | Total PV production since commissioning | kWh |
| `sensor.grid_export_energy_today` | Export today | kWh |

### PV forecast

Any forecast integration with a "today" and a "tomorrow" energy sensor works,
e.g. Forecast.Solar or Open-Meteo Solar Forecast. With several roof faces,
add them up in a template sensor first.

If the integration also provides the Energy dashboard's solar forecast, select
its config entries (one per roof face) as the hourly forecast as well: the
planner then follows the real shape of each day instead of an even curve. See
[PV forecast](configuration.md#5-pv-forecast-optional).

| Entity | Description |
|--------|-------------|
| `sensor.pv_forecast_today` | Forecasted PV today (kWh) |
| `sensor.pv_forecast_tomorrow` | Forecasted PV tomorrow (kWh) |

---

## 5. Outdoor temperature

Optional, and most useful with a heat pump: the consumption model then learns
how the load follows the temperature. Any temperature sensor works; a local
sensor beats a weather service.

| Entity | Description |
|--------|-------------|
| `sensor.outdoor_temperature` | Current outdoor temperature (°C) |

Weather entities give daily forecasts through `weather.get_forecasts`
(`type: daily`), which some template sensors use for a daily high and low.

---

## 6. Summary — what the pilot is configured with

```python
# Prices (attribute lists for today and tomorrow):
sensor.nordpool_kwh_de_eur_3_10_0      # plus the price offset in the options

# Battery:
sensor.battery_soc                     # SOC %
sensor.battery_temperature             # optional, for cold-weather charging
script.sbp_force_charge / sbp_block_discharge / sbp_auto_mode / sbp_force_discharge

# Consumption and PV:
sensor.house_load_power                # W, long-term statistics
sensor.pv_forecast_today / sensor.pv_forecast_tomorrow
sensor.pv_power                        # optional, live PV on the card

# Temperature:
sensor.outdoor_temperature             # optional
```
