"""PV production forecast helper.

Distributes daily PV forecast values (kWh for today/tomorrow, e.g. from
Open-Meteo Solar Forecast or Forecast.Solar) over the daylight hours
with a sine-shaped curve so the optimizer can subtract expected PV from
the consumption forecast per slot. Pure Python, no HA dependencies.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

# Daylight window the sine curve is spread over (local time).
SUNRISE_HOUR = 6.0
SUNSET_HOUR = 21.0


def _shape(hour: float) -> float:
    """Relative production at local `hour` (0..1, sine over daylight)."""
    if hour <= SUNRISE_HOUR or hour >= SUNSET_HOUR:
        return 0.0
    return math.sin(math.pi * (hour - SUNRISE_HOUR) / (SUNSET_HOUR - SUNRISE_HOUR))


def pv_kwh_for_slot(
    start: datetime,
    hours: float,
    daily_kwh_today: float | None,
    daily_kwh_tomorrow: float | None,
    now: datetime,
) -> float:
    """Expected PV energy (kWh) in the slot, from daily forecast totals."""
    day_offset = (start.date() - now.date()).days
    if day_offset == 0:
        daily = daily_kwh_today
    elif day_offset == 1:
        daily = daily_kwh_tomorrow
    else:
        daily = None
    if not daily or daily <= 0:
        return 0.0

    # Integrate the shape over the slot (10-minute steps) and normalize by
    # the shape's integral over the full day.
    total_shape = sum(_shape(h / 6.0) for h in range(int(24 * 6))) / 6.0
    if total_shape <= 0:
        return 0.0
    steps = max(1, int(hours * 6))
    step = timedelta(hours=hours / steps)
    slot_shape = 0.0
    for i in range(steps):
        t = start + (i + 0.5) * step
        slot_shape += _shape(t.hour + t.minute / 60.0) * (hours / steps)
    return daily * slot_shape / total_shape
