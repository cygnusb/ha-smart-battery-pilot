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

Added to the **battery** step (setup and options):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `charge_derating` | bool | `false` | Master switch for the feature. |
| `battery_temperature_entity` | entity (sensor, °C) | — | Cell/pack temperature, e.g. the BMS module temperature. **Required** when derating is on. Separate from the outdoor temperature used by the consumption model. |
| `charge_derating_curve` | text | `0:0, 5:20, 10:50, 15:80, 20:100` | Support points `temperature °C : percent of max charge power`. |

Curve rules, enforced in the config flow (`invalid_derating_curve` error):
at least two points, temperatures strictly increasing, percentages 0–100.
Between points the factor is linear; below the first / above the last point it
is clamped to that point's value. The default is a conservative generic LFP
curve; the documentation tells users to check their battery's datasheet.

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
    def __init__(self, curve: list[tuple[float, float]]) -> None
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
power_w = min(max_charge_power_w, planned_power_w / charge_factor)   # factor > 0
```

The script asks for the power that, at the expected acceptance, stores the
planned energy. If the battery is warmer than modelled, it charges faster, the
SOC runs ahead of the forecast, and the next re-plan (≤ 30 min) accounts for
it. A factor of `0` means no charge slots are planned at all, so the division
never happens.

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
| Invalid curve text | config flow error, entry not saved |

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
  * factor 0 → no charge slots,
  * the charge script's `power_w` is never below the non-derated plan's.
* Coordinator/executor:
  * a sample is opened on an applied charge and closed at the next decision,
  * each discard rule (short slot, taper SOC, meter reset, dry run, small
    request) has its own test,
  * persistence round trip.
* Config flow: new fields, curve validation, temperature entity required when
  enabled; translations en/de (the existing translation test enforces key
  parity).
* `tests_ha` smoke: setup with derating on still loads.

## Rollout

One PR, version 0.8.0 (new config keys). Off by default, so existing
installations see no change until they switch it on. Documentation:
`docs/configuration.md` (fields, curve format, datasheet hint) and
`docs/optimizer.md` (charge cap and requested power).
