# Smart Battery Pilot documentation

Smart Battery Pilot is a Home Assistant integration that plans your home
battery a day ahead. It charges when electricity is cheap and uses the stored
energy when it is expensive, based on dynamic prices, your household
consumption and the PV forecast.

| Page | What it covers |
|---|---|
| [Configuration](configuration.md) | Every setup step and option, going live, troubleshooting and the dashboard card. |
| [How the optimizer works](optimizer.md) | The planning algorithm, what export and curtailed PV are worth, and how the savings are estimated. |
| [EV charging, evcc and other controllers](ev-charging.md) | Keeping a wallbox out of the consumption forecast, sharing the battery with evcc, planned EV sessions and the grid import limit. |
| [Energy-system reference](energy-system.md) | A worked example of the signals the pilot needs and where they come from, for a BYD battery behind a Fronius Gen24. |
| [Examples](examples/README.md) | Ready-to-use control scripts for specific battery and inverter combinations. |

Installation through HACS and an overview of the entities are in the
[README](https://github.com/cygnusb/ha-smart-battery-pilot#readme); the
[project website](https://cygnusb.github.io/ha-smart-battery-pilot/) shows how
the plan looks through the seasons.
