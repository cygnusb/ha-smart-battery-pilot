"""Stub of homeassistant.exceptions."""


class HomeAssistantError(Exception):
    pass


class ConfigEntryNotReady(HomeAssistantError):
    pass
