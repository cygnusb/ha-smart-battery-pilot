"""Consistency checks: does what the plan counts on really happen?

The plan rests on assumptions nothing else verifies: a script that returned
really changed the battery's mode, an auto slot really charges from surplus,
the SOC sensor is alive. When one fails nothing errors, the plan and the
battery just drift apart. These checks compare the two and name the problem.
They only ever report; no plan or script call depends on them.

No Home Assistant imports, like the optimizer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from .const import ACTION_AUTO, ACTION_CHARGE, ACTION_EXPORT, ACTION_IDLE

_LOGGER = logging.getLogger(__name__)

CHECK_SCRIPT_FAILING = "script_failing"
CHECK_PLAN_INVALID = "plan_invalid"
CHECK_EXTERNAL_STUCK = "external_control_stuck"
CHECK_SOC_FROZEN = "soc_frozen"
CHECK_PV_REMAINING = "pv_today_is_remaining"
CHECK_CAPACITY = "capacity_mismatch"
CHECK_CHARGE = "charge_ineffective"
CHECK_IDLE = "idle_discharges"
CHECK_EXPORT = "export_ineffective"
CHECK_COVER = "auto_not_covering"
CHECK_CAPTURE = "auto_not_capturing"

ALL_CHECKS = (
    CHECK_SCRIPT_FAILING,
    CHECK_PLAN_INVALID,
    CHECK_EXTERNAL_STUCK,
    CHECK_SOC_FROZEN,
    CHECK_PV_REMAINING,
    CHECK_CAPACITY,
    CHECK_CHARGE,
    CHECK_IDLE,
    CHECK_EXPORT,
    CHECK_COVER,
    CHECK_CAPTURE,
)
# These say something about the sensors or the plan, so they hold in dry-run too.
_WITHOUT_STEERING = frozenset(
    {CHECK_PLAN_INVALID, CHECK_SOC_FROZEN, CHECK_PV_REMAINING, CHECK_CAPACITY}
)

STRIKES = 3  # days (or capacity windows) in a row before a check fires
SCRIPT_FAILURES = 3
PLAN_INVALID_AFTER = timedelta(hours=12)
EXTERNAL_STUCK_AFTER = timedelta(hours=24)
# A longer gap is a restart or an outage: what moved in it has no known mode.
MAX_SEGMENT = timedelta(hours=2)
# The BMS tapers near the top, and the battery rightly stops near the floor.
SOC_MARGIN = 5.0

MIN_AUTO_SHARE = 0.10  # of capacity at stake before an auto day says anything
MIN_FORCED_SHARE = 0.05  # the same for charge and export slots
AUTO_BAD = 0.20
AUTO_GOOD = 0.50
FORCED_BAD = 0.50
FORCED_GOOD = 0.75
IDLE_MIN_HOURS = 1.0
IDLE_LEAK_BAD = 0.05  # of capacity
IDLE_LEAK_GOOD = 0.02

FROZEN_MIN_TIME = timedelta(hours=2)
FROZEN_MIN_MOVED = 0.10  # of capacity

PV_MIN_HIGH_KWH = 2.0
PV_LAST_BAD = 0.15
PV_LAST_GOOD = 0.50

# Middle of the SOC range only: outside it the BMS tapers and recalibrates.
CAPACITY_SOC_LOW = 15.0
CAPACITY_SOC_HIGH = 90.0
CAPACITY_WINDOW_SOC = 40.0
CAPACITY_BAD = 0.25
CAPACITY_GOOD = 0.15


def issue_id(entry_id: str, check: str) -> str:
    """Issue registry id of one check on one config entry."""
    return f"{check}_{entry_id}"


@dataclass(frozen=True, slots=True)
class Observation:
    """The battery and what was asked of it at one instant."""

    at: datetime
    day: str  # local date, where a day's verdict closes
    steering: bool  # the pilot really drives the battery
    action: str | None  # mode really applied
    power_w: float = 0.0  # what the current slot asked the script for
    soc: float | None = None
    charge_kwh: float | None = None
    discharge_kwh: float | None = None
    pv_w: float | None = None
    load_w: float | None = None  # the house without a car
    no_pv_expected: bool = False  # no PV forecast for this slot: dark
    ev_active: bool = False
    external: bool = False
    pv_today_kwh: float | None = None  # only set in daylight, when the plan uses it
    capacity_kwh: float = 0.0
    floor_soc: float = 0.0  # min SOC, or the reserve above it
    max_soc: float = 100.0
    eta_one_way: float = 1.0
    max_charge_w: float = 0.0
    max_discharge_w: float = 0.0
    charge_factor: float = 1.0


def _fresh_tally() -> dict[str, float]:
    return {
        "surplus": 0.0,
        "taken": 0.0,
        "deficit": 0.0,
        "given": 0.0,
        "charge_expected": 0.0,
        "charge_taken": 0.0,
        "export_expected": 0.0,
        "export_given": 0.0,
        "idle_hours": 0.0,
        "idle_leak": 0.0,
    }


def _kwh(value: float) -> str:
    return f"{value:.1f}"


def _delta(after: float | None, before: float | None) -> float | None:
    """What a cumulative meter moved; a reset or a gap tells nothing."""
    if after is None or before is None or after < before:
        return None
    return after - before


class HealthMonitor:
    """Tallies each day's expectations against what the meters saw."""

    def __init__(self) -> None:
        self._prev: Observation | None = None
        self._capacity = 0.0
        self._active: dict[str, dict[str, str]] = {}
        self._strikes: dict[str, int] = {}
        self._day: str | None = None
        self._tally = _fresh_tally()
        self._pv_high: float | None = None
        self._pv_last: float | None = None
        self._cap_stored = 0.0
        self._cap_soc = 0.0
        self._frozen_soc: float | None = None
        self._frozen_since: datetime | None = None
        self._frozen_moved = 0.0
        self._invalid_since: datetime | None = None
        self._external_since: datetime | None = None
        self._script_failures: dict[str, int] = {}
        self._script_entities: dict[str, str] = {}

    # --- what the coordinator reads ----------------------------------------------

    def issues(self, *, enabled: bool, dry_run: bool) -> dict[str, dict[str, str]]:
        """Checks to show, with their translation placeholders."""
        if not enabled:
            return {}
        return {
            check: placeholders
            for check, placeholders in self._active.items()
            if not dry_run or check in _WITHOUT_STEERING
        }

    # --- verdicts --------------------------------------------------------------

    def _raise(self, check: str, placeholders: dict[str, str]) -> None:
        if check not in self._active:
            _LOGGER.warning("Consistency check '%s' failed: %s", check, placeholders)
        self._active[check] = placeholders

    def _clear(self, check: str) -> None:
        if self._active.pop(check, None) is not None:
            _LOGGER.info("Consistency check '%s' passes again", check)

    def _strike(self, check: str, placeholders: dict[str, str]) -> None:
        self._strikes[check] = self._strikes.get(check, 0) + 1
        _LOGGER.debug("Consistency check '%s': strike %d", check, self._strikes[check])
        if self._strikes[check] >= STRIKES:
            self._raise(check, placeholders)

    def _pass(self, check: str) -> None:
        self._strikes.pop(check, None)
        self._clear(check)

    def _judge_share(
        self, check: str, expected: float, actual: float, minimum: float, bad: float, good: float
    ) -> None:
        """A day where `actual` should have reached `expected`; too little at
        stake, or a share between the bounds, leaves the series as it is."""
        if expected < minimum or minimum <= 0:
            return
        share = actual / expected
        if share < bad:
            self._strike(check, {"expected_kwh": _kwh(expected), "actual_kwh": _kwh(actual)})
        elif share >= good:
            self._pass(check)

    def _close_day(self) -> None:
        tally = self._tally
        capacity = self._capacity
        auto_min = MIN_AUTO_SHARE * capacity
        forced_min = MIN_FORCED_SHARE * capacity
        self._judge_share(
            CHECK_CAPTURE, tally["surplus"], tally["taken"], auto_min, AUTO_BAD, AUTO_GOOD
        )
        self._judge_share(
            CHECK_COVER, tally["deficit"], tally["given"], auto_min, AUTO_BAD, AUTO_GOOD
        )
        self._judge_share(
            CHECK_CHARGE,
            tally["charge_expected"],
            tally["charge_taken"],
            forced_min,
            FORCED_BAD,
            FORCED_GOOD,
        )
        self._judge_share(
            CHECK_EXPORT,
            tally["export_expected"],
            tally["export_given"],
            forced_min,
            FORCED_BAD,
            FORCED_GOOD,
        )
        if tally["idle_hours"] >= IDLE_MIN_HOURS and capacity > 0:
            if tally["idle_leak"] >= IDLE_LEAK_BAD * capacity:
                self._strike(CHECK_IDLE, {"actual_kwh": _kwh(tally["idle_leak"])})
            elif tally["idle_leak"] <= IDLE_LEAK_GOOD * capacity:
                self._pass(CHECK_IDLE)
        if self._pv_high is not None and self._pv_high >= PV_MIN_HIGH_KWH:
            last = self._pv_last or 0.0
            if last < PV_LAST_BAD * self._pv_high:
                self._strike(
                    CHECK_PV_REMAINING, {"high_kwh": _kwh(self._pv_high), "last_kwh": _kwh(last)}
                )
            elif last >= PV_LAST_GOOD * self._pv_high:
                self._pass(CHECK_PV_REMAINING)
        self._tally = _fresh_tally()
        self._pv_high = self._pv_last = None

    # --- inputs --------------------------------------------------------------------

    def plan_result(self, now: datetime, error: str | None) -> None:
        """Called after every refresh with why there is no plan, or None."""
        if error is None:
            self._invalid_since = None
            self._clear(CHECK_PLAN_INVALID)
            return
        if self._invalid_since is None:
            self._invalid_since = now
        if now - self._invalid_since >= PLAN_INVALID_AFTER:
            self._raise(CHECK_PLAN_INVALID, {"error": error})

    def script_result(self, action: str, entity_id: str | None, success: bool) -> None:
        """Failures count per action: a failed charge falls back to a working
        auto script, which must not make the charge script look fine."""
        if success:
            self._script_failures.pop(action, None)
        else:
            self._script_failures[action] = self._script_failures.get(action, 0) + 1
            self._script_entities[action] = entity_id or "-"
        failing = next(
            (a for a, count in self._script_failures.items() if count >= SCRIPT_FAILURES), None
        )
        if failing is None:
            self._clear(CHECK_SCRIPT_FAILING)
        else:
            self._raise(
                CHECK_SCRIPT_FAILING,
                {"action": failing, "script": self._script_entities.get(failing, "-")},
            )

    def observe(self, obs: Observation) -> None:
        """Take one sample; settles the span since the previous one."""
        self._capacity = obs.capacity_kwh
        if self._day is not None and obs.day != self._day:
            self._close_day()
        self._day = obs.day
        if obs.pv_today_kwh is not None:
            self._pv_high = max(self._pv_high or 0.0, obs.pv_today_kwh)
            self._pv_last = obs.pv_today_kwh
        self._check_external(obs)
        prev, self._prev = self._prev, obs
        if prev is not None and timedelta(0) < obs.at - prev.at <= MAX_SEGMENT:
            self._settle(prev, obs)
        self._check_frozen(prev, obs)

    def _check_external(self, obs: Observation) -> None:
        if not obs.external:
            self._external_since = None
            self._clear(CHECK_EXTERNAL_STUCK)
            return
        if self._external_since is None:
            self._external_since = obs.at
        if obs.at - self._external_since >= EXTERNAL_STUCK_AFTER:
            self._raise(CHECK_EXTERNAL_STUCK, {})

    def _check_frozen(self, prev: Observation | None, obs: Observation) -> None:
        """A SOC that stands still means nothing unless energy demonstrably moved."""
        if obs.soc is None:
            return
        if obs.soc != self._frozen_soc:
            self._frozen_soc = obs.soc
            self._frozen_since = obs.at
            self._frozen_moved = 0.0
            self._clear(CHECK_SOC_FROZEN)
            return
        if prev is None or not timedelta(0) < obs.at - prev.at <= MAX_SEGMENT:
            return
        charged = _delta(obs.charge_kwh, prev.charge_kwh)
        discharged = _delta(obs.discharge_kwh, prev.discharge_kwh)
        if charged is not None or discharged is not None:
            self._frozen_moved += (charged or 0.0) + (discharged or 0.0)
        elif prev.steering and prev.action in (ACTION_CHARGE, ACTION_EXPORT):
            hours = (obs.at - prev.at).total_seconds() / 3600.0
            self._frozen_moved += prev.power_w / 1000.0 * hours
        since = self._frozen_since
        if (
            since is not None
            and obs.capacity_kwh > 0
            and obs.at - since >= FROZEN_MIN_TIME
            and self._frozen_moved >= FROZEN_MIN_MOVED * obs.capacity_kwh
        ):
            self._raise(
                CHECK_SOC_FROZEN,
                {"soc": f"{obs.soc:g}", "moved_kwh": _kwh(self._frozen_moved)},
            )

    def _settle(self, a: Observation, b: Observation) -> None:
        hours = (b.at - a.at).total_seconds() / 3600.0
        charged = _delta(b.charge_kwh, a.charge_kwh)
        discharged = _delta(b.discharge_kwh, a.discharge_kwh)
        self._track_capacity(a, b, charged, discharged)
        if not (a.steering and b.steering) or a.external or b.external:
            return
        if a.ev_active or b.ev_active or a.soc is None or b.soc is None:
            return
        soc_kwh = (b.soc - a.soc) / 100.0 * b.capacity_kwh
        taken = charged if charged is not None else max(0.0, soc_kwh)
        given = discharged if discharged is not None else max(0.0, -soc_kwh)
        room_above = max(a.soc, b.soc) < b.max_soc - SOC_MARGIN
        room_below = min(a.soc, b.soc) > b.floor_soc + SOC_MARGIN
        tally = self._tally

        if a.action == ACTION_CHARGE:
            limit = b.max_charge_w * b.charge_factor
            power = min(a.power_w, limit) if limit > 0 else a.power_w
            if room_above and power > 0:
                tally["charge_expected"] += power / 1000.0 * hours * b.eta_one_way
                tally["charge_taken"] += taken
        elif a.action == ACTION_EXPORT:
            if room_below and a.power_w > 0:
                tally["export_expected"] += a.power_w / 1000.0 * hours
                tally["export_given"] += given
        elif a.action == ACTION_IDLE:
            # Net: with PV around, some inverters trade small amounts in and out
            # around a held SOC. That moves both meters but spends nothing.
            tally["idle_hours"] += hours
            tally["idle_leak"] += max(0.0, given - taken)
        elif a.action == ACTION_AUTO:
            self._settle_auto(a, b, hours, taken, given, room_above, room_below)

    def _settle_auto(
        self,
        a: Observation,
        b: Observation,
        hours: float,
        taken: float,
        given: float,
        room_above: bool,
        room_below: bool,
    ) -> None:
        if a.load_w is None or b.load_w is None:
            return
        tally = self._tally
        if a.pv_w is not None and b.pv_w is not None:
            # A battery can't take more than its limit, and less when cold.
            surplus = (max(0.0, a.pv_w - a.load_w) + max(0.0, b.pv_w - b.load_w)) / 2.0
            surplus = min(surplus, b.max_charge_w * b.charge_factor)
            if room_above and surplus > 0:
                tally["surplus"] += surplus / 1000.0 * hours
                tally["taken"] += taken
            deficit = (max(0.0, a.load_w - a.pv_w) + max(0.0, b.load_w - b.pv_w)) / 2.0
        elif a.no_pv_expected and b.no_pv_expected:
            deficit = (a.load_w + b.load_w) / 2.0
        else:
            return
        deficit = min(deficit, b.max_discharge_w)
        if room_below and deficit > 0:
            tally["deficit"] += deficit / 1000.0 * hours
            tally["given"] += given

    def _track_capacity(
        self, a: Observation, b: Observation, charged: float | None, discharged: float | None
    ) -> None:
        """Energy charged per SOC point, in spans where nothing else moved."""
        if (
            charged is None
            or discharged is None
            or discharged > 0.01
            or charged <= 0
            or a.soc is None
            or b.soc is None
            or a.soc < CAPACITY_SOC_LOW
            or b.soc > CAPACITY_SOC_HIGH
            or b.capacity_kwh <= 0
        ):
            return
        self._cap_stored += charged * b.eta_one_way
        self._cap_soc += b.soc - a.soc
        if self._cap_soc < CAPACITY_WINDOW_SOC:
            return
        implied = self._cap_stored / (self._cap_soc / 100.0)
        off = abs(implied / b.capacity_kwh - 1.0)
        if off > CAPACITY_BAD:
            self._strike(
                CHECK_CAPACITY,
                {"implied_kwh": _kwh(implied), "configured_kwh": _kwh(b.capacity_kwh)},
            )
        elif off < CAPACITY_GOOD:
            self._pass(CHECK_CAPACITY)
        self._cap_stored = self._cap_soc = 0.0

    # --- persistence ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Everything but the last sample, which a restart makes meaningless."""
        return {
            "active": self._active,
            "strikes": self._strikes,
            "day": self._day,
            "tally": {key: round(value, 3) for key, value in self._tally.items()},
            "capacity_kwh": self._capacity,
            "pv_high": self._pv_high,
            "pv_last": self._pv_last,
            "cap_stored": round(self._cap_stored, 3),
            "cap_soc": round(self._cap_soc, 2),
            "invalid_since": self._invalid_since.isoformat() if self._invalid_since else None,
            "external_since": self._external_since.isoformat() if self._external_since else None,
            "script_failures": self._script_failures,
            "script_entities": self._script_entities,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HealthMonitor:
        monitor = cls()
        monitor._active = {
            str(check): {str(k): str(v) for k, v in placeholders.items()}
            for check, placeholders in data.get("active", {}).items()
            if check in ALL_CHECKS
        }
        monitor._strikes = {
            str(check): int(count)
            for check, count in data.get("strikes", {}).items()
            if check in ALL_CHECKS
        }
        monitor._day = data.get("day")
        monitor._tally = {
            **_fresh_tally(),
            **{
                key: float(value)
                for key, value in data.get("tally", {}).items()
                if key in _fresh_tally()
            },
        }
        monitor._capacity = float(data.get("capacity_kwh", 0.0))
        for name in ("pv_high", "pv_last"):
            if data.get(name) is not None:
                setattr(monitor, f"_{name}", float(data[name]))
        monitor._cap_stored = float(data.get("cap_stored", 0.0))
        monitor._cap_soc = float(data.get("cap_soc", 0.0))
        for name in ("invalid_since", "external_since"):
            if data.get(name):
                setattr(monitor, f"_{name}", datetime.fromisoformat(data[name]))
        monitor._script_failures = {
            str(k): int(v) for k, v in data.get("script_failures", {}).items()
        }
        monitor._script_entities = {
            str(k): str(v) for k, v in data.get("script_entities", {}).items()
        }
        return monitor
