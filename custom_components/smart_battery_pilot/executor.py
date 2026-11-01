"""Executes the plan: calls the user-configured scripts at slot boundaries."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
from typing import Any, NamedTuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    CONF_EXTERNAL_CONTROL_ENTITY,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_EXPORT,
    CONF_SCRIPT_IDLE,
    CONF_SOC_ENTITY,
    EV_POLICY_BLOCK,
    SCRIPT_CALL_TIMEOUT_SECONDS,
)
from .coordinator import ChargeReading, SBPCoordinator
from .optimizer import PlanSlot

_LOGGER = logging.getLogger(__name__)

# The discharge block releases this many SOC points above the reserve, so a
# battery hovering at the reserve does not flip the inverter every minute.
RESERVE_RELEASE_MARGIN = 2.0

# The EV block engages above the threshold and releases below this share of
# it: evcc and most wallboxes ramp and pause between phases, and every flip is
# a script call and an inverter write.
EV_RELEASE_SHARE = 0.5

# Decisions kept for the diagnostics dump. Identical consecutive ones collapse
# into a single entry, so this covers many hours of steady operation.
MAX_DECISIONS = 50

REFRESH_ACTIONS = (ACTION_IDLE, ACTION_CHARGE, ACTION_EXPORT)


class _Sent(NamedTuple):
    """The charge or export command the inverter last accepted."""

    slot_start: datetime
    action: str
    power_w: int
    reserve_soc: int


class PlanExecutor:
    """Applies the planned action whenever a slot boundary is crossed."""

    def __init__(self, hass: HomeAssistant, coordinator: SBPCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._unsub_timer = None
        self._unsub_refresh_timer = None
        self._unsub_coordinator = None
        # Serialises the three triggers (coordinator update, slot boundary,
        # switches). Script calls are awaited, so without it two runs can
        # interleave and leave `last_applied` describing a mode the inverter
        # is not in.
        self._lock = asyncio.Lock()
        # An apply already queued but not yet started. The boundary timer and
        # the coordinator listener fire together, and the lock serialises them
        # without deduplicating - two runs would call the charge script twice.
        self._apply_queued = False
        # Set by async_stop. Tasks queued just before the unload are already
        # scheduled and would otherwise re-arm the timer and re-apply a forced
        # mode behind an entry that is gone.
        self._stopped = False
        # Discharge block at the backup reserve currently in force (fallback
        # for inverters without a reserve of their own), and the SOC listener
        # that engages it between coordinator refreshes.
        self._reserve_blocking = False
        self._unsub_soc = None
        # Discharge block while a wallbox draws (EV policy "block"), and the
        # listener on the wallbox power and external-control entities.
        self._ev_blocking = False
        self._unsub_ev = None
        # Another controller had the battery. Whatever it left behind is not
        # what `last_applied` says, so the next run sends its action even when
        # the plan did not change. Not persisted: a restart re-applies anyway
        # as soon as the planned action differs, and the controller re-asserts
        # its own mode on its next cycle.
        self._external_active = False
        self._reassert = False
        # A re-plan mid-slot must not repeat a command: integrations may limit
        # how often a register can be written, and the failure would cancel a
        # charge that is running fine. Not persisted: a restart sends again.
        self._sent: _Sent | None = None
        # Persisted state cannot prove that an expiring inverter mode is still
        # active.  Confirmation is therefore process-local and advances only
        # after a successful blocking script call.
        self._refresh_success_action: str | None = None
        self._refresh_success_at: datetime | None = None
        self._refresh_attempt_at: datetime | None = None
        # What the executor decided and why, newest last. A support report
        # rarely comes with debug logging switched on; the diagnostics dump
        # carries this instead.
        self.decisions: deque[dict[str, Any]] = deque(maxlen=MAX_DECISIONS)

    def _record(
        self,
        outcome: str,
        slot: PlanSlot | None,
        detail: str | None = None,
    ) -> None:
        """Append a decision, or bump the last one if nothing differs."""
        entry = {
            "slot_start": slot.start.isoformat() if slot else None,
            "planned": slot.action if slot else None,
            "power_w": round(slot.power_w) if slot else None,
            "outcome": outcome,
            "detail": detail,
            "last_applied": self._last_applied,
        }
        now = dt_util.now().isoformat()
        if self.decisions:
            last = self.decisions[-1]
            if all(last[key] == value for key, value in entry.items()):
                last["repeats"] += 1
                last["last_at"] = now
                return
        self.decisions.append({"at": now, "last_at": now, "repeats": 1, **entry})

    @property
    def _last_applied(self) -> str | None:
        """Last action really sent to the inverter, restored across restarts."""
        return self.coordinator.last_applied

    async def _remember(self, action: str | None) -> None:
        if self.coordinator.last_applied == action:
            return
        self.coordinator.last_applied = action
        await self.coordinator.async_persist()

    async def async_release_stale_mode(self) -> None:
        """Hand the battery back to auto after a restart that cannot plan.

        Setup aborts before the executor ever runs when the price entity is
        not up yet - very common right after a restart. If the previous run
        left the inverter force-charging, nobody else is going to release it
        while Home Assistant keeps retrying the setup.
        """
        async with self._lock:
            if self._last_applied in (None, ACTION_AUTO):
                return
            _LOGGER.warning(
                "Setup incomplete while the battery is in '%s' mode - restoring auto mode",
                self._last_applied,
            )
            if await self._call_script(ACTION_AUTO, 0.0):
                await self._remember(ACTION_AUTO)
        await self.coordinator.async_persist()

    async def async_start(self) -> None:
        # The master switch stops and restarts the executor, so a start has to
        # clear the stop flag - otherwise switching off once would silence the
        # executor for good and no boundary timer would ever be armed again.
        self._stopped = False
        self._unsub_coordinator = self.coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        soc_entity = self.coordinator.conf(CONF_SOC_ENTITY)
        if self.coordinator.reserve_block_enabled() and soc_entity:
            self._unsub_soc = async_track_state_change_event(
                self.hass, [soc_entity], self._handle_soc_event
            )
        watched = list(self.coordinator.ev_power_entities()) if self._ev_block_enabled() else []
        external = self.coordinator.conf(CONF_EXTERNAL_CONTROL_ENTITY)
        if external:
            watched.append(external)
        if watched:
            self._unsub_ev = async_track_state_change_event(
                self.hass, watched, self._handle_ev_event
            )
        await self.async_apply_current()

    async def async_stop(self, restore_auto: bool = True) -> None:
        """Stop execution; optionally hand the battery back to auto mode."""
        # Close the gate before unsubscribing so a callback already delivered
        # in this event-loop turn cannot queue another device command.
        self._stopped = True
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_refresh_timer:
            self._unsub_refresh_timer()
            self._unsub_refresh_timer = None
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        if self._unsub_soc:
            self._unsub_soc()
            self._unsub_soc = None
        if self._unsub_ev:
            self._unsub_ev()
            self._unsub_ev = None
        async with self._lock:
            self._clear_refresh_state()
            self.coordinator.charge_observation(None)
            # `last_applied` is only set after a real script call, so dry-run
            # never triggers a restore on unload.
            if (
                restore_auto
                and self._last_applied not in (None, ACTION_AUTO)
                and await self._call_script(ACTION_AUTO, 0.0)
            ):
                await self._remember(ACTION_AUTO)
        await self.coordinator.async_persist()

    @callback
    def _queue_apply(self) -> None:
        """Schedule one apply run; collapses simultaneous triggers into one."""
        if self._apply_queued or self._stopped:
            return
        self._apply_queued = True
        self.hass.async_create_task(self.async_apply_current())

    @callback
    def _handle_soc_event(self, event) -> None:
        new_state = event.data.get("new_state")
        try:
            soc = float(new_state.state) if new_state is not None else None
        except (TypeError, ValueError):
            soc = None
        self._handle_soc_change(soc)

    @callback
    def _handle_soc_change(self, soc: float | None) -> None:
        """Re-apply as soon as the discharge block would engage or release.

        Waiting for the next refresh could let the battery run up to 30
        minutes below the reserve.
        """
        # Only an auto slot the pilot really drives can be blocked; in charge,
        # idle or export slots - a refill slot starts below the reserve by
        # definition - SOC ticks must not re-run the script.
        coordinator = self.coordinator
        slot = self.current_slot()
        if (
            slot is None
            or slot.action != ACTION_AUTO
            or not coordinator.enabled
            or coordinator.dry_run
        ):
            return
        if self._block_wanted(soc, self._reserve_blocking) != self._reserve_blocking:
            _LOGGER.debug("SOC %s crosses the reserve block threshold - re-applying", soc)
            self._queue_apply()

    def _block_wanted(self, soc: float | None, blocking: bool) -> bool:
        """Whether the fallback block should be in force at `soc`, given whether
        it is in force now (hysteresis)."""
        if not self.coordinator.reserve_block_enabled():
            return False
        reserve, _, _ = self.coordinator.reserve_state()
        if reserve is None or soc is None:
            return False
        if blocking:
            return soc < reserve + RESERVE_RELEASE_MARGIN
        return soc <= reserve

    def _ev_block_enabled(self) -> bool:
        return self.coordinator.ev_policy() == EV_POLICY_BLOCK and bool(
            self.coordinator.ev_power_entities()
        )

    def _ev_block_wanted(self, power_w: float | None, blocking: bool) -> bool:
        """Whether the EV block should be in force at `power_w` (hysteresis)."""
        if not self._ev_block_enabled() or power_w is None:
            return False
        threshold = self.coordinator.ev_threshold_w()
        if blocking:
            return power_w >= threshold * EV_RELEASE_SHARE
        return power_w > threshold

    @callback
    def _handle_ev_event(self, _event) -> None:
        """Re-apply when another controller takes or hands back the battery,
        or when the EV block would engage or release."""
        coordinator = self.coordinator
        if not coordinator.enabled or coordinator.dry_run:
            return
        external_active = coordinator.external_control_active()
        if external_active != self._external_active:
            _LOGGER.debug("External control changed - re-applying")
            self._external_active = external_active
            if external_active:
                # Invalidate ownership synchronously. A takeover and release
                # can otherwise both arrive before the queued apply runs.
                if self._unsub_refresh_timer:
                    self._unsub_refresh_timer()
                    self._unsub_refresh_timer = None
                self._clear_refresh_state()
                self._reassert = True
            self._queue_apply()
            return
        slot = self.current_slot()
        if slot is None or slot.action != ACTION_AUTO:
            return
        power = coordinator.ev_power_w()
        if self._ev_block_wanted(power, self._ev_blocking) != self._ev_blocking:
            _LOGGER.debug("EV power %s W crosses the block threshold - re-applying", power)
            self._queue_apply()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._queue_apply()

    def _plan_is_live(self) -> bool:
        data = self.coordinator.data
        if not data or not data.valid:
            return False
        return getattr(self.coordinator, "last_update_success", True) is not False

    def current_slot(self) -> PlanSlot | None:
        if not self._plan_is_live():
            return None
        now = dt_util.now()
        return next((slot for slot in self.coordinator.data.plan.slots if slot.covers(now)), None)

    def next_slot(self) -> PlanSlot | None:
        if not self._plan_is_live():
            return None
        now = dt_util.now()
        return next(
            (slot for slot in self.coordinator.data.plan.slots if slot.starts_after(now)),
            None,
        )

    async def async_apply_current(self) -> None:
        """Apply the action of the current slot and arm the next boundary timer."""
        async with self._lock:
            # Cleared under the lock, not when the task starts: a trigger
            # arriving while this run waits for the lock is covered by it, as
            # it has not read the plan yet. One arriving while it waits for a
            # script may bring a newer plan and queues a fresh run.
            self._apply_queued = False
            if self._stopped:
                return
            self._schedule_boundary()
            # Before any script runs: the one that ends a charge slot may take
            # minutes, and the observation must end when the charge did.
            reading = self.coordinator.charge_reading()
            await self._apply_locked()
            self._report_charge(reading)
            # A successful call starts a fresh refresh window. Re-arm after it
            # returns so script runtime cannot shorten that window.
            self._schedule_refresh()

    def _report_charge(self, reading: ChargeReading) -> None:
        """Tell the coordinator whether a charge slot is really running.

        It watches those slots to learn how much a cold battery actually
        takes; only a charge the inverter really received tells it anything.
        """
        last = self.decisions[-1] if self.decisions else None
        charging = (
            last is not None
            and last["planned"] == ACTION_CHARGE
            and last["outcome"] in ("applied", "unchanged", "failed_kept_running")
        )
        self.coordinator.charge_observation(
            float(self._sent.power_w) if charging and self._sent else None, closing=reading
        )

    async def _apply_locked(self) -> None:
        coordinator = self.coordinator
        # Re-established below for an auto slot only; any other outcome ends
        # the block, so a stale flag cannot drive the SOC listener.
        was_blocking, self._reserve_blocking = self._reserve_blocking, False
        was_ev_blocking, self._ev_blocking = self._ev_blocking, False
        slot = self.current_slot()
        if slot is None or not self._plan_is_live():
            self._clear_refresh_state()
            reason = self._why_no_live_plan()
            _LOGGER.debug(
                "No live plan slot (%s); last applied mode '%s'", reason, self._last_applied
            )
            # Invalid plan: fail safe to auto mode once. `last_applied`
            # survives restarts, so a battery left in a forced mode by the
            # previous run is released here too.
            if (
                coordinator.enabled
                and not coordinator.dry_run
                and self._last_applied not in (None, ACTION_AUTO)
            ):
                _LOGGER.warning("Plan invalid (%s) - restoring battery auto mode", reason)
                if await self._call_script(ACTION_AUTO, 0.0):
                    await self._remember(ACTION_AUTO)
            self.coordinator.ev_hold_active = False
            self._record("no_live_plan", None, reason)
            return

        if not coordinator.enabled:
            self._clear_refresh_state()
            _LOGGER.debug("Pilot disabled - leaving slot action '%s' unapplied", slot.action)
            self.coordinator.ev_hold_active = False
            self._record("disabled", slot)
            return

        action = slot.action
        if coordinator.dry_run:
            self._clear_refresh_state()
            if self._last_applied not in (None, ACTION_AUTO):
                _LOGGER.info("Dry-run enabled - restoring battery auto mode")
                if await self._call_script(ACTION_AUTO, 0.0):
                    await self._remember(ACTION_AUTO)
            _LOGGER.info(
                "DRY RUN: would apply action '%s' (%.0f W) for slot %s - %s",
                action,
                slot.power_w,
                slot.start.isoformat(),
                slot.end.isoformat(),
            )
            self.coordinator.ev_hold_active = False
            self._record("dry_run", slot)
            return

        self._external_active = coordinator.external_control_active()
        if self._external_active:
            # Another controller (evcc holding the battery for a car, an
            # automation ...) has the inverter. Writing now would only start a
            # tug of war; the plan resumes when it lets go.
            _LOGGER.debug("External control active - leaving slot action '%s' to it", slot.action)
            self._reassert = True
            self._sent = None
            self._clear_refresh_state()
            self.coordinator.ev_hold_active = True
            self._record("external_control", slot)
            return

        blocked: str | None = None
        if action == ACTION_AUTO:
            self._reserve_blocking = self._block_wanted(coordinator.live_soc(), was_blocking)
            self._ev_blocking = self._ev_block_wanted(coordinator.ev_power_w(), was_ev_blocking)
            if self._reserve_blocking:
                _LOGGER.debug("SOC at the backup reserve - blocking discharge instead of auto")
                action = ACTION_IDLE
                blocked = "reserve_block"
            elif self._ev_blocking:
                _LOGGER.debug("EV charging - blocking discharge instead of auto")
                action = ACTION_IDLE
                blocked = "ev_block"
        # A hold for the car is the EV policy's doing; the pilot ledger must
        # not book it as an arbitrage decision.
        self.coordinator.ev_hold_active = blocked == "ev_block"

        if self._refresh_success_action is not None and self._refresh_success_action != action:
            self._clear_refresh_state()

        wanted = _Sent(slot.start, action, round(slot.power_w), coordinator.reserve_for_scripts())
        reserve_changed = (
            coordinator.last_reserve_sent is not None
            and wanted.reserve_soc != coordinator.last_reserve_sent
        )
        refresh_enabled = self._refresh_enabled(action)
        refresh_due = self._refresh_due(action)
        running = (
            self._sent is not None
            and self._sent.slot_start == slot.start
            and self._sent.action == action
            and action == self._last_applied
        )
        if self._reassert:
            _LOGGER.debug("Re-sending action '%s'", action)
        elif running and wanted == self._sent and not refresh_due:
            _LOGGER.debug(
                "Action '%s' already applied with these values - no script call for slot %s",
                action,
                slot.start.isoformat(),
            )
            self._record(blocked or "unchanged", slot)
            return
        elif (
            action == self._last_applied
            and action not in (ACTION_CHARGE, ACTION_EXPORT)
            and not refresh_enabled
        ):
            if coordinator.reserve_for_scripts() != coordinator.last_reserve_sent:
                # Same mode, new reserve: the inverter only learns it from the
                # script, so the script runs again.
                _LOGGER.debug(
                    "Reserve changed to %s %% - re-sending action '%s'",
                    coordinator.reserve_for_scripts(),
                    action,
                )
                if await self._call_script(action, slot.power_w):
                    self._record("reserve_updated", slot)
                else:
                    self._record("failed", slot)
                return
            _LOGGER.debug(
                "Action '%s' already applied - no script call for slot %s",
                action,
                slot.start.isoformat(),
            )
            self._record(blocked or "unchanged", slot)
            return

        if await self._call_script(action, slot.power_w):
            await self._remember(action)
            self._reassert = False
            self._sent = (
                wanted if action in (ACTION_CHARGE, ACTION_EXPORT) or refresh_enabled else None
            )
            self._record(blocked or ("reserve_updated" if reserve_changed else "applied"), slot)
            return
        if running:
            # The slot's first call worked, so the inverter is still in this
            # mode. Falling back would cancel it over a failed update; the
            # script may have stopped halfway, so the next run sends again.
            _LOGGER.warning(
                "Updating action '%s' failed - keeping it running for slot %s",
                action,
                slot.start.isoformat(),
            )
            self._reassert = True
            self._record("failed_kept_running", slot)
            return
        if action in REFRESH_ACTIONS:
            self._sent = None
            self._clear_refresh_state()
        if action != ACTION_AUTO and await self._call_script(ACTION_AUTO, 0.0):
            await self._remember(ACTION_AUTO)
            self._reassert = False
            self._record("failed_fell_back_to_auto", slot)
            return
        if action == ACTION_AUTO:
            _LOGGER.warning(
                "Restoring battery auto mode failed for slot %s; will retry on a later trigger",
                slot.start.isoformat(),
            )
        self._record("failed", slot)

    def _refresh_enabled(self, action: str) -> bool:
        return action in REFRESH_ACTIONS and self._refresh_interval() > 0

    def _refresh_interval(self) -> int:
        getter = getattr(self.coordinator, "script_refresh_interval", None)
        return int(getter() or 0) if getter else 0

    def _clear_refresh_state(self) -> None:
        """Cancel periodic ownership state when the effective mode changes."""
        self._refresh_success_action = None
        self._refresh_success_at = None
        self._refresh_attempt_at = None
        self._sent = None

    def _refresh_due(self, action: str) -> bool:
        if not self._refresh_enabled(action) or self._refresh_success_action != action:
            return False
        anchor = self._refresh_attempt_at or self._refresh_success_at
        return bool(
            anchor and dt_util.now() >= anchor + timedelta(seconds=self._refresh_interval())
        )

    def _why_no_live_plan(self) -> str:
        """Short reason the executor has no slot to apply, for logs and diagnostics."""
        data = self.coordinator.data
        if data is None:
            return "no_data"
        if not data.valid:
            return data.error or "invalid_plan"
        if getattr(self.coordinator, "last_update_success", True) is False:
            return "last_refresh_failed"
        return "no_slot_covers_now"

    def _schedule_boundary(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        slot = self.current_slot()
        boundary: datetime | None = slot.end if slot else None
        if boundary is None:
            nxt = self.next_slot()
            boundary = nxt.start if nxt else None
        if boundary is None:
            _LOGGER.debug("No upcoming slot boundary - timer not armed")
            return
        _LOGGER.debug("Next slot boundary armed for %s", boundary.isoformat())

        @callback
        def _fire(_now: datetime) -> None:
            # The entities are coordinator-driven and would otherwise keep
            # showing the previous slot until the next 30-minute refresh -
            # long enough to miss a whole 15-minute slot.
            # async_update_listeners re-enters _handle_coordinator_update
            # synchronously, so the queue below already holds this boundary's
            # run; _queue_apply collapses the two into one.
            self.coordinator.async_update_listeners()
            self.coordinator.note_conditions()
            self._queue_apply()

        self._unsub_timer = async_track_point_in_time(self.hass, _fire, boundary)

    def _schedule_refresh(self) -> None:
        """Arm renewal of a successfully applied time-limited mode."""
        if self._unsub_refresh_timer:
            self._unsub_refresh_timer()
            self._unsub_refresh_timer = None
        if (
            self._stopped
            or self._refresh_success_action not in REFRESH_ACTIONS
            or self._refresh_success_at is None
            or not self.coordinator.enabled
            or self.coordinator.dry_run
            or self.coordinator.external_control_active()
        ):
            return
        slot = self.current_slot()
        if (
            slot is None
            or self._sent is None
            or self._sent.slot_start != slot.start
            or self._sent.action != self._refresh_success_action
            or self._last_applied != self._refresh_success_action
        ):
            return
        anchor = self._refresh_attempt_at or self._refresh_success_at
        refresh_at = anchor + timedelta(seconds=self._refresh_interval())
        if refresh_at <= dt_util.now():
            refresh_at = dt_util.now() + timedelta(seconds=self._refresh_interval())

        @callback
        def _fire(_now: datetime) -> None:
            self._unsub_refresh_timer = None
            self._queue_apply()

        _LOGGER.debug(
            "Time-limited action '%s' renewal armed for %s",
            self._refresh_success_action,
            refresh_at.isoformat(),
        )
        self._unsub_refresh_timer = async_track_point_in_time(self.hass, _fire, refresh_at)

    async def _call_script(self, action: str, power_w: float) -> bool:
        if self._refresh_enabled(action):
            self._refresh_attempt_at = dt_util.now()
        success = await self._run_script(action, power_w)
        self.coordinator.note_script_result(action, self._script_entity(action), success)
        if success:
            if self._refresh_enabled(action):
                self._refresh_success_action = action
                self._refresh_success_at = dt_util.now()
                self._refresh_attempt_at = self._refresh_success_at
            else:
                self._refresh_success_action = None
                self._refresh_success_at = None
                self._refresh_attempt_at = None
        return success

    def _script_entity(self, action: str) -> str | None:
        key = {
            ACTION_CHARGE: CONF_SCRIPT_CHARGE,
            ACTION_IDLE: CONF_SCRIPT_IDLE,
            ACTION_AUTO: CONF_SCRIPT_AUTO,
            ACTION_EXPORT: CONF_SCRIPT_EXPORT,
        }.get(action)
        return self.coordinator.conf(key) if key else None

    async def _run_script(self, action: str, power_w: float) -> bool:
        entity_id = self._script_entity(action)
        if not entity_id:
            if action == ACTION_EXPORT:
                _LOGGER.warning("No export script configured - skipping export action")
            else:
                _LOGGER.warning("No script configured for action '%s'", action)
            return False

        object_id = entity_id.split(".", 1)[-1]
        reserve = self.coordinator.reserve_for_scripts()
        _LOGGER.info(
            "Applying action '%s' via script.%s (power_w=%.0f, reserve_soc=%d)",
            action,
            object_id,
            power_w,
            reserve,
        )
        payload: dict[str, Any] = (
            {"power_w": round(power_w)} if action in (ACTION_CHARGE, ACTION_EXPORT) else {}
        )
        payload["reserve_soc"] = reserve
        try:
            async with asyncio.timeout(SCRIPT_CALL_TIMEOUT_SECONDS):
                await self.hass.services.async_call(
                    "script",
                    object_id,
                    payload,
                    blocking=True,
                )
        except TimeoutError:
            # Not `.exception`: a timeout carries no traceback worth printing,
            # and the stack of an awaited service call says nothing about the
            # script that is actually stuck.
            _LOGGER.error(  # noqa: TRY400
                "script.%s did not return within %d s - treating '%s' as failed. "
                "A control script must not wait indefinitely; give any retry "
                "loop inside it a bound of its own.",
                object_id,
                SCRIPT_CALL_TIMEOUT_SECONDS,
                action,
            )
            return False
        except Exception:
            _LOGGER.exception("Calling script.%s failed", object_id)
            return False
        _LOGGER.debug("script.%s returned for action '%s'", object_id, action)
        if self.coordinator.last_reserve_sent != reserve:
            self.coordinator.last_reserve_sent = reserve
            await self.coordinator.async_persist()
        return True
