def async_track_state_change_event(hass, entity_ids, action):
    return lambda: None


# Timers registered by async_track_point_in_time, newest last. Tests fire them
# by hand to exercise slot-boundary behaviour; the real helper would need the
# clock to advance.
SCHEDULED_POINTS: list[tuple[object, object, object]] = []


def async_track_point_in_time(hass, action, when):
    entry = (hass, action, when)
    SCHEDULED_POINTS.append(entry)

    def _cancel():
        if entry in SCHEDULED_POINTS:
            SCHEDULED_POINTS.remove(entry)

    return _cancel


def fire_scheduled_point(index: int = -1) -> None:
    """Invoke a registered timer callback as Home Assistant would."""
    _hass, action, when = SCHEDULED_POINTS[index]
    action(when)
