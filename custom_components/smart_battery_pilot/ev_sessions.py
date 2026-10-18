"""Expected EV charging sessions, derived from evcc's state.

Pure functions, no Home Assistant dependencies. evcc already decides when a
car charges; this module only reads that decision back so the planner can
leave room for it:

* ``plan``       - evcc's charge plan, whose slots are taken as they are
* ``now``        - fast charging from now until the car is full
* ``smart_cost`` - charging wherever evcc's price is at or below its limit
* ``charging``   - a running session nothing else explains (e.g. PV mode),
                   counted for the slot in progress only

Every session is bounded by the energy the car still needs. A session whose
energy cannot be bounded (a car without SOC and without an energy limit)
keeps evcc's plan slots and otherwise counts only while it is running -
anything more would block the house battery all day on a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import math
import statistics
from typing import Any

SOURCE_PLAN = "plan"
SOURCE_NOW = "now"
SOURCE_SMART_COST = "smart_cost"
SOURCE_CHARGING = "charging"

# evcc assumes the same when it turns a SOC gap into a charge duration: what
# the car stores is less than what the wallbox draws.
CHARGE_EFFICIENCY = 0.9
# A running session with nothing to bound it is counted this far ahead - the
# slot in progress, whatever the price resolution.
LIVE_WINDOW = timedelta(minutes=15)
NOMINAL_VOLTAGE = 230.0
MIN_SESSION_KWH = 0.05
# evcc's limit is only moved onto the planner's prices when both series are
# known for this many slots and stay this close to a constant offset.
MIN_LIMIT_OVERLAP = 4
LIMIT_OFFSET_TOLERANCE = 0.02  # EUR/kWh; the caller scales it to its currency


@dataclass(frozen=True, slots=True)
class EvWindow:
    """Energy the car is expected to draw between start and end (AC side)."""

    start: datetime
    end: datetime
    kwh: float
    source: str

    @property
    def hours(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)


@dataclass(slots=True)
class EvSession:
    loadpoint: int  # evcc's loadpoint id (1-based)
    source: str  # the dominant source of its windows
    windows: list[EvWindow]
    energy_kwh: float
    bounded: bool
    budget_kwh: float | None


@dataclass(slots=True)
class EvForecast:
    sessions: list[EvSession] = field(default_factory=list)
    # evcc's cheap-price limit in evcc's own units, when a session uses it.
    price_limit: float | None = None
    # The rate series evcc compares that limit with.
    rates: list[tuple[datetime, datetime, float]] = field(default_factory=list)
    battery_discharge_control: bool | None = None
    loadpoints: list[dict[str, Any]] = field(default_factory=list)  # diagnostics

    @property
    def windows(self) -> list[EvWindow]:
        return [w for s in self.sessions for w in s.windows]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _moment(value: Any) -> datetime | None:
    """A unix timestamp or an ISO string as an aware datetime."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # evcc's "no plan" placeholder.
        if moment.year <= 1:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return None


def parse_rates(raw: Any) -> list[tuple[datetime, datetime, float]]:
    """evcc rate series in either shape it uses.

    The state publisher compresses series into ``[start_unix, end_unix, value]``
    triples, the plan endpoint marshals ``{"start", "end", "value"}`` objects.
    Anything else is skipped.
    """
    rates = []
    for item in raw or []:
        if isinstance(item, dict):
            start, end, value = item.get("start"), item.get("end"), item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            start, end, value = item[0], item[1], item[2]
        else:
            continue
        start_dt, end_dt, number = _moment(start), _moment(end), _number(value)
        if start_dt and end_dt and end_dt > start_dt:
            rates.append((start_dt, end_dt, number if number is not None else 0.0))
    return sorted(rates, key=lambda r: r[0])


def _budget_kwh(lp: dict[str, Any], vehicles: dict[str, Any]) -> float | None:
    """Energy the car still draws from the wallbox, or None if unknown."""
    known: list[float] = []
    vehicle = vehicles.get(lp.get("vehicleName") or "") or {}
    capacity = _number(vehicle.get("capacity"))
    soc = _number(lp.get("vehicleSoc"))
    target = _number(lp.get("effectiveLimitSoc")) or 100.0
    if capacity and capacity > 0 and soc is not None and soc > 0:
        known.append(max(0.0, (target - soc) / 100.0 * capacity / CHARGE_EFFICIENCY))
    limit_energy = _number(lp.get("limitEnergy"))
    if limit_energy and limit_energy > 0:
        charged = (_number(lp.get("chargedEnergy")) or 0.0) / 1000.0
        known.append(max(0.0, limit_energy - charged))
    return min(known) if known else None


def _power_w(lp: dict[str, Any], plan_power: float | None) -> float:
    charge_power = _number(lp.get("chargePower")) or 0.0
    if lp.get("charging") and charge_power > 0:
        return charge_power
    if plan_power and plan_power > 0:
        return plan_power
    current = _number(lp.get("effectiveMaxCurrent")) or 16.0
    phases = _number(lp.get("phasesActive")) or _number(lp.get("phasesConfigured")) or 3.0
    return current * phases * NOMINAL_VOLTAGE


def _window(start: datetime, end: datetime, power_w: float, source: str) -> EvWindow:
    hours = max(0.0, (end - start).total_seconds() / 3600.0)
    return EvWindow(start, end, hours * power_w / 1000.0, source)


def _consume(windows: list[EvWindow], budget: float) -> list[EvWindow]:
    """Take windows in time order until the car's energy need is met."""
    taken = []
    remaining = budget
    for w in windows:
        if remaining <= 1e-6:
            break
        if w.kwh <= remaining:
            taken.append(w)
            remaining -= w.kwh
            continue
        share = remaining / w.kwh
        end = w.start + (w.end - w.start) * share
        taken.append(EvWindow(w.start, end, remaining, w.source))
        remaining = 0.0
    return taken


def derive(
    state: dict[str, Any],
    plans: dict[int, dict[str, Any]],
    now: datetime,
    *,
    trim: bool = True,
) -> EvForecast:
    """Expected sessions per connected loadpoint.

    `plans` holds what ``/api/loadpoints/{id}/plan`` returned, keyed by
    loadpoint id: the plan's slots (``plan``) and the charge power evcc sized
    it with (``power``). The plan in the state is the fallback for the
    slots. With `trim` False a
    window already under way keeps its real start, so the slot in progress
    counts in full - stable enough to compare one poll with the next.
    """
    forecast = EvForecast()
    if not isinstance(state, dict):
        return forecast
    vehicles = state.get("vehicles") if isinstance(state.get("vehicles"), dict) else {}
    tariffs = state.get("forecast") if isinstance(state.get("forecast"), dict) else {}
    rates = parse_rates(tariffs.get("planner")) or parse_rates(tariffs.get("grid"))
    control = state.get("batteryDischargeControl")
    forecast.battery_discharge_control = control if isinstance(control, bool) else None
    # The smart cost limit is a CO2 intensity with a CO2 tariff, not a price.
    price_limits = str(state.get("smartCostType") or "").lower() != "co2"

    for index, lp in enumerate(state.get("loadpoints") or []):
        if not isinstance(lp, dict):
            continue
        lp_id = index + 1
        mode = str(lp.get("mode") or "")
        budget = _budget_kwh(lp, vehicles)
        info: dict[str, Any] = {
            "id": lp_id,
            "mode": mode,
            "connected": bool(lp.get("connected")),
            "charging": bool(lp.get("charging")),
            "vehicle_known": bool(vehicles.get(lp.get("vehicleName") or "")),
            "vehicle_soc": _number(lp.get("vehicleSoc")),
            "effective_limit_soc": _number(lp.get("effectiveLimitSoc")),
            "plan_time": lp.get("effectivePlanTime"),
            "smart_cost_limit": _number(lp.get("smartCostLimit")),
            "budget_kwh": round(budget, 2) if budget is not None else None,
            "derived_kwh": 0.0,
        }
        forecast.loadpoints.append(info)
        if not lp.get("connected") or mode == "off":
            continue
        if budget is not None and budget < MIN_SESSION_KWH:
            continue

        endpoint = plans.get(lp_id) or {}
        power = _power_w(lp, endpoint.get("power"))
        windows: list[EvWindow] = []
        if endpoint.get("plan"):
            plan_rates = parse_rates(endpoint["plan"])
        elif lp.get("effectivePlanTime"):
            plan_rates = parse_rates(lp.get("plan"))
        else:
            plan_rates = []
        windows += [_window(s, e, power, SOURCE_PLAN) for s, e, _ in plan_rates]
        if mode == "now":
            hours = (budget if budget is not None else 0.0) / (power / 1000.0)
            if hours > 0:
                windows.append(_window(now, now + timedelta(hours=hours), power, SOURCE_NOW))
        limit = info["smart_cost_limit"] if price_limits else None
        if mode in ("smart", "pv", "minpv") and limit is not None and budget is not None:
            cheap = [(s, e) for s, e, value in rates if value <= limit and e > now]
            if cheap:
                forecast.price_limit = limit
            windows += [_window(s, e, power, SOURCE_SMART_COST) for s, e in cheap]

        # Past windows are dropped, the one under way counts from now.
        live: list[EvWindow] = []
        for w in sorted(windows, key=lambda w: w.start):
            if w.end <= now:
                continue
            if trim and w.start < now:
                w = _window(now, w.end, w.kwh / w.hours * 1000.0, w.source)
            live.append(w)

        # Unbounded, only evcc's own plan is more than a guess.
        bounded = budget is not None
        live = _consume(live, budget) if bounded else [w for w in live if w.source == SOURCE_PLAN]

        covers_now = any(w.start <= now < w.end for w in live)
        if lp.get("charging") and not covers_now:
            charge_power = _number(lp.get("chargePower")) or power
            start = now if trim else now - LIVE_WINDOW / 2
            live.append(_window(start, now + LIVE_WINDOW, charge_power, SOURCE_CHARGING))

        energy = sum(w.kwh for w in live)
        if energy < MIN_SESSION_KWH:
            continue
        info["derived_kwh"] = round(energy, 2)
        by_source: dict[str, float] = {}
        for w in live:
            by_source[w.source] = by_source.get(w.source, 0.0) + w.kwh
        forecast.sessions.append(
            EvSession(
                loadpoint=lp_id,
                source=max(by_source, key=by_source.get),
                windows=sorted(live, key=lambda w: w.start),
                energy_kwh=energy,
                bounded=bounded,
                budget_kwh=budget,
            )
        )
    forecast.rates = rates
    return forecast


def _overlap_hours(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    seconds = (min(a1, b1) - max(a0, b0)).total_seconds()
    return max(0.0, seconds / 3600.0)


def per_slot(
    windows: list[EvWindow], bounds: list[tuple[datetime, datetime]]
) -> list[tuple[float, str | None]]:
    """(EV kWh, dominant source) for each planner slot, by time overlap.

    Overlap weighting fits evcc's 15-minute slots onto hourly price slots
    and the other way round, and the trimmed partial slots evcc plans.
    """
    out = []
    for start, end in bounds:
        shares: dict[str, float] = {}
        for w in windows:
            hours = w.hours
            if hours <= 0:
                continue
            overlap = _overlap_hours(start, end, w.start, w.end)
            if overlap > 0:
                shares[w.source] = shares.get(w.source, 0.0) + w.kwh * overlap / hours
        total = sum(shares.values())
        out.append((total, max(shares, key=shares.get) if shares else None))
    return out


def limit_on_price_basis(
    limit: float | None,
    rates: list[tuple[datetime, datetime, float]],
    slots: list[tuple[datetime, datetime, float]],
    tolerance: float = LIMIT_OFFSET_TOLERANCE,
) -> float | None:
    """evcc's price limit moved onto the planner's own price basis, or None.

    evcc and the planner may price the same hour differently (fees, VAT, a
    different provider). When the two series are a constant offset apart,
    the offset is added to the limit, so a line drawn at the result
    separates the slots evcc picks on the planner's price axis. When they
    are not (a percentage VAT on one side, another tariff), no single line
    does that, and None says so.
    """
    if limit is None:
        return None
    gaps = []
    for start, end, price in slots:
        middle = start + (end - start) / 2
        for r_start, r_end, value in rates:
            if r_start <= middle < r_end:
                gaps.append(price - value)
                break
    if len(gaps) < MIN_LIMIT_OVERLAP or max(gaps) - min(gaps) > tolerance:
        return None
    return round(limit + statistics.median(gaps), 4)
