# Spec: Home Assistant integration for smart home-battery charging

Implement a Home Assistant integration for smart home-battery charging that
makes the most of dynamic electricity tariffs. Keep it generic enough to work
with different battery vendors, control paths, and dynamic-tariff sources.

The idea is, during dark autumn/winter weeks (Dunkelflaute) when prices spike
at certain times of day, to charge the home battery at cheap prices by enough
to cover typical household demand, then discharge it when prices are
substantially higher (configurable offset; in Europe probably at least
> 0.20 EUR/kWh). Do this as close to optimally as practical, to maximise
savings and cover household demand at peak-price hours. (Use a neural net for
the optimiser?)

A generic integration matters so the same code works with quite different
setups. The config flow should let you pick the sensors, control scripts,
price sources, and so on — comfortably, without becoming extremely complex.

Include the BYD HVM + Fronius example (description, scripts, configuration)
in the documentation. Structure the docs so further vendor examples can be
added later.

This should be a GitHub project published as a HACS integration. A project
name and logo still need to be added.

## Household / energy-consumption inputs

The following information could feed the charge strategy:

- Heat pump present yes/no
- Weather forecast
- Temperature sensor
- PV production forecast and live PV production
- Home-battery state of charge
- Household energy consumption with a detailed profile
- Electricity price for the next ~24–36 h (European power exchange)

## Home Assistant notes

- `docs/energy-system.md` documents the specific household setup
- Home Assistant access (read-only) is described in `~/claude/ha`
