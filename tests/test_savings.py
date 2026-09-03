"""Estimated savings must be measured against doing nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntry
import pytest

from smart_battery_pilot.const import (
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
)
from smart_battery_pilot.coordinator import SBPCoordinator
from smart_battery_pilot.optimizer import (
    BatteryState,
    InputSlot,
    OptimizerConfig,
    build_plan,
)
from smart_battery_pilot.price_adapters.base import PriceSlot

T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def _plan(prices, *, soc, demand=0.5, spread=0.20, capacity=20.0):
    slots = [
        InputSlot(
            price_slot=PriceSlot(
                start=T0 + timedelta(hours=i),
                end=T0 + timedelta(hours=i + 1),
                price=price,
            ),
            net_demand_kwh=demand,
        )
        for i, price in enumerate(prices)
    ]
    battery = BatteryState(
        capacity_kwh=capacity,
        soc=soc,
        min_soc=10,
        max_soc=95,
        max_charge_power_w=5000,
        max_discharge_power_w=5000,
        efficiency=90,
    )
    config = OptimizerConfig(
        spread_threshold=spread,
        discharge_mode="self_consumption",
        feed_in_tariff=0.08,
    )
    return build_plan(slots, battery, config)


def test_a_plan_that_changes_nothing_saves_nothing():
    """A full battery on a flat curve is pure self consumption, not a saving."""
    prices = [0.25] * 24
    prices[19] = 0.40
    plan = _plan(prices, soc=95)

    assert {slot.action for slot in plan.slots} == {"auto"}
    assert plan.grid_charge_kwh == 0.0
    assert plan.estimated_savings_eur == 0.0


def test_flat_prices_never_produce_a_saving():
    plan = _plan([0.30] * 24, soc=10, demand=1.0)
    assert plan.estimated_savings_eur == 0.0


def test_arbitrage_across_a_price_spread_does_save():
    prices = [0.10] * 6 + [0.45] * 4 + [0.20] * 8 + [0.50] * 6
    plan = _plan(prices, soc=10, demand=1.5, spread=0.10, capacity=12.8)

    assert "charge" in {slot.action for slot in plan.slots}
    assert plan.estimated_savings_eur > 0
    # Bounded by the value of everything the household consumes.
    assert plan.estimated_savings_eur < sum(1.5 * price for price in prices)


def test_reserving_energy_for_a_peak_beats_spending_it_early():
    """No grid charging involved - the gain is purely better timing."""
    prices = [0.20] * 20 + [0.60] * 4
    plan = _plan(prices, soc=60, demand=1.0, capacity=10.0)

    assert plan.grid_charge_kwh == 0.0
    assert "idle" in {slot.action for slot in plan.slots}
    assert plan.estimated_savings_eur > 0


# --- accumulated savings need both meters ----------------------------------


class _States:
    def __init__(self):
        self._data = {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, state):
        self._data[entity_id] = SimpleNamespace(state=str(state), attributes={})


class _Hass:
    def __init__(self):
        self.states = _States()

    def async_create_task(self, coro):
        coro.close()
        return


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, False),
        ({CONF_BATTERY_CHARGE_ENERGY_ENTITY: "sensor.a"}, False),
        ({CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: "sensor.b"}, False),
        (
            {
                CONF_BATTERY_CHARGE_ENERGY_ENTITY: "sensor.a",
                CONF_BATTERY_DISCHARGE_ENERGY_ENTITY: "sensor.b",
            },
            True,
        ),
    ],
)
def test_actual_savings_needs_both_meters(config, expected):
    """One meter alone cannot produce a net figure, so publish nothing."""
    coordinator = SBPCoordinator(_Hass(), ConfigEntry(data=config))
    assert coordinator._has_energy_entities() is expected
