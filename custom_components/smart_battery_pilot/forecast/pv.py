"""PV production forecast helper.

Two ways to say how much PV a slot gets, both pure Python, no HA dependencies:

- `pv_kwh_for_slot` distributes daily PV forecast values (kWh for
  today/tomorrow, e.g. from Open-Meteo Solar Forecast or Forecast.Solar) over
  the daylight hours with a sine-shaped curve.
- `HourlyPv` reads the hourly curves forecast integrations hand to the Energy
  dashboard (`wh_hours`), which know the shape of the day: an east roof
  peaks in the morning, a cloudy morning shifts the yield to the afternoon.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
import math
from typing import Any

# One forecast as (start, end, kWh) intervals, start/end as POSIX timestamps.
# Timestamps rather than datetimes: subtracting two datetimes that share a
# zoneinfo is wall-clock arithmetic, which is an hour off across a DST switch.
Interval = tuple[float, float, float]

_HOUR = 3600.0

# Fallback daylight window when the caller has no sunrise/sunset to offer.
SUNRISE_HOUR = 6.0
SUNSET_HOUR = 21.0


def _shape(hour: float, sunrise: float, sunset: float) -> float:
    """Relative production at local `hour` (0..1, sine over daylight)."""
    if hour <= sunrise or hour >= sunset:
        return 0.0
    return math.sin(math.pi * (hour - sunrise) / (sunset - sunrise))


def pv_kwh_for_slot(
    start: datetime,
    hours: float,
    daily_kwh_today: float | None,
    daily_kwh_tomorrow: float | None,
    now: datetime,
    sunrise_hour: float = SUNRISE_HOUR,
    sunset_hour: float = SUNSET_HOUR,
) -> float:
    """Expected PV energy (kWh) in the slot, from daily forecast totals.

    `sunrise_hour`/`sunset_hour` are the local clock hours of the daylight
    window; the caller passes the real ones from `sun.sun` so a December day
    is not modelled as a 15-hour summer day.
    """
    day_offset = (start.date() - now.date()).days
    if day_offset == 0:
        daily = daily_kwh_today
    elif day_offset == 1:
        daily = daily_kwh_tomorrow
    else:
        daily = None
    if not daily or daily <= 0:
        return 0.0
    if not 0.0 <= sunrise_hour < sunset_hour <= 24.0:
        sunrise_hour, sunset_hour = SUNRISE_HOUR, SUNSET_HOUR

    # Integrate the shape over the slot (10-minute steps) and normalize by
    # the shape's integral over the full day.
    total_shape = sum(_shape(h / 6.0, sunrise_hour, sunset_hour) for h in range(24 * 6)) / 6.0
    if total_shape <= 0:
        return 0.0
    steps = max(1, int(hours * 6))
    step = timedelta(hours=hours / steps)
    slot_shape = 0.0
    for i in range(steps):
        t = start + (i + 0.5) * step
        slot_shape += _shape(t.hour + t.minute / 60.0, sunrise_hour, sunset_hour) * (hours / steps)
    return daily * slot_shape / total_shape


def _parse_key(key: Any, default_tz: tzinfo | None) -> datetime | None:
    if isinstance(key, datetime):
        parsed = key
    else:
        try:
            parsed = datetime.fromisoformat(str(key))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        if default_tz is None:
            return None
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def intervals_from_wh_hours(
    wh_hours: Mapping[Any, Any],
    period_end: bool,
    default_tz: tzinfo | None = None,
) -> list[Interval]:
    """Turn one `wh_hours` mapping ({timestamp: Wh}) into kWh intervals.

    Integrations disagree on what the timestamp names. Open-Meteo Solar
    Forecast and Helios key each value by the start of its hour. Forecast.Solar
    keys it by the end: its first key is sunrise with 0 Wh, "08:00" holds what
    was produced from sunrise to 08:00, and its last key is sunset. With
    `period_end` a value covers the hour before its key, cut at the previous
    key; otherwise the hour after it, cut at the next.
    """
    points: list[tuple[float, float]] = []
    for key, value in wh_hours.items():
        when = _parse_key(key, default_tz)
        if when is None or isinstance(value, bool):
            continue
        try:
            wh = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(wh):
            continue
        points.append((when.timestamp(), max(0.0, wh) / 1000.0))
    points.sort()

    intervals: list[Interval] = []
    for i, (ts, kwh) in enumerate(points):
        if period_end:
            start = ts - _HOUR
            if i > 0:
                start = max(start, points[i - 1][0])
            end = ts
        else:
            start = ts
            end = ts + _HOUR
            if i + 1 < len(points):
                end = min(end, points[i + 1][0])
        if end > start:
            intervals.append((start, end, kwh))
    return intervals


class HourlyPv:
    """The sum of one or more hourly forecasts, read per slot."""

    def __init__(self, forecasts: list[list[Interval]]) -> None:
        self._intervals = sorted(iv for forecast in forecasts for iv in forecast)
        # A slot is only answered when every forecast spans it - otherwise a
        # 2-day forecast next to a 7-day one would undercount from day 3 on.
        spans = [(f[0][0], max(iv[1] for iv in f)) for f in (sorted(x) for x in forecasts) if f]
        if forecasts and len(spans) == len(forecasts):
            self._span: tuple[float, float] | None = (
                max(s for s, _ in spans),
                min(e for _, e in spans),
            )
        else:
            self._span = None

    def covers(self, start: datetime, end: datetime) -> bool:
        if self._span is None:
            return False
        return self._span[0] <= start.timestamp() and end.timestamp() <= self._span[1]

    def kwh(self, start: datetime, end: datetime) -> float:
        """Forecast PV energy in [start, end), each interval spread evenly."""
        a, b = start.timestamp(), end.timestamp()
        total = 0.0
        for iv_start, iv_end, kwh in self._intervals:
            if iv_start >= b:
                break
            overlap = min(b, iv_end) - max(a, iv_start)
            if overlap > 0:
                total += kwh * overlap / (iv_end - iv_start)
        return total
