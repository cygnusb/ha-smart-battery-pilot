"""Sensor metadata that affects recorder statistics."""

from homeassistant.components.sensor import SensorStateClass

from smart_battery_pilot.sensor import SavingsSensor


def test_estimated_savings_is_not_an_accumulating_total():
    """Horizon re-estimates jump; TOTAL would be treated as meter resets."""
    assert SavingsSensor._attr_state_class == SensorStateClass.MEASUREMENT
