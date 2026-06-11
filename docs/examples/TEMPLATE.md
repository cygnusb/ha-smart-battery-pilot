# <Battery model> + <Inverter / control path>

Short description of the setup: battery, inverter, how Home Assistant talks
to it (Modbus, MQTT, vendor cloud, …), which tariff integration provides
prices.

## Architecture

How the control path works, ideally a small diagram.

## Prerequisites

Integrations/hardware needed (e.g. Modbus hub config, MQTT broker, …).

## Scripts

The four scripts Smart Battery Pilot calls. The charge script receives the
variable `power_w`.

```yaml
sbp_force_charge:
  alias: "SBP: Force charge"
  fields:
    power_w:
      description: Charge power in W
  sequence:
    # vendor-specific actions

sbp_block_discharge:
  alias: "SBP: Block discharge (idle)"
  sequence:
    # vendor-specific actions

sbp_auto_mode:
  alias: "SBP: Auto mode"
  sequence:
    # vendor-specific actions

# optional, only for export mode:
sbp_force_discharge:
  alias: "SBP: Force discharge to grid"
  sequence:
    # vendor-specific actions
```

## Integration configuration

| Config flow field | Entity / value |
|---|---|
| Price forecast entity | |
| SOC entity | |
| Capacity | |
| Scripts | |
| Consumption sensor | |

## Verification

Which sensors to watch to confirm each action actually works.

## Notes / pitfalls

Anything vendor specific (retry needs, register quirks, rate limits, …).
