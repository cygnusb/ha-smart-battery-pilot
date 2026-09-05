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
All assignments are validated against SOC and power limits over time -
including PV curtailment, which makes energy spent before a full battery
free to spend. A plan that still comes out worse than doing nothing is
discarded in favour of leaving the inverter alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math

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
    # Import surcharge already included in slot prices (EUR/kWh). Subtracted
    # again when valuing grid export, which does not collect those fees.
    price_offset: float = 0.0


@dataclass(frozen=True, slots=True)
class InputSlot:
    """One price slot with the forecasted net demand (consumption - PV)."""

    price_slot: PriceSlot
    net_demand_kwh: float
    pv_kwh: float = 0.0  # forecasted PV production in this slot (for display/simulation)


@dataclass(slots=True)
class PlanSlot:
    """Planned action for one slot."""

    start: datetime
    end: datetime
    action: str
    price: float
    net_demand_kwh: float
    pv_kwh: float = 0.0
    # Power the slot's action runs at: charge power in a `charge` slot,
    # discharge power in an `export` one. Named for the action, not for one
    # direction, because it is handed to whichever script the action selects.
    power_w: float = 0.0
    discharge_kwh: float = 0.0  # energy delivered (to load or grid)
    soc_forecast: float = 0.0  # SOC at end of slot, percent

    # Both comparisons go through timestamps on purpose. Comparing two aware
    # datetimes that share one tzinfo object is documented to ignore the
    # timezone, which picks the wrong one of the two 02:00 slots on the
    # autumn clock-change day - and those two have different prices.
    def covers(self, moment: datetime) -> bool:
        """True when `moment` falls inside this slot."""
        return self.start.timestamp() <= moment.timestamp() < self.end.timestamp()

    def starts_after(self, moment: datetime) -> bool:
        """True when this slot has not begun at `moment`."""
        return self.start.timestamp() > moment.timestamp()


@dataclass(slots=True)
class Plan:
    """The optimization result."""

    slots: list[PlanSlot] = field(default_factory=list)
    estimated_savings_eur: float = 0.0
    grid_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    warnings: list[str] = field(default_factory=list)


def build_plan(slots: list[InputSlot], battery: BatteryState, config: OptimizerConfig) -> Plan:
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

    # PV surplus (negative net demand) charges the battery in auto mode -
    # model it so summer days don't trigger needless reservations (idle) or
    # grid charging, and the SOC forecast rises with the sun.
    pv_surplus_stored = [
        min(
            max(0.0, -slots[i].net_demand_kwh) * eta_one_way,
            charge_cap[i],
        )
        for i in range(n)
    ]

    def timeline() -> tuple[list[float], list[float]]:
        """Stored energy (above min SOC) at the END of each slot, and the PV
        curtailed in each slot.

        PV surplus charging is clamped at max SOC; whatever does not fit is
        curtailed (exported or throttled away) and is reported per slot."""
        levels = []
        curtailed = []
        e = e_init
        for i in range(n):
            e += charge_stored[i] - discharge_stored[i] - export_stored[i]
            e += pv_surplus_stored[i]
            curtailed.append(max(0.0, e - e_max))
            e = min(e, e_max)
            levels.append(e)
        return levels, curtailed

    def withdrawable(levels: list[float], curtailed: list[float], d: int) -> float:
        """Stored energy that slot d may spend without emptying a later slot.

        Taking energy out at d lowers every later level by the same amount -
        unless a slot in between curtails PV. There the surplus that was about
        to be thrown away refills the gap for free, so a withdrawal before such
        a slot costs the tail nothing up to the curtailed amount. Reserving
        energy across a curtailment window therefore buys nothing: it only
        replaces free PV with energy the household paid for.
        """
        if d >= n:
            return 0.0
        room = math.inf
        absorbed = 0.0
        for j in range(d, n):
            absorbed += curtailed[j]
            room = min(room, levels[j] + absorbed)
        return max(0.0, room)

    def _export_sell_price(i: int) -> float:
        if config.feed_in_tariff > 0:
            return config.feed_in_tariff
        return prices[i] - config.price_offset

    def _delivers(i: int) -> bool:
        """True when slot i is already planned to take energy out of the battery."""
        return discharge_stored[i] > 1e-9 or export_stored[i] > 1e-9

    def assign_discharge(d: int, want_stored: float, store: list[float]) -> float:
        """Try to supply `want_stored` kWh at slot d; returns assigned amount."""
        assigned = 0.0

        # 1. Use energy that is already in the battery at slot d.
        levels, curtailed = timeline()
        available = withdrawable(levels, curtailed, d)
        use = min(want_stored, available)
        if use > 1e-9:
            store[d] += use
            assigned += use

        # 2. Pair with cheap earlier grid-charge slots.
        remaining = want_stored - assigned
        if remaining > 1e-9:
            sell_price = _export_sell_price(d) if store is export_stored else prices[d]
            candidates = sorted(
                (
                    i
                    for i in range(d)
                    if charge_cap[i] - charge_stored[i] - pv_surplus_stored[i] > 1e-9
                    and not _delivers(i)
                ),
                key=lambda i: prices[i],
            )
            for c in candidates:
                if remaining <= 1e-9:
                    break
                # Cost to deliver 1 kWh from grid via battery vs. discharge value
                if prices[c] / eta + config.spread_threshold >= sell_price:
                    break  # candidates are price-sorted: none cheaper left
                levels, _ = timeline()
                headroom = e_max - max(levels[c:d])
                q = min(
                    remaining,
                    charge_cap[c] - charge_stored[c] - pv_surplus_stored[c],
                    max(0.0, headroom),
                )
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
        if prices[d] <= 0:
            # Import is paid (or free). Hold the energy for a later positive hour.
            continue
        if charge_stored[d] > 1e-9:
            # A slot already paired as a cheap charge slot for a pricier hour
            # will be force-charged. The inverter cannot serve the house from
            # the battery at the same time, so that demand comes from the grid.
            continue
        # Energy delivered to the load is limited by demand and power.
        deliverable = min(demand[d], discharge_cap[d])
        want_stored = deliverable / eta_one_way
        if want_stored <= 1e-9:
            continue
        assign_discharge(d, want_stored, discharge_stored)

    export_spread_unreachable = False
    # --- export mode: sell remaining/cheaply-chargeable energy at peaks ---
    if config.discharge_mode == DISCHARGE_MODE_EXPORT:
        export_spread_unreachable = all(
            _export_sell_price(i) <= config.spread_threshold for i in range(n)
        )
        for d in order:
            sell_price = _export_sell_price(d)
            if sell_price <= config.spread_threshold:
                continue
            if charge_stored[d] > 1e-9:
                continue  # force-charging; the battery cannot export as well
            room = discharge_cap[d] - discharge_stored[d] * eta_one_way
            want_stored = max(0.0, room) / eta_one_way
            if want_stored <= 1e-9:
                continue
            assign_discharge(d, want_stored, export_stored)

    # --- build plan slots ----------------------------------------------------
    warnings = ["export_spread_unreachable"] if export_spread_unreachable else []
    plan = Plan(warnings=list(warnings))
    levels, _ = timeline()

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
            if hours[i] > 0:
                charge_power = min(
                    delivered / hours[i] * 1000.0,
                    battery.max_discharge_power_w,
                )
        elif discharge_stored[i] > 1e-9:
            action = ACTION_AUTO
        elif demand[i] > 1e-9 and levels[i] > 1e-9:
            # The battery holds energy and the house is drawing, so the
            # inverter would discharge here - but the assignment did not spend
            # this slot's energy, which means it is reserved for a pricier slot
            # later (or the price here is negative, where importing pays).
            # Blocking is what makes the rest of the plan come true; without it
            # the cost model and the inverter disagree. Surplus slots (no
            # demand) stay in auto mode: the inverter charges from PV and won't
            # discharge anyway.
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
                pv_kwh=slot.pv_kwh,
                power_w=round(charge_power, 1),
                discharge_kwh=round(delivered, 3),
                soc_forecast=round(battery.min_soc + levels[i] / capacity * 100.0, 1),
            )
        )

    # --- savings: planned grid cost vs. doing nothing ------------------------
    # The baseline is what the inverter does on its own - plain self
    # consumption from whatever the battery happens to hold - not "buy
    # everything from the grid". Otherwise an all-auto plan that changes
    # nothing would still report a fat saving.
    baseline_delivered, baseline_levels = _simulate_self_consumption(
        n, demand, discharge_cap, pv_surplus_stored, e_init, e_max, eta_one_way
    )
    cost_plan = 0.0
    cost_baseline = 0.0
    for i in range(n):
        delivered_to_load = discharge_stored[i] * eta_one_way
        cost_plan += (demand[i] - delivered_to_load) * prices[i]
        cost_plan += charge_stored[i] / eta_one_way * prices[i]
        if export_stored[i] > 1e-9:
            cost_plan -= export_stored[i] * eta_one_way * _export_sell_price(i)
        cost_baseline += (demand[i] - baseline_delivered[i]) * prices[i]
    savings = cost_baseline - cost_plan

    if savings < -1e-9:
        # Doing nothing is always available and is the very baseline the number
        # is measured against, so a plan that loses money is never worth
        # running. Should not happen - but a wrong plan costs the user money,
        # while a needless fallback only costs an optimization.
        warnings.append("plan_worse_than_baseline")
        return _auto_plan(slots, prices, baseline_delivered, baseline_levels, battery, warnings)

    plan.estimated_savings_eur = round(savings, 2)
    return plan


def _auto_plan(
    slots: list[InputSlot],
    prices: list[float],
    delivered: list[float],
    levels: list[float],
    battery: BatteryState,
    warnings: list[str],
) -> Plan:
    """The do-nothing plan: leave the inverter in auto mode all the way."""
    plan = Plan(warnings=list(warnings))
    for i, slot in enumerate(slots):
        plan.battery_discharge_kwh += delivered[i]
        plan.slots.append(
            PlanSlot(
                start=slot.price_slot.start,
                end=slot.price_slot.end,
                action=ACTION_AUTO,
                price=prices[i],
                net_demand_kwh=slot.net_demand_kwh,
                pv_kwh=slot.pv_kwh,
                discharge_kwh=round(delivered[i], 3),
                soc_forecast=round(battery.min_soc + levels[i] / battery.capacity_kwh * 100.0, 1),
            )
        )
    return plan


def _simulate_self_consumption(
    n: int,
    demand: list[float],
    discharge_cap: list[float],
    pv_surplus_stored: list[float],
    e_init: float,
    e_max: float,
    eta_one_way: float,
) -> tuple[list[float], list[float]]:
    """Energy the battery would deliver to the load without any planning, and
    the stored energy left at the end of each slot.

    The do-nothing reference: discharge whatever is stored as soon as there
    is demand, recharge from PV surplus, never touch the grid.
    """
    delivered = [0.0] * n
    levels = [0.0] * n
    energy = e_init
    for i in range(n):
        want_stored = min(demand[i], discharge_cap[i]) / eta_one_way
        used = min(want_stored, energy)
        delivered[i] = used * eta_one_way
        energy = min(energy - used + pv_surplus_stored[i], e_max)
        levels[i] = energy
    return delivered, levels
