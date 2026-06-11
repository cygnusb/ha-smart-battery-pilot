"""Master enable and dry-run switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SBPConfigEntry
from .coordinator import SBPCoordinator
from .entity import SBPEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SBPConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        [
            EnabledSwitch(runtime.coordinator, runtime.executor),
            DryRunSwitch(runtime.coordinator, runtime.executor),
        ]
    )


class _ExecutorSwitch(SBPEntity, SwitchEntity, RestoreEntity):
    """Switch that controls a coordinator flag and re-applies the plan."""

    _flag: str
    _default: bool

    def __init__(self, coordinator: SBPCoordinator, executor, key: str) -> None:
        super().__init__(coordinator, key)
        self._executor = executor

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            setattr(self.coordinator, self._flag, last.state == "on")

    @property
    def is_on(self) -> bool:
        return getattr(self.coordinator, self._flag)

    async def _set(self, value: bool) -> None:
        setattr(self.coordinator, self._flag, value)
        self.async_write_ha_state()
        await self._executor.async_apply_current()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class EnabledSwitch(_ExecutorSwitch):
    """Master switch: when off, no scripts are called."""

    _flag = "enabled"
    _default = False

    def __init__(self, coordinator: SBPCoordinator, executor) -> None:
        super().__init__(coordinator, executor, "enabled")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await super().async_turn_off(**kwargs)
        # Hand the battery back to the inverter's auto mode.
        await self._executor.async_stop(restore_auto=not self.coordinator.dry_run)
        await self._executor.async_start()


class DryRunSwitch(_ExecutorSwitch):
    """When on, the plan is computed and logged but no scripts are called."""

    _flag = "dry_run"
    _default = True

    def __init__(self, coordinator: SBPCoordinator, executor) -> None:
        super().__init__(coordinator, executor, "dry_run")
