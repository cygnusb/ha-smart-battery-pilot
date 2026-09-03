"""Smoke test: every integration module must be importable (under stubs)."""

import importlib

import pytest

MODULES = [
    "smart_battery_pilot",
    "smart_battery_pilot.config_flow",
    "smart_battery_pilot.coordinator",
    "smart_battery_pilot.diagnostics",
    "smart_battery_pilot.executor",
    "smart_battery_pilot.sensor",
    "smart_battery_pilot.switch",
    "smart_battery_pilot.binary_sensor",
    "smart_battery_pilot.entity",
    "smart_battery_pilot.optimizer",
    "smart_battery_pilot.price_adapters",
    "smart_battery_pilot.forecast",
    "smart_battery_pilot.forecast.consumption",
    "smart_battery_pilot.forecast.pv",
]


@pytest.mark.parametrize("module", MODULES)
def test_import(module):
    importlib.import_module(module)
