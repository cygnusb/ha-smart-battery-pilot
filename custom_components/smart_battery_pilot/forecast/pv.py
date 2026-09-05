"""PV production forecast helper.

Distributes daily PV forecast values (kWh for today/tomorrow, e.g. from
Open-Meteo Solar Forecast or Forecast.Solar) over the daylight hours
with a sine-shaped curve so the optimizer can subtract expected PV from
the consumption forecast per slot. Pure Python, no HA dependencies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math

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
