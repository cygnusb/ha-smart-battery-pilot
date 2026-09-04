"""Tests for consumption and PV forecasting."""

from datetime import datetime, timedelta, timezone

import pytest

from smart_battery_pilot.forecast.consumption import (
    ConsumptionForecaster,
    TrainingSample,
)
from smart_battery_pilot.forecast.pv import pv_kwh_for_slot

TZ = timezone(timedelta(hours=2))


def _synthetic_samples(days: int, with_temp: bool = False) -> list[TrainingSample]:
    """Synthetic household: base 0.3 kWh/h, morning/evening peaks, weekend +20%."""
    samples = []
    start = datetime(2026, 4, 1, 0, 0, tzinfo=TZ)
    for d in range(days):
        for h in range(24):
            when = start + timedelta(days=d, hours=h)
            kwh = 0.3
            if 6 <= h <= 8:
                kwh += 0.5
            if 18 <= h <= 21:
                kwh += 0.8
            if when.weekday() >= 5:
                kwh *= 1.2
            temp = 10.0 + 8 * (h - 12) / 12 if with_temp else None
            samples.append(TrainingSample(start=when, kwh=kwh, temperature=temp))
    return samples


def test_profile_fallback_with_few_days():
    forecaster = ConsumptionForecaster()
    forecaster.train(_synthetic_samples(days=5))
    assert forecaster.model_type == "hourly_profile"

    monday_evening = datetime(2026, 6, 8, 19, 0, tzinfo=TZ)
    monday_night = datetime(2026, 6, 8, 2, 0, tzinfo=TZ)
    assert forecaster.predict_kwh(monday_evening, 1.0) == pytest.approx(1.1, abs=0.05)
    assert forecaster.predict_kwh(monday_night, 1.0) == pytest.approx(0.3, abs=0.05)


def test_ridge_with_enough_days():
    forecaster = ConsumptionForecaster()
    forecaster.train(_synthetic_samples(days=30))
    assert forecaster.model_type == "ridge_regression"

    evening = forecaster.predict_kwh(datetime(2026, 6, 8, 19, 30, tzinfo=TZ), 1.0)
    night = forecaster.predict_kwh(datetime(2026, 6, 8, 2, 0, tzinfo=TZ), 1.0)
    # Smooth model: peaks less sharp than profile, but ordering must hold
    assert evening > night
    assert evening > 0.6
    assert 0.0 <= night < 0.6


def test_slot_scaling():
    forecaster = ConsumptionForecaster()
    forecaster.train(_synthetic_samples(days=5))
    when = datetime(2026, 6, 8, 2, 0, tzinfo=TZ)
    full = forecaster.predict_kwh(when, 1.0)
    quarter = forecaster.predict_kwh(when, 0.25)
    assert quarter == pytest.approx(full / 4)


def test_empty_training_uses_default():
    forecaster = ConsumptionForecaster()
    forecaster.train([])
    assert forecaster.model_type == "default"
    assert forecaster.predict_kwh(datetime(2026, 6, 8, 12, 0, tzinfo=TZ), 1.0) > 0


def test_heat_pump_flag_uses_sparse_temperature():
    """has_heat_pump forces the heating-demand feature even with few temp samples."""
    samples = _synthetic_samples(days=30, with_temp=False)
    for i, s in enumerate(samples):
        if i % 5 == 0:
            samples[i] = TrainingSample(start=s.start, kwh=s.kwh, temperature=5.0)
    forced = ConsumptionForecaster()
    forced.train(samples, require_temperature=True)
    assert forced._uses_temperature is True

    auto = ConsumptionForecaster()
    auto.train(samples)
    assert auto._uses_temperature is False


def test_roundtrip_serialization():
    forecaster = ConsumptionForecaster()
    forecaster.train(_synthetic_samples(days=30, with_temp=True))
    restored = ConsumptionForecaster.from_dict(forecaster.to_dict())
    when = datetime(2026, 6, 8, 19, 0, tzinfo=TZ)
    assert restored.predict_kwh(when, 1.0, temperature=5.0) == pytest.approx(
        forecaster.predict_kwh(when, 1.0, temperature=5.0)
    )


def test_pv_distribution():
    now = datetime(2026, 6, 11, 8, 0, tzinfo=TZ)
    noon = pv_kwh_for_slot(datetime(2026, 6, 11, 13, 0, tzinfo=TZ), 1.0, 30.0, 20.0, now)
    night = pv_kwh_for_slot(datetime(2026, 6, 11, 23, 0, tzinfo=TZ), 1.0, 30.0, 20.0, now)
    tomorrow_noon = pv_kwh_for_slot(
        datetime(2026, 6, 12, 13, 0, tzinfo=TZ), 1.0, 30.0, 20.0, now
    )
    assert noon > 2.0  # noon hour carries far more than average
    assert night == 0.0
    assert tomorrow_noon == pytest.approx(noon * 20 / 30, rel=0.05)

    # Sum over all hours of today ≈ daily total
    total = sum(
        pv_kwh_for_slot(datetime(2026, 6, 11, h, 0, tzinfo=TZ), 1.0, 30.0, None, now)
        for h in range(24)
    )
    assert total == pytest.approx(30.0, rel=0.02)


def test_pv_none_forecast():
    now = datetime(2026, 6, 11, 8, 0, tzinfo=TZ)
    assert pv_kwh_for_slot(now, 1.0, None, None, now) == 0.0


def _with_frozen_hours(
    samples: list[TrainingSample], hours: range, days: int
) -> list[TrainingSample]:
    """Pin `hours` to exactly 0.0 on the first `days` days, as a stuck meter does."""
    out = []
    first = min(s.start for s in samples).date()
    for s in samples:
        stuck = (s.start.date() - first).days < days and s.start.hour in hours
        out.append(TrainingSample(s.start, 0.0, s.temperature) if stuck else s)
    return out


def test_frozen_meter_samples_are_dropped():
    """A meter stuck at 0 W must not drag the afternoon forecast down.

    Fronius `P_Load` reports a constant 0 W for whole hours while the rest of
    the inverter data keeps flowing; those hours reach training as genuine
    zeroes and the model learns an afternoon hole that is not there.
    """
    clean = _synthetic_samples(days=30)
    poisoned = _with_frozen_hours(clean, range(13, 18), days=20)

    good = ConsumptionForecaster()
    good.train(clean)
    bad = ConsumptionForecaster()
    bad.train(poisoned)

    # Under-forecasting is the harmful direction: the planner then spends the
    # battery before the evening peak. Filtering cannot recover the discarded
    # hours, so it lands high - that is the safe side.
    for hour in (14, 15, 16):
        when = datetime(2026, 6, 8, hour, 0, tzinfo=TZ)
        reference = good.predict_kwh(when, 1.0)
        assert reference > bad.predict_kwh(when, 1.0) * 0.6
        assert bad.predict_kwh(when, 1.0) >= reference


def test_exact_zero_samples_never_train():
    forecaster = ConsumptionForecaster()
    samples = _synthetic_samples(days=30)
    forecaster.train(samples + [TrainingSample(s.start, 0.0, None) for s in samples])
    assert forecaster.sample_count == len(samples)


def test_all_zero_history_falls_back_to_default():
    """Dropping implausible samples must not leave the model untrained."""
    forecaster = ConsumptionForecaster()
    forecaster.train([TrainingSample(s.start, 0.0, None) for s in _synthetic_samples(5)])
    assert forecaster.model_type == "default"
    assert forecaster.predict_kwh(datetime(2026, 6, 8, 12, 0, tzinfo=TZ), 1.0) > 0
