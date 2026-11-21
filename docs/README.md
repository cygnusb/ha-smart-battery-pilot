# Smart Battery Pilot documentation

Smart Battery Pilot is a Home Assistant integration that plans your home
battery a day ahead. It charges when electricity is cheap and uses the stored
energy when it is expensive, based on dynamic prices, your household
consumption and the PV forecast.

| Page | What it covers |
|---|---|
| [Getting started](getting-started.md) | What you need, installation, the control scripts and going live step by step. |
| [Configuration](configuration.md) | Every setup step and option, the backup reserve, cold-weather charging and the consistency checks. |
| [Dashboard card](dashboard.md) | The bundled card: views, sizing, YAML dashboards. |
| [How the optimizer works](optimizer.md) | The planning algorithm, what export and curtailed PV are worth, and how the savings are estimated. |
| [EV charging, evcc and other controllers](ev-charging.md) | Keeping a wallbox out of the consumption forecast, sharing the battery with evcc, planned EV sessions and the grid import limit. |
| [Price bridges](price-bridges.md) | Publishing the price curve of integrations the pilot cannot read directly (official Nord Pool and Tibber, Belgian prices) as a sensor it can. |
| [Troubleshooting & FAQ](troubleshooting.md) | Diagnostics, what each plan status and warning means, and why the plan does what it does. |
| [Examples](examples/README.md) | Ready-to-use control scripts for specific battery and inverter combinations. |

An overview of the entities is in the
[README](https://github.com/cygnusb/ha-smart-battery-pilot#entities); the
[project website](https://cygnusb.github.io/ha-smart-battery-pilot/) shows how
the plan looks through the seasons.
