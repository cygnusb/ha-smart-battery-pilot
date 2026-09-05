"""Sensor metadata that Home Assistant validates or the recorder acts on."""

from types import SimpleNamespace

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from smart_battery_pilot.const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    ATTR_SLOTS,
)
from smart_battery_pilot.sensor import (
    ActualSavingsEurSensor,
    ActualSavingsKwhSensor,
    ChargePlanSensor,
    CurrentActionSensor,
    NextActionSensor,
    PlanStatusSensor,
    SavingsSensor,
)


def test_estimated_savings_is_not_an_accumulating_total():
    """Horizon re-estimates jump; TOTAL would be treated as meter resets."""
    assert SavingsSensor._attr_state_class == SensorStateClass.MEASUREMENT


def test_estimated_savings_has_no_monetary_device_class():
    """HA only accepts MONETARY together with TOTAL and logs an error otherwise."""
    assert getattr(SavingsSensor, "_attr_device_class", None) is None
    assert SavingsSensor._attr_native_unit_of_measurement == "EUR"


def test_accumulated_savings_pairs_monetary_with_total():
    assert ActualSavingsEurSensor._attr_device_class == SensorDeviceClass.MONETARY
    assert ActualSavingsEurSensor._attr_state_class == SensorStateClass.TOTAL
    assert ActualSavingsKwhSensor._attr_state_class == SensorStateClass.TOTAL


def test_plan_slots_are_kept_out_of_the_recorder():
    """~190 slot dicts twice an hour is pure ballast in the database."""
    assert ATTR_SLOTS in ChargePlanSensor._unrecorded_attributes


def test_every_reported_status_is_a_declared_option():
    """A status the sensor can return but has not declared breaks the enum."""
    reported = {"ok", "no_price_data", "no_soc", "no_price_adapter", "error"}
    assert reported <= set(PlanStatusSensor._attr_options)


def test_current_action_does_not_declare_a_reserved_state():
    """`unknown` and `unavailable` belong to Home Assistant.

    As an enum option `unknown` is indistinguishable from a genuinely unknown
    state and its translation is never rendered, so "no plan" needs a name of
    its own.
    """
    options = set(CurrentActionSensor._attr_options)
    assert not options & {"unknown", "unavailable", "none", ""}
    assert "no_plan" in options


def test_current_action_falls_back_to_a_declared_option():
    """A state the sensor can return but has not declared breaks the enum."""
    coordinator = SimpleNamespace(data=None)
    sensor = object.__new__(CurrentActionSensor)
    sensor.coordinator = coordinator

    assert sensor.native_value in CurrentActionSensor._attr_options


def test_declared_actions_cover_every_plan_action():
    planned = {ACTION_CHARGE, ACTION_AUTO, ACTION_IDLE, ACTION_EXPORT}
    assert planned <= set(CurrentActionSensor._attr_options)
    assert planned <= set(NextActionSensor._attr_options)
