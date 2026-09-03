"""Regression test against a real Nordpool entity snapshot (2026-06-11)."""

from datetime import datetime, timedelta, timezone
import itertools
import json
from pathlib import Path

from smart_battery_pilot.price_adapters import detect_adapter

FIXTURE = Path(__file__).parent / "fixture_nordpool_live.json"
NOW = datetime(2026, 6, 11, 7, 50, tzinfo=timezone(timedelta(hours=2)))


def test_live_nordpool_snapshot():
    attrs = json.loads(FIXTURE.read_text())
    adapter = detect_adapter(attrs)
    assert adapter is not None and adapter.name == "nordpool"

    slots = adapter.parse(attrs, NOW)
    assert len(slots) == 65  # rest of today, 15-min resolution
    assert slots[0].hours == 0.25
    assert all(s.end > NOW for s in slots)
    assert all(-0.5 < s.price < 2.0 for s in slots)
    # chronological and gapless
    for a, b in itertools.pairwise(slots):
        assert a.end == b.start
