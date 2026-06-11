"""Stub of homeassistant.util.dt."""

from datetime import datetime, timezone


def now():
    return datetime.now().astimezone()


def parse_datetime(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def as_local(value):
    return value.astimezone() if value.tzinfo else value


def utc_from_timestamp(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)
