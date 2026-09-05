"""Fixtures for the tests that run against a real Home Assistant.

Deliberately separate from `tests/`: that suite runs against the hand-written
stubs in `tests/stubs` and is fast, but by construction it cannot notice when
Home Assistant changes an API underneath us. These tests install the real
thing and set the integration up end to end. Never collect both suites in one
pytest run - the stub path would shadow the real `homeassistant` package.
"""

from __future__ import annotations

from pathlib import Path
import sys

from homeassistant.core import HomeAssistant
import pytest

# The tests import `custom_components.smart_battery_pilot`, the way Home
# Assistant loads it. pytest only puts the test file's own directory on
# sys.path, so the repo root has to go on explicitly - otherwise collection
# dies with "No module named 'custom_components'" under a bare `pytest`.
# (`python -m pytest` happens to work because it adds the current directory,
# which is exactly the difference that let this pass locally and fail in CI.)
sys.path.insert(0, str(Path(__file__).parent.parent))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def sbp_hass(recorder_mock, enable_custom_integrations, hass) -> HomeAssistant:
    """A Home Assistant that can load this custom integration.

    The order of the parameters is load-bearing: `recorder_db_url` refuses to
    run once `hass` exists, so the recorder has to be requested before
    anything that pulls `hass` in.
    """
    return hass
