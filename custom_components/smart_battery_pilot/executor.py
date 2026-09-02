"""Executes the plan: calls the user-configured scripts at slot boundaries."""

from __future__ import annotations

import logging
from datetime import datetime

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
)
from .coordinator import SBPCoordinator
from .optimizer import PlanSlot

_LOGGER = logging.getLogger(__name__)


class PlanExecutor:
    """Applies the planned action whenever a slot boundary is crossed."""

    def __init__(self, hass: HomeAssistant, coordinator: SBPCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self._unsub_timer = None
        self._unsub_coordinator = None
        self._last_applied: str | None = None

    async def async_start(self) -> None:
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
        # `_last_applied` is only set after a real script call, so dry-run
        # never triggers a restore on unload.
        if restore_auto and self._last_applied not in (None, ACTION_AUTO):
            if await self._call_script(ACTION_AUTO, 0.0):
                self._last_applied = ACTION_AUTO

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self.async_apply_current())

    def _plan_is_live(self) -> bool:
        data = self.coordinator.data
        if not data or not data.valid:
            return False
        if getattr(self.coordinator, "last_update_success", True) is False:
            return False
        return True

    def current_slot(self) -> PlanSlot | None:
        if not self._plan_is_live():
            return None
        now = dt_util.now()
        for slot in self.coordinator.data.plan.slots:
            if slot.start <= now < slot.end:
                return slot
        return None

    def next_slot(self) -> PlanSlot | None:
        if not self._plan_is_live():
            return None
        now = dt_util.now()
        for slot in self.coordinator.data.plan.slots:
            if slot.start > now:
                return slot
        return None

    async def async_apply_current(self) -> None:
        """Apply the action of the current slot and arm the next boundary timer."""
        self._schedule_boundary()

        coordinator = self.coordinator
        slot = self.current_slot()
        if slot is None or not self._plan_is_live():
            # Invalid plan: fail safe to auto mode once.
            if coordinator.enabled and not coordinator.dry_run and self._last_applied not in (None, ACTION_AUTO):
                _LOGGER.warning("Plan invalid - restoring battery auto mode")
                if await self._call_script(ACTION_AUTO, 0.0):
                    self._last_applied = ACTION_AUTO
            return

        if not coordinator.enabled:
            return

        action = slot.action
        if coordinator.dry_run:
            if self._last_applied not in (None, ACTION_AUTO):
                _LOGGER.info("Dry-run enabled - restoring battery auto mode")
                if await self._call_script(ACTION_AUTO, 0.0):
                    self._last_applied = ACTION_AUTO
            _LOGGER.info(
                "DRY RUN: would apply action '%s' (%.0f W) for slot %s - %s",
                action,
                slot.charge_power_w,
                slot.start.isoformat(),
                slot.end.isoformat(),
            )
            return

        if action == self._last_applied and action != ACTION_CHARGE:
            return

        if await self._call_script(action, slot.charge_power_w):
            self._last_applied = action
            return
        if action != ACTION_AUTO and await self._call_script(ACTION_AUTO, 0.0):
            self._last_applied = ACTION_AUTO

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
            return

        @callback
        def _fire(_now: datetime) -> None:
            self.hass.async_create_task(self.async_apply_current())

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
        _LOGGER.info("Applying action '%s' via script.%s (power_w=%.0f)", action, object_id, power_w)
        try:
            await self.hass.services.async_call(
                "script",
                object_id,
                {"power_w": round(power_w)} if action == ACTION_CHARGE else {},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Calling script.%s failed: %s", object_id, err)
            return False
        return True
