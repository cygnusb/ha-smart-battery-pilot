# BYD Battery-Box Premium HVM + Fronius Gen24 (Modbus TCP)

Reference setup: BYD Battery-Box Premium HVM (12.8 kWh) controlled through a
Fronius Gen24 inverter via Modbus TCP, Nordpool prices, Fronius SolarNet
consumption sensor, Open-Meteo Solar Forecast.

## Architecture

The BYD battery is **not** controlled directly — all control goes through the
Fronius Gen24 via Modbus TCP (sunspec storage model):

```
Home Assistant ── modbus.write_register (hub: gen24, slave: 1)
   └─ Fronius Gen24 ── internal BYD interface ── BYD Battery-Box Premium HVM
```

## Modbus hub (configuration.yaml)

```yaml
modbus:
  - name: gen24
    type: tcp
    host: <inverter-ip>
    port: 502
```

## Home Assistant 2026.9 (Fronius Modbus in core)

From 2026.9 the built-in **Fronius** integration (Solar API) also talks
Modbus TCP (SunSpec) when the inverter has it enabled. Existing config
entries are migrated to port 502 automatically — no extra setup.

Keep the YAML hub and the scripts below. Do **not** drive the new core
number/switch entities (`AC power limit`, `Battery charge/discharge power
limit`, `Battery minimum reserve`, `Battery grid charging`) in parallel:
they write the same SunSpec registers (40232/40236, 40348–40356, 40360)
and will fight the SBP plan. Reading them is fine; they catch up on the
next Fronius poll (~1 min).

The core entities are **limits and permissions**, not force-charge /
force-export. StorCtl_Mod `1`/`2` and the two's-complement InWRte encoding
are not exposed, so export in particular cannot be replaced 1:1.

The Gen24 accepts only a handful of simultaneous Modbus TCP sessions
(YAML hub, core Fronius, often EVCC). After upgrading, check that
`modbus.write_register` still succeeds. Core claims it shares the
connection when host+port match; YAML hubs historically opened their own
socket — treat a second session as possible until proven otherwise.

`Battery charging energy total` / `Battery discharging energy total` from
core Fronius are DC counters on the device (no Riemann, survive restarts)
and are a better pair for SBP's optional actual-savings inputs than
integrating power.

## Relevant Modbus registers (Fronius Gen24, slave 1)

| Register | Name        | Description |
|----------|-------------|-------------|
| 40348    | StorCtl_Mod | Mode: `0` auto, `1` force charge, `2` force discharge |
| 40350    | MinRsvPct   | Minimum SOC reserve ×100 (`500` = 5 %, `9900` = 99 % → keeps battery charged) |
| 40355    | InWRte      | Charge rate, ‰ of max power. When **charging**: `65536 − value` (two's complement) |
| 40356    | OutWRte     | Discharge rate, ‰ of max power. Set `0` while charging to block discharge |

## Scripts (scripts.yaml)

The charge and export scripts receive `power_w` from Smart Battery Pilot and
convert it to the Gen24's per-mille encoding. `max_power` is the value WChaMax
(e.g. `12800` from register 40346 / `sensor.reading_battery_settings`).

```yaml
sbp_force_charge:
  alias: "SBP: Laden erzwingen"
  fields:
    power_w:
      description: Charge power in W (provided by Smart Battery Pilot)
      default: 6000
  variables:
    max_power: 12800
    rate: "{{ ((power_w | default(6000)) / max_power * 10000) | int }}"
  sequence:
    # Block discharging
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: 0 }
    # Charge rate, two's complement encoding for charging
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: "{{ 65536 - rate }}" }
    # Keep reserve at 99% so the inverter charges up
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40350, value: 9900 }
    # Force charge mode
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 1 }

sbp_block_discharge:
  alias: "SBP: Entladen sperren (Idle)"
  sequence:
    # Auto mode + discharge blocked: PV may still charge, grid-charge from a
    # previous `charge` slot must not continue. Always rewrite MinRsvPct so a
    # preceding 99 % reserve cannot leak into idle hours.
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 0 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: 0 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: 10000 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40350, value: 500 }   # 5% reserve

sbp_auto_mode:
  alias: "SBP: Auto-Modus"
  sequence:
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 0 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: 10000 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40350, value: 500 }   # 5% reserve
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: 10000 }

sbp_force_discharge:
  alias: "SBP: Entladen ins Netz erzwingen (Export-Modus)"
  fields:
    power_w:
      description: Discharge power in W (provided by Smart Battery Pilot)
      default: 6000
  variables:
    max_power: 12800
    rate: "{{ [((power_w | default(6000)) / max_power * 10000) | int, 10000] | min }}"
  sequence:
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: "{{ rate }}" }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 2 }
```

## Integration configuration

| Config flow field | Entity / value |
|---|---|
| Price forecast entity | `sensor.nordpool_kwh_ger_eur_3_10_019` (Nordpool HACS) |
| Price offset | `0.187` EUR/kWh (German grid fees + taxes) |
| SOC entity | `sensor.byd_battery_box_premium_hv_ladezustand` |
| Capacity | `12.8` kWh |
| Max charge / discharge power | `6000` / `6000` W |
| Min / Max SOC | `10` / `95` % |
| Efficiency | `90` % |
| Script: force charge | `script.sbp_force_charge` |
| Script: idle | `script.sbp_block_discharge` |
| Script: auto | `script.sbp_auto_mode` |
| Script: export (optional) | `script.sbp_force_discharge` |
| Consumption sensor | `sensor.solarnet_leistung_verbrauch` (W, Fronius SolarNet) |
| Temperature sensor | `sensor.aussen_temperatur` |
| PV forecast today / tomorrow | `sensor.vorhersage_solarproduktion_gesamt_heute` / `…_morgen` (Open-Meteo Solar Forecast) |
| Current PV power (optional) | e.g. `sensor.solarnet_leistung_produktion` — shown live on the card |
| Battery charge / discharge energy (optional) | cumulative kWh or Wh meters, both needed for actual-savings EUR. Prefer the core Fronius DC counters `Battery charging/discharging energy total` (HA 2026.9+) over a Riemann sum |

## Verification sensors

Watch these while testing (dry-run first!):

| Entity | Forced charge | Idle |
|---|---|---|
| `sensor.byd_storctl_mod` | `1` | `0` |
| `sensor.byd_…` MinRsvPct (40350) | `9900` (99 %) | `500` (5 %) |
| `sensor.solarnet_ladeleistung` | ≈ planned `power_w` | 0 from grid |
| `sensor.byd_battery_box_premium_hv_stromstarke_dc` | negative (charging) | ~0 discharge |

## Notes

* The Gen24 occasionally ignores a single Modbus write. If you see this,
  wrap the writes in a retry (`repeat` with a `stop` on success).
* Negative price handling (stopping PV production below −0.10 EUR/kWh via
  registers 40232/40236) is independent of this integration and can coexist.
  From HA 2026.9 the same registers are also `number.*_ac_power_limit` +
  `switch.*_ac_power_limiting`; do not bind both. Core's AC power limit is
  inverter output, not a grid export cap, and a value below 10 % may put
  the Gen24 into standby.
