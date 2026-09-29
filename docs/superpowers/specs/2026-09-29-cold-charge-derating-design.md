# Cold-weather charge derating — design

Status: draft for review · 2026-09-29

## Goal

A cold LFP battery accepts far less charge power than its nominal limit: the
BMS throttles below roughly 15 °C and blocks charging near 0 °C. The planner
does not know this today. It assumes `max_charge_power_w` in every slot, so on
a cold winter night it plans two cheap hours of grid charging that in reality
deliver a third of the energy. The expensive morning then finds the battery
half empty, and the plan's SOC forecast and savings estimate are wrong.

Derating teaches the **model** how much the battery will actually take at its
current temperature, so the planner spreads the charge over more (or longer)
cheap slots. It never makes the pilot throttle the battery itself.

### Success criteria

1. With derating enabled and a cold battery, a plan that needs *E* kWh stored
   reserves enough cheap slots to store *E* kWh at the derated rate.
2. The power handed to the charge script is never lower because of derating
   than it would be without it (see "Requested power").
3. Out of the box a configurable curve is used; once enough real charge slots
   have been observed in a temperature band, the learned value replaces the
   curve for that band.
4. Disabled (the default), nothing changes: plans are bit-identical to today.
5. Every derating decision is visible in the debug log and the diagnostics.

### Non-goals

* No battery temperature forecast. The current reading applies to the whole
  horizon; the plan is rebuilt every 30 minutes and on every slot boundary.
* No discharge derating (cold also limits discharge, much less so; later).
* No usable-capacity derating at low temperature (belongs with the backup
  reserve work).
* No learning from PV-only charging — see "Why only forced charge slots".

## Configuration

A new section **Cold-weather charging** (`derating`) in the options menu, next
to battery, consumption, PV and so on. It is not part of the initial setup:
the feature is off by default and is switched on later, once the installation
runs.

| Key | Selector | Default | Meaning |
|---|---|---|---|
| `charge_derating` | boolean | `false` | Master switch for the feature. |
| `battery_temperature_entity` | entity (sensor; deliberately no device-class filter, since Modbus/template battery sensors often lack one) | — | Cell/pack temperature, e.g. the BMS module temperature. **Required** when derating is on. Separate from the outdoor temperature used by the consumption model. |
| `derating_0c` | slider 0–100 %, step 5 | `10` | Charge power at **0 °C and below**, percent of max charge power |
| `derating_5c` | slider 0–100 %, step 5 | `20` | … at 5 °C |
| `derating_10c` | slider 0–100 %, step 5 | `50` | … at 10 °C |
| `derating_15c` | slider 0–100 %, step 5 | `80` | … at 15 °C |
| `derating_20c` | slider 0–100 %, step 5 | `100` | … at **20 °C and above** |

The support temperatures are fixed, and the user only moves five sliders.
The labels carry the temperature, so no format has to be learned and nothing
can be mistyped. Between the points the factor is linear. Below 0 °C it stays
at the 0 °C value, above 20 °C at the 20 °C value.

Validation in the flow:
* The values must not fall with rising temperature (error
  `derating_not_monotonic`), since a warmer battery never takes less.
* The temperature entity is required when the switch is on (error
  `derating_needs_temperature`).

The default is a conservative generic LFP curve. At 0 °C it still assumes
10 %, because many BMS allow a trickle charge there. The documentation tells
users to check their battery's datasheet.

Learning additionally needs the existing `battery_charge_energy_entity`.
Without it the curve is used permanently; the config sensor says so.

## Components

### `forecast/charge_rate.py` (new, no Home Assistant imports)

```python
@dataclass(frozen=True, slots=True)
class ChargeSample:
    temperature: float      # °C at the start of the slot
    ratio: float            # achieved kW / max_charge_power kW, 0..1.x
    saturated: bool         # achieved clearly below what was requested
    at: datetime

class ChargeRateModel:
    def __init__(self, curve: list[tuple[float, float]]) -> None  # [(0, .10), (5, .20), ...]
    def factor(self, temperature: float | None) -> float           # 0..1
    def source(self, temperature: float | None) -> str             # "curve" | "learned" | "off"
    def add_sample(self, sample: ChargeSample) -> None
    def to_dict(self) -> dict / from_dict(...)                     # persistence
    def bands(self) -> list[dict]                                  # diagnostics
```

* **Bands** are 5 °C wide (`floor(t / 5)`), keyed by their lower bound.
* A band is **learned** once it holds `MIN_BAND_SAMPLES = 6` saturated samples.
  Its value is the median of their ratios, clamped to 0..1.
* Unsaturated samples (the battery took everything requested) are **lower
  bounds**: the band's value is raised to at least the 75th percentile of
  their ratios. They never make a band count as learned on their own.
* `factor(t)` interpolates linearly between band centres. Each centre uses the
  learned value if the band is learned, otherwise the curve at that centre.
  Temperature `None` → `1.0` (no derating) and `source = "off"`.
* Samples are capped at 400, newest kept; samples older than 2 years dropped
  (batteries age, and so does their cold behaviour).

### Observation: `ChargeRateRecorder` in the coordinator

A sample describes one **forced charge slot the pilot really ran**:

* **Open** when the executor has successfully applied `charge` (not in
  dry-run, pilot enabled): record charge meter kWh, battery temperature, SOC,
  the slot's requested `power_w` and the time.
* **Close** at the next executor decision (slot boundary, new plan, switch
  change). Kept only if all of this holds:
  * the elapsed time is at least 10 minutes,
  * the charge meter was readable at both ends and did not go backwards,
  * the SOC at the end is below `max_soc − 5` — above that the BMS tapers
    because the battery is nearly full, not because it is cold,
  * the requested power was at least 20 % of `max_charge_power_w` (tiny
    requests say nothing about the limit).
* `achieved_kw = Δkwh / hours`; `ratio = achieved_kw / max_charge_kw`;
  `saturated = achieved_kw < 0.85 × requested_kw`.

The executor calls `coordinator.charge_observation(applied_action, slot)`
from `_apply_locked` after each decision; the coordinator owns the open
observation and the model. Samples are persisted in the existing store under
a new `charge_rate` key (additive; `STORAGE_VERSION` unchanged).

#### Why only forced charge slots

In a charge slot the demand on the battery is known: the script asked for
`power_w`. A shortfall is therefore the battery's limit. During PV charging
the battery takes whatever surplus the sun happens to give. A low reading
there says nothing about the limit. Such readings could at best serve as lower
bounds, and they would need an extra power entity. The forced charge slots
happen exactly in the cold season this feature is about, so they are enough
data.

### Optimizer

`BatteryState` gains `charge_factor: float = 1.0`. The per-slot charge cap
becomes

```python
charge_cap = [max_charge_power_w * charge_factor / 1000 * h * eta_one_way for h in hours]
```

That limits grid charging and PV-surplus charging alike, and the timeline, the
SOC forecast and the savings estimate follow from it. With `charge_factor ==
1.0` the arithmetic is unchanged (criterion 4).

### Requested power

Today a charge slot's `power_w` is the *planned* grid power. With derating,
that number already has the cold limit baked in, so passing it on would make
the pilot request 30 % and throttle the battery itself. Instead:

```python
power_w = max_charge_power_w if slot_is_at_its_derated_cap else planned_power_w
```

A slot filled to its derated cap plans exactly the cold limit, so it asks for
the maximum and the BMS caps it. A slot that needs less keeps its planned
power, as it would without derating. A cold BMS caps the *current*. It does
not take a share of the request, so asking such a slot for
`planned / factor` would store up to `1 / factor` times the plan and could
overshoot max SOC (found in the final review; the first version did exactly
that). If the battery is warmer than modelled, capped slots charge faster, the
SOC runs ahead of the forecast, and the next re-plan (≤ 30 min) accounts for
it. The default curve never reaches 0. A user can still set a slider to 0 %;
then no charge slots are planned at all, so the division never happens.

### Coordinator wiring

* Reads the battery temperature and computes `factor` and `source` per update.
  Both go into `SBPData.inputs` (diagnostics) and into the debug log line
  "Planning inputs".
* Derating on but temperature unavailable: `factor = 1.0`, one warning when
  the entity first goes unavailable (same once-per-reason pattern as invalid
  plans), debug afterwards.
* Configuration sensor attributes: `charge_derating` (on/off),
  `charge_factor`, `charge_factor_source`, `learned_bands` (count).

## Error handling

| Situation | Behaviour |
|---|---|
| Derating off | factor 1.0, no temperature read, recorder inactive |
| Temperature unavailable | factor 1.0, warn once, recorder skips the sample |
| Charge meter missing | curve only, recorder inactive, config sensor says "curve (no charge meter)" |
| Meter unavailable / reset during a slot | sample discarded, debug log with the reason |
| Stored samples unreadable | model starts empty, warning (same as the consumption model) |
| Falling slider values / missing temperature entity | config flow error, section not saved |

## Testing

* `test_charge_rate.py` (pure):
  * curve interpolation and clamping, curve parsing and validation,
  * band learning threshold, median, lower-bound raise, interpolation that
    mixes learned and curve bands,
  * sample cap and age limit, `to_dict`/`from_dict` round trip.
* Optimizer:
  * factor 1.0 gives a plan identical to today (existing suite plus an
    explicit equality test),
  * a cold factor spreads the charge over more slots and keeps the SOC
    forecast within the derated rate,
  * factor 0 (slider set to 0 %) → no charge slots,
  * the charge script's `power_w` is never below the non-derated plan's.
* Coordinator/executor:
  * a sample is opened on an applied charge and closed at the next decision,
  * each discard rule (short slot, taper SOC, meter reset, dry run, small
    request) has its own test,
  * persistence round trip.
* Config flow: new options menu section, default values, monotonic
  validation, temperature entity required when enabled; translations en/de
  (the existing translation test enforces key parity).
* `tests_ha` smoke: setup with derating on still loads.

## Rollout

One PR, version 0.8.0 (new config keys). Off by default, so existing
installations see no change until they switch it on. Documentation:
`docs/configuration.md` (fields, curve format, datasheet hint) and
`docs/optimizer.md` (charge cap and requested power).
