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

### Modbus registers (Fronius Gen24)

| Register | Name | Description |
|----------|------|-------------|
| 40348 | StorCtl_Mod | **Bitfield** arming the rate limits: bit 0 = InWRte (charge limit), bit 1 = OutWRte (discharge limit). `0` = none, `1` = charge limit, `2` = discharge limit, `3` = both |
| 40350 | Minimum reserve (MinRsvPct) | Minimum SOC buffer (×100), e.g. `500` = 5%, `1000` = 10%, `2000` = 20%, `9900` = 99% (full reserve while force-charging). Below it the Gen24 tops up from the grid at only ~500 W |
| 40355 | Discharge limit (OutWRte) | % of max charge power ×100 (`10000` = 100%, signed). `0` blocks discharging. **Negative** (`65536 - value`) forces charging at that rate. Needs bit 1 (StorCtl_Mod `2`) |
| 40356 | Charge limit (InWRte) | % of max charge power ×100 (`10000` = 100%, signed). `0` blocks charging. **Negative** forces discharging. Needs bit 0 (StorCtl_Mod `1`) |
| 40232 | PV production stop | `0` = stop PV (write 40232 to 0, then 40236 to 1) |
| 40236 | PV production enable | `0` = PV running, `1` = PV blocked |

**Power calculation:** the requested power is divided by the battery's
maximum capacity (the first field of the storage settings block, e.g.
`12800` for a 12.8 kWh battery) and multiplied by 10000:

```
value = (charging_power / max_capacity) * 10000
      = (6000 / 12800) * 10000 ≈ 4687
```

### Battery control scripts

| Script | Function | Modbus actions |
|--------|----------|----------------|
| `script.force_charging` | Force charge at a configurable power | 40355 = −charge rate (`65536 - rate`), 40356 = charge rate, 40350 = 9900, 40348 = 2 |
| `script.force_discharge` | Limit discharging to a configurable power (does **not** force an export) | 40356 = 0, 40355 = discharge rate, 40348 = 2 |
| `script.charge_limit` | Limit charging to a configurable power (without max reserve) | 40356 = charge rate, 40348 = 1 |
| `script.reset_charging` | Back to auto mode with a reserve | 40348 = 0, 40355 = 10000, 40350 = reserve × 100, 40356 = 10000 |
| `script.pv_stop` | Stop PV production | 40232 = 0, 40236 = 1 |
| `script.pv_start` | Start PV production | 40236 = 0 |

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
enough that exporting costs money, and start it again once it recovers:

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

Tibber, aWATTar, EPEX Spot and ENTSO-E work the same way as price sources;
see the README for the full list.

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
