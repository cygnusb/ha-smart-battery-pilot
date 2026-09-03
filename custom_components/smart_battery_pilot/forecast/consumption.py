"""Household consumption forecast.

Two models, picked automatically based on available data:

* Hourly profile (fallback, works from day one): exponentially weighted
  average consumption per (weekday/weekend, hour-of-day).
* Ridge regression (pure Python, no numpy/scikit-learn): cyclic
  hour-of-day features, weekend flag, optional outdoor temperature with
  a heating-demand term. Used once enough samples exist.

Both are trained from Home Assistant long-term statistics (hourly kWh)
by the coordinator; this module itself has no HA dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

# Minimum hourly samples before the regression model is trusted.
MIN_REGRESSION_SAMPLES = 14 * 24
RIDGE_LAMBDA = 1.0
HEATING_BASE_TEMP = 15.0  # °C, below this heating demand kicks in


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """One hour of historic consumption."""

    start: datetime
    kwh: float
    temperature: float | None = None


def _features(when: datetime, temperature: float | None, use_temp: bool) -> list[float]:
    hour = when.hour + when.minute / 60.0
    angle = 2 * math.pi * hour / 24.0
    weekend = 1.0 if when.weekday() >= 5 else 0.0
    feats = [
        1.0,
        math.sin(angle),
        math.cos(angle),
        math.sin(2 * angle),
        math.cos(2 * angle),
        weekend,
        weekend * math.sin(angle),
        weekend * math.cos(angle),
    ]
    if use_temp:
        temp = temperature if temperature is not None else HEATING_BASE_TEMP
        feats.append(temp / 10.0)
        feats.append(max(0.0, HEATING_BASE_TEMP - temp) / 10.0)  # heating demand
    return feats


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve matrix @ x = rhs via Gaussian elimination with partial pivoting."""
    n = len(matrix)
    a = [[*row, rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, n):
            factor = a[row][col] / a[col][col]
            for k in range(col, n + 1):
                a[row][k] -= factor * a[col][k]
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        x[row] = (a[row][n] - sum(a[row][k] * x[k] for k in range(row + 1, n))) / a[row][row]
    return x


class ConsumptionForecaster:
    """Predicts household consumption in kWh for arbitrary time slots."""

    def __init__(self) -> None:
        self._weights: list[float] | None = None
        self._uses_temperature = False
        self._profile: dict[tuple[int, int], float] = {}
        self._mean_kwh = 0.5
        self._sample_count = 0

    @property
    def model_type(self) -> str:
        if self._weights is not None:
            return "ridge_regression"
        if self._profile:
            return "hourly_profile"
        return "default"

    @property
    def sample_count(self) -> int:
        return self._sample_count

    # --- training -----------------------------------------------------------

    def train(
        self, samples: list[TrainingSample], require_temperature: bool = False
    ) -> None:
        """Fit profile and (if enough data) the ridge regression model.

        ``require_temperature`` (heat-pump households) enables the heating-demand
        feature as soon as any temperature samples exist, instead of waiting
        until half the history is tagged.
        """
        samples = [s for s in samples if s.kwh is not None and s.kwh >= 0]
        self._sample_count = len(samples)
        if not samples:
            return

        self._mean_kwh = sum(s.kwh for s in samples) / len(samples)
        self._train_profile(samples)

        if len(samples) >= MIN_REGRESSION_SAMPLES:
            n_temp = sum(1 for s in samples if s.temperature is not None)
            use_temp = (
                n_temp > 0 if require_temperature else n_temp >= len(samples) / 2
            )
            try:
                self._weights = self._train_ridge(samples, use_temp)
                self._uses_temperature = use_temp
            except ValueError:
                self._weights = None

    def _train_profile(self, samples: list[TrainingSample]) -> None:
        """Exponentially weighted mean per (weekend, hour): recent days count more."""
        latest = max(s.start for s in samples)
        sums: dict[tuple[int, int], float] = {}
        weights: dict[tuple[int, int], float] = {}
        for s in samples:
            key = (1 if s.start.weekday() >= 5 else 0, s.start.hour)
            age_days = (latest - s.start).total_seconds() / 86400.0
            w = 0.5 ** (age_days / 14.0)  # half-life of two weeks
            sums[key] = sums.get(key, 0.0) + w * s.kwh
            weights[key] = weights.get(key, 0.0) + w
        self._profile = {k: sums[k] / weights[k] for k in sums}

    def _train_ridge(self, samples: list[TrainingSample], use_temp: bool) -> list[float]:
        rows = [_features(s.start, s.temperature, use_temp) for s in samples]
        targets = [s.kwh for s in samples]
        n_feat = len(rows[0])
        xtx = [[0.0] * n_feat for _ in range(n_feat)]
        xty = [0.0] * n_feat
        for row, y in zip(rows, targets, strict=True):
            for i in range(n_feat):
                xty[i] += row[i] * y
                for j in range(n_feat):
                    xtx[i][j] += row[i] * row[j]
        for i in range(n_feat):
            xtx[i][i] += RIDGE_LAMBDA
        return _solve(xtx, xty)

    # --- prediction -----------------------------------------------------------

    def predict_kwh(
        self, start: datetime, hours: float, temperature: float | None = None
    ) -> float:
        """Predict consumption in kWh for a slot of `hours` starting at `start`."""
        if self._weights is not None:
            feats = _features(start, temperature, self._uses_temperature)
            per_hour = sum(w * f for w, f in zip(self._weights, feats, strict=True))
        else:
            key = (1 if start.weekday() >= 5 else 0, start.hour)
            per_hour = self._profile.get(key, self._mean_kwh)
        return max(0.0, per_hour) * hours

    # --- persistence ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self._weights,
            "uses_temperature": self._uses_temperature,
            "profile": {f"{k[0]}_{k[1]}": v for k, v in self._profile.items()},
            "mean_kwh": self._mean_kwh,
            "sample_count": self._sample_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionForecaster:
        forecaster = cls()
        forecaster._uses_temperature = data.get("uses_temperature", False)
        weights = data.get("weights")
        # A model stored under an older feature layout would silently be
        # truncated at prediction time; drop it and fall back to the profile
        # until the next nightly training run replaces it.
        expected = len(_features(datetime(2024, 1, 1), None, forecaster._uses_temperature))
        forecaster._weights = (
            list(weights) if isinstance(weights, list) and len(weights) == expected else None
        )
        forecaster._profile = {
            (int(k.split("_")[0]), int(k.split("_")[1])): v
            for k, v in (data.get("profile") or {}).items()
        }
        forecaster._mean_kwh = data.get("mean_kwh", 0.5)
        forecaster._sample_count = data.get("sample_count", 0)
        return forecaster
