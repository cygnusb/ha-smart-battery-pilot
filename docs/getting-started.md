# Getting started

From an empty setup to a pilot that steers the battery: what you need, the
setup, and how to go live without surprises. Every option is described in
[Configuration](configuration.md).

## 1. What you need

| Signal | Required | Typical source |
|---|---|---|
| Price forecast | yes | Your tariff integration: a sensor with today's and tomorrow's prices in its attributes (Nordpool, EPEX Spot, ENTSO-E, aWATTar …). Sources that only offer the curve through an action need a [price bridge](price-bridges.md). |
| Battery SOC | yes | A sensor in percent from the battery or inverter integration. |
| Control scripts | yes | Three scripts you write for your hardware: *force charge*, *block discharge*, *auto mode*; a fourth for the optional export mode. See [step 3](#3-write-the-control-scripts). |
| House consumption | yes | A sensor of the **whole house load** with long-term statistics, see [below](#house-consumption). |
| PV forecast | no | A forecast integration (Forecast.Solar, Open-Meteo Solar Forecast, Solcast, Helios …). |
| Live PV power | no | Shown on the card, and needed by some [consistency checks](configuration.md#consistency-checks). |
| Battery charge / discharge energy | no | Cumulative kWh or Wh meters. Needed for the savings figures and for learning the cold-weather charge limit. |
| Outdoor temperature | no | Any local temperature sensor; most useful with a heat pump. |
| Battery temperature | no | Only for [cold-weather charging](configuration.md#cold-weather-charging). |

### House consumption

The pilot learns the household's consumption from the long-term statistics of
one sensor. A **power sensor of the whole house load** is the best choice. Many
inverters compute it; otherwise build it from the power flows:

```
house load = PV power + battery discharge + grid import − grid export − battery charge
```

A grid-import meter alone (e.g. from the grid operator's portal) is **not** the
house load: it misses everything PV and the battery cover. If the house load
includes a wallbox, set the wallbox power sensor in the pilot's EV section so
the car is taken out of the training data ([EV charging](ev-charging.md)).

### PV forecast

Any forecast integration with a "today" and a "tomorrow" energy sensor works.
With several roof faces, add them up in a template sensor first. If the
integration also feeds the Energy dashboard's solar forecast, select its
config entries (one per roof face) as the hourly forecast too: the planner then
follows the real shape of each day instead of an even curve. See
[PV forecast](configuration.md#5-pv-forecast-optional).

### Outdoor temperature

Optional, and most useful with a heat pump: the consumption model then learns
how the load follows the temperature. A local sensor beats a weather service.

## 2. Install and set up

1. On Home Assistant 2026.11 or newer, go to *Settings → Marketplace*, search
   for **Smart Battery Pilot** and install it. On older versions, search for it
   in HACS, download it and restart Home Assistant.
2. *Settings → Devices & Services → Add Integration → Smart Battery Pilot.*
3. The setup asks for the price source, the battery, the control scripts, the
   consumption sensor and the PV forecast, in that order. Each step is
   described in [Configuration](configuration.md#config-flow-steps).

Set Home Assistant's currency to the one your tariff is billed in **before**
adding the integration ([currency](configuration.md#currency)).

## 3. Write the control scripts

The pilot never talks to the inverter itself. It calls the script of the
planned action when that action begins (*charge* and *export* at every slot,
since their power may change), with the planned power (`power_w`) and the
backup reserve (`reserve_soc`) as variables. What each script has to do, and what the plan
assumes of it, is in [Control scripts](configuration.md#3-control-scripts).

Start from an [example](examples/README.md) if one matches your hardware. Test
each script by hand (Developer Tools → Actions) before the pilot runs it: the
battery should charge, hold, and return to normal operation as expected.

## 4. Go live

1. After setup the integration is **disabled** with **dry-run on**.
2. Watch `sensor.…_charge_plan` and the log (`DRY RUN: would apply …`) for a
   day or two.
3. Turn on `switch.…_enabled` — still dry-run, nothing is called yet.
4. When the plan looks sensible, turn off `switch.…_dry_run`.

If anything goes wrong (price entity unavailable, no prices, SOC missing),
the integration calls your *auto mode* script once, logs one warning naming
the reason, and stops interfering until planning works again. Turning dry-run
**on** after a live script was applied also restores auto.

## 5. After going live

* **Settings → Repairs** tells you when the battery does not do what the plan
  expects: charge slots that don't charge, an idle slot that leaks, PV surplus
  that never reaches the battery ([consistency checks](configuration.md#consistency-checks)).
  Most of them need a few days of data.
* The [dashboard card](dashboard.md) shows the plan, the prices and the SOC
  forecast.
* Optional features in the options menu, all off until you switch them on:
  [cold-weather charging](configuration.md#cold-weather-charging),
  [backup reserve](configuration.md#backup-reserve) and
  [EV charging & other controllers](ev-charging.md).
* If the plan surprises you, see [Troubleshooting & FAQ](troubleshooting.md).
