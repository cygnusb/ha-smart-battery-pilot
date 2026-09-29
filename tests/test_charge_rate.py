"""Charge rate model: curve, per-band learning, persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

AT = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
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
