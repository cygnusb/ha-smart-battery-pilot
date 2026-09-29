"""Executes the plan: calls the user-configured scripts at slot boundaries."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_EXPORT,
    CONF_SCRIPT_IDLE,
    SCRIPT_CALL_TIMEOUT_SECONDS,
)
from .coordinator import ChargeReading, SBPCoordinator
from .optimizer import PlanSlot

_LOGGER = logging.getLogger(__name__)

# Decisions kept for the diagnostics dump. Identical consecutive ones collapse
# into a single entry, so this covers many hours of steady operation.
MAX_DECISIONS = 50


class PlanExecutor:
    """Applies the planned action whenever a slot boundary is crossed."""

    def __init__(self, hass: HomeAssistant, coordinator: SBPCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._unsub_timer = None
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
        await self.async_apply_current()

    async def async_stop(self, restore_auto: bool = True) -> None:
        """Stop execution; optionally hand the battery back to auto mode."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_coordinator:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        async with self._lock:
            self._stopped = True
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
            # arriving while this run waits for the lock has to queue a fresh
            # run, but one arriving while it waits for a script must not.
            self._apply_queued = False
            if self._stopped:
                return
            self._schedule_boundary()
            # Before any script runs: the one that ends a charge slot may take
            # minutes, and the observation must end when the charge did.
            reading = self.coordinator.charge_reading()
            await self._apply_locked()
            self._report_charge(reading)

    def _report_charge(self, reading: ChargeReading) -> None:
        """Tell the coordinator whether a charge slot is really running.

        It watches those slots to learn how much a cold battery actually
        takes; only a charge the inverter really received tells it anything.
        """
        last = self.decisions[-1] if self.decisions else None
        charging = (
            last is not None and last["outcome"] == "applied" and last["planned"] == ACTION_CHARGE
        )
        self.coordinator.charge_observation(
            float(last["power_w"]) if charging else None, closing=reading
        )

    async def _apply_locked(self) -> None:
        coordinator = self.coordinator
        slot = self.current_slot()
        if slot is None or not self._plan_is_live():
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
            self._record("no_live_plan", None, reason)
            return

        if not coordinator.enabled:
            _LOGGER.debug("Pilot disabled - leaving slot action '%s' unapplied", slot.action)
            self._record("disabled", slot)
            return

        action = slot.action
        if coordinator.dry_run:
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
            self._record("dry_run", slot)
            return

        if action == self._last_applied and action not in (
            ACTION_CHARGE,
            ACTION_EXPORT,
        ):
            _LOGGER.debug(
                "Action '%s' already applied - no script call for slot %s",
                action,
                slot.start.isoformat(),
            )
            self._record("unchanged", slot)
            return

        if await self._call_script(action, slot.power_w):
            await self._remember(action)
            self._record("applied", slot)
            return
        if action != ACTION_AUTO and await self._call_script(ACTION_AUTO, 0.0):
            await self._remember(ACTION_AUTO)
            self._record("failed_fell_back_to_auto", slot)
            return
        self._record("failed", slot)

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

    async def _call_script(self, action: str, power_w: float) -> bool:
        key = {
            ACTION_CHARGE: CONF_SCRIPT_CHARGE,
            ACTION_IDLE: CONF_SCRIPT_IDLE,
            ACTION_AUTO: CONF_SCRIPT_AUTO,
            ACTION_EXPORT: CONF_SCRIPT_EXPORT,
        }.get(action)
        entity_id = self.coordinator.conf(key) if key else None
        if not entity_id:
            if action == ACTION_EXPORT:
                _LOGGER.warning("No export script configured - skipping export action")
            else:
                _LOGGER.warning("No script configured for action '%s'", action)
            return False

        object_id = entity_id.split(".", 1)[-1]
        _LOGGER.info(
            "Applying action '%s' via script.%s (power_w=%.0f)", action, object_id, power_w
        )
        payload = {"power_w": round(power_w)} if action in (ACTION_CHARGE, ACTION_EXPORT) else {}
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
        return True
