"""Charge power a cold battery actually accepts.

Two sources: the curve the user set with five sliders, and what forced charge
slots have shown the battery to take.

The learned curve is fitted to the single observations, not to fixed
temperature bands. A BMS often limits in steps - one current limit up to some
cell temperature, full power above it - and a 5 °C grid blurs exactly the edge
that matters. The fit is the best curve that never falls with rising
temperature (isotonic regression on medians, so one bad slot does not bend
it); where it is flat it yields a temperature range with one value, and those
ranges begin and end where the observations do.

Only slots in which the battery took clearly less than it was asked for
measure the limit. A slot in which it took everything is a lower bound: it
counts where it lies *above* what the limited slots say, and never learns
anything on its own.

Outside the observed temperatures the sliders stay in charge; the fit only
bounds them (never more than the coldest learned limit below it, never less
than the warmest above it).

The factor is a planning assumption only. The pilot never asks the battery for
less power because of it; the BMS does the throttling.

No Home Assistant imports, like the rest of `forecast`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
import statistics
from typing import Any

CURVE_TEMPERATURES: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0)
# Limited slots needed before anything counts as learned.
MIN_LEARNED_SAMPLES = 6
# Neighbouring ranges this close are one range: the difference is noise.
MERGE_TOLERANCE = 0.03
# A limited slot is contradicted when a slot at the same or a lower temperature
# took this much more of the max charge power: a colder battery never takes
# more, so something other than this temperature set the limit.
CONFLICT_MARGIN = 0.10
# This many contradicted slots, and at least half of the limited ones, and the
# temperature does not explain the limit. One slot that started late is
# contradicted on its own, hence the floor.
MIN_CONFLICTS = 3
MAX_SAMPLES = 400
# Batteries age, and so does their cold behaviour.
MAX_SAMPLE_AGE = timedelta(days=730)

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


@dataclass(frozen=True, slots=True)
class LearnedRange:
    """A stretch of battery temperature with one learned factor."""

    low: float  # °C, coldest observation in the range
    high: float  # °C, warmest observation in the range
    factor: float  # share of the max charge power
    samples: int


def _isotonic_medians(points: list[tuple[float, float]]) -> list[LearnedRange]:
    """Best non-decreasing step curve through (temperature, ratio) points.

    Pool-adjacent-violators on medians: wherever a warmer group reads lower
    than a colder one, the two are pooled and share the median of all their
    values. The median makes it the least-absolute-error fit, so a single
    slot that started late or was cut short moves nothing.
    """
    blocks: list[tuple[float, float, list[float]]] = []
    for temperature, ratio in sorted(points):
        if blocks and blocks[-1][1] == temperature:
            low, high, values = blocks[-1]
            blocks[-1] = (low, high, [*values, ratio])
        else:
            blocks.append((temperature, temperature, [ratio]))
        while len(blocks) >= 2 and (
            statistics.median(blocks[-2][2]) > statistics.median(blocks[-1][2])
        ):
            low, _, colder = blocks[-2]
            _, high, warmer = blocks[-1]
            blocks[-2:] = [(low, high, colder + warmer)]
    ranges: list[LearnedRange] = []
    for low, high, values in blocks:
        factor = max(0.0, min(1.0, statistics.median(values)))
        if ranges and factor - ranges[-1].factor <= MERGE_TOLERANCE:
            previous = ranges[-1]
            total = previous.samples + len(values)
            merged = (previous.factor * previous.samples + factor * len(values)) / total
            ranges[-1] = LearnedRange(previous.low, high, merged, total)
        else:
            ranges.append(LearnedRange(low, high, factor, len(values)))
    return ranges


def _step_value(ranges: list[LearnedRange], temperature: float) -> float:
    """The fitted curve at `temperature`: flat outside, linear between ranges."""
    if temperature <= ranges[0].low:
        return ranges[0].factor
    for colder, warmer in pairwise(ranges):
        if temperature <= colder.high:
            return colder.factor
        if temperature < warmer.low:
            share = (temperature - colder.high) / (warmer.low - colder.high)
            return colder.factor + (warmer.factor - colder.factor) * share
    return ranges[-1].factor


@dataclass(frozen=True, slots=True)
class ChargeSample:
    """One forced charge slot the pilot really ran."""

    temperature: float  # °C at the start of the slot
    ratio: float  # achieved / nominal charge power (battery side)
    saturated: bool  # the battery took clearly less than was requested
    at: datetime


class ChargeRateModel:
    """Curve plus learned bands; answers "which share of max charge power"."""

    def __init__(self, curve: Curve, use_learned: bool = True) -> None:
        self.curve: Curve = sorted(curve)
        # Off: the curve alone plans. Observations are still collected and
        # shown, so the user can compare before trusting them.
        self.use_learned = use_learned
        self._samples: list[ChargeSample] = []
        # The fit is asked for many times per plan and per chart.
        self._ranges: list[LearnedRange] | None = None
        self._conflicts: int | None = None

    @property
    def samples(self) -> tuple[ChargeSample, ...]:
        return tuple(self._samples)

    def add_sample(self, sample: ChargeSample) -> None:
        self._samples.append(sample)
        self._prune()

    def clear(self) -> None:
        """Forget every observation - after a battery swap or a wrong sensor."""
        self._samples = []
        self._ranges = None
        self._conflicts = None

    def _prune(self) -> None:
        """Drop samples past the age limit (from the newest one) and the cap."""
        self._ranges = None
        self._conflicts = None
        if not self._samples:
            return
        self._samples.sort(key=lambda s: s.at)
        cutoff = self._samples[-1].at - MAX_SAMPLE_AGE
        self._samples = [s for s in self._samples if s.at >= cutoff][-MAX_SAMPLES:]

    def ranges(self) -> list[LearnedRange]:
        """The learned curve as temperature ranges, coldest first; empty while
        too few slots have shown the battery's limit."""
        if self._ranges is None:
            self._ranges = self._fit()
        return list(self._ranges)

    def _fit(self) -> list[LearnedRange]:
        limited = [(s.temperature, s.ratio) for s in self._samples if s.saturated]
        if len(limited) < MIN_LEARNED_SAMPLES:
            return []
        first = _isotonic_medians(limited)
        # A slot in which the battery took everything says "at least this
        # much" - and a warmer battery takes no less, so each such slot also
        # holds for every warmer one: a small request met in full at 12 °C
        # must not pull down full power seen at 11 °C. Below the fit a bound
        # is no news; above it, it is evidence the limit lies higher there,
        # and it joins the fit as a point of its own.
        bounds: list[tuple[float, float]] = []
        least = 0.0
        for sample in sorted(self._samples, key=lambda s: s.temperature):
            if sample.saturated:
                continue
            least = max(least, min(1.0, sample.ratio))
            if least > _step_value(first, sample.temperature):
                bounds.append((sample.temperature, least))
        return _isotonic_medians(limited + bounds) if bounds else first

    def factor(self, temperature: float | None) -> float:
        """Share of the max charge power the battery takes at `temperature`."""
        if temperature is None:
            return 1.0
        if not self.applies:
            return curve_factor(self.curve, temperature)
        return self.measured_factor(temperature)

    def measured_factor(self, temperature: float) -> float:
        """The factor with everything learned applied, whatever `use_learned`
        says - what the planner would use, and what "adopt" writes back.

        Inside the observed temperatures: the fitted curve. Outside them
        nothing was measured, so the sliders stay in charge and the fit only
        bounds them the way physics does: a colder battery never takes more
        than the coldest learned limit, a warmer one never less than the
        warmest. That leaves a step at the edge of the measurements - honest
        about where knowledge ends, and never more optimistic in the cold
        than what the user set.
        """
        ranges = self.ranges()
        if not ranges:
            return curve_factor(self.curve, temperature)
        coldest, warmest = ranges[0], ranges[-1]
        if temperature < coldest.low:
            return min(coldest.factor, curve_factor(self.curve, temperature))
        if temperature > warmest.high:
            return max(warmest.factor, curve_factor(self.curve, temperature))
        return _step_value(ranges, temperature)

    def conflicts(self) -> int:
        """Limited slots contradicted by a colder slot that took clearly more."""
        if self._conflicts is None:
            self._conflicts = sum(
                1
                for limited in self._samples
                if limited.saturated
                and any(
                    other.temperature <= limited.temperature
                    and min(1.0, other.ratio) >= limited.ratio + CONFLICT_MARGIN
                    for other in self._samples
                )
            )
        return self._conflicts

    @property
    def explained(self) -> bool:
        """Whether the temperature explains what was measured.

        A battery that limits to the same power at 8 and 13 °C and takes full
        power at 9 °C is limited by something this sensor does not show. A
        curve over the temperature would then be a guess with a confident
        face, so the planner falls back to the sliders.
        """
        found = self.conflicts()
        return not (found >= MIN_CONFLICTS and found * 2 >= self.counts()[1])

    @property
    def applies(self) -> bool:
        """Whether the planner uses what was learned."""
        return self.use_learned and self.explained

    def source(self, temperature: float | None) -> str:
        if temperature is None:
            return SOURCE_OFF
        ranges = self.ranges() if self.applies else []
        if not ranges:
            return SOURCE_CURVE
        if ranges[0].low <= temperature <= ranges[-1].high:
            return SOURCE_LEARNED
        # Outside the observed temperatures the sliders give the shape; the
        # value still counts as learned wherever the fit moved it.
        moved = abs(self.measured_factor(temperature) - curve_factor(self.curve, temperature))
        return SOURCE_LEARNED if moved > 1e-9 else SOURCE_CURVE

    def bands(self) -> list[dict[str, Any]]:
        """The learned ranges for the sensor attributes and the diagnostics."""
        in_use = SOURCE_LEARNED if self.applies else SOURCE_CURVE
        return [
            {
                "from_c": r.low,
                "to_c": r.high,
                "learned": round(r.factor, 3),
                "samples": r.samples,
                "curve": round(curve_factor(self.curve, (r.low + r.high) / 2), 3),
                "in_use": in_use,
            }
            for r in self.ranges()
        ]

    def counts(self) -> tuple[int, int]:
        """(charge slots measured, of which the battery limited)."""
        return len(self._samples), sum(1 for s in self._samples if s.saturated)

    def adopted_percentages(self) -> list[int] | None:
        """Slider values that come closest to what was learned, or None if
        nothing was.

        Each slider takes the measured factor at its own temperature. A warmer
        battery never takes less, so a value below its colder neighbour is
        lifted to it - the flow would refuse a falling curve anyway. Five
        points cannot draw a step exactly; that is what planning with the
        learned values is for.
        """
        if not self.ranges():
            return None
        values: list[int] = []
        for temperature in CURVE_TEMPERATURES:
            percent = round(self.measured_factor(temperature) * 100.0)
            values.append(max(percent, values[-1]) if values else percent)
        return values

    @property
    def learned_band_count(self) -> int:
        return len(self.ranges())

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
    def from_dict(
        cls, data: dict[str, Any], curve: Curve, use_learned: bool = True
    ) -> ChargeRateModel:
        """Restore the samples; curve and switch come from the current options."""
        model = cls(curve, use_learned)
        model._samples = [
            ChargeSample(
                temperature=float(row["temperature"]),
                ratio=float(row["ratio"]),
                saturated=bool(row["saturated"]),
                at=datetime.fromisoformat(row["at"]),
            )
            for row in data.get("samples", [])
        ]
        model._prune()
        return model
