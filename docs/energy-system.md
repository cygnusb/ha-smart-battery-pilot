# Home Assistant energy-system notes

Snapshot: 2026-06-11

House-specific reference for the BYD + Fronius setup this integration was
built against. Entity IDs are from that installation and are left as-is.

---

## 1. BYD Battery-Box Premium HV + Fronius Gen24 control

### Architecture

The BYD HVM (High Voltage Module) battery is **not controlled directly** —
all control goes through the **Fronius Gen24 inverter** via **Modbus TCP**
(HA hub name: `gen24`, slave: 1).

HA 2026.9: the core Fronius integration starts polling the same inverter
over Modbus TCP (port 502) on its own. Keep these YAML scripts as the only
writer. Do not also set the new core number/switch entities (AC power
limit, battery charge/discharge limits, min reserve, grid charging) —
same registers, they would override the SBP plan. Core entities are
limits, not force-charge/force-export (StorCtl_Mod `1`/`2` is not
exposed). Watch Modbus session count on the Gen24 (YAML hub + core +
often EVCC). Prefer core's `Battery charging/discharging energy total`
DC counters for SBP actual-savings inputs.

```
Home Assistant
    └─ modbus.write_register (hub: gen24, slave: 1)
          └─ Fronius Gen24 inverter
                └─ BYD Battery-Box Premium HV (via the internal BYD interface)
```

### Modbus registers (Fronius Gen24)

| Register | Name | Description |
|----------|------|-------------|
| 40348 | StorCTL_Mod | Mode: `0` = auto, `1` = force charge, `2` = force discharge |
| 40350 | Minimum reserve | Minimum SOC buffer (×100), e.g. `500` = 5%, `1000` = 10%, `2000` = 20%, `9900` = 99% (full reserve while force-charging) |
| 40355 | Charge rate (InWRte) | Charge power as per-mille of max capacity (10000 = 100%). **Discharging:** write the rate directly. **Charging:** `65536 - value` (two's complement) |
| 40356 | Discharge rate (OutWRte) | Discharge power as per-mille (10000 = 100%). **While charging:** set `0` to block discharge |
| 40232 | PV production stop | `0` = stop PV (write 40232 to 0, then 40236 to 1) |
| 40236 | PV production enable | `0` = PV running, `1` = PV blocked |

**Power calculation:**
`input_number.charging_power` (default: 6000 W) is divided by max capacity
from `sensor.reading_battery_settings` (first field: `12800`) and multiplied
by 10000:

```
value = (charging_power / max_capacity) * 10000
      = (6000 / 12800) * 10000 ≈ 4687
```

### Battery control scripts

| Script | Function | Modbus actions |
|--------|----------|----------------|
| `script.force_charging` | Force charge at configurable power | 40355 = charge rate (negatively encoded), 40356 = 0, 40350 = 9900, 40348 = 1 |
| `script.force_discharge` | Force discharge | 40356 = 0, 40355 = discharge rate, 40348 = 2 |
| `script.charge_limit` | Charge to a limit (without max reserve) | 40356 = charge rate, 40348 = 1 |
| `script.reset_charging` | Back to auto mode, 5% reserve | 40348 = 0, 40355 = 10000, 40350 = 500, 40356 = 10000 |
| `script.reset_charging_10` | Auto, 10% reserve | 40348 = 0, 40355 = 10000, 40350 = 1000, 40356 = 10000 |
| `script.reset_charging_20` | Auto, 20% reserve | 40348 = 0, 40355 = 10000, 40350 = 2000, 40356 = 10000 |
| `script.pv_stop` | Stop PV production (Gen24 + balcony inverter) | 40232 = 0, 40236 = 1, `button.garage_turn_inverter_off` |
| `script.pv_start` | Start PV production | 40236 = 0, `button.garage_turn_inverter_on` |

### Status sensors (BYD / battery)

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.byd_battery_box_premium_hv_ladezustand` | State of charge (SOC) | % |
| `sensor.byd_battery_box_premium_hv_temperatur` | Battery temperature | °C |
| `sensor.byd_battery_box_premium_hv_spannung_dc` | DC voltage | V |
| `sensor.byd_battery_box_premium_hv_stromstarke_dc` | DC current (negative = charging) | A |
| `sensor.byd_battery_box_premium_hv_maximale_kapazitat` | Maximum capacity | Wh |
| `sensor.byd_storctl_mod` | Current control mode | auto / 1 / 2 |
| `sensor.byd_minrsvpct` | Current min-reserve | % |
| `sensor.byd_outwrte` | Discharge rate | % |
| `sensor.byd_inwrte` | Charge rate | % |
| `sensor.reading_battery_settings` | Raw Modbus settings (comma-separated) | – |
| `sensor.solarnet_ladeleistung` | Current charge power | W |
| `sensor.solarnet_entladeleistung` | Current discharge power | W |

### Automations (negative spot price)

```yaml
# Stop PV when the spot price is below -0.10 EUR/kWh
trigger: sensor.strompreis_zanderweg5 below: -0.1
action: script.pv_stop + set EVCC to PV mode

# Restart PV when the price is >= -0.10 EUR/kWh
trigger: sensor.strompreis_zanderweg5 above: -0.101
action: script.pv_start + EVCC off + battery reset
```

---

## 2. Dynamic tariff — Nordpool & Tibber

### Nordpool (active, primary source)

**Integration:** `nordpool` HACS custom component
**Main entity:** `sensor.nordpool_kwh_ger_eur_3_10_019`

- Config: region = GER, currency = EUR, 3% VAT, 10% surcharge, 0.019 base price
- Value: current spot hourly price in EUR/kWh (net market price)
- Update: hourly; tomorrow's prices available from ~14:00

**Attributes with price lists:**

```python
# Via REST API:
GET /api/states/sensor.nordpool_kwh_ger_eur_3_10_019

# Attributes:
{
  "today": [0.175, 0.168, ...],      # 96 values = 15-minute intervals
  "tomorrow": [0.12, 0.13, ...],     # 96 values (available from ~14:00)
  "tomorrow_valid": true/false,      # whether tomorrow prices are in
  "raw_today": [
    {"start": "2026-06-11T00:00:00+02:00", "end": "2026-06-11T00:15:00+02:00", "value": 0.175},
    ...
  ],
  "raw_tomorrow": [...],             # same structure for tomorrow
  "current_price": 0.167,
  "average": 0.133,
  "min": 0.036,
  "max": 0.223
}
```

> **Note:** `today` / `tomorrow` contain **96 values** (15-minute intervals, not
> 24 hourly values). Nordpool prices are hourly — the same price is repeated
> four times.

**Derived total electricity price:**
`sensor.strompreis_zanderweg5` = Nordpool value + 0.187 EUR/kWh (grid fees + taxes + levies)

```yaml
# template.yaml
- name: "Strompreis Zanderweg5"
  state: >
    {{ (states('sensor.nordpool_kwh_ger_eur_3_10_019') | float(0) + 0.187) | round(4) }}
  unit_of_measurement: "EUR/kWh"
```

**Fetching the next 1–2 days (15-minute intervals):**

```python
import requests

HASS_URL = "https://ha.valerius.email"
TOKEN = "<bearer_token>"
headers = {"Authorization": f"Bearer {TOKEN}"}

r = requests.get(f"{HASS_URL}/api/states/sensor.nordpool_kwh_ger_eur_3_10_019", headers=headers)
attrs = r.json()["attributes"]

# Today (96 × 15-minute slots):
today_prices = attrs["raw_today"]   # list of {start, end, value}

# Tomorrow (available from ~14:00):
if attrs.get("tomorrow_valid"):
    tomorrow_prices = attrs["raw_tomorrow"]

# Total electricity price (including grid fees):
total_prices_today = [
    {"start": p["start"], "end": p["end"], "price_eur_kwh": round(p["value"] + 0.187, 4)}
    for p in today_prices
]
```

### Tibber (partially active)

**Status:** Tibber Pulse hardware is installed (`update.tibber_pulse_local_update: on`),
but most Tibber API price sensors are `unavailable`.

| Entity | Status | Description |
|--------|--------|-------------|
| `sensor.electricity_price_zander` | active (0.353 EUR/kWh) | Tibber price (similar to Nordpool + surcharge) |
| `sensor.monthly_cost_zander` | active (52.69 EUR) | Monthly cost |
| `sensor.monthly_net_consumption_zander` | active (172.31 kWh) | Monthly net consumption |
| `sensor.accumulated_consumption_zander` | unavailable | Accumulated daily consumption |
| `sensor.electricity_price_prognose_zanderweg5` | unavailable | Price forecast |
| `sensor.electricity_price_max/min/avg_zanderweg5` | unavailable | Daily statistics |

**Takeaway:** For a 1–2 day price forecast, **Nordpool is the reliable source**.
The Tibber API price sensors are currently not usable.

---

## 3. Household consumption

### Real-time power (recommended)

**Primary source:** Fronius SolarNet (via the `fronius` integration)

| Entity | Description | Unit | Update |
|--------|-------------|------|--------|
| `sensor.solarnet_leistung_verbrauch` | Total house consumption (live) | W | ~30 s |
| `sensor.solarnet_leistung_netzbezug` | Power currently drawn from the grid | W | ~30 s |
| `sensor.solarnet_leistung_netzeinspeisung` | Power currently exported to the grid | W | ~30 s |
| `sensor.smart_meter_ts_65a_3_wirkleistung` | Grid power (Tibber Pulse) | W | live |

**Sanity-check formula:**
```
house load = PV power + discharge power + grid import − grid export − charge power
           = sensor.solarnet_leistung_verbrauch  ← computed directly by Fronius
```

### Daily / hourly energy (kWh)

| Entity | Period | Source | Description |
|--------|--------|--------|-------------|
| `sensor.verbrauch_tagesverbrauch` | day | Netze BW portal | Daily grid import (kWh) |
| `sensor.verbrauch_stundenverbrauch` | hour | Netze BW portal | Hourly consumption (kWh) |
| `sensor.verbrauch_15_minuten_verbrauch` | 15 min | Netze BW portal | 15-minute consumption (kWh) |
| `sensor.solarnet_netzbezug_tag` | day | SolarNet | Grid import today (kWh) |
| `sensor.solarnet_netzbezug_stunde` | hour | SolarNet | Grid import last hour (kWh) |
| `sensor.solarnet_netzbezug_15_min` | 15 min | SolarNet | Grid import last 15 min (kWh) |
| `sensor.solarnet_netzbezug_monat` | month | SolarNet | Grid import this month (kWh) |
| `sensor.solarnet_netzbezug_jahr` | year | SolarNet | Grid import this year (kWh) |
| `sensor.monthly_net_consumption_zander` | month | Tibber | Monthly net consumption (kWh) |

> **Note:** `sensor.verbrauch_tagesverbrauch` is **grid import only** (Netze BW
> meter). Actual house load = grid import + PV self-consumption. For total
> household consumption, `sensor.solarnet_leistung_verbrauch` is the better
> source (integrate via the statistics API).

### Historical data via the statistics API

```python
# Hourly grid-import values (last day):
POST /api/recorder/statistics_during_period
{
  "start_time": "2026-06-10T00:00:00Z",
  "end_time": "2026-06-11T00:00:00Z",
  "statistic_ids": ["sensor.smart_meter_ts_65a_3_bezogene_wirkenergie"],
  "period": "hour"
}

# Daily values over weeks:
{
  "period": "day",
  "statistic_ids": ["sensor.solarnet_netzbezug_energie"]
}
```

---

## 4. PV production

### Real-time power

| Entity | Description | Unit |
|--------|-------------|------|
| `sensor.solarnet_pv_leistung` | Total PV power (SolarNet) | W |
| `sensor.solarproduktion_dach_ost_leistung` | Fronius Gen24 AC power (east roof) | W |
| `sensor.solarproduktion_leistung_gesamt` | Total including balcony PV | W |
| `sensor.pv_valerius_stromstarke_ac` | Inverter AC current | A |
| `sensor.pv_valerius_spannung_dc` | DC voltage string 1 | V |
| `sensor.pv_valerius_dc_spannung_2` | DC voltage string 2 | V |
| `sensor.pv_valerius_wechselrichterstatus` | Inverter status (Running / …) | – |

### Energy (kWh)

| Entity | Period | Description |
|--------|--------|-------------|
| `sensor.solarenergie_gesamt` | lifetime | Total PV production since commissioning |
| `sensor.solarenergie_jahr` | year | PV production this year (kWh) |
| `sensor.solarenergie_monat` | month | PV production this month (kWh) |
| `sensor.solarenergie_dach_ostseite` | lifetime | East roof (kWh) |
| `sensor.solarenergie_dach_westseite` | lifetime | West roof (kWh) |
| `sensor.fronius_energie_gesamt` | lifetime | Fronius Gen24 total energy (Wh) |
| `sensor.solarnet_netzeinspeisung_tag` | day | Export today (kWh) |
| `sensor.solarnet_netzeinspeisung_monat` | month | Export this month (kWh) |
| `sensor.solarnet_netzeinspeisung_jahr` | year | Export this year (kWh) |
| `sensor.einspeisung_stundenverbrauch` | hour | Export last hour (kWh) |
| `sensor.einspeisung_tagesverbrauch` | day | Export today total (kWh) |
| `sensor.solarnet_autarkiegrad` | live | Self-sufficiency (%) |

### PV forecast (Open-Meteo Solar Forecast)

| Entity | Description |
|--------|-------------|
| `sensor.vorhersage_pv_produktion_heute` | Forecasted PV today (kWh) |
| `sensor.vorhersage_pv_produktion_morgen` | Forecasted PV tomorrow (kWh) |
| `sensor.vorhersage_pv_produktion_tage_2` | Day after tomorrow (kWh) |
| `sensor.vorhersage_pv_produktion_tage_3` | In 3 days (kWh) |
| `sensor.energy_current_hour_west` | West roof — this hour (kWh) |
| `sensor.energy_next_hour_west` | West roof — next hour (kWh) |
| `sensor.energy_current_hour_bkw` | Balcony PV — this hour (kWh) |
| `sensor.energy_production_today_west` | West roof today (kWh) |
| `sensor.energy_production_tomorrow_west` | West roof tomorrow (kWh) |
| `sensor.vorhersage_solarproduktion_gesamt_heute` | Whole plant today (kWh) |
| `sensor.vorhersage_solarproduktion_gesamt_morgen` | Whole plant tomorrow (kWh) |

### Hourly / daily PV data via the statistics API

```python
# Hourly production (via long-term statistics):
POST /api/recorder/statistics_during_period
{
  "start_time": "2026-06-10T00:00:00Z",
  "end_time": "2026-06-11T00:00:00Z",
  "statistic_ids": ["sensor.solarenergie_gesamt"],
  "period": "hour",
  "types": ["change"]  # delta per hour
}

# For daily values:
{
  "period": "day",
  "types": ["change"]
}
```

---

## 5. Weather forecast

### Active weather sources

| Entity | Integration | Current | Forecast |
|--------|-------------|---------|----------|
| `weather.forecast_home` | Meteorologis / HA | yes | daily yes |
| `weather.eggstoi` | unknown source | yes | no |

### Current weather values

```python
GET /api/states/weather.forecast_home
# Attributes: temperature, humidity, wind_speed, wind_bearing, pressure, dew_point, visibility

GET /api/states/weather.eggstoi
# temperature, wind_speed, wind_bearing (no humidity)
```

### Fetching the daily forecast

```python
# Daily forecast (coming days):
POST /api/services/weather/get_forecasts?return_response
{
  "entity_id": "weather.forecast_home",
  "type": "daily"
}

# Response:
{
  "weather.forecast_home": {
    "forecast": [
      {
        "datetime": "2026-06-11T12:00:00+00:00",
        "temperature": 18.4,    # daily high
        "templow": 10.1,        # daily low
        "precipitation": 0.6,   # precipitation mm
        "condition": "partlycloudy",
        "wind_speed": 12.0,
        "humidity": 70
      },
      ...
    ]
  }
}
```

> **Note:** Hourly forecast (`type: "hourly"`) currently returns no data for
> `weather.forecast_home`. Solar-forecast sensors are a better proxy for
> hourly temperatures.

### Template sensors (derived, always current)

| Entity | Source | Description |
|--------|--------|-------------|
| `sensor.aussen_temperatur` | local sensors + eggstoi | Best available outdoor temperature (°C) |
| `sensor.si_temperatur` | weather.forecast_home | Current temperature from the weather service |
| `sensor.si_temperatur_max` | daily forecast | Today's max temperature (°C) |
| `sensor.si_temperatur_min` | daily forecast | Today's min temperature (°C) |
| `sensor.si_niederschlag_prognose` | daily forecast | Today's precipitation (mm) |
| `sensor.si_luftfeuchtigkeit` | weather.forecast_home | Humidity (%) |
| `sensor.si_luftdruck` | weather.forecast_home | Pressure (hPa) |
| `sensor.si_taupunkt` | weather.forecast_home | Dew point (°C) |
| `sensor.si_windgeschwindigkeit` | weather.forecast_home | Wind speed (km/h) |

> The SI trigger sensors (`si_temperatur_max`, `si_temperatur_min`,
> `si_niederschlag_prognose`) are updated daily at 01:00 and on HA start via
> `weather.get_forecasts`.

---

## 6. Summary — key entities for energy optimisation

```python
# Current electricity price (total, including grid fees):
sensor.strompreis_zanderweg5           # EUR/kWh

# Prices for the next 1–2 days (15-minute slots in attributes):
sensor.nordpool_kwh_ger_eur_3_10_019   # raw_today, raw_tomorrow

# Battery state:
sensor.byd_battery_box_premium_hv_ladezustand  # SOC %
sensor.byd_storctl_mod                          # auto / 1 / 2
sensor.solarnet_ladeleistung                    # charge W
sensor.solarnet_entladeleistung                 # discharge W

# PV now:
sensor.solarproduktion_leistung_gesamt  # W total
sensor.vorhersage_pv_produktion_morgen  # kWh forecast tomorrow

# House load now:
sensor.solarnet_leistung_verbrauch      # W (live)
sensor.solarnet_leistung_netzbezug      # W (grid import live)

# Historical energy:
sensor.solarnet_netzbezug_tag           # kWh today
sensor.solarnet_netzeinspeisung_tag     # kWh exported today
sensor.solarenergie_monat               # kWh PV this month

# Weather:
sensor.si_temperatur_max                # today's high
sensor.si_temperatur_min                # today's low
sensor.aussen_temperatur                # current outdoor temperature
```
