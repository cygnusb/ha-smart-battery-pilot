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
from itertools import pairwise
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
        raise ValueError(f"expected {len(CURVE_TEMPERATURES)} curve values, got {len(percentages)}")
    return [
        (temperature, max(0.0, min(100.0, float(percent))) / 100.0)
        for temperature, percent in zip(CURVE_TEMPERATURES, percentages, strict=True)
    ]


def is_non_decreasing(values: Sequence[float]) -> bool:
    """A warmer battery never takes less - the flow refuses falling sliders."""
    return all(b >= a for a, b in pairwise(values))


def curve_factor(curve: Curve, temperature: float) -> float:
    """Linear between the support points, clamped outside them."""
    if temperature <= curve[0][0]:
        return curve[0][1]
    if temperature >= curve[-1][0]:
        return curve[-1][1]
    for (t0, f0), (t1, f1) in pairwise(curve):
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
