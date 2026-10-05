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
force-export. The negative (two's-complement) OutWRte/InWRte rates that
force a charge or an export are not exposed, so these scripts cannot be
replaced 1:1.

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
| 40345    | WChaMax     | Maximum charge power in W (e.g. `12800`), the base for both rates |
| 40348    | StorCtl_Mod | **Bitfield** that arms the rate limits: bit 0 = InWRte (charge limit), bit 1 = OutWRte (discharge limit). `0` none, `1` charge limit, `2` discharge limit, `3` both |
| 40350    | MinRsvPct   | Minimum SOC reserve ×100 (`500` = 5 %, `9900` = 99 %). The Gen24 tops a battery below it up from the grid, but only at ~500 W |
| 40355    | OutWRte     | Discharge limit, % of WChaMax ×100 (`10000` = 100 %, signed). `0` blocks discharging; a **negative** value (`65536 − rate`) forces charging at that rate. Needs bit 1 |
| 40356    | InWRte      | Charge limit, % of WChaMax ×100 (`10000` = 100 %, signed). `0` blocks charging; a **negative** value forces discharging at that rate. Needs bit 0 |
| 40360    | ChaGriSet   | Grid charging: `1` = allowed (required for forced charging), `0` = PV only |

The rates only take effect while their bit is set in StorCtl_Mod. Measured on
the reference installation (night, 21 °C battery, 3000 W requested): StorCtl_Mod
`1` with a negative OutWRte charged at only ~500 W (the MinRsvPct top-up),
StorCtl_Mod `2` charged at 3000 W. EVCC's Gen24 template uses the same
semantics (hold = StorCtl_Mod `2` + OutWRte `0`; charge = StorCtl_Mod `2` +
negative OutWRte) and rewrites StorCtl_Mod when a car starts or stops
charging, which can end a running charge slot early.

## Scripts (scripts.yaml)

The charge and export scripts receive `power_w` from Smart Battery Pilot and
convert it to a rate in hundredths of a percent of WChaMax. Every script also
receives `reserve_soc` — the backup reserve, or the minimum SOC while none is
set. The auto script writes it into `MinRsvPct` (40350) so the Gen24 holds the
reserve on its own, even while Home Assistant is down; the idle script raises
MinRsvPct to the current SOC so the battery holds what it has. The charge
script keeps 99 % there on purpose. `max_power` is the value of WChaMax
(e.g. `12800` from register 40345, the first field of
`sensor.reading_battery_settings`).

```yaml
sbp_force_charge:
  alias: "SBP: Laden erzwingen"
  fields:
    power_w:
      description: Charge power in W (provided by Smart Battery Pilot)
      default: 6000
  variables:
    max_power: 12800
    rate: "{{ [((power_w | default(6000)) / max_power * 10000) | int, 10000] | min }}"
  sequence:
    # Charge limit (InWRte) at 100 %; not armed by StorCtl_Mod 2 anyway
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: 10000 }
    # Negative discharge limit (OutWRte) = charge at least at this rate
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: "{{ 65536 - rate }}" }
    # Keep reserve at 99% so the battery keeps topping up (slowly) even if
    # something resets StorCtl_Mod during the slot
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40350, value: 9900 }
    # Arm the discharge limit (bit 1). 1 would arm only the charge limit and
    # leave the negative OutWRte without effect.
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 2 }

sbp_block_discharge:
  alias: "SBP: Entladen sperren (Idle)"
  sequence:
    # No limits armed: PV may still charge, grid-charge from a previous
    # `charge` slot must not continue.
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 0 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: 10000 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: 10000 }
    # Hold the battery: MinRsvPct = current SOC (rounded down, so it never
    # sits above the SOC and triggers a grid top-up), at least the backup
    # reserve, at most 98 %. Always rewritten, so a preceding 99 % reserve
    # cannot leak into idle hours.
    - service: modbus.write_register
      data:
        hub: gen24
        slave: 1
        address: 40350
        value: >-
          {{ [[states('sensor.byd_battery_box_premium_hv_ladezustand') | int(5),
               reserve_soc | default(5) | int] | max, 98] | min * 100 }}

sbp_auto_mode:
  alias: "SBP: Auto-Modus"
  sequence:
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 0 }
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: 10000 }
    # Backup reserve from Smart Battery Pilot (minimum SOC while none is set);
    # the inverter keeps it even while Home Assistant is down.
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40350, value: "{{ ((reserve_soc | default(5)) * 100) | int }}" }
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
    # Discharge limit (OutWRte) at 100 %; not armed by StorCtl_Mod 1 anyway
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40355, value: 10000 }
    # Negative charge limit (InWRte) = discharge at least at this rate
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40356, value: "{{ 65536 - rate }}" }
    # Arm the charge limit (bit 0)
    - service: modbus.write_register
      data: { hub: gen24, slave: 1, address: 40348, value: 1 }
```

`sbp_block_discharge` holds the battery through `MinRsvPct`, which EVCC never
touches. The alternative, StorCtl_Mod `2` with OutWRte `0` (EVCC's *hold*),
blocks discharging just as well but is undone when EVCC rewrites StorCtl_Mod.

`sbp_force_discharge` follows from the register semantics above (negative
InWRte, the mirror image of the verified charge script) but has not been
tested on the reference installation. Try it in a short manual run before you
let the pilot export.

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
| Battery charge / discharge energy (optional) | cumulative kWh or Wh meters, both needed for the battery-benefit and pilot-savings sensors and for learning the cold-weather charge limit. Prefer the core Fronius DC counters `Battery charging/discharging energy total` (HA 2026.9+) over a Riemann sum |

Optional features in the options menu:

| Option | Entity / value |
|---|---|
| Cold-weather charging | on, with the BYD module/cell temperature sensor of your `byd_hvs` installation as battery temperature; defaults `10/20/50/80/100 %` suit the LFP cells — check the BYD datasheet |
| Backup reserve | e.g. `20` %, optionally an `input_number` your storm-warning automation raises; *block discharging* is not needed, the Gen24 holds `MinRsvPct` itself |

## Verification sensors

Watch these while testing (dry-run first!):

| Entity | Forced charge | Idle |
|---|---|---|
| `sensor.byd_storctl_mod` | `2` | `0` |
| `sensor.byd_…` OutWRte (40355) | `65536 − rate`, shown as a negative % | `10000` |
| `sensor.byd_…` MinRsvPct (40350) | `9900` (99 %) | current SOC × 100, at least `reserve_soc` × 100 |
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
