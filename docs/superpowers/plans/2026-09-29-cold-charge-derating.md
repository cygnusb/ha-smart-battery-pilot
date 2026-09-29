# Cold-Weather Charge Derating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the planner assume a cold battery accepts less charge power (from a slider curve, later from learned observations), so it schedules more cheap charge slots. The pilot itself never requests less power because of it.

**Architecture:**
* A new pure module `forecast/charge_rate.py` holds the curve, the per-band learning and the factor lookup.
* The optimizer gets one new input, `BatteryState.charge_factor`. It scales the per-slot charge cap and scales the requested `power_w` back up.
* The coordinator reads the battery temperature, computes the factor, persists the learned samples and records observations. The executor's only job is to tell the coordinator when it really applied a charge slot.

**Tech Stack:**
* Python 3.12+ and a Home Assistant custom integration.
* Unit tests with pytest against the HA stubs in `tests/stubs`. Tests against a real Home Assistant (`tests_ha/`) run in CI only.
* `ruff` 0.16.5 for lint and format.

**Spec:** `docs/superpowers/specs/2026-09-29-cold-charge-derating-design.md`

## Global Constraints

- Feature is **off by default**; with it off, plans must be bit-identical to today (`charge_factor = 1.0`).
- The charge script's `power_w` must never be lower because of derating: `power_w = min(max_charge_power_w, planned_power_w / charge_factor)` for `0 < charge_factor < 1`.
- Curve support temperatures are fixed: `0, 5, 10, 15, 20` °C; defaults `10, 20, 50, 80, 100` %. Clamp below 0 °C / above 20 °C; linear in between.
- Sliders: 0–100 %, step 5, unit `%`, `mode="slider"`.
- Flow errors: `derating_not_monotonic` (values fall with rising temperature), `derating_needs_temperature` (switch on, no entity).
- The section lives in the **options menu only** (menu key `derating`), not in the initial config flow.
- Bands 5 °C wide (`floor(t/5)*5`), learned at `MIN_BAND_SAMPLES = 6` saturated samples, value = median of saturated ratios, raised to at least the 75th percentile of unsaturated ratios, clamped 0..1.
- Samples capped at 400 (newest kept), older than 730 days dropped.
- Observation discard rules: under 10 minutes; charge meter unreadable or going backwards; SOC at close ≥ `max_soc − 5`; requested power < 20 % of max charge power; temperature unreadable at open.
- `saturated = achieved_kw < 0.85 × requested_kw`; `ratio = achieved_kw / (max_charge_kw × sqrt(efficiency))`.
- Persist samples under the store key `charge_rate`; `STORAGE_VERSION` stays 1.
- The optimizer and `charge_rate.py` must not import Home Assistant.
- All 10 translation files (`da de en et fi lt lv nb nl sv`) must have identical keys; non-English strings longer than 24 chars must not equal the English ones.
- Version 0.8.0.
- Run tests with `.venv/bin/python -m pytest tests -q`, lint with `.venv/bin/ruff check . && .venv/bin/ruff format --check .`.

## Review Focus

1. A battery temperature entity reporting **°F** must be converted to °C before any lookup — pinned in Task 4.
2. A charge meter reporting **Wh** must yield the same ratio as one reporting kWh (reuse `_read_energy_kwh`) — pinned in Task 5.
3. An existing store written by v0.7.x (no `charge_rate` key) must load with an empty model and no warning — pinned in Task 4.
4. Moving the sliders after bands were learned must keep the learned samples and use the new curve for unlearned bands — pinned in Task 4.
5. A coordinator refresh in the middle of a charge slot re-applies charge; the observation must be closed and re-opened, not lost or double counted — pinned in Task 5.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `custom_components/smart_battery_pilot/forecast/charge_rate.py` | create | Curve math, samples, per-band learning, persistence dicts |
| `custom_components/smart_battery_pilot/optimizer.py` | modify | `BatteryState.charge_factor`, charge cap, requested power |
| `custom_components/smart_battery_pilot/const.py` | modify | New config keys and defaults |
| `custom_components/smart_battery_pilot/config_flow.py` | modify | `schema_derating`, options menu step, validation |
| `custom_components/smart_battery_pilot/translations/*.json` | modify | Labels, help, menu entry, errors in 10 languages |
| `custom_components/smart_battery_pilot/coordinator.py` | modify | Temperature read, factor, persistence, observations |
| `custom_components/smart_battery_pilot/executor.py` | modify | Report applied charge slots to the coordinator |
| `custom_components/smart_battery_pilot/sensor.py` | modify | Config sensor attributes |
| `custom_components/smart_battery_pilot/diagnostics.py` | modify | Learned bands |
| `docs/configuration.md`, `docs/optimizer.md`, `README.md` | modify | User docs |
| `custom_components/smart_battery_pilot/manifest.json` | modify | Version 0.8.0 |
| `tests/test_charge_rate.py` | create | Pure model tests |
| `tests/test_optimizer_derating.py` | create | Optimizer tests |
| `tests/test_config_flow.py`, `tests/test_translations.py` | modify | Flow and translation tests |
| `tests/test_coordinator_derating.py` | create | Coordinator factor, persistence, observation tests |
| `tests/test_executor.py` | modify | Fake coordinator gets `charge_observation` |
| `tests_ha/test_smoke.py` | modify | Setup with derating on |

---

### Task 1: Charge rate model (pure)

**Files:**
- Create: `custom_components/smart_battery_pilot/forecast/charge_rate.py`
- Test: `tests/test_charge_rate.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CURVE_TEMPERATURES: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0)`
  - `SOURCE_OFF = "off"`, `SOURCE_CURVE = "curve"`, `SOURCE_LEARNED = "learned"`
  - `Curve = list[tuple[float, float]]` (temperature °C, factor 0..1)
  - `curve_from_percentages(percentages: Sequence[float]) -> Curve`
  - `is_non_decreasing(values: Sequence[float]) -> bool`
  - `curve_factor(curve: Curve, temperature: float) -> float`
  - `band_of(temperature: float) -> float`
  - `ChargeSample(temperature: float, ratio: float, saturated: bool, at: datetime)` (frozen dataclass)
  - `ChargeRateModel(curve: Curve)` with `.curve`, `.samples -> tuple[ChargeSample, ...]`, `.add_sample(sample) -> None`, `.learned(band: float) -> float | None`, `.factor(temperature: float | None) -> float`, `.source(temperature: float | None) -> str`, `.bands() -> list[dict[str, Any]]`, `.learned_band_count -> int`, `.to_dict() -> dict[str, Any]`, `ChargeRateModel.from_dict(data: dict[str, Any], curve: Curve) -> ChargeRateModel`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_charge_rate.py`:

```python
"""Charge rate model: curve, per-band learning, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from smart_battery_pilot.forecast.charge_rate import (
    CURVE_TEMPERATURES,
    MAX_SAMPLES,
    SOURCE_CURVE,
    SOURCE_LEARNED,
    SOURCE_OFF,
    ChargeRateModel,
    ChargeSample,
    band_of,
    curve_factor,
    curve_from_percentages,
    is_non_decreasing,
)

AT = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
DEFAULT = curve_from_percentages([10, 20, 50, 80, 100])


def _sample(temperature, ratio, saturated=True, at=AT):
    return ChargeSample(temperature=temperature, ratio=ratio, saturated=saturated, at=at)


def _model_with(samples):
    model = ChargeRateModel(DEFAULT)
    for s in samples:
        model.add_sample(s)
    return model


# --- curve --------------------------------------------------------------------


def test_curve_uses_the_fixed_temperatures():
    assert [t for t, _ in DEFAULT] == list(CURVE_TEMPERATURES)
    assert [f for _, f in DEFAULT] == [0.10, 0.20, 0.50, 0.80, 1.00]


def test_curve_clamps_slider_values():
    assert [f for _, f in curve_from_percentages([-5, 20, 50, 80, 150])] == [
        0.0,
        0.2,
        0.5,
        0.8,
        1.0,
    ]


def test_curve_needs_one_value_per_temperature():
    with pytest.raises(ValueError):
        curve_from_percentages([10, 20])


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [(-10.0, 0.10), (0.0, 0.10), (2.5, 0.15), (12.0, 0.62), (20.0, 1.0), (35.0, 1.0)],
)
def test_curve_is_linear_and_clamped(temperature, expected):
    assert curve_factor(DEFAULT, temperature) == pytest.approx(expected)


def test_monotonic_check():
    assert is_non_decreasing([10, 20, 20, 80, 100])
    assert not is_non_decreasing([10, 30, 20, 80, 100])


def test_bands_are_five_degrees_wide():
    assert band_of(3.0) == 0.0
    assert band_of(5.0) == 5.0
    assert band_of(-0.1) == -5.0


# --- lookup without learning -------------------------------------------------


def test_no_temperature_means_no_derating():
    model = ChargeRateModel(DEFAULT)
    assert model.factor(None) == 1.0
    assert model.source(None) == SOURCE_OFF


def test_an_empty_model_returns_the_curve_itself():
    model = ChargeRateModel(DEFAULT)
    for t in (-3.0, 0.0, 4.0, 12.0, 19.0, 30.0):
        assert model.factor(t) == pytest.approx(curve_factor(DEFAULT, t))
        assert model.source(t) == SOURCE_CURVE


# --- learning -----------------------------------------------------------------


def test_a_band_is_learned_at_the_sixth_saturated_sample():
    model = _model_with([_sample(3.0, 0.4)] * 5)
    assert model.source(2.5) == SOURCE_CURVE
    assert model.factor(2.5) == pytest.approx(0.15)

    model.add_sample(_sample(3.0, 0.4))
    assert model.source(2.5) == SOURCE_LEARNED
    assert model.factor(2.5) == pytest.approx(0.4)


def test_the_learned_value_is_the_median():
    ratios = [0.3, 0.3, 0.4, 0.4, 0.5, 0.9]
    model = _model_with([_sample(3.0, r) for r in ratios])
    assert model.learned(0.0) == pytest.approx(0.4)


def test_unsaturated_samples_raise_the_band_as_lower_bounds():
    samples = [_sample(3.0, 0.4)] * 6 + [_sample(3.0, 0.6, saturated=False)] * 4
    assert _model_with(samples).learned(0.0) == pytest.approx(0.6)


def test_unsaturated_samples_alone_never_learn_a_band():
    model = _model_with([_sample(3.0, 0.6, saturated=False)] * 10)
    assert model.learned(0.0) is None
    assert model.source(3.0) == SOURCE_CURVE


def test_learned_values_are_clamped_to_one():
    assert _model_with([_sample(3.0, 1.2)] * 6).learned(0.0) == 1.0


def test_interpolation_mixes_learned_and_curve_bands():
    model = _model_with([_sample(3.0, 0.4)] * 6)
    # Between the band-0 centre (2.5 °C, learned 0.4) and the band-5 centre
    # (7.5 °C, curve 0.35): halfway -> 0.375.
    assert model.factor(5.0) == pytest.approx(0.375)
    # Far from the learned band the curve applies unchanged.
    assert model.factor(12.0) == pytest.approx(curve_factor(DEFAULT, 12.0))


def test_the_factor_is_continuous_around_a_learned_band():
    model = _model_with([_sample(3.0, 0.4)] * 6)
    for edge in (7.5, -2.5):
        assert model.factor(edge - 1e-6) == pytest.approx(model.factor(edge + 1e-6), abs=1e-4)


# --- bookkeeping --------------------------------------------------------------


def test_samples_are_capped_newest_kept():
    model = _model_with(
        [_sample(3.0, 0.4, at=AT + timedelta(minutes=i)) for i in range(MAX_SAMPLES + 10)]
    )
    assert len(model.samples) == MAX_SAMPLES
    assert model.samples[-1].at == AT + timedelta(minutes=MAX_SAMPLES + 9)


def test_samples_older_than_two_years_are_dropped():
    model = _model_with([_sample(3.0, 0.4, at=AT - timedelta(days=800)), _sample(3.0, 0.5)])
    assert [s.ratio for s in model.samples] == [0.5]


def test_round_trip_keeps_samples_and_takes_the_new_curve():
    model = _model_with([_sample(3.0, 0.4)] * 6 + [_sample(12.0, 0.7, saturated=False)])
    other_curve = curve_from_percentages([0, 0, 50, 80, 100])
    restored = ChargeRateModel.from_dict(model.to_dict(), other_curve)
    assert restored.samples == model.samples
    assert restored.curve == other_curve
    assert restored.factor(2.5) == pytest.approx(0.4)


def test_bands_summary_for_diagnostics():
    model = _model_with([_sample(3.0, 0.4)] * 6 + [_sample(12.0, 0.7, saturated=False)])
    bands = model.bands()
    assert bands[0] == {
        "band_c": 0.0,
        "samples": 6,
        "saturated": 6,
        "learned": 0.4,
        "curve": 0.15,
    }
    assert bands[1]["band_c"] == 10.0
    assert bands[1]["learned"] is None
    assert model.learned_band_count == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_charge_rate.py -q`
Expected: collection ERROR, `ModuleNotFoundError: No module named 'smart_battery_pilot.forecast.charge_rate'`

- [ ] **Step 3: Implement the module**

Create `custom_components/smart_battery_pilot/forecast/charge_rate.py`:

```python
"""Charge power a cold battery actually accepts.

Two sources per 5 °C band: the curve the user set with five sliders, and what
forced charge slots have shown the battery to take. A band switches from the
curve to the learned value once it holds enough observations in which the
battery took clearly less than it was asked for - only those measure the
limit. Observations where it took everything are lower bounds: they can raise
a learned band but never learn one on their own.

The factor is a planning assumption only. The pilot never asks the battery for
less power because of it; the BMS does the throttling.

No Home Assistant imports, like the rest of `forecast`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import statistics
from typing import Any

CURVE_TEMPERATURES: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0)
BAND_WIDTH = 5.0
MIN_BAND_SAMPLES = 6
MAX_SAMPLES = 400
# Batteries age, and so does their cold behaviour.
MAX_SAMPLE_AGE = timedelta(days=730)
LOWER_BOUND_QUANTILE = 0.75

SOURCE_OFF = "off"
SOURCE_CURVE = "curve"
SOURCE_LEARNED = "learned"

Curve = list[tuple[float, float]]


def curve_from_percentages(percentages: Sequence[float]) -> Curve:
    """Pair the slider values (percent) with the fixed support temperatures."""
    if len(percentages) != len(CURVE_TEMPERATURES):
        raise ValueError(
            f"expected {len(CURVE_TEMPERATURES)} curve values, got {len(percentages)}"
        )
    return [
        (temperature, max(0.0, min(100.0, float(percent))) / 100.0)
        for temperature, percent in zip(CURVE_TEMPERATURES, percentages, strict=True)
    ]


def is_non_decreasing(values: Sequence[float]) -> bool:
    """A warmer battery never takes less - the flow refuses falling sliders."""
    return all(b >= a for a, b in zip(values, values[1:], strict=False))


def curve_factor(curve: Curve, temperature: float) -> float:
    """Linear between the support points, clamped outside them."""
    if temperature <= curve[0][0]:
        return curve[0][1]
    if temperature >= curve[-1][0]:
        return curve[-1][1]
    for (t0, f0), (t1, f1) in zip(curve, curve[1:], strict=False):
        if t0 <= temperature <= t1:
            return f0 + (f1 - f0) * (temperature - t0) / (t1 - t0)
    return curve[-1][1]


def band_of(temperature: float) -> float:
    """Lower bound of the 5 °C band the temperature falls into."""
    return math.floor(temperature / BAND_WIDTH) * BAND_WIDTH


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True, slots=True)
class ChargeSample:
    """One forced charge slot the pilot really ran."""

    temperature: float  # °C at the start of the slot
    ratio: float  # achieved / nominal charge power (battery side)
    saturated: bool  # the battery took clearly less than was requested
    at: datetime


class ChargeRateModel:
    """Curve plus learned bands; answers "which share of max charge power"."""

    def __init__(self, curve: Curve) -> None:
        self.curve: Curve = sorted(curve)
        self._samples: list[ChargeSample] = []

    @property
    def samples(self) -> tuple[ChargeSample, ...]:
        return tuple(self._samples)

    def add_sample(self, sample: ChargeSample) -> None:
        self._samples.append(sample)
        cutoff = sample.at - MAX_SAMPLE_AGE
        self._samples = [s for s in self._samples if s.at >= cutoff][-MAX_SAMPLES:]

    def _in_band(self, band: float) -> list[ChargeSample]:
        return [s for s in self._samples if band_of(s.temperature) == band]

    def learned(self, band: float) -> float | None:
        """The band's learned factor, or None while it lacks observations."""
        in_band = self._in_band(band)
        saturated = [s.ratio for s in in_band if s.saturated]
        if len(saturated) < MIN_BAND_SAMPLES:
            return None
        value = statistics.median(saturated)
        bounds = [s.ratio for s in in_band if not s.saturated]
        if bounds:
            value = max(value, _quantile(bounds, LOWER_BOUND_QUANTILE))
        return max(0.0, min(1.0, value))

    def factor(self, temperature: float | None) -> float:
        """Share of the max charge power the battery takes at `temperature`.

        Interpolates between band centres. Where neither neighbouring band is
        learned the curve applies unchanged, so an empty model is exactly the
        curve; a learned centre is joined to the curve value at the next one,
        which keeps the factor continuous.
        """
        if temperature is None:
            return 1.0
        half = BAND_WIDTH / 2
        centre_lo = band_of(temperature - half) + half
        centre_hi = centre_lo + BAND_WIDTH
        learned_lo = self.learned(band_of(centre_lo))
        learned_hi = self.learned(band_of(centre_hi))
        if learned_lo is None and learned_hi is None:
            return curve_factor(self.curve, temperature)
        lo = learned_lo if learned_lo is not None else curve_factor(self.curve, centre_lo)
        hi = learned_hi if learned_hi is not None else curve_factor(self.curve, centre_hi)
        share = (temperature - centre_lo) / BAND_WIDTH
        return max(0.0, min(1.0, lo + (hi - lo) * share))

    def source(self, temperature: float | None) -> str:
        if temperature is None:
            return SOURCE_OFF
        if self.learned(band_of(temperature)) is not None:
            return SOURCE_LEARNED
        return SOURCE_CURVE

    def bands(self) -> list[dict[str, Any]]:
        """Per-band summary for the diagnostics dump."""
        summary = []
        for band in sorted({band_of(s.temperature) for s in self._samples}):
            in_band = self._in_band(band)
            learned = self.learned(band)
            summary.append(
                {
                    "band_c": band,
                    "samples": len(in_band),
                    "saturated": sum(1 for s in in_band if s.saturated),
                    "learned": round(learned, 3) if learned is not None else None,
                    "curve": round(curve_factor(self.curve, band + BAND_WIDTH / 2), 3),
                }
            )
        return summary

    @property
    def learned_band_count(self) -> int:
        return sum(1 for band in self.bands() if band["learned"] is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": [
                {
                    "temperature": s.temperature,
                    "ratio": s.ratio,
                    "saturated": s.saturated,
                    "at": s.at.isoformat(),
                }
                for s in self._samples
            ]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], curve: Curve) -> ChargeRateModel:
        """Restore the samples; the curve always comes from the current options."""
        model = cls(curve)
        model._samples = [
            ChargeSample(
                temperature=float(row["temperature"]),
                ratio=float(row["ratio"]),
                saturated=bool(row["saturated"]),
                at=datetime.fromisoformat(row["at"]),
            )
            for row in data.get("samples", [])
        ]
        return model
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_charge_rate.py -q`
Expected: all PASS. Then `.venv/bin/ruff check . && .venv/bin/ruff format --check .` (run `.venv/bin/ruff format .` first if it reports files).

- [ ] **Step 5: Commit**

```bash
git add custom_components/smart_battery_pilot/forecast/charge_rate.py tests/test_charge_rate.py
git commit -m "Add the charge rate model: slider curve plus learned 5 °C bands"
```

---

### Task 2: Optimizer takes a charge factor

**Files:**
- Modify: `custom_components/smart_battery_pilot/optimizer.py` (`BatteryState`, `build_plan`: `charge_cap`, debug input line, charge `power_w`)
- Test: `tests/test_optimizer_derating.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `BatteryState.charge_factor: float = 1.0` (last field, default keeps every existing constructor call valid).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_optimizer_derating.py`:

```python
"""A cold battery takes less: the planner spreads the charge, the script
still gets asked for full power."""

from __future__ import annotations

from dataclasses import replace
import math

from smart_battery_pilot.const import ACTION_CHARGE
from smart_battery_pilot.optimizer import build_plan
from test_optimizer import BATTERY, CONFIG, make_slots

PRICES = [0.10] * 6 + [0.50] * 4
ETA_ONE_WAY = math.sqrt(BATTERY.efficiency / 100.0)


def _charge_slots(plan):
    return [s for s in plan.slots if s.action == ACTION_CHARGE]


def test_factor_one_is_todays_plan():
    slots = make_slots(PRICES, demand_kwh=2.0)
    assert build_plan(slots, replace(BATTERY, charge_factor=1.0), CONFIG) == build_plan(
        slots, BATTERY, CONFIG
    )


def test_a_cold_battery_is_charged_over_more_slots():
    slots = make_slots(PRICES, demand_kwh=2.0)
    warm = build_plan(slots, BATTERY, CONFIG)
    cold = build_plan(slots, replace(BATTERY, charge_factor=0.25), CONFIG)
    assert len(_charge_slots(cold)) > len(_charge_slots(warm))


def test_the_soc_forecast_respects_the_derated_rate():
    factor = 0.25
    cold = build_plan(make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=factor), CONFIG)
    cap_kwh = BATTERY.max_charge_power_w / 1000.0 * factor * ETA_ONE_WAY  # 1 h slots
    previous = BATTERY.soc
    for slot in cold.slots:
        if slot.action == ACTION_CHARGE:
            stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
            assert stored <= cap_kwh + 0.01
        previous = slot.soc_forecast


def test_the_script_is_asked_for_full_power_not_the_derated_one():
    cold = build_plan(
        make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=0.25), CONFIG
    )
    powers = [s.power_w for s in _charge_slots(cold)]
    assert all(p <= BATTERY.max_charge_power_w + 0.5 for p in powers)
    # A slot charged up to the derated cap requests the nominal maximum.
    assert any(abs(p - BATTERY.max_charge_power_w) < 1.0 for p in powers)


def test_requested_power_is_the_planned_grid_power_scaled_up():
    factor = 0.5
    cold = build_plan(
        make_slots(PRICES, demand_kwh=0.5), replace(BATTERY, charge_factor=factor), CONFIG
    )
    previous = BATTERY.soc
    for slot in cold.slots:
        if slot.action == ACTION_CHARGE:
            stored = (slot.soc_forecast - previous) / 100.0 * BATTERY.capacity_kwh
            planned_grid_w = stored / ETA_ONE_WAY * 1000.0  # 1 h slots
            expected = min(BATTERY.max_charge_power_w, planned_grid_w / factor)
            # soc_forecast is rounded to 0.1 %, i.e. ~13 Wh on 12.8 kWh.
            assert abs(slot.power_w - expected) < 40.0
            assert slot.power_w >= planned_grid_w
        previous = slot.soc_forecast


def test_factor_zero_plans_no_grid_charging():
    plan = build_plan(
        make_slots(PRICES, demand_kwh=2.0), replace(BATTERY, charge_factor=0.0), CONFIG
    )
    assert _charge_slots(plan) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_optimizer_derating.py -q`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'charge_factor'` (from `replace`).

- [ ] **Step 3: Implement**

In `optimizer.py`, extend `BatteryState` (after `efficiency`):

```python
    efficiency: float  # roundtrip efficiency, percent (e.g. 90)
    # Share of max_charge_power_w a cold battery is expected to accept. A
    # planning assumption only: it shrinks the charge the model counts on per
    # slot, while the power requested from the script is scaled back up.
    charge_factor: float = 1.0
```

In `build_plan`, replace the `charge_cap` line:

```python
    charge_factor = max(0.0, min(1.0, battery.charge_factor))
    charge_cap = [
        battery.max_charge_power_w * charge_factor / 1000.0 * h * eta_one_way for h in hours
    ]
```

In the debug input line (the `"Planning %d slots ..."` call), append `", charge factor %.2f"` to the format string and `charge_factor` as the last argument.

In the plan-building loop, the charge branch becomes:

```python
        if charge_stored[i] > 1e-9:
            action = ACTION_CHARGE
            grid_kwh = charge_stored[i] / eta_one_way
            charge_power = grid_kwh / hours[i] * 1000.0
            if 0.0 < charge_factor < 1.0:
                # The planned power already has the cold limit in it. Passing
                # it on would make the pilot throttle the battery itself;
                # request what stores the planned energy at the expected
                # acceptance instead, and leave the limiting to the BMS.
                charge_power = min(battery.max_charge_power_w, charge_power / charge_factor)
            plan.grid_charge_kwh += grid_kwh
```

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all PASS (existing optimizer tests unchanged because the default factor is 1.0). Then ruff check/format.

- [ ] **Step 5: Commit**

```bash
git add custom_components/smart_battery_pilot/optimizer.py tests/test_optimizer_derating.py
git commit -m "Optimizer: derate the charge cap, keep requesting full power"
```

---

### Task 3: Options section "Cold-weather charging"

**Files:**
- Modify: `custom_components/smart_battery_pilot/const.py`
- Modify: `custom_components/smart_battery_pilot/config_flow.py`
- Modify: all 10 files in `custom_components/smart_battery_pilot/translations/`
- Test: `tests/test_config_flow.py`, `tests/test_translations.py`

**Interfaces:**
- Consumes: `is_non_decreasing` from Task 1.
- Produces (in `const.py`):
  - `CONF_CHARGE_DERATING = "charge_derating"`
  - `CONF_BATTERY_TEMPERATURE_ENTITY = "battery_temperature_entity"`
  - `CONF_DERATING_0C = "derating_0c"`, `CONF_DERATING_5C = "derating_5c"`, `CONF_DERATING_10C = "derating_10c"`, `CONF_DERATING_15C = "derating_15c"`, `CONF_DERATING_20C = "derating_20c"`
  - `DERATING_CURVE_KEYS: tuple[str, ...]` (the five above, in temperature order)
  - `DEFAULT_CHARGE_DERATING = False`
  - `DEFAULT_DERATING_CURVE: tuple[int, ...] = (10, 20, 50, 80, 100)`
- Produces (in `config_flow.py`): `schema_derating(d: dict[str, Any]) -> vol.Schema`, `SBPOptionsFlow.async_step_derating`, `STEP_FIELDS["derating"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_flow.py` (add the new constants to its `from smart_battery_pilot.const import (...)` block: `CONF_BATTERY_TEMPERATURE_ENTITY, CONF_CHARGE_DERATING, DEFAULT_DERATING_CURVE, DERATING_CURVE_KEYS`):

```python
# --- cold-weather charging ------------------------------------------------------


def _derating_input(enabled=True, entity="sensor.battery_temp", values=DEFAULT_DERATING_CURVE):
    data = {CONF_CHARGE_DERATING: enabled, **dict(zip(DERATING_CURVE_KEYS, values, strict=True))}
    if entity:
        data[CONF_BATTERY_TEMPERATURE_ENTITY] = entity
    return data


def test_the_derating_section_is_in_the_options_menu_only():
    flow = _options_flow(_FakeHass(), _entry())
    menu = _run(flow.async_step_init())
    assert "derating" in menu["menu_options"]
    assert menu["menu_options"][-1] == "apply"
    assert not hasattr(cf.SBPConfigFlow, "async_step_derating")


def test_the_derating_defaults_are_off_and_the_generic_lfp_curve():
    defaults = {str(m): m.default() for m in cf.schema_derating({}).schema if callable(getattr(m, "default", None))}
    assert defaults[CONF_CHARGE_DERATING] is False
    assert [defaults[k] for k in DERATING_CURVE_KEYS] == [10, 20, 50, 80, 100]


def test_a_valid_derating_section_is_saved_on_apply():
    flow = _options_flow(_FakeHass(), _entry())
    result = _run(flow.async_step_derating(_derating_input(values=(10, 30, 60, 90, 100))))
    assert result["type"] == "menu"
    applied = _run(flow.async_step_apply())
    assert applied["data"][CONF_CHARGE_DERATING] is True
    assert applied["data"][CONF_BATTERY_TEMPERATURE_ENTITY] == "sensor.battery_temp"
    assert applied["data"]["derating_5c"] == 30


def test_falling_derating_values_are_refused():
    flow = _options_flow(_FakeHass(), _entry())
    result = _run(flow.async_step_derating(_derating_input(values=(10, 50, 40, 80, 100))))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "derating_not_monotonic"}


def test_derating_without_a_temperature_entity_is_refused():
    flow = _options_flow(_FakeHass(), _entry())
    result = _run(flow.async_step_derating(_derating_input(entity=None)))
    assert result["errors"] == {"base": "derating_needs_temperature"}


def test_switched_off_derating_needs_no_temperature_entity():
    flow = _options_flow(_FakeHass(), _entry())
    result = _run(flow.async_step_derating(_derating_input(enabled=False, entity=None)))
    assert result["type"] == "menu"
```

In `tests/test_translations.py`, add to `OPTIONS_STEPS`:

```python
    "derating": config_flow.schema_derating,
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_config_flow.py tests/test_translations.py -q`
Expected: collection ERROR `ImportError: cannot import name 'CONF_BATTERY_TEMPERATURE_ENTITY'`.

- [ ] **Step 3: Add the constants**

In `const.py`, after the `# Step: PV (optional)` block:

```python
# Options section: cold-weather charging (not part of the initial setup)
CONF_CHARGE_DERATING = "charge_derating"
CONF_BATTERY_TEMPERATURE_ENTITY = "battery_temperature_entity"
# Percent of max charge power at 0, 5, 10, 15 and 20 °C - the support points
# of forecast.charge_rate.CURVE_TEMPERATURES, in that order.
CONF_DERATING_0C = "derating_0c"
CONF_DERATING_5C = "derating_5c"
CONF_DERATING_10C = "derating_10c"
CONF_DERATING_15C = "derating_15c"
CONF_DERATING_20C = "derating_20c"
DERATING_CURVE_KEYS = (
    CONF_DERATING_0C,
    CONF_DERATING_5C,
    CONF_DERATING_10C,
    CONF_DERATING_15C,
    CONF_DERATING_20C,
)
```

and in the defaults block:

```python
DEFAULT_CHARGE_DERATING = False
# Conservative generic LFP: many BMS still allow a trickle charge at 0 °C.
DEFAULT_DERATING_CURVE = (10, 20, 50, 80, 100)
```

- [ ] **Step 4: Add schema, step and validation**

In `config_flow.py`, import the new constants and `from .forecast.charge_rate import is_non_decreasing`. Add after `_SCRIPT`:

```python
_BATTERY_TEMPERATURE = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)


def _percent_slider() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=100, step=5, unit_of_measurement="%", mode="slider"
        )
    )
```

Add after `schema_tuning`:

```python
def schema_derating(d: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_CHARGE_DERATING, default=d.get(CONF_CHARGE_DERATING, DEFAULT_CHARGE_DERATING)
        ): selector.BooleanSelector(),
        vol.Optional(
            CONF_BATTERY_TEMPERATURE_ENTITY,
            description=_sugg(d.get(CONF_BATTERY_TEMPERATURE_ENTITY)),
        ): _BATTERY_TEMPERATURE,
    }
    for key, default in zip(DERATING_CURVE_KEYS, DEFAULT_DERATING_CURVE, strict=True):
        fields[vol.Required(key, default=d.get(key, default))] = _percent_slider()
    return vol.Schema(fields)
```

Add to `STEP_FIELDS`:

```python
    "derating": [CONF_CHARGE_DERATING, CONF_BATTERY_TEMPERATURE_ENTITY, *DERATING_CURVE_KEYS],
```

Add a validator next to `_export_script_is_configured`:

```python
def _derating_error(merged: dict[str, Any]) -> str | None:
    """Error key for the cold-weather section, or None."""
    values = [
        float(merged.get(key, default))
        for key, default in zip(DERATING_CURVE_KEYS, DEFAULT_DERATING_CURVE, strict=True)
    ]
    if not is_non_decreasing(values):
        return "derating_not_monotonic"
    if merged.get(CONF_CHARGE_DERATING) and not merged.get(CONF_BATTERY_TEMPERATURE_ENTITY):
        return "derating_needs_temperature"
    return None
```

In `SBPOptionsFlow.async_step_init`, insert `"derating"` between `"pv"` and `"apply"` in `menu_options`. Add the step after `async_step_pv`:

```python
    async def async_step_derating(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _derating_error(self._would_be("derating", user_input))
            if error:
                errors["base"] = error
            else:
                return await self._save_step("derating", user_input)
        return self.async_show_form(
            step_id="derating", data_schema=schema_derating(self._merged), errors=errors
        )
```

- [ ] **Step 5: Add the translations**

Run this one-off merge inline (it is not meant to live in the repo):

```bash
.venv/bin/python - <<'EOF'
import json
from pathlib import Path

T = {
 "en": ("Cold-weather charging",
  {"charge_derating": "Plan with reduced charge power in the cold",
   "battery_temperature_entity": "Battery temperature entity",
   "derating_0c": "Charge power at 0 °C and below", "derating_5c": "Charge power at 5 °C",
   "derating_10c": "Charge power at 10 °C", "derating_15c": "Charge power at 15 °C",
   "derating_20c": "Charge power at 20 °C and above"},
  {"charge_derating": "The planner expects a cold battery to accept less than the maximum charge power and schedules more cheap charge slots. The charge script is still asked for full power; the battery's BMS does the throttling.",
   "battery_temperature_entity": "Cell or module temperature of the battery, not the outdoor temperature. Required when the option is on.",
   "derating_0c": "Percent of the maximum charge power the battery accepts at 0 °C and colder. Check your battery's datasheet.",
   "derating_5c": "Percent of the maximum charge power the battery accepts at 5 °C.",
   "derating_10c": "Percent of the maximum charge power the battery accepts at 10 °C.",
   "derating_15c": "Percent of the maximum charge power the battery accepts at 15 °C.",
   "derating_20c": "Percent of the maximum charge power at 20 °C and warmer. Usually 100 %."},
  {"derating_not_monotonic": "The charge power must not fall as the temperature rises. Check the sliders.",
   "derating_needs_temperature": "Cold-weather charging needs a battery temperature entity."}),
 "de": ("Laden bei Kälte",
  {"charge_derating": "Reduzierte Ladeleistung bei Kälte einplanen",
   "battery_temperature_entity": "Batterietemperatur-Entität",
   "derating_0c": "Ladeleistung bei 0 °C und kälter", "derating_5c": "Ladeleistung bei 5 °C",
   "derating_10c": "Ladeleistung bei 10 °C", "derating_15c": "Ladeleistung bei 15 °C",
   "derating_20c": "Ladeleistung bei 20 °C und wärmer"},
  {"charge_derating": "Der Planer rechnet damit, dass eine kalte Batterie weniger als die maximale Ladeleistung aufnimmt, und plant mehr günstige Ladeslots ein. Das Lade-Skript bekommt weiterhin die volle Leistung; die Drosselung übernimmt das BMS der Batterie.",
   "battery_temperature_entity": "Zellen- oder Modultemperatur der Batterie, nicht die Außentemperatur. Pflicht, wenn die Option aktiv ist.",
   "derating_0c": "Prozent der maximalen Ladeleistung, die die Batterie bei 0 °C und kälter aufnimmt. Werte stehen im Datenblatt der Batterie.",
   "derating_5c": "Prozent der maximalen Ladeleistung, die die Batterie bei 5 °C aufnimmt.",
   "derating_10c": "Prozent der maximalen Ladeleistung, die die Batterie bei 10 °C aufnimmt.",
   "derating_15c": "Prozent der maximalen Ladeleistung, die die Batterie bei 15 °C aufnimmt.",
   "derating_20c": "Prozent der maximalen Ladeleistung bei 20 °C und wärmer. Meist 100 %."},
  {"derating_not_monotonic": "Die Ladeleistung darf mit steigender Temperatur nicht sinken. Bitte die Schieberegler prüfen.",
   "derating_needs_temperature": "Laden bei Kälte braucht eine Entität für die Batterietemperatur."}),
 "da": ("Opladning i kulde",
  {"charge_derating": "Planlæg med reduceret ladeeffekt i kulde",
   "battery_temperature_entity": "Entitet for batteritemperatur",
   "derating_0c": "Ladeeffekt ved 0 °C og derunder", "derating_5c": "Ladeeffekt ved 5 °C",
   "derating_10c": "Ladeeffekt ved 10 °C", "derating_15c": "Ladeeffekt ved 15 °C",
   "derating_20c": "Ladeeffekt ved 20 °C og derover"},
  {"charge_derating": "Planlæggeren regner med, at et koldt batteri modtager mindre end den maksimale ladeeffekt, og planlægger flere billige ladeperioder. Ladescriptet beder stadig om fuld effekt; batteriets BMS står for begrænsningen.",
   "battery_temperature_entity": "Celle- eller modultemperatur for batteriet, ikke udetemperaturen. Påkrævet, når indstillingen er slået til.",
   "derating_0c": "Procent af den maksimale ladeeffekt, som batteriet modtager ved 0 °C og koldere. Se batteriets datablad.",
   "derating_5c": "Procent af den maksimale ladeeffekt, som batteriet modtager ved 5 °C.",
   "derating_10c": "Procent af den maksimale ladeeffekt, som batteriet modtager ved 10 °C.",
   "derating_15c": "Procent af den maksimale ladeeffekt, som batteriet modtager ved 15 °C.",
   "derating_20c": "Procent af den maksimale ladeeffekt ved 20 °C og varmere. Normalt 100 %."},
  {"derating_not_monotonic": "Ladeeffekten må ikke falde, når temperaturen stiger. Kontrollér skyderne.",
   "derating_needs_temperature": "Opladning i kulde kræver en entitet for batteritemperaturen."}),
 "et": ("Laadimine külmaga",
  {"charge_derating": "Arvesta külmaga vähendatud laadimisvõimsusega",
   "battery_temperature_entity": "Aku temperatuuri olem",
   "derating_0c": "Laadimisvõimsus 0 °C ja alla selle", "derating_5c": "Laadimisvõimsus 5 °C juures",
   "derating_10c": "Laadimisvõimsus 10 °C juures", "derating_15c": "Laadimisvõimsus 15 °C juures",
   "derating_20c": "Laadimisvõimsus 20 °C ja üle selle"},
  {"charge_derating": "Planeerija eeldab, et külm aku võtab vastu vähem kui maksimaalse laadimisvõimsuse, ja planeerib rohkem odavaid laadimisperioode. Laadimisskript küsib endiselt täit võimsust; piiramise teeb aku BMS.",
   "battery_temperature_entity": "Aku elemendi või mooduli temperatuur, mitte välistemperatuur. Kohustuslik, kui valik on sisse lülitatud.",
   "derating_0c": "Protsent maksimaalsest laadimisvõimsusest, mida aku 0 °C ja külmemal vastu võtab. Vaata aku andmelehte.",
   "derating_5c": "Protsent maksimaalsest laadimisvõimsusest, mida aku 5 °C juures vastu võtab.",
   "derating_10c": "Protsent maksimaalsest laadimisvõimsusest, mida aku 10 °C juures vastu võtab.",
   "derating_15c": "Protsent maksimaalsest laadimisvõimsusest, mida aku 15 °C juures vastu võtab.",
   "derating_20c": "Protsent maksimaalsest laadimisvõimsusest 20 °C ja soojemal. Tavaliselt 100 %."},
  {"derating_not_monotonic": "Laadimisvõimsus ei tohi temperatuuri tõustes langeda. Kontrolli liugureid.",
   "derating_needs_temperature": "Laadimine külmaga vajab aku temperatuuri olemit."}),
 "fi": ("Lataus kylmällä",
  {"charge_derating": "Suunnittele kylmällä alennetulla latausteholla",
   "battery_temperature_entity": "Akun lämpötilan entiteetti",
   "derating_0c": "Latausteho 0 °C:ssa ja alle", "derating_5c": "Latausteho 5 °C:ssa",
   "derating_10c": "Latausteho 10 °C:ssa", "derating_15c": "Latausteho 15 °C:ssa",
   "derating_20c": "Latausteho 20 °C:ssa ja yli"},
  {"charge_derating": "Suunnittelija olettaa, että kylmä akku ottaa vastaan vähemmän kuin suurimman lataustehon, ja suunnittelee enemmän edullisia latausjaksoja. Latausskripti pyytää edelleen täyttä tehoa; rajoituksen hoitaa akun BMS.",
   "battery_temperature_entity": "Akun kennon tai moduulin lämpötila, ei ulkolämpötila. Pakollinen, kun asetus on päällä.",
   "derating_0c": "Prosentti suurimmasta lataustehosta, jonka akku ottaa vastaan 0 °C:ssa ja kylmemmässä. Katso akun tuotetiedot.",
   "derating_5c": "Prosentti suurimmasta lataustehosta, jonka akku ottaa vastaan 5 °C:ssa.",
   "derating_10c": "Prosentti suurimmasta lataustehosta, jonka akku ottaa vastaan 10 °C:ssa.",
   "derating_15c": "Prosentti suurimmasta lataustehosta, jonka akku ottaa vastaan 15 °C:ssa.",
   "derating_20c": "Prosentti suurimmasta lataustehosta 20 °C:ssa ja lämpimämmässä. Yleensä 100 %."},
  {"derating_not_monotonic": "Lataustehon ei pidä laskea lämpötilan noustessa. Tarkista liukusäätimet.",
   "derating_needs_temperature": "Lataus kylmällä vaatii akun lämpötilan entiteetin."}),
 "lt": ("Įkrovimas šaltyje",
  {"charge_derating": "Planuoti su sumažinta įkrovimo galia šaltyje",
   "battery_temperature_entity": "Baterijos temperatūros objektas",
   "derating_0c": "Įkrovimo galia esant 0 °C ir mažiau", "derating_5c": "Įkrovimo galia esant 5 °C",
   "derating_10c": "Įkrovimo galia esant 10 °C", "derating_15c": "Įkrovimo galia esant 15 °C",
   "derating_20c": "Įkrovimo galia esant 20 °C ir daugiau"},
  {"charge_derating": "Planuoklis tikisi, kad šalta baterija priims mažiau nei didžiausią įkrovimo galią, ir suplanuoja daugiau pigių įkrovimo intervalų. Įkrovimo scenarijus vis tiek prašo visos galios; ribojimą atlieka baterijos BMS.",
   "battery_temperature_entity": "Baterijos elemento ar modulio temperatūra, ne lauko temperatūra. Privaloma, kai parinktis įjungta.",
   "derating_0c": "Didžiausios įkrovimo galios procentas, kurį baterija priima esant 0 °C ir šalčiau. Žr. baterijos duomenų lapą.",
   "derating_5c": "Didžiausios įkrovimo galios procentas, kurį baterija priima esant 5 °C.",
   "derating_10c": "Didžiausios įkrovimo galios procentas, kurį baterija priima esant 10 °C.",
   "derating_15c": "Didžiausios įkrovimo galios procentas, kurį baterija priima esant 15 °C.",
   "derating_20c": "Didžiausios įkrovimo galios procentas esant 20 °C ir šilčiau. Paprastai 100 %."},
  {"derating_not_monotonic": "Įkrovimo galia neturi mažėti kylant temperatūrai. Patikrinkite slankiklius.",
   "derating_needs_temperature": "Įkrovimui šaltyje reikia baterijos temperatūros objekto."}),
 "lv": ("Uzlāde aukstumā",
  {"charge_derating": "Plānot ar samazinātu uzlādes jaudu aukstumā",
   "battery_temperature_entity": "Akumulatora temperatūras entītija",
   "derating_0c": "Uzlādes jauda pie 0 °C un zemāk", "derating_5c": "Uzlādes jauda pie 5 °C",
   "derating_10c": "Uzlādes jauda pie 10 °C", "derating_15c": "Uzlādes jauda pie 15 °C",
   "derating_20c": "Uzlādes jauda pie 20 °C un augstāk"},
  {"charge_derating": "Plānotājs pieņem, ka auksts akumulators pieņem mazāk nekā maksimālo uzlādes jaudu, un ieplāno vairāk lētu uzlādes periodu. Uzlādes skripts joprojām pieprasa pilnu jaudu; ierobežošanu veic akumulatora BMS.",
   "battery_temperature_entity": "Akumulatora šūnas vai moduļa temperatūra, nevis āra temperatūra. Obligāta, ja opcija ir ieslēgta.",
   "derating_0c": "Procenti no maksimālās uzlādes jaudas, ko akumulators pieņem pie 0 °C un aukstākā laikā. Skatiet akumulatora datu lapu.",
   "derating_5c": "Procenti no maksimālās uzlādes jaudas, ko akumulators pieņem pie 5 °C.",
   "derating_10c": "Procenti no maksimālās uzlādes jaudas, ko akumulators pieņem pie 10 °C.",
   "derating_15c": "Procenti no maksimālās uzlādes jaudas, ko akumulators pieņem pie 15 °C.",
   "derating_20c": "Procenti no maksimālās uzlādes jaudas pie 20 °C un siltākā laikā. Parasti 100 %."},
  {"derating_not_monotonic": "Uzlādes jauda nedrīkst samazināties, temperatūrai pieaugot. Pārbaudiet slīdņus.",
   "derating_needs_temperature": "Uzlādei aukstumā nepieciešama akumulatora temperatūras entītija."}),
 "nb": ("Lading i kulde",
  {"charge_derating": "Planlegg med redusert ladeeffekt i kulde",
   "battery_temperature_entity": "Entitet for batteritemperatur",
   "derating_0c": "Ladeeffekt ved 0 °C og lavere", "derating_5c": "Ladeeffekt ved 5 °C",
   "derating_10c": "Ladeeffekt ved 10 °C", "derating_15c": "Ladeeffekt ved 15 °C",
   "derating_20c": "Ladeeffekt ved 20 °C og høyere"},
  {"charge_derating": "Planleggeren regner med at et kaldt batteri tar imot mindre enn maksimal ladeeffekt, og planlegger flere billige ladeperioder. Ladeskriptet ber fortsatt om full effekt; batteriets BMS står for begrensningen.",
   "battery_temperature_entity": "Celle- eller modultemperatur for batteriet, ikke utetemperaturen. Påkrevd når innstillingen er på.",
   "derating_0c": "Prosent av maksimal ladeeffekt som batteriet tar imot ved 0 °C og kaldere. Se batteriets datablad.",
   "derating_5c": "Prosent av maksimal ladeeffekt som batteriet tar imot ved 5 °C.",
   "derating_10c": "Prosent av maksimal ladeeffekt som batteriet tar imot ved 10 °C.",
   "derating_15c": "Prosent av maksimal ladeeffekt som batteriet tar imot ved 15 °C.",
   "derating_20c": "Prosent av maksimal ladeeffekt ved 20 °C og varmere. Vanligvis 100 %."},
  {"derating_not_monotonic": "Ladeeffekten skal ikke synke når temperaturen stiger. Kontroller glidebryterne.",
   "derating_needs_temperature": "Lading i kulde krever en entitet for batteritemperaturen."}),
 "nl": ("Laden bij kou",
  {"charge_derating": "Plannen met verlaagd laadvermogen bij kou",
   "battery_temperature_entity": "Entiteit voor batterijtemperatuur",
   "derating_0c": "Laadvermogen bij 0 °C en lager", "derating_5c": "Laadvermogen bij 5 °C",
   "derating_10c": "Laadvermogen bij 10 °C", "derating_15c": "Laadvermogen bij 15 °C",
   "derating_20c": "Laadvermogen bij 20 °C en hoger"},
  {"charge_derating": "De planner gaat ervan uit dat een koude batterij minder dan het maximale laadvermogen opneemt en plant meer goedkope laadperiodes in. Het laadscript vraagt nog steeds het volle vermogen; het BMS van de batterij regelt de begrenzing.",
   "battery_temperature_entity": "Cel- of moduletemperatuur van de batterij, niet de buitentemperatuur. Verplicht als de optie aan staat.",
   "derating_0c": "Percentage van het maximale laadvermogen dat de batterij opneemt bij 0 °C en kouder. Zie het gegevensblad van de batterij.",
   "derating_5c": "Percentage van het maximale laadvermogen dat de batterij opneemt bij 5 °C.",
   "derating_10c": "Percentage van het maximale laadvermogen dat de batterij opneemt bij 10 °C.",
   "derating_15c": "Percentage van het maximale laadvermogen dat de batterij opneemt bij 15 °C.",
   "derating_20c": "Percentage van het maximale laadvermogen bij 20 °C en warmer. Meestal 100 %."},
  {"derating_not_monotonic": "Het laadvermogen mag niet dalen als de temperatuur stijgt. Controleer de schuifregelaars.",
   "derating_needs_temperature": "Laden bij kou heeft een entiteit voor de batterijtemperatuur nodig."}),
 "sv": ("Laddning i kyla",
  {"charge_derating": "Planera med reducerad laddeffekt i kyla",
   "battery_temperature_entity": "Entitet för batteritemperatur",
   "derating_0c": "Laddeffekt vid 0 °C och lägre", "derating_5c": "Laddeffekt vid 5 °C",
   "derating_10c": "Laddeffekt vid 10 °C", "derating_15c": "Laddeffekt vid 15 °C",
   "derating_20c": "Laddeffekt vid 20 °C och högre"},
  {"charge_derating": "Planeraren räknar med att ett kallt batteri tar emot mindre än den maximala laddeffekten och planerar fler billiga laddperioder. Laddskriptet begär fortfarande full effekt; batteriets BMS sköter begränsningen.",
   "battery_temperature_entity": "Cell- eller modultemperatur för batteriet, inte utomhustemperaturen. Krävs när alternativet är på.",
   "derating_0c": "Procent av den maximala laddeffekten som batteriet tar emot vid 0 °C och kallare. Se batteriets datablad.",
   "derating_5c": "Procent av den maximala laddeffekten som batteriet tar emot vid 5 °C.",
   "derating_10c": "Procent av den maximala laddeffekten som batteriet tar emot vid 10 °C.",
   "derating_15c": "Procent av den maximala laddeffekten som batteriet tar emot vid 15 °C.",
   "derating_20c": "Procent av den maximala laddeffekten vid 20 °C och varmare. Vanligtvis 100 %."},
  {"derating_not_monotonic": "Laddeffekten får inte sjunka när temperaturen stiger. Kontrollera skjutreglagen.",
   "derating_needs_temperature": "Laddning i kyla kräver en entitet för batteritemperaturen."}),
}

base = Path("custom_components/smart_battery_pilot/translations")
for lang, (title, data, desc, errors) in T.items():
    path = base / f"{lang}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    options = doc["options"]
    menu = options["step"]["init"]["menu_options"]
    apply = menu.pop("apply")
    menu["derating"] = title
    menu["apply"] = apply
    options["step"]["derating"] = {"title": title, "data": data, "data_description": desc}
    options.setdefault("error", {}).update(errors)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
EOF
git diff --stat custom_components/smart_battery_pilot/translations
```

Expected: 10 files changed, only additions (if `git diff` shows reformatting of untouched lines, the file used a different indent — revert with `git checkout` and match the original `indent`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all PASS, including every `test_translations.py` case for `derating`. Then ruff check/format.

- [ ] **Step 7: Commit**

```bash
git add custom_components/smart_battery_pilot/const.py custom_components/smart_battery_pilot/config_flow.py custom_components/smart_battery_pilot/translations tests/test_config_flow.py tests/test_translations.py
git commit -m "Options: cold-weather charging section with five temperature sliders"
```

---

### Task 4: Coordinator computes the factor and persists the model

**Files:**
- Modify: `custom_components/smart_battery_pilot/coordinator.py`
- Modify: `custom_components/smart_battery_pilot/sensor.py` (`ConfigSensor.extra_state_attributes`)
- Modify: `custom_components/smart_battery_pilot/diagnostics.py`
- Test: `tests/test_coordinator_derating.py`

**Interfaces:**
- Consumes: `ChargeRateModel`, `curve_from_percentages`, `SOURCE_OFF` (Task 1); `BatteryState.charge_factor` (Task 2); constants (Task 3).
- Produces:
  - `SBPCoordinator.charge_model: ChargeRateModel`
  - `SBPCoordinator.derating_enabled() -> bool`
  - `SBPCoordinator.charge_factor() -> tuple[float, str, float | None]` (factor, source, temperature °C). Source is one of `SOURCE_OFF`, `SOURCE_CURVE`, `SOURCE_LEARNED` or `"no_temperature"`.
  - `SBPCoordinator._read_temperature_c(entity_id: str | None) -> float | None`
  - `SBPData.inputs["charge_derating"] = {"enabled": bool, "temperature": float | None, "factor": float, "source": str}`
  - Store payload key `"charge_rate"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coordinator_derating.py`:

```python
"""Coordinator side of cold-weather charging: factor, warnings, persistence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

import pytest

from smart_battery_pilot.const import (
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CHARGE_DERATING,
    CONF_DERATING_0C,
    CONF_DERATING_5C,
)
from smart_battery_pilot.forecast.charge_rate import ChargeSample
from test_coordinator import _coordinator
from test_coordinator import _FakeHass as _CoordHass

DERATING = {
    CONF_CHARGE_DERATING: True,
    CONF_BATTERY_TEMPERATURE_ENTITY: "sensor.battery_temp",
}


def _run(coro):
    return asyncio.run(coro)


def _hass_with_prices(temp=None, unit="°C"):
    hass = _CoordHass()
    hass.states.set("sensor.price", 0.30, {"today": [0.10] * 12 + [0.50] * 12})
    hass.states.set("sensor.soc", 20.0)
    if temp is not None:
        hass.states.set("sensor.battery_temp", temp, {"unit_of_measurement": unit})
    return hass


def test_derating_off_passes_factor_one():
    hass = _hass_with_prices(temp=0.0)
    coord = _coordinator(hass)
    result = _run(coord._async_update_data())
    assert result.inputs["battery"]["charge_factor"] == 1.0
    assert result.inputs["charge_derating"] == {
        "enabled": False,
        "temperature": None,
        "factor": 1.0,
        "source": "off",
    }


def test_a_cold_battery_uses_the_curve():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    result = _run(coord._async_update_data())
    assert result.inputs["charge_derating"]["factor"] == 0.15
    assert result.inputs["charge_derating"]["source"] == "curve"
    assert result.inputs["battery"]["charge_factor"] == pytest.approx(0.15)


def test_fahrenheit_is_converted():
    hass = _hass_with_prices(temp=36.5, unit="°F")  # 2.5 °C
    coord = _coordinator(hass, **DERATING)
    factor, _, temperature = coord.charge_factor()
    assert round(temperature, 2) == 2.5
    assert round(factor, 3) == 0.15


def test_the_sliders_shape_the_curve():
    hass = _hass_with_prices(temp=0.0)
    coord = _coordinator(hass, **DERATING, **{CONF_DERATING_0C: 40, CONF_DERATING_5C: 60})
    assert coord.charge_factor()[0] == 0.4


def test_missing_temperature_falls_back_to_one_and_warns_once(caplog):
    hass = _hass_with_prices()
    coord = _coordinator(hass, **DERATING)
    caplog.set_level(logging.DEBUG, logger="smart_battery_pilot.coordinator")
    first = _run(coord._async_update_data())
    _run(coord._async_update_data())
    assert first.inputs["charge_derating"]["factor"] == 1.0
    assert first.inputs["charge_derating"]["source"] == "no_temperature"
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith("coordinator")
    ]
    assert len(warnings) == 1
    assert "sensor.battery_temp" in warnings[0].getMessage()


def test_samples_survive_a_restart():
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    for _ in range(6):
        coord.charge_model.add_sample(ChargeSample(3.0, 0.4, True, at))
    _run(coord.async_persist())

    restarted = _coordinator(hass, **DERATING)
    restarted._store = coord._store
    _run(restarted.async_setup())
    assert len(restarted.charge_model.samples) == 6
    assert restarted.charge_factor()[1] == "learned"


def test_moving_the_sliders_keeps_learned_samples():
    hass = _hass_with_prices(temp=12.0)
    coord = _coordinator(hass, **DERATING)
    at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    for _ in range(6):
        coord.charge_model.add_sample(ChargeSample(3.0, 0.4, True, at))
    _run(coord.async_persist())

    changed = _coordinator(hass, **DERATING, **{CONF_DERATING_0C: 0})
    changed._store = coord._store
    _run(changed.async_setup())
    assert len(changed.charge_model.samples) == 6
    assert changed.charge_model.curve[0] == (0.0, 0.0)


def test_a_store_from_v07_loads_an_empty_model(caplog):
    hass = _hass_with_prices(temp=2.5)
    coord = _coordinator(hass, **DERATING)
    coord._store._data = {"model": None, "last_applied": "auto"}
    caplog.set_level(logging.WARNING)
    _run(coord.async_setup())
    assert coord.charge_model.samples == ()
    assert caplog.records == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_coordinator_derating.py -q`
Expected: FAIL, e.g. `KeyError: 'charge_derating'` and `AttributeError: ... 'charge_factor'`.

- [ ] **Step 3: Implement in the coordinator**

Imports: add the Task 3 constants (`CONF_BATTERY_TEMPERATURE_ENTITY, CONF_CHARGE_DERATING, DEFAULT_CHARGE_DERATING, DEFAULT_DERATING_CURVE, DERATING_CURVE_KEYS`) and

```python
from .forecast.charge_rate import SOURCE_OFF, ChargeRateModel, Curve, curve_from_percentages
```

Add a module constant next to the other ones:

```python
SOURCE_NO_TEMPERATURE = "no_temperature"
```

In `__init__`, after `self.forecaster = ConsumptionForecaster()`:

```python
        self.charge_model = ChargeRateModel(self._derating_curve())
        # Battery temperature entity already warned about as unreadable; reset
        # once it reads again, so a later outage is reported afresh.
        self._warned_battery_temperature = False
```

Add helpers in the "config helpers" section:

```python
    def derating_enabled(self) -> bool:
        return bool(self.conf(CONF_CHARGE_DERATING, DEFAULT_CHARGE_DERATING))

    def _derating_curve(self) -> Curve:
        return curve_from_percentages(
            [
                float(self.conf(key, default))
                for key, default in zip(DERATING_CURVE_KEYS, DEFAULT_DERATING_CURVE, strict=True)
            ]
        )
```

In `async_setup`, after the savings restore block:

```python
        if stored and stored.get("charge_rate"):
            try:
                self.charge_model = ChargeRateModel.from_dict(
                    stored["charge_rate"], self._derating_curve()
                )
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not restore charge rate observations: %s", err)
```

In `_store_payload`, add `"charge_rate": self.charge_model.to_dict(),`.

Add the temperature reader next to `_read_float_state`:

```python
    def _read_temperature_c(self, entity_id: str | None) -> float | None:
        """A temperature in °C, whatever unit the entity reports in."""
        value = self._read_float_state(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = str(state.attributes.get("unit_of_measurement") or "") if state else ""
        if unit == "°F":
            return (value - 32.0) * 5.0 / 9.0
        return value
```

Add the factor lookup:

```python
    def charge_factor(self) -> tuple[float, str, float | None]:
        """(factor, source, battery temperature °C) for the planner."""
        if not self.derating_enabled():
            return 1.0, SOURCE_OFF, None
        entity_id = self.conf(CONF_BATTERY_TEMPERATURE_ENTITY)
        temperature = self._read_temperature_c(entity_id)
        if temperature is None:
            if not self._warned_battery_temperature:
                self._warned_battery_temperature = True
                _LOGGER.warning(
                    "Battery temperature %s is unavailable - planning with full "
                    "charge power until it reads again",
                    entity_id,
                )
            else:
                _LOGGER.debug("Battery temperature %s still unavailable", entity_id)
            return 1.0, SOURCE_NO_TEMPERATURE, None
        self._warned_battery_temperature = False
        return (
            self.charge_model.factor(temperature),
            self.charge_model.source(temperature),
            temperature,
        )
```

In `_async_update_data`, just before `battery = BatteryState(`:

```python
        factor, factor_source, battery_temperature = self.charge_factor()
        _LOGGER.debug(
            "Charge factor %.3f (%s) at battery temperature %s",
            factor,
            factor_source,
            battery_temperature,
        )
```

Pass `charge_factor=factor,` as the last `BatteryState(...)` argument, and add to the `inputs` dict:

```python
            "charge_derating": {
                "enabled": self.derating_enabled(),
                "temperature": (
                    round(battery_temperature, 2) if battery_temperature is not None else None
                ),
                "factor": round(factor, 3),
                "source": factor_source,
            },
```

(`inputs["battery"]` already carries `charge_factor` via `asdict(battery)`.)

- [ ] **Step 4: Config sensor and diagnostics**

In `sensor.py`, import `CONF_BATTERY_CHARGE_ENERGY_ENTITY` if not yet imported, and extend `ConfigSensor.extra_state_attributes` before `"dry_run"`:

```python
            "charge_derating": self.coordinator.derating_enabled(),
            "charge_factor": self._derating().get("factor"),
            "charge_factor_source": self._derating().get("source"),
            "charge_rate_learning": self._learning_state(),
            "learned_bands": self.coordinator.charge_model.learned_band_count,
```

with the helper methods on `ConfigSensor`:

```python
    def _learning_state(self) -> str:
        """off | active | no_charge_meter (the curve then stays for good)."""
        if not self.coordinator.derating_enabled():
            return "off"
        if self.coordinator.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY):
            return "active"
        return "no_charge_meter"

    def _derating(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None or not data.inputs:
            return {}
        return data.inputs.get("charge_derating", {})
```

In `diagnostics.py`, add to the `"runtime"` dict:

```python
            "charge_rate_bands": coordinator.charge_model.bands(),
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all PASS. Then ruff check/format.

- [ ] **Step 6: Commit**

```bash
git add custom_components/smart_battery_pilot/coordinator.py custom_components/smart_battery_pilot/sensor.py custom_components/smart_battery_pilot/diagnostics.py tests/test_coordinator_derating.py
git commit -m "Coordinator: plan with the cold-weather charge factor, persist observations"
```

---

### Task 5: Observe real charge slots

**Files:**
- Modify: `custom_components/smart_battery_pilot/coordinator.py`
- Modify: `custom_components/smart_battery_pilot/executor.py`
- Modify: `tests/test_executor.py`, `tests/test_executor_restart.py` (fake coordinators)
- Test: `tests/test_coordinator_derating.py` (append), `tests/test_debug_logging.py` (append one executor test)

**Interfaces:**
- Consumes: `ChargeSample` (Task 1), `charge_model`, `derating_enabled`, `_read_temperature_c` (Task 4).
- Produces:
  - `SBPCoordinator.charge_observation(requested_w: float | None, now: datetime | None = None) -> None`. It closes any open observation (recording a sample if valid) and opens a new one when `requested_w` is not None and learning is possible.
  - Executor calls it after every apply (with the charge slot's `power_w` only when the last decision was an applied charge, else `None`) and on stop.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coordinator_derating.py`:

```python
# --- observations -----------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from smart_battery_pilot.const import (  # noqa: E402
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_MAX_CHARGE_POWER_W,
)

T0 = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)
LEARNING = {**DERATING, CONF_MAX_CHARGE_POWER_W: 5000, "efficiency": 100}


def _observed(hass, coord, *, kwh_end, minutes=60, soc_end=40.0, requested=5000.0, unit="kWh"):
    hass.states.set("sensor.charge_energy", 100.0 if unit == "kWh" else 100000.0, {"unit_of_measurement": unit})
    coord.charge_observation(requested, now=T0)
    hass.states.set("sensor.charge_energy", kwh_end if unit == "kWh" else kwh_end * 1000, {"unit_of_measurement": unit})
    hass.states.set("sensor.soc", soc_end)
    coord.charge_observation(None, now=T0 + timedelta(minutes=minutes))
    return coord.charge_model.samples


def test_a_throttled_charge_slot_becomes_a_saturated_sample():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=101.5)  # 1.5 kW of 5 kW asked
    assert sample.temperature == 3.0
    assert round(sample.ratio, 3) == 0.3
    assert sample.saturated is True


def test_a_full_power_slot_is_a_lower_bound():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=104.8)
    assert sample.saturated is False


def test_a_wh_meter_gives_the_same_ratio():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    [sample] = _observed(hass, coord, kwh_end=101.5, unit="Wh")
    assert round(sample.ratio, 3) == 0.3


def test_the_ratio_is_measured_on_the_battery_side():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **{**LEARNING, "efficiency": 81})  # one way 0.9
    [sample] = _observed(hass, coord, kwh_end=104.5)
    assert round(sample.ratio, 3) == 1.0


def test_short_slots_are_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=100.1, minutes=5) == ()


def test_a_nearly_full_battery_is_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)  # max_soc default 95
    assert _observed(hass, coord, kwh_end=101.0, soc_end=91.0) == ()


def test_a_meter_reset_is_discarded():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=0.5) == ()


def test_a_small_request_is_not_observed():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    assert _observed(hass, coord, kwh_end=100.5, requested=500.0) == ()


def test_no_learning_without_a_charge_meter():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING, **{CONF_BATTERY_CHARGE_ENERGY_ENTITY: None})
    assert _observed(hass, coord, kwh_end=101.5) == ()


def test_no_learning_while_derating_is_off():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **{**LEARNING, CONF_CHARGE_DERATING: False})
    assert _observed(hass, coord, kwh_end=101.5) == ()


def test_a_mid_slot_refresh_splits_the_observation():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    hass.states.set("sensor.charge_energy", 100.0, {"unit_of_measurement": "kWh"})
    coord.charge_observation(5000.0, now=T0)
    hass.states.set("sensor.charge_energy", 100.75, {"unit_of_measurement": "kWh"})
    coord.charge_observation(5000.0, now=T0 + timedelta(minutes=30))  # refresh re-applies
    hass.states.set("sensor.charge_energy", 101.5, {"unit_of_measurement": "kWh"})
    coord.charge_observation(None, now=T0 + timedelta(minutes=60))
    ratios = [round(s.ratio, 3) for s in coord.charge_model.samples]
    assert ratios == [0.3, 0.3]


def test_a_new_sample_is_persisted():
    hass = _hass_with_prices(temp=3.0)
    coord = _coordinator(hass, **LEARNING)
    _observed(hass, coord, kwh_end=101.5)
    assert coord._store._data["charge_rate"]["samples"]
```

In `tests/test_executor.py`, extend `_FakeCoordinator`: in `__init__` add `self.charge_requests: list = []`, and add

```python
    def charge_observation(self, requested_w, now=None):
        self.charge_requests.append(requested_w)
```

`tests/test_executor_restart.py` has its own fake coordinator (next to its `note_conditions`); give it the same no-op so the restart tests keep passing:

```python
    def charge_observation(self, requested_w, now=None):
        pass
```

Append to `tests/test_debug_logging.py`:

```python
def test_executor_reports_applied_charges_to_the_coordinator():
    coord = _FakeCoordinator([_slot(ACTION_CHARGE, power=3000.0)], dry_run=False)
    executor = PlanExecutor(_FakeHass(), coord)
    _run(executor.async_apply_current())
    coord.dry_run = True
    _run(executor.async_apply_current())
    _run(executor.async_stop())
    assert coord.charge_requests == [3000.0, None, None]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_coordinator_derating.py tests/test_debug_logging.py -q`
Expected: FAIL with `AttributeError: 'SBPCoordinator' object has no attribute 'charge_observation'` and the executor test failing on `charge_requests == []`.

- [ ] **Step 3: Implement in the coordinator**

Add imports `import math` and `from .forecast.charge_rate import ChargeSample` (extend the existing Task 4 import line), plus `CONF_EFFICIENCY` is already imported. Add module constants:

```python
# Observation rules for learning the cold charge limit (see the spec).
MIN_OBSERVATION = timedelta(minutes=10)
TAPER_MARGIN_SOC = 5.0  # above max_soc - this, the BMS tapers because it is full
MIN_REQUEST_SHARE = 0.2  # smaller requests say nothing about the limit
SATURATION_SHARE = 0.85  # took less than this share of the request -> limited
```

Add a small dataclass next to `IntervalPrices`:

```python
@dataclass(frozen=True, slots=True)
class _OpenCharge:
    """A forced charge slot being watched."""

    started: datetime
    kwh: float
    temperature: float
    requested_w: float
```

In `__init__`: `self._open_charge: _OpenCharge | None = None`.

Add the methods (in a new `# --- charge rate observation ---` section):

```python
    def charge_observation(self, requested_w: float | None, now: datetime | None = None) -> None:
        """Close the watched charge slot and, if charging goes on, watch the next.

        Called by the executor after every decision: with the requested power
        while it really runs a charge slot, with None otherwise. A coordinator
        refresh re-applies charge mid-slot, which simply splits the
        observation in two.
        """
        now = now or dt_util.now()
        self._close_charge_observation(now)
        if requested_w is not None:
            self._open_charge_observation(requested_w, now)

    def _open_charge_observation(self, requested_w: float, now: datetime) -> None:
        if not self.derating_enabled():
            return
        meter = self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
        max_w = float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000))
        reason = None
        kwh = self._read_energy_kwh(meter) if meter else None
        temperature = self._read_temperature_c(self.conf(CONF_BATTERY_TEMPERATURE_ENTITY))
        if not meter:
            reason = "no charge meter"
        elif requested_w < MIN_REQUEST_SHARE * max_w:
            reason = f"request {requested_w:.0f} W too small"
        elif kwh is None:
            reason = "charge meter unavailable"
        elif temperature is None:
            reason = "battery temperature unavailable"
        if reason:
            _LOGGER.debug("Not observing this charge slot: %s", reason)
            return
        self._open_charge = _OpenCharge(now, kwh, temperature, requested_w)

    def _close_charge_observation(self, now: datetime) -> None:
        opened, self._open_charge = self._open_charge, None
        if opened is None:
            return
        elapsed = now - opened.started
        kwh = self._read_energy_kwh(self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY))
        soc = self._read_float_state(self.conf(CONF_SOC_ENTITY))
        max_soc = float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC))
        reason = None
        if elapsed < MIN_OBSERVATION:
            reason = f"only {elapsed.total_seconds() / 60:.0f} min"
        elif kwh is None:
            reason = "charge meter unavailable"
        elif kwh < opened.kwh:
            reason = "charge meter went backwards"
        elif soc is None or soc >= max_soc - TAPER_MARGIN_SOC:
            reason = f"SOC {soc} too close to max SOC {max_soc:.0f}"
        if reason:
            _LOGGER.debug("Charge observation discarded: %s", reason)
            return

        hours = elapsed.total_seconds() / 3600.0
        achieved_kw = (kwh - opened.kwh) / hours
        eta_one_way = math.sqrt(
            max(0.5, min(1.0, float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)) / 100.0))
        )
        max_kw = float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000)) / 1000.0
        sample = ChargeSample(
            temperature=opened.temperature,
            ratio=achieved_kw / (max_kw * eta_one_way),
            saturated=achieved_kw < SATURATION_SHARE * opened.requested_w / 1000.0,
            at=now,
        )
        self.charge_model.add_sample(sample)
        _LOGGER.debug(
            "Charge observation at %.1f °C: %.2f kW of %.2f kW requested "
            "(ratio %.3f, %s) over %.0f min",
            sample.temperature,
            achieved_kw,
            opened.requested_w / 1000.0,
            sample.ratio,
            "limited" if sample.saturated else "took all",
            elapsed.total_seconds() / 60,
        )
        self.schedule_persist()
```

- [ ] **Step 4: Implement in the executor**

In `executor.py`, change `async_apply_current`:

```python
            self._schedule_boundary()
            await self._apply_locked()
            self._report_charge()
```

add the method:

```python
    def _report_charge(self) -> None:
        """Tell the coordinator whether a charge slot is really running.

        It watches those slots to learn how much a cold battery actually
        takes; only a charge the inverter really received tells it anything.
        """
        last = self.decisions[-1] if self.decisions else None
        charging = (
            last is not None and last["outcome"] == "applied" and last["planned"] == ACTION_CHARGE
        )
        self.coordinator.charge_observation(float(last["power_w"]) if charging else None)
```

and in `async_stop`, inside `async with self._lock:` right after `self._stopped = True`:

```python
            self.coordinator.charge_observation(None)
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests -q`
Expected: all PASS. Then ruff check/format.

- [ ] **Step 6: Commit**

```bash
git add custom_components/smart_battery_pilot/coordinator.py custom_components/smart_battery_pilot/executor.py tests/test_coordinator_derating.py tests/test_executor.py tests/test_executor_restart.py tests/test_debug_logging.py
git commit -m "Learn the cold charge limit from charge slots the pilot really ran"
```

---

### Task 6: Docs, smoke test, version

**Files:**
- Modify: `docs/configuration.md`, `docs/optimizer.md`, `README.md`
- Modify: `tests_ha/test_smoke.py`
- Modify: `custom_components/smart_battery_pilot/manifest.json`

**Interfaces:**
- Consumes: everything above. Produces: nothing for later tasks.

- [ ] **Step 1: Smoke test with derating on**

In `tests_ha/test_smoke.py`, add after the existing setup test (reuse its `_setup` helper; if `_setup` takes no options, add an `options: dict | None = None` parameter that is merged into the `MockConfigEntry(options=...)` it builds):

```python
async def test_setup_with_cold_weather_charging_on(hass: HomeAssistant) -> None:
    """The new options section must not break setup on a real Home Assistant."""
    hass.states.async_set("sensor.battery_temp", "3.0", {"unit_of_measurement": "°C"})
    entry = await _setup(
        hass,
        options={
            "charge_derating": True,
            "battery_temperature_entity": "sensor.battery_temp",
        },
    )
    assert entry.state.name == "LOADED"
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.inputs["charge_derating"]["source"] == "curve"
```

This cannot run locally (no `pytest_homeassistant_custom_component` in `.venv`); CI runs it. Read `_setup` first and adapt the call to its real signature.

- [ ] **Step 2: User documentation**

In `docs/configuration.md`, add a section after the consumption section:

```markdown
### Cold-weather charging (options only)

Off by default. Open *Configure → Cold-weather charging*.

| Setting | Meaning |
|---|---|
| Plan with reduced charge power in the cold | The planner expects a cold battery to take less than the maximum charge power and books more cheap charge slots. The charge script is **still asked for full power** — the battery's BMS does the throttling. |
| Battery temperature entity | Cell or module temperature of the battery (not the outdoor sensor). Required when the option is on. °F is converted. |
| Charge power at 0 / 5 / 10 / 15 / 20 °C | Percent of the maximum charge power the battery accepts. Linear in between; below 0 °C the 0 °C value applies, above 20 °C the 20 °C value. The values must not fall as the temperature rises. Default `10 / 20 / 50 / 80 / 100 %` — a conservative generic LFP curve; check your battery's datasheet. |

**Learning.** With a battery charge energy meter configured, every forced
charge slot the pilot really runs is measured: how much went into the battery
compared with what was asked. Per 5 °C band, once six slots have shown the
battery taking clearly less than requested, the measured value replaces the
slider curve for that band. Slots near max SOC (the battery tapers because it is
full), shorter than 10 minutes, or with very small requests are ignored. Dry-run
learns nothing — no charge actually happens. The configuration sensor shows the
current factor, its source (`curve` / `learned`) and the number of learned
bands; the diagnostics dump lists every band.
```

In `docs/optimizer.md`, add to the "Inputs" list:

```markdown
* Charge factor (cold-weather charging, off by default): the share of the max
  charge power the battery is expected to accept at its current temperature.
  It scales the per-slot charge cap, so a cold battery is charged over more
  slots. The power requested from the charge script is scaled back up
  (`planned / factor`, at most the max), so the pilot never throttles the
  battery itself.
```

In `README.md`, in the feature list or configuration overview, add one line:

```markdown
- **Cold-weather charging** (optional): plans with the reduced charge power of a cold battery — slider curve out of the box, learned from real charge slots over time.
```

(Place it where the README lists features; read the file first and match its list style.)

- [ ] **Step 3: Version**

Set `"version": "0.8.0"` in `custom_components/smart_battery_pilot/manifest.json`.

- [ ] **Step 4: Full check**

Run: `.venv/bin/python -m pytest tests -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .`
Expected: all PASS, no lint findings.

- [ ] **Step 5: Commit**

```bash
git add docs README.md tests_ha/test_smoke.py custom_components/smart_battery_pilot/manifest.json
git commit -m "Document cold-weather charging, smoke-test it, version 0.8.0"
```
