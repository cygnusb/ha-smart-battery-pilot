"""Household consumption forecast.

Two models, picked automatically based on available data:

* Hourly profile (works from day one): exponentially weighted average
  consumption per (weekday/weekend, hour-of-day).
* Ridge regression (pure Python, no numpy/scikit-learn): cyclic
  hour-of-day features, weekend flag, optional outdoor temperature with
  a heating-demand term. Used once enough samples exist.

Neither is better everywhere. Two daily harmonics cannot draw a sharp
recurring block - an appliance that runs for two hours at the same time
every day is smeared over half the day - while the profile knows nothing
about temperature. So once the history is long enough to hold the last two
weeks back, both are fitted on the rest, scored on those two weeks, and the
one with the lower error forecasts.

Both are trained from Home Assistant long-term statistics (hourly kWh)
by the coordinator; this module itself has no HA dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import statistics
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Minimum hourly samples before the regression model is trusted.
MIN_REGRESSION_SAMPLES = 14 * 24
RIDGE_LAMBDA = 1.0
# The most recent days are held back to score the two models against each
# other. Two weeks cover both day types twice, and the comparison only runs
# when the history before them is enough for a ridge of its own.
HOLDOUT_DAYS = 14
# A holdout that is mostly gaps says little about either model.
MIN_HOLDOUT_SAMPLES = HOLDOUT_DAYS * 24 // 2

MODEL_RIDGE = "ridge_regression"
MODEL_PROFILE = "hourly_profile"
MODEL_DEFAULT = "default"

Profile = dict[tuple[int, int], float]
HEATING_BASE_TEMP = 15.0  # °C, below this heating demand kicks in

# A household's floor is its base load, never zero. Meters that lose their
# source often report a constant 0 instead of going unavailable (Fronius
# `P_Load` does this for whole hours while the rest of the inverter data keeps
# flowing), and those hours arrive here as ordinary samples. Anything below
# this fraction of the household's own median is treated as a dropout.
IMPLAUSIBLE_FRACTION = 0.05


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


def _drop_implausible(samples: list[TrainingSample]) -> list[TrainingSample]:
    """Keep only samples that could plausibly be a real household hour.

    The threshold is relative to the household's own median rather than a
    fixed wattage, so a one-room flat and a heat-pump house are judged on
    their own scale. Exact zeroes go regardless of the median.
    """
    usable = [s for s in samples if s.kwh is not None and s.kwh > 0]
    if not usable:
        return []
    floor = IMPLAUSIBLE_FRACTION * statistics.median(s.kwh for s in usable)
    kept = [s for s in usable if s.kwh > floor]
    dropped = len(samples) - len(kept)
    if dropped:
        _LOGGER.debug(
            "Dropped %d of %d consumption samples below %.3f kWh/h "
            "(stuck or missing meter readings)",
            dropped,
            len(samples),
            floor,
        )
    return kept


def _fit_profile(samples: list[TrainingSample]) -> Profile:
    """Exponentially weighted mean per (weekend, hour): recent days count more."""
    latest = max(s.start for s in samples)
    sums: Profile = {}
    weights: Profile = {}
    for s in samples:
        key = (1 if s.start.weekday() >= 5 else 0, s.start.hour)
        age_days = (latest - s.start).total_seconds() / 86400.0
        w = 0.5 ** (age_days / 14.0)  # half-life of two weeks
        sums[key] = sums.get(key, 0.0) + w * s.kwh
        weights[key] = weights.get(key, 0.0) + w
    return {k: sums[k] / weights[k] for k in sums}


def _fit_ridge(samples: list[TrainingSample], use_temp: bool) -> list[float]:
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


def _profile_kwh_per_hour(profile: Profile, mean_kwh: float, start: datetime) -> float:
    key = (1 if start.weekday() >= 5 else 0, start.hour)
    return max(0.0, profile.get(key, mean_kwh))


def _ridge_kwh_per_hour(
    weights: list[float], use_temp: bool, start: datetime, temperature: float | None
) -> float:
    feats = _features(start, temperature, use_temp)
    return max(0.0, sum(w * f for w, f in zip(weights, feats, strict=True)))


def _mean_abs_error(
    predict: Callable[[TrainingSample], float], samples: list[TrainingSample]
) -> float:
    """Mean absolute error in kWh per hourly sample."""
    return sum(abs(predict(s) - s.kwh) for s in samples) / len(samples)


def _holdout_errors(samples: list[TrainingSample], use_temp: bool) -> dict[str, float] | None:
    """Score both models on the most recent days, fitted on everything before.

    Returns None when the comparison cannot be made: too little history
    before the holdout for a ridge, or a holdout that is mostly gaps.
    """
    cutoff = max(s.start for s in samples) - timedelta(days=HOLDOUT_DAYS)
    fit = [s for s in samples if s.start <= cutoff]
    holdout = [s for s in samples if s.start > cutoff]
    if len(fit) < MIN_REGRESSION_SAMPLES or len(holdout) < MIN_HOLDOUT_SAMPLES:
        return None
    try:
        weights = _fit_ridge(fit, use_temp)
    except ValueError:
        return None
    profile = _fit_profile(fit)
    mean_kwh = sum(s.kwh for s in fit) / len(fit)
    return {
        # Each holdout hour is scored with its own measured temperature: the
        # best case for the ridge, which in production gets a single current
        # reading for the whole horizon.
        MODEL_RIDGE: _mean_abs_error(
            lambda s: _ridge_kwh_per_hour(weights, use_temp, s.start, s.temperature), holdout
        ),
        MODEL_PROFILE: _mean_abs_error(
            lambda s: _profile_kwh_per_hour(profile, mean_kwh, s.start), holdout
        ),
    }


def _valid_errors(value: Any) -> dict[str, float] | None:
    """A stored pair of holdout errors, or None for anything else."""
    if not isinstance(value, dict):
        return None
    try:
        return {key: float(value[key]) for key in (MODEL_RIDGE, MODEL_PROFILE)}
    except (KeyError, TypeError, ValueError):
        return None


class ConsumptionForecaster:
    """Predicts household consumption in kWh for arbitrary time slots."""

    def __init__(self) -> None:
        self._weights: list[float] | None = None
        self._uses_temperature = False
        self._profile: Profile = {}
        self._mean_kwh = 0.5
        self._sample_count = 0
        # Set only when the profile beat a fitted ridge on the holdout.
        self._profile_selected = False
        self._holdout_mae: dict[str, float] | None = None

    @property
    def _active_weights(self) -> list[float] | None:
        """The ridge weights when the ridge is the model in use, else None."""
        if self._profile_selected and self._profile:
            return None
        return self._weights

    @property
    def model_type(self) -> str:
        """The model that answers `predict_kwh`."""
        if self._active_weights is not None:
            return MODEL_RIDGE
        if self._profile:
            return MODEL_PROFILE
        return MODEL_DEFAULT

    @property
    def holdout_mae(self) -> dict[str, float] | None:
        """Holdout error of both models in kWh per hour, None if not compared."""
        return dict(self._holdout_mae) if self._holdout_mae is not None else None

    @property
    def sample_count(self) -> int:
        return self._sample_count

    # --- training -----------------------------------------------------------

    def train(self, samples: list[TrainingSample], require_temperature: bool = False) -> None:
        """Fit profile and (if enough data) the ridge regression model.

        ``require_temperature`` (heat-pump households) enables the heating-demand
        feature as soon as any temperature samples exist, instead of waiting
        until half the history is tagged.

        With enough history to hold the most recent two weeks back, the model
        that forecast those two weeks better is the one used for prediction.
        Both are then fitted on all samples either way.
        """
        samples = _drop_implausible(samples)
        self._sample_count = len(samples)
        if not samples:
            return

        self._mean_kwh = sum(s.kwh for s in samples) / len(samples)
        self._profile = _fit_profile(samples)

        # Cleared first, not only replaced: pointing the integration at a
        # different consumption entity restarts the history at zero, and a
        # ridge left over from the previous entity would keep answering every
        # prediction while the refitted profile sat unused.
        # The same goes for the verdict of the last comparison.
        self._weights = None
        self._uses_temperature = False
        self._profile_selected = False
        self._holdout_mae = None

        if len(samples) < MIN_REGRESSION_SAMPLES:
            return
        n_temp = sum(1 for s in samples if s.temperature is not None)
        use_temp = n_temp > 0 if require_temperature else n_temp >= len(samples) / 2
        try:
            self._weights = _fit_ridge(samples, use_temp)
            self._uses_temperature = use_temp
        except ValueError:
            return

        self._holdout_mae = _holdout_errors(samples, use_temp)
        if self._holdout_mae is not None:
            # Strictly lower: on a tie, and whenever no comparison was made,
            # the ridge stays, as it did before the two were compared.
            self._profile_selected = (
                self._holdout_mae[MODEL_PROFILE] < self._holdout_mae[MODEL_RIDGE]
            )
            _LOGGER.debug(
                "Holdout error over the last %d days: ridge %.3f, profile %.3f kWh/h -> %s",
                HOLDOUT_DAYS,
                self._holdout_mae[MODEL_RIDGE],
                self._holdout_mae[MODEL_PROFILE],
                self.model_type,
            )

    # --- prediction -----------------------------------------------------------

    def predict_kwh(self, start: datetime, hours: float, temperature: float | None = None) -> float:
        """Predict consumption in kWh for a slot of `hours` starting at `start`."""
        weights = self._active_weights
        if weights is not None:
            per_hour = _ridge_kwh_per_hour(weights, self._uses_temperature, start, temperature)
        else:
            per_hour = _profile_kwh_per_hour(self._profile, self._mean_kwh, start)
        return per_hour * hours

    # --- persistence ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self._weights,
            "uses_temperature": self._uses_temperature,
            "profile": {f"{k[0]}_{k[1]}": v for k, v in self._profile.items()},
            "mean_kwh": self._mean_kwh,
            "sample_count": self._sample_count,
            "selected_model": self.model_type,
            "holdout_mae": self.holdout_mae,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConsumptionForecaster:
        forecaster = cls()
        forecaster._uses_temperature = data.get("uses_temperature", False)
        forecaster._sample_count = data.get("sample_count", 0)
        weights = data.get("weights")
        # Two ways a stored ridge is unusable, both ending in the profile until
        # the next training run: an older feature layout, which would silently
        # be truncated at prediction time, and a sample count too small to have
        # produced those weights - the mark of a model fitted to a history that
        # is no longer the one being forecast.
        expected = len(_features(datetime(2024, 1, 1), None, forecaster._uses_temperature))
        forecaster._weights = (
            list(weights)
            if isinstance(weights, list)
            and len(weights) == expected
            and forecaster._sample_count >= MIN_REGRESSION_SAMPLES
            else None
        )
        forecaster._profile = {
            (int(k.split("_")[0]), int(k.split("_")[1])): v
            for k, v in (data.get("profile") or {}).items()
        }
        forecaster._mean_kwh = data.get("mean_kwh", 0.5)
        # A model stored before the two were compared has neither field and
        # keeps forecasting with the ridge until the next training run.
        forecaster._profile_selected = data.get("selected_model") == MODEL_PROFILE
        forecaster._holdout_mae = _valid_errors(data.get("holdout_mae"))
        return forecaster
