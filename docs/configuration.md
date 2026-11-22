# Configuration

Every setup step and option in detail. For a first installation, start with
[Getting started](getting-started.md).

## Config flow steps

### 1. Price source

| Field | Description |
|---|---|
| Price forecast entity | Entity of your tariff integration holding the price forecast in its attributes. Supported formats are detected automatically: **Nordpool** (`raw_today`/`raw_tomorrow`; `price_in_cents` or ct/öre/cEUR units are scaled to whole currency units per kWh), **EPEX Spot** (`data` with `price_eur_per_mwh`/`price_ct_per_kwh`), **ENTSO-E** (`prices` with `time`/`price`; the declared unit decides the scale, so a sensor configured for cents is converted too), **aWATTar** (`marketprice` entries) and plain **hourly arrays** (`today`/`tomorrow` float lists). Arrays may hold 23, 24 or 25 values (96 / 92 / 100 at quarter-hour resolution) — on the two clock-change days a local day is not 24 hours long, and the grid follows the real day. |
| Price offset | Fixed surcharge added to every market price: grid fees, taxes, levies (e.g. `0.187` EUR/kWh in Germany). Self-consumption decisions use the *total* import price you actually pay. Grid export is valued at the feed-in tariff, or at the raw market price when the tariff is `0` — the import surcharge is not treated as export revenue. |
| Feed-in tariff | What you earn per exported kWh. `0` means the market price is used (dynamic feed-in): the price entity's value without the price offset. The planner only uses it in the export discharge mode. The [battery benefit](optimizer.md#savings-estimate) uses it in every mode, to value the PV energy the battery stores instead of exporting it. If the price entity already delivers the final consumer price (offset `0`), enter a fixed feed-in tariff; `0` would value every exported kWh at the full consumer price, and the battery benefit would charge stored PV at it. |

If your integration is not recognized, create a template sensor with `today`/
`tomorrow` attributes as plain price arrays — that format is always accepted.

#### Price sources without forecast attributes

Some integrations only provide today's and tomorrow's prices through a service
response, not as sensor attributes. The official Home Assistant **Tibber**
integration is one of them: its price sensor exposes the current price and a few
statistics, but no price list. The pilot reads attributes and does not call the
services of individual integrations, so such a source needs a bridge: a template
sensor that calls the service and exposes the result as `today` / `tomorrow`
lists (or `raw_today` / `raw_tomorrow`), selected as the price entity. The
[price bridges](price-bridges.md) page has worked examples for the official
Tibber and Nord Pool integrations and for Belgian prices.

Keep the native resolution (for example 15 minutes). If the service returns the
final consumer price, leave the price offset at `0` so fees are not counted twice,
and enter a fixed feed-in tariff.

The alternative is a price integration that exposes the attributes, such as the
Nordpool HACS integration, with fees and taxes covered by the price offset. That
is only an approximation when part of the surcharge is proportional (VAT, for
example). Note that the official Home Assistant **Nord Pool** integration is not
that integration: like Tibber, it only offers the curve through an action
(`nordpool.get_prices_for_date`), and its sensors carry no price list.

The Nordpool HACS integration is a third-party component, and it has been
reported to keep serving the previous day's curve as "today" for many hours
after its upstream API stopped answering, while the sensor still looked alive.
Depending on how much of the stale curve still lies in the future, the pilot
either refuses to plan (the plan status shows `no_price_data`, the battery is
handed back to auto mode, and after 12 hours the *No valid plan* repair
appears) or plans on stale prices. If you use it, check now and then that
`raw_today` of the sensor starts at today's date.

#### Currency

Prices are **never converted** between currencies. The planner works in
whatever currency the price entity delivers — only sub-units such as cents or
öre are scaled to whole units — and takes that to be the currency set in Home
Assistant (*Settings → System → General*). Everything is labelled with it: the
price fields in the config flow, the current-price and savings sensors, and
the card.

Set Home Assistant's currency to the one your tariff is billed in *before*
adding the integration: a new entry then starts with defaults sized to that
currency (a spread of 2.2 SEK/kWh instead of 0.20, for example), and the
check that refuses implausibly large prices scales with it. An entry created
earlier keeps the values it has — review the price offset, feed-in tariff and
spread once. A changed currency setting takes effect after reloading the
integration.

When the price entity declares a currency of its own (a `currency` attribute,
or a unit such as `SEK/kWh`) that differs from Home Assistant's, you are told
in three places: the price step of the config and options flow points it out
once (submitting again goes on anyway), a repair appears under *Settings →
Repairs* until both currencies match, and the log gets a warning. Planning
carries on unchanged in the meantime.

### 2. Battery parameters

| Field | Description |
|---|---|
| State of charge entity | Live SOC in percent, e.g. `sensor.battery_state_of_charge`. |
| Usable capacity | Battery capacity in kWh. |
| Max charge / discharge power | Inverter limits in W. |
| Min / Max SOC | The plan never leaves this window (e.g. 10–95 %). The minimum reaches the scripts as `reserve_soc` while no backup reserve is set. The maximum is a planning bound only: no script receives it, and in `auto` slots the battery charges from PV up to the inverter's own limit. If the inverter stops charging earlier, set Max SOC to that limit, or the plan expects PV to fit into the battery that does not. |
| Roundtrip efficiency | Grid → battery → load efficiency, typically 88–92 %. Losses are priced into every charge decision. |
| Battery charge / discharge energy (optional) | Cumulative kWh or Wh meters. **Both** are required before the battery benefit (net, gross), pilot savings and energy sensors report a value. Unavailable readings are skipped so a glitch cannot inflate the totals. See [how the three money totals differ](optimizer.md) — in short: *battery benefit* is what the battery is worth (counted whether or not the pilot steers), *pilot savings* is only what the pilot added by charging from the grid or holding energy back. |

### 3. Control scripts

The integration never talks to your inverter directly — it calls Home
Assistant scripts that you provide. This is what makes it vendor neutral.

| Script | Called when | Receives |
|---|---|---|
| Force charge | a `charge` slot starts | variables `power_w` (planned charge power of the battery, forecast PV surplus included), `reserve_soc` |
| Block discharge (idle) | an `idle` slot starts | variable `reserve_soc` |
| Auto mode | an `auto` slot starts; also when the integration is disabled, unloaded, the plan becomes invalid, or dry-run is turned on after a live script was applied | variable `reserve_soc` |
| Force discharge to grid | an `export` slot starts | variables `power_w` (planned discharge power), `reserve_soc` |

`reserve_soc` (whole percent) is the [backup reserve](#backup-reserve), or the
minimum SOC while no reserve is set. A script that writes it into the
inverter's own reserve setting keeps the reserve even when Home Assistant is
down. Scripts that ignore it keep working.

Within a running charge or export slot, a re-plan calls the script again only
when `power_w` or `reserve_soc` changed; the next slot always calls it. If such
an update fails, for example because an integration limits how often a register
can be written, the mode keeps running and the next re-plan or slot boundary
tries again. If the first call of a slot fails, the pilot falls back to auto
mode. Two calls can still come close together, so a script should tolerate a
rate limit or skip writes that wouldn't change anything. Ways to do that:

* Skip a write when the register or entity already holds the target value.
* Wait out a write cooldown with a bounded `delay`, well inside the 120 s
  limit below.
* Put `continue_on_error: true` on a step that may be rate-limited, so the
  writes after it (the power, for example) still go out.

Only the force-discharge script is optional — and only while the discharge
mode is *self-consumption*. Selecting **export** mode without it is refused,
because the planner would keep scheduling export slots that the executor can
only answer with a warning and a fallback to auto mode. It must keep covering
the house from the battery while it exports; see
[what an export slot assumes](optimizer.md#what-an-export-slot-assumes).

The auto script has to leave the battery in **self-consumption in both
directions**: it covers the house load, and it charges from PV surplus. The
plan counts on both ([what an auto slot assumes](optimizer.md#what-an-auto-slot-assumes)).
A hybrid inverter or an AC battery in its smart-meter mode does this by itself.
A battery that only follows charge and discharge commands does not: there, the
auto script has to hand over to something that does, such as an automation that
follows the grid meter.

The idle script only **blocks discharging**; everything else stays as in auto.
The pilot plans `idle` only in slots where the forecast PV is below the house
load (slots with a PV surplus stay `auto`), and counts on PV still covering part
of the house there: the grid supplies the rest, and the battery stays where it
is. Backup or "charge from PV" modes on some inverters do something else: they
send PV to the battery first and supply the whole house from the grid. Each kWh
the battery gains that way is one more kWh imported in that slot, at that slot's
price. That is a grid charge the plan decided against. The SOC forecast stays
flat while the battery fills, and the battery benefit books the charge as PV at
the feed-in tariff. Prefer a setting that only stops discharging, for example
raising the inverter's minimum SOC to the current SOC, as long as the inverter
does not charge from the grid to reach it.

PV surplus that turns up in an idle slot anyway still belongs in the battery,
as in auto. That happens when the sky beats the forecast, or when the pilot
blocks discharging at the [backup reserve](#backup-reserve) in a slot planned
as `auto`. A battery that only follows commands needs its meter-following
automation to keep charging from surplus during idle, and to drop only the
discharging. The [surplus check](#consistency-checks) counts idle slots too.

The charge script's `power_w` is the power the battery should charge at: the
planned grid charge plus the PV surplus forecast for the slot. Set the battery
to charge at that power. PV covers what it can, and the grid supplies the rest.
A hybrid inverter does this when its forced charge rate sends PV to the battery
first. A battery that only follows commands does it by drawing the power from
the house, where the surplus covers part of it. More PV than forecast may still
charge the battery on top.

Ready-made scripts for specific hardware: see [examples](examples/README.md).

> Each script is awaited (`blocking: true`) and must return within **120 s**,
> or the action counts as failed and the battery is handed back to auto mode.
> Give any retry loop inside a script a bound of its own — an unbounded one
> would otherwise hold up integration reloads and Home Assistant's shutdown.

### 4. Household consumption

| Field | Description |
|---|---|
| Consumption sensor | Total house consumption. Either a **power** sensor in W (recommended, e.g. `sensor.house_load_power`) or an **energy** sensor in kWh with long-term statistics. Used to train the forecast from recorder history. |
| Outdoor temperature (optional) | Adds a heating-demand feature to the model — recommended for heat pump households. |
| Heat pump present | Forces the heating-demand feature in the consumption model as soon as any outdoor-temperature history exists (instead of waiting until half the samples are tagged). |

The forecast model:

* **Day 1+**: weighted hourly profile (weekday/weekend, recent days count more).
* **After ~14 days of history**: ridge regression with cyclic hour-of-day,
  weekend and temperature features (pure Python, no extra dependencies).
* **After ~4 weeks of history**: whichever of the two forecasts better. Both
  are fitted on everything but the last 14 days and scored on those days by
  their mean absolute error per hour; the lower error wins, the regression on
  a tie. A load that follows the outdoor temperature favours the regression,
  a sharp block at the same time every day (an appliance on a timer) favours
  the profile, which the regression's smooth daily curve cannot draw. The
  `model_type` attribute of the plan status and consumption forecast sensors
  names the model in use; the plan status sensor and the diagnostics dump
  also carry both errors as `model_holdout_mae`.
* Retrained automatically every 24 h, which repeats the comparison; the model
  is persisted across restarts.
* Pointing the integration at a different consumption sensor starts the history
  over, so the forecast falls back to the profile until the new sensor has ~14
  days of its own — the regression is never carried over from the old one.

### 5. PV forecast (optional)

Daily production forecast sensors in kWh (e.g. Open-Meteo Solar Forecast's
`sensor.energy_production_today` / `…_tomorrow`). The daily total is
distributed over the daylight hours — read from `sun.sun`, so a December day
is modelled as the ~8 hours it really is — and subtracted from the
consumption forecast.
Optional current PV power is shown live on the Lovelace card. Without PV,
simply leave the fields empty.

Enter the forecast for the **whole** day, not "remaining today": the
integration already drops the hours that are over, so a remaining-today
sensor would be cut down a second time. Helios Forecast numbers its days from
today: use `energy_day_1` for today and `energy_day_2` for tomorrow.

The plan is only as good as the forecast. Much of a forecast's error is often
systematic rather than the weather: compare a few weeks of forecast against the real production,
then correct the forecast integration's own settings (efficiency, damping) or
use one that calibrates itself.

**Hourly forecast (Energy dashboard).** The even daylight curve knows nothing
about your roof: an east-facing array produces most of its energy before noon,
a west-facing one after, and a cloudy morning moves the yield into the
afternoon. If a forecast integration also feeds the
[Energy dashboard](https://www.home-assistant.io/docs/energy/solar-panels/)
— Forecast.Solar, Open-Meteo Solar Forecast, Solcast, Helios and others do —
pick its config entries in **Hourly forecast**, one per roof face. Their
hourly curves are added up and replace the daylight curve slot by slot.

* A new setup pre-selects the entries your Energy dashboard already uses.
  Existing installations keep planning exactly as before until you pick
  entries yourself.
* The daily sensors stay the fallback. When one of the selected entries
  delivers no curve (integration not loaded, API down), the whole cycle plans
  with the daily sensors rather than silently leaving a roof out, and the log
  says so once. Slots beyond the end of the shortest curve use the daily
  sensors too, so keep them configured.
* Forecast.Solar stamps each value with the end of its hour; other
  integrations with the start. Both are read correctly. Forecast.Solar's
  curve ends at tomorrow's sunset, so the slot around that sunset and
  everything after it come from the daily sensors.
* The diagnostics download shows under `inputs.pv_hourly` which entries were
  used, which failed, and how many slots came from the curve.

## Options (Settings → Integrations → Configure)

The configure dialog opens a **menu** in which every input from the initial
setup can be reviewed and changed later — each section shows the currently
configured entities/values pre-filled:

* **Optimizer tuning** — spread, discharge mode, training days
* **Price source & offsets** — price entity, price offset, feed-in tariff
* **Battery parameters** — SOC entity, capacity, power limits, SOC window, efficiency
* **Control scripts** — the four action scripts
* **Consumption & temperature** — consumption sensor, temperature, heat pump
* **PV forecast** — hourly forecast entries, daily forecast entities and optional live PV power
* **Cold-weather charging** — only here, not in the initial setup; see below
* **Backup reserve** — only here; see below
* **EV charging & other controllers** — only here; see [EV charging, evcc and other controllers](ev-charging.md)

Sections return to the menu after submitting; changes are collected and only
persisted via **“💾 Save & close”** (closing the dialog otherwise discards
them). Saving reloads the integration and recomputes the plan.

| Option | Default | Description |
|---|---|---|
| Minimum price spread | 0.20 EUR/kWh | Grid charging only happens if `discharge price > charge price / efficiency + spread`. Raise it to be more conservative (fewer cycles), lower it to arbitrage more aggressively. |
| Discharge mode | Self-consumption | `Self-consumption`: the battery only ever covers the house load. `Export`: additionally force-discharge into the grid during extreme price peaks — requires the force-discharge script. **Check your tariff/regulatory situation before enabling export.** |
| Price offset / feed-in tariff | – | Same as in the config flow. Feed-in `0` uses the market price for export. A fixed tariff at or below the spread (default 0.20 EUR/kWh) could never schedule export, so the options flow refuses that combination instead of letting the setting look active while doing nothing. |
| Training days | 60 | History window for the consumption model. |

### Cold-weather charging

Off by default. A cold LFP battery accepts far less than its nominal charge
power — the BMS throttles below roughly 15 °C and nearly stops near 0 °C. With
this option on, the planner knows that and books more (or longer) cheap charge
slots instead of counting on energy the battery will not take.

| Setting | Default | Meaning |
|---|---|---|
| Plan with reduced charge power in the cold | off | The planner expects a cold battery to take less than the maximum charge power. A slot planned up to that cold limit **still asks the charge script for full power** — the battery's BMS does the throttling, never the pilot. A slot that only needs part of it asks for exactly what it needs, as it would without the option. |
| Battery temperature entity | – | Cell or module temperature of the battery (not the outdoor sensor). Required when the option is on. °F is converted. If it becomes unavailable, the planner falls back to full charge power and logs one warning. |
| Charge power at 0 / 5 / 10 / 15 / 20 °C | 10 / 20 / 50 / 80 / 100 % | Percent of the maximum charge power the battery accepts. Linear in between; below 0 °C the 0 °C value applies, above 20 °C the 20 °C value. The values must not fall as the temperature rises. The defaults are a conservative generic LFP curve — check your battery's datasheet. |

The five sliders sit in a collapsible block ("Sliders: charge power by
temperature"). It is open while the option is still off and folded once it
is on - the chart shows the sliders as the dashed line either way - and opens
again right after "Adopt into the sliders".

**Learning.** With a battery charge energy meter configured (battery step),
every forced charge slot the pilot really runs is measured: how much went
into the battery compared with what was asked for. Slots near max SOC (the
battery tapers because it is full), shorter than 10 minutes, with requests
below 20 % of the max charge power, or with less than 0.2 kWh in under
30 minutes (too close to the resolution of typical energy meters) are
ignored. Dry-run learns nothing, because no charge actually happens.

Once six slots have shown the battery taking clearly less than requested, a
curve is fitted through the individual measurements, each at the temperature
it was taken at. There is no fixed grid: the curve consists of temperature
ranges that begin and end where the measurements do, each with one charge
limit, for example "4..9 °C: 55 %". Ranges are split where the measurements
differ and joined where they agree, so a battery whose BMS switches at 10 °C
shows a step at 10 °C. The rules of the fit:

* The limit never falls with rising temperature.
* Each range takes the median of its slots, so one odd slot does not bend it.
* A slot in which the battery took everything it was asked for says only that
  the limit is at least that high, there and at every warmer temperature. It
  counts where it lies above the curve and is ignored otherwise.
* Between two ranges the limit rises in a straight line.
* Outside the measured temperatures the sliders stay in charge. The curve
  only bounds them: colder than the coldest measurement the planner never
  assumes more than the coldest learned limit, warmer than the warmest never
  less than the warmest learned limit. Where sliders and measurements
  disagree, that shows as a step at the edge of the measured temperatures.

A single slot can form a range of its own when it lies colder and lower, or
warmer and higher, than all others - the number behind each range tells how
much stands behind it.

**When the temperature does not explain the limit.** Some batteries limit
the charge power by something the configured sensor does not show - the
coldest cell rather than the average, for instance. The measurements then
contradict each other: the same limit at 8 and at 13 °C, and full power at
9 °C. A limited slot counts as contradicted when a slot at the same or a lower
temperature took at least 10 percentage points more. With three contradicted
slots that are at least half of the limited ones, the pilot stops planning
with the learned curve and uses the sliders. The dialog marks the count with a
warning sign and draws no learned curve; the configuration sensor shows
`charge_rate_learning: temperature_mismatch`. Measuring continues, and the
verdict lifts on its own once the contradicted slots are a minority. After
switching to a better temperature sensor, discard the measurements: the old
ones carry the old sensor's temperatures.

**Seeing and steering what was learned.** The section in the options dialog
draws it: the learned curve as a blue line (bold where it was measured, thin
where the sliders stand in), the sliders as a dashed
orange line, a dot for every charge slot the battery limited, a small
triangle for every slot in which it took all that was asked (the limit lies
at least there) and a vertical line at the
current battery temperature. Below the chart the learned ranges are listed
with their limit and the number of slots behind each.

| Setting | Default | Meaning |
|---|---|---|
| Plan with the learned values | on | On: the learned curve replaces the slider curve wherever there are measurements. Off: only the sliders count. Measuring continues either way, and the values stay visible. |
| Learned values | Leave as they are | **Adopt into the sliders** writes the measured values into the five sliders and shows the form again, so you can still adjust them before saving. Each slider takes the learned curve's value at its own temperature. Five sliders cannot hold a step between two of them, so adopting is a coarser copy of the curve. **Discard all measurements** deletes every observation when you save the dialog — use it after a battery swap or when the temperature sensor was wrong. |

With learning on, adopting changes little about the plan: the learned curve
applies either way. It is the way to freeze a measured curve — adopt, then
switch "Plan with the learned values" off.

The configuration sensor shows `charge_factor`, its source (`curve`,
`learned`, `no_temperature`), whether learning is `active`, has
`no_charge_meter` or found a `temperature_mismatch`, whether the learned values are used, the number of learned
ranges (`learned_bands`) and the ranges themselves (`charge_rate_bands`, each
with `from_c`, `to_c`, the learned limit and its number of slots). The
diagnostics dump lists the same ranges.

### Backup reserve

Off by default. Keeps enough energy in the battery for a grid outage.

| Setting | Default | Meaning |
|---|---|---|
| Backup reserve | 0 % | SOC that arbitrage never spends. At or below the minimum SOC the option is off. |
| Reserve entity | – | Optional `input_number`, `number` or `sensor` in percent. While it reads a number it replaces the fixed value — let an automation raise it on a storm warning, in winter, or before a holiday. If it becomes unavailable, the fixed value applies and one warning is logged. Any number counts, `0` included: `0` (or anything at or below the minimum SOC) switches the reserve **off** rather than falling back to the fixed value. To return to a baseline, set the entity to that baseline. |
| Refill deadline | 12 h | If the SOC is below the reserve, it is brought back up within this many hours, counted from each re-plan: forecast PV first, the rest in the cheapest grid slots, whether or not the price spread pays for it. Until the refill the battery spends nothing, so the plan shows no arbitrage and no `idle` before it. When the cheapest slot comes after a price peak, the pilot refills earlier at a slightly higher price if serving that peak pays more. |
| Block discharging at the reserve | off | Fallback for inverters without a reserve setting of their own: when the SOC reaches the reserve, the pilot runs the *block discharge* script instead of *auto*, and releases it 2 percentage points above the reserve. It reacts to SOC changes immediately, not only at the next re-plan. |

The effective reserve is the entity value (or the fixed value), clamped to the
SOC window.

> **For a real outage, the reserve has to live in the inverter.** The pilot can
> only enforce it while Home Assistant runs, and a grid outage may be exactly
> when it does not. Have your `auto` and `idle` scripts write `reserve_soc`
> into the inverter's minimum reserve (for the Fronius GEN24 see the
> [example](examples/byd-fronius-gen24.md)). When the reserve changes, the
> pilot runs the current action's script again, so the new value reaches the
> inverter without waiting for the next action change.
>
> Some inverters have more than one SOC floor, for example one for normal
> operation and one for backup or off-grid mode. Check that the setting your
> scripts write is the one enforced while the grid is up: if the battery drains
> below the reserve and is then charged back up to it, it is probably the wrong
> one.

Below the reserve the plan counts on the battery not discharging, but it still
shows those slots as `auto`: on an inverter that knows the reserve, `auto` can't
go below it. If neither the inverter nor the fallback holds the reserve, `auto`
keeps discharging. The pilot then says so under *Settings → Repairs*
(*Battery discharges below the backup reserve*, see
[consistency checks](#consistency-checks)).

Refilling costs money, and that cost is **not** counted against the estimated
savings — it is reported separately as `reserve_refill_kwh` and
`reserve_refill_cost_eur` on the plan sensor. Only the cheapest refill is
mandatory, though: when the pilot refills earlier to serve a peak, what that
costs on top does come off the estimated savings. The battery benefit
and pilot savings sensors count what the meters measured, so the energy bought
for a refill does show up there (a refill charge is a pilot lot like any grid
charge) — estimated and measured figures drift apart by the refill cost. A
deadline too short for the charge power shows the plan warning
`reserve_refill_incomplete`. The
configuration sensor shows `reserve_soc`, its source (`fixed`, `entity`,
`off`) and whether the discharge block is on.

### EV charging & other controllers

Off by default. Takes wallbox energy out of the consumption history, can block
discharging while an EV charges, and pauses the pilot while another controller
such as evcc holds the battery. With evcc, the plan can also leave room for
sessions evcc has planned. Settings, the evcc setup and other wallboxes:
[EV charging, evcc and other controllers](ev-charging.md).

## Going live

The integration starts **disabled** and in **dry-run**. How to switch it on
step by step, and what happens when planning fails, is in
[Getting started](getting-started.md#4-go-live).

## Consistency checks

The plan rests on assumptions the pilot can't enforce. When one fails, nothing
errors: the plan and the battery just drift apart. So the pilot compares the two
and raises a message under **Settings → Repairs** when they disagree. The checks
only report, they never change a plan or a script call. A message disappears by
itself once the problem is gone. If it describes something you do on purpose,
such as limiting charging, use *Ignore* on the message.

| Message | Raised when | Needs |
|---|---|---|
| A control script keeps failing | one script failed 3 times in a row | – |
| No valid plan | no plan for 12 hours | – |
| Another controller holds the battery | the external control entity is active for 24 hours | External control entity |
| SOC sensor frozen | the SOC stands still for 2 hours while 10 % of the capacity moves | Energy meters, or a forced slot |
| PV forecast is a "remaining today" sensor | the forecast for today falls under 15 % of its daily high by sunset, 3 days | Daily PV sensors without hourly forecast entries |
| Battery capacity looks wrong | charging implies a capacity more than 25 % off, 3 times | Both energy meters |
| Charge slots don't charge | under 50 % of the requested energy arrives, 3 days | SOC |
| Battery discharges in idle slots | over 5 % of the capacity net leaves during idle slots, 3 days | SOC |
| Export slots don't discharge | under 50 % of the planned energy leaves, 3 days | SOC |
| Battery doesn't cover the house | under 20 % of the measured deficit is delivered in auto slots, 3 days | Consumption, and live PV power or a slot with no forecast PV |
| PV surplus doesn't reach the battery | under 20 % of the measured surplus arrives in auto and idle slots, 3 days | Live PV power and consumption |
| Battery discharges below the backup reserve | 3 % of the capacity leaves in auto slots at or below the reserve, at once | A backup reserve, with the discharge block fallback off |

Good to know:

* **What counts:** only slots the pilot really drives. Dry-run, external control
  and a charging car are left out, and so is a battery within 5 SOC points of
  its limit, since it may rightly stop there.
* **Series:** the 3 days need not be consecutive. A day with too little at stake
  (under 10 % of the capacity, 5 % in charge and export slots) doesn't count
  either way. A clearly good day resets the series and clears the message.
* **A wrong forecast doesn't fire a check.** Surplus and deficit come from the
  live PV sensor, not from the plan, read every minute together with the
  consumption. If the consumption sensor only counts energy, the forecast load
  stands in for the live load.
* **Meters and accuracy:** with charge and discharge meters the checks read those;
  without them they use the SOC. A live PV sensor that misses part of the PV
  underestimates the surplus, so the check stays quiet rather than raising a
  false message.
* **The backup reserve check doesn't wait for 3 days:** the reserve is there for
  an outage that may come tonight. It counts from the highest SOC the battery
  reached below the reserve, so a SOC sensor flickering by one point moves
  nothing, and PV lifting the SOC first hides nothing. The message clears at
  the end of a day on which auto slots held the reserve for at least an hour,
  or as soon as the discharge block fallback is on.
* **Partial problems stay unreported.** A battery that takes half of the surplus
  passes. The limits are set to catch what is clearly broken.
* The state of the checks is in the diagnostics download under `runtime.health`.
  `history` lists the last 7 days: per check what was expected and what
  happened, the day's verdict (`strike`, `pass`, `between` or `too_little`)
  and the strikes after it. A series that a good day reset shows up there too;
  a check missing from a day had nothing to judge on it.

## Troubleshooting

Diagnostics, debug logging, what each plan status and plan warning means, and
answers to common questions ("why doesn't it charge?", "why does the SOC
forecast end at the minimum?") are on
[Troubleshooting & FAQ](troubleshooting.md).

## Dashboard

The bundled card, its views and sizing are on [Dashboard card](dashboard.md).
