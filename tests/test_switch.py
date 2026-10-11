"""The pilot's switches restore their flag and tell every entity about it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from smart_battery_pilot.switch import DryRunSwitch, EnabledSwitch


class _Coordinator:
    def __init__(self) -> None:
        self.entry = SimpleNamespace(entry_id="entry")
        self.enabled = False
        self.dry_run = True
        self.updates = 0

    def async_update_listeners(self) -> None:
        self.updates += 1


class _Executor:
    def __init__(self) -> None:
        self.applied = 0

    async def async_apply_current(self) -> None:
        self.applied += 1

    async def async_stop(self, restore_auto: bool) -> None:
        pass

    async def async_start(self) -> None:
        pass


def _restored(switch, state: str):
    async def last_state():
        return SimpleNamespace(state=state)

    switch.async_get_last_state = last_state
    return switch


def test_a_restored_flag_reaches_every_entity():
    """The configuration sensor is set up first and showed "inactive" after
    a restart until the next refresh."""
    coord = _Coordinator()
    switch = _restored(EnabledSwitch(coord, _Executor()), "on")
    asyncio.run(switch.async_added_to_hass())
    assert coord.enabled is True
    assert coord.updates == 1


def test_no_restored_state_keeps_the_safe_default():
    coord = _Coordinator()
    switch = DryRunSwitch(coord, _Executor())
    asyncio.run(switch.async_added_to_hass())
    assert coord.dry_run is True
    assert coord.updates == 0


def test_switching_updates_every_entity_and_re_applies():
    coord = _Coordinator()
    executor = _Executor()
    switch = DryRunSwitch(coord, executor)
    asyncio.run(switch.async_turn_off())
    assert coord.dry_run is False
    assert coord.updates == 1
    assert executor.applied == 1
