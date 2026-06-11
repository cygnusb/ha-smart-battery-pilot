"""Deterministic charge plan optimizer.

Pure functions, no Home Assistant dependencies. The optimizer works on
price slots (any resolution) with a net consumption forecast per slot
and produces an action per slot:

* ``charge``  – force-charge the battery from the grid (cheap slot)
* ``auto``    – inverter auto mode, battery covers house consumption
* ``idle``    – block discharging to preserve energy for pricier slots
* ``export``  – force-discharge into the grid (export mode only)

Algorithm: greedy pairing with a stored-energy timeline simulation.
Discharge candidates are visited from the most expensive slot down;
each first consumes already-stored (PV/initial) energy, then is paired
with the cheapest earlier grid-charge slots whose price plus efficiency
losses and the configured spread still undercut the discharge price.
All assignments are validated against SOC and power limits over time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    DISCHARGE_MODE_EXPORT,
)
from .price_adapters.base import PriceSlot


@dataclass(frozen=True, slots=True)
class BatteryState:
    """Battery parameters and current state."""

    capacity_kwh: float
    soc: float  # current state of charge, percent
    min_soc: float
    max_soc: float
    max_charge_power_w: float
    max_discharge_power_w: float
    efficiency: float  # roundtrip efficiency, percent (e.g. 90)


@dataclass(frozen=True, slots=True)
class OptimizerConfig:
    """Tunables for the planner."""

    spread_threshold: float  # EUR/kWh minimum charge/discharge spread
    discharge_mode: str  # self_consumption | export
    feed_in_tariff: float = 0.0  # EUR/kWh; 0 = use market price for export


@dataclass(frozen=True, slots=True)
class InputSlot:
    """One price slot with the forecasted net demand (consumption - PV)."""

    price_slot: PriceSlot
    net_demand_kwh: float


@dataclass(slots=True)
class PlanSlot:
    """Planned action for one slot."""

    start: datetime
    end: datetime
    action: str
    price: float
    net_demand_kwh: float
    charge_power_w: float = 0.0
    discharge_kwh: float = 0.0  # energy delivered (to load or grid)
    soc_forecast: float = 0.0  # SOC at end of slot, percent


@dataclass(slots=True)
class Plan:
    """The optimization result."""

    slots: list[PlanSlot] = field(default_factory=list)
    estimated_savings_eur: float = 0.0
    grid_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0


def build_plan(
    slots: list[InputSlot], battery: BatteryState, config: OptimizerConfig
) -> Plan:
    """Compute the charge/discharge plan over the given horizon."""
    if not slots or battery.capacity_kwh <= 0:
        return Plan()

    n = len(slots)
    eta = max(0.5, min(1.0, battery.efficiency / 100.0))
    eta_one_way = math.sqrt(eta)

    capacity = battery.capacity_kwh
    e_init = max(0.0, (battery.soc - battery.min_soc) / 100.0 * capacity)
    e_max = max(0.0, (battery.max_soc - battery.min_soc) / 100.0 * capacity)
    e_init = min(e_init, e_max)

    hours = [s.price_slot.hours for s in slots]
    prices = [s.price_slot.price for s in slots]
    demand = [max(0.0, s.net_demand_kwh) for s in slots]

    # Stored energy added/removed per slot (kWh measured inside the battery).
    charge_stored = [0.0] * n
    discharge_stored = [0.0] * n
    export_stored = [0.0] * n

    charge_cap = [battery.max_charge_power_w / 1000.0 * h * eta_one_way for h in hours]
    discharge_cap = [battery.max_discharge_power_w / 1000.0 * h for h in hours]

    def timeline() -> list[float]:
        """Stored energy (above min SOC) at the END of each slot."""
        levels = []
        e = e_init
        for i in range(n):
            e += charge_stored[i] - discharge_stored[i] - export_stored[i]
            levels.append(e)
        return levels

    def assign_discharge(d: int, want_stored: float, store: list[float]) -> float:
        """Try to supply `want_stored` kWh at slot d; returns assigned amount."""
        assigned = 0.0

        # 1. Use energy that is already in the battery at slot d.
        levels = timeline()
        available = min(levels[d:]) if d < n else 0.0
        use = min(want_stored, max(0.0, available))
        if use > 1e-9:
            store[d] += use
            assigned += use

        # 2. Pair with cheap earlier grid-charge slots.
        remaining = want_stored - assigned
        if remaining > 1e-9:
            sell_price = prices[d]
            if store is export_stored and config.feed_in_tariff > 0:
                sell_price = config.feed_in_tariff
            candidates = sorted(
                (i for i in range(d) if charge_cap[i] - charge_stored[i] > 1e-9),
                key=lambda i: prices[i],
            )
            for c in candidates:
                if remaining <= 1e-9:
                    break
                # Cost to deliver 1 kWh from grid via battery vs. discharge value
                if prices[c] / eta + config.spread_threshold >= sell_price:
                    break  # candidates are price-sorted: none cheaper left
                levels = timeline()
                headroom = e_max - max(levels[c:d])
                q = min(remaining, charge_cap[c] - charge_stored[c], max(0.0, headroom))
                if q <= 1e-9:
                    continue
                charge_stored[c] += q
                store[d] += q
                assigned += q
                remaining -= q
        return assigned

    # --- self-consumption: cover demand in the most expensive slots first ---
    order = sorted(range(n), key=lambda i: prices[i], reverse=True)
    for d in order:
        # Energy delivered to the load is limited by demand and power.
        deliverable = min(demand[d], discharge_cap[d])
        want_stored = deliverable / eta_one_way
        if want_stored <= 1e-9:
            continue
        assign_discharge(d, want_stored, discharge_stored)

    # --- export mode: sell remaining/cheaply-chargeable energy at peaks ---
    if config.discharge_mode == DISCHARGE_MODE_EXPORT:
        for d in order:
            sell_price = config.feed_in_tariff if config.feed_in_tariff > 0 else prices[d]
            if sell_price <= config.spread_threshold:
                continue
            room = discharge_cap[d] - discharge_stored[d] * eta_one_way
            want_stored = max(0.0, room) / eta_one_way
            if want_stored <= 1e-9:
                continue
            assign_discharge(d, want_stored, export_stored)

    # --- build plan slots ----------------------------------------------------
    plan = Plan()
    levels = timeline()
    future_discharge_prices: list[float] = []
    max_future_price = [0.0] * n
    running_max = 0.0
    for i in range(n - 1, -1, -1):
        max_future_price[i] = running_max
        if discharge_stored[i] > 1e-9 or export_stored[i] > 1e-9:
            running_max = max(running_max, prices[i])

    for i, slot in enumerate(slots):
        action = ACTION_AUTO
        charge_power = 0.0
        delivered = (discharge_stored[i] + export_stored[i]) * eta_one_way

        if charge_stored[i] > 1e-9:
            action = ACTION_CHARGE
            grid_kwh = charge_stored[i] / eta_one_way
            charge_power = grid_kwh / hours[i] * 1000.0
            plan.grid_charge_kwh += grid_kwh
        elif export_stored[i] > 1e-9:
            action = ACTION_EXPORT
        elif discharge_stored[i] > 1e-9:
            action = ACTION_AUTO
        elif max_future_price[i] > prices[i] and levels[i] > 1e-9:
            # Battery holds energy reserved for a pricier future slot.
            action = ACTION_IDLE

        if delivered > 0:
            plan.battery_discharge_kwh += delivered

        plan.slots.append(
            PlanSlot(
                start=slot.price_slot.start,
                end=slot.price_slot.end,
                action=action,
                price=prices[i],
                net_demand_kwh=slot.net_demand_kwh,
                charge_power_w=round(charge_power, 1),
                discharge_kwh=round(delivered, 3),
                soc_forecast=round(
                    battery.min_soc + levels[i] / capacity * 100.0, 1
                ),
            )
        )

    # --- savings: value of delivered energy minus grid charging cost --------
    savings = 0.0
    for i in range(n):
        savings += discharge_stored[i] * eta_one_way * prices[i]
        if export_stored[i] > 1e-9:
            sell = config.feed_in_tariff if config.feed_in_tariff > 0 else prices[i]
            savings += export_stored[i] * eta_one_way * sell
        savings -= charge_stored[i] / eta_one_way * prices[i]
    plan.estimated_savings_eur = round(savings, 2)
    return plan
