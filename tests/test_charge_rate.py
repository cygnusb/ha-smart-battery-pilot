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
    LearnedRange,
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


def test_nothing_is_learned_before_the_sixth_limited_slot():
    model = _model_with([_sample(3.0, 0.4)] * 5)
    assert model.ranges() == []
    assert model.source(3.0) == SOURCE_CURVE
    assert model.factor(3.0) == pytest.approx(curve_factor(DEFAULT, 3.0))

    model.add_sample(_sample(3.0, 0.4))
    assert model.ranges() == [LearnedRange(3.0, 3.0, 0.4, 6)]
    assert model.source(3.0) == SOURCE_LEARNED
    assert model.factor(3.0) == pytest.approx(0.4)


def test_a_step_is_learned_where_the_observations_put_it():
    """One limit up to 9 °C, full power from 11 °C: two ranges that begin
    and end at the observations, not at multiples of five."""
    samples = [_sample(t, 0.55) for t in (4.0, 5.5, 7.0, 8.0, 8.5, 9.0)]
    samples += [_sample(t, 1.0, saturated=False) for t in (11.0, 13.0)]
    ranges = _model_with(samples).ranges()
    assert ranges == [
        LearnedRange(4.0, 9.0, pytest.approx(0.55), 6),
        LearnedRange(11.0, 13.0, 1.0, 2),
    ]


def test_between_two_ranges_the_factor_rises_in_a_straight_line():
    samples = [_sample(t, 0.6) for t in (8.0, 9.0, 10.0)]
    samples += [_sample(t, 0.9) for t in (14.0, 15.0, 16.0)]
    model = _model_with(samples)
    assert model.factor(10.0) == pytest.approx(0.6)
    assert model.factor(12.0) == pytest.approx(0.75)
    assert model.factor(14.0) == pytest.approx(0.9)


def test_one_bad_slot_does_not_bend_the_curve():
    """A slot that started late reads far too low. The median ignores it."""
    samples = [_sample(8.0, r) for r in (0.55, 0.56, 0.30, 0.55, 0.54, 0.55, 0.56)]
    [only] = _model_with(samples).ranges()
    assert only.factor == pytest.approx(0.55)


def test_an_odd_slot_among_warmer_and_colder_ones_is_absorbed():
    samples = [_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 8.0, 9.0, 10.0)]
    [only] = _model_with([*samples, _sample(7.0, 0.30)]).ranges()
    assert only == LearnedRange(4.0, 10.0, pytest.approx(0.55), 7)


def test_a_lone_slot_at_the_cold_edge_stands_alone_when_it_reads_lower():
    """Colder and lower fits a curve that rises with temperature, so nothing
    contradicts it. It can only make the plan more careful, and its count of
    one is shown next to it."""
    samples = [_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    model = _model_with([*samples, _sample(2.0, 0.30)])
    assert model.ranges()[0] == LearnedRange(2.0, 2.0, pytest.approx(0.30), 1)
    assert model.factor(4.0) == pytest.approx(0.55)
    assert model.factor(1.0) <= 0.30


def test_a_lone_slot_at_the_cold_edge_that_reads_higher_is_absorbed():
    samples = [_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    [only] = _model_with([*samples, _sample(2.0, 0.90)]).ranges()
    assert only.factor == pytest.approx(0.55)
    assert only.low == 2.0


def test_a_lone_slot_at_the_warm_edge_that_reads_lower_is_absorbed():
    samples = [_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    [only] = _model_with([*samples, _sample(12.0, 0.30)]).ranges()
    assert only.factor == pytest.approx(0.55)


def test_the_learned_curve_never_falls_with_rising_temperature():
    # Colder slots that read higher than warmer ones are pooled with them.
    samples = [_sample(5.0, 0.8)] * 3 + [_sample(10.0, 0.5)] * 3
    model = _model_with(samples)
    factors = [model.factor(t) for t in (5.0, 6.0, 8.0, 10.0)]
    assert factors == sorted(factors)
    assert len(model.ranges()) == 1


def test_a_slot_that_took_everything_counts_only_above_the_fit():
    limited = [_sample(t, 0.5) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    # Small requests the battery took in full: below the fit, no news.
    below = _model_with(limited + [_sample(6.0, 0.2, saturated=False)] * 4)
    assert below.ranges() == [LearnedRange(4.0, 9.0, pytest.approx(0.5), 6)]
    # Full power taken at 12 °C: the limit is higher up there.
    above = _model_with([*limited, _sample(12.0, 1.0, saturated=False)])
    assert above.ranges()[-1] == LearnedRange(12.0, 12.0, 1.0, 1)


def test_a_small_request_met_in_full_does_not_pull_down_a_colder_full_one():
    """Full power taken at 11 °C holds for 12 °C too. A slot that asked for
    little there and got it says nothing against that."""
    limited = [_sample(t, 0.5) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    took_all = [_sample(11.0, 1.0, saturated=False), _sample(12.0, 0.6, saturated=False)]
    assert _model_with(limited + took_all).ranges()[-1] == LearnedRange(11.0, 12.0, 1.0, 2)


def test_slots_that_took_everything_never_learn_on_their_own():
    model = _model_with([_sample(3.0, 0.6, saturated=False)] * 10)
    assert model.ranges() == []
    assert model.source(3.0) == SOURCE_CURVE


def test_learned_values_are_clamped_to_one():
    assert _model_with([_sample(3.0, 1.2)] * 6).ranges()[0].factor == 1.0


def test_colder_than_any_observation_the_sliders_stay_in_charge():
    """Measured 55 % from 4 °C up. Below that nothing is known, and assuming
    more than the user set would book too few charge slots on a cold night."""
    model = _model_with([_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)])
    assert model.factor(4.0) == pytest.approx(0.55)
    for t in (-5.0, 0.0, 2.0, 3.9):
        assert model.factor(t) == pytest.approx(curve_factor(DEFAULT, t))
        assert model.source(t) == SOURCE_CURVE
    # Sliders more optimistic than the measurement are capped by it.
    low = _model_with([_sample(t, 0.12) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)])
    assert low.factor(3.0) == pytest.approx(0.12)
    assert low.source(3.0) == SOURCE_LEARNED
    assert low.factor(0.0) == pytest.approx(0.10)


def test_warmer_than_any_observation_the_sliders_apply_but_never_below_the_fit():
    model = _model_with([_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)])
    assert model.factor(9.5) == pytest.approx(0.55)  # sliders: 47 %, below the fit
    assert model.source(9.5) == SOURCE_LEARNED
    assert model.factor(12.0) == pytest.approx(0.62)  # sliders: 62 % at 12 °C
    assert model.source(12.0) == SOURCE_CURVE
    assert model.factor(20.0) == pytest.approx(1.0)


def test_the_fit_follows_new_samples_and_a_clear():
    model = _model_with([_sample(3.0, 0.4)] * 6)
    assert model.factor(3.0) == pytest.approx(0.4)
    for _ in range(7):
        model.add_sample(_sample(3.0, 0.6))
    assert model.factor(3.0) == pytest.approx(0.6)
    model.clear()
    assert model.ranges() == []


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
    assert restored.factor(3.0) == pytest.approx(0.4)


def test_loading_applies_the_cap_and_the_age_limit():
    rows = [
        {
            "temperature": 3.0,
            "ratio": 0.4,
            "saturated": True,
            "at": (AT - timedelta(days=800)).isoformat(),
        }
    ] + [
        {
            "temperature": 3.0,
            "ratio": 0.4,
            "saturated": True,
            "at": (AT + timedelta(minutes=i)).isoformat(),
        }
        for i in range(MAX_SAMPLES + 5)
    ]
    model = ChargeRateModel.from_dict({"samples": rows}, DEFAULT)
    assert len(model.samples) == MAX_SAMPLES
    assert min(s.at for s in model.samples) == AT + timedelta(minutes=5)


# --- the learned values under the user's control ------------------------------


def test_adopted_values_never_fall_with_rising_temperature():
    # A cold band learned far above the curve of the warmer ones.
    model = _model_with([_sample(3.0, 0.95)] * 6)
    adopted = model.adopted_percentages()
    assert adopted == sorted(adopted)


def test_nothing_learned_means_nothing_to_adopt():
    assert _model_with([_sample(3.0, 0.4)] * 5).adopted_percentages() is None


def test_clear_forgets_every_observation():
    model = _model_with([_sample(3.0, 0.4)] * 6)
    model.clear()
    assert model.samples == ()
    assert model.factor(2.5) == pytest.approx(0.15)


def test_bands_summary_for_sensor_and_diagnostics():
    model = _model_with([_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)])
    assert model.bands() == [
        {
            "from_c": 4.0,
            "to_c": 9.0,
            "learned": 0.55,
            "samples": 6,
            "curve": pytest.approx(curve_factor(DEFAULT, 6.5), abs=1e-3),
            "in_use": "learned",
        }
    ]
    assert model.learned_band_count == 1
    assert model.counts() == (6, 6)


def test_switched_off_the_sliders_plan_but_the_learned_curve_stays_visible():
    model = _model_with([_sample(3.0, 0.4)] * 6)
    model.use_learned = False
    assert model.factor(3.0) == pytest.approx(curve_factor(DEFAULT, 3.0))
    assert model.source(3.0) == "curve"
    assert model.bands()[0]["in_use"] == "curve"
    assert model.measured_factor(3.0) == pytest.approx(0.4)


def test_adopting_reads_the_learned_curve_at_the_slider_temperatures():
    samples = [_sample(t, 0.55) for t in (4.0, 5.5, 7.0, 8.0, 8.5, 9.0)]
    samples += [_sample(t, 1.0, saturated=False) for t in (11.0, 13.0)]
    model = _model_with(samples)
    adopted = model.adopted_percentages()
    assert adopted == [round(model.measured_factor(t) * 100) for t in (0, 5, 10, 15, 20)]
    # 0 °C was never measured and keeps its slider; 5 °C takes the measurement.
    assert adopted[:2] == [10, 55]
    assert adopted[3:] == [100, 100]
    assert adopted == sorted(adopted)


# --- a temperature that does not explain the limit ---------------------------


def _two_level_battery():
    """Limits to 70 % at 8, 12 and 13 °C, yet took 90 % at 9 °C: whatever
    sets the limit, it is not this temperature."""
    limited = [_sample(t, 0.70) for t in (8.0, 10.0, 11.0, 12.0, 12.5, 13.0)]
    return _model_with([*limited, _sample(9.0, 0.90, saturated=False)])


def test_a_colder_slot_that_took_clearly_more_contradicts_a_limited_one():
    model = _two_level_battery()
    # The slot at 8 °C is colder than the one that took 90 % and stands.
    assert model.conflicts() == 5
    assert model.explained is False


def test_an_unexplained_limit_plans_with_the_sliders():
    model = _two_level_battery()
    assert model.ranges()  # still fitted, for the user to look at or adopt
    assert model.factor(12.0) == pytest.approx(curve_factor(DEFAULT, 12.0))
    assert model.source(12.0) == SOURCE_CURVE
    assert model.bands()[0]["in_use"] == "curve"
    assert model.adopted_percentages() is not None


def test_a_curve_that_rises_with_temperature_has_no_conflicts():
    samples = [_sample(t, 0.55) for t in (4.0, 5.5, 7.0, 8.0, 8.5, 9.0)]
    samples += [_sample(t, 1.0, saturated=False) for t in (11.0, 13.0)]
    model = _model_with(samples)
    assert model.conflicts() == 0
    assert model.explained is True


def test_measurement_noise_is_not_a_conflict():
    ratios = (0.70, 0.74, 0.69, 0.75, 0.71, 0.78)
    model = _model_with([_sample(4.0 + i, r) for i, r in enumerate(ratios)])
    assert model.conflicts() == 0


def test_one_slot_that_started_late_does_not_block_learning():
    samples = [_sample(t, 0.55) for t in (4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    model = _model_with([*samples, _sample(8.5, 0.30)])
    assert model.conflicts() == 1
    assert model.explained is True
    assert model.source(6.0) == SOURCE_LEARNED


def test_conflicts_follow_new_samples_and_a_clear():
    model = _two_level_battery()
    model.clear()
    assert model.conflicts() == 0
    assert model.explained is True
