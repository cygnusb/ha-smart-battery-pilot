"""Stub of homeassistant.config_entries."""


class AbortFlow(Exception):
    """Raised by _abort_if_unique_id_configured, like data_entry_flow does."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConfigEntry:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, data=None, options=None, entry_id="test", unique_id=None):
        self.unique_id = unique_id
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.runtime_data = None

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, unsub):
        return unsub


class ConfigFlowResult(dict):
    pass


class _FlowBase:
    hass = None

    def async_show_form(self, **kwargs):
        return ConfigFlowResult(type="form", **kwargs)

    def async_show_menu(self, **kwargs):
        return ConfigFlowResult(type="menu", **kwargs)

    def async_create_entry(self, **kwargs):
        return ConfigFlowResult(type="create_entry", **kwargs)

    def async_abort(self, **kwargs):
        return ConfigFlowResult(type="abort", **kwargs)


class ConfigFlow(_FlowBase):
    handler = None
    unique_id = None

    def __init_subclass__(cls, **kwargs):
        cls.handler = kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)

    def _configured_entries(self):
        if self.hass is None:
            return []
        return self.hass.config_entries.async_entries(self.handler)

    async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
        self.unique_id = unique_id
        return next((e for e in self._configured_entries() if e.unique_id == unique_id), None)

    def _abort_if_unique_id_configured(self):
        if any(e.unique_id == self.unique_id for e in self._configured_entries()):
            raise AbortFlow("already_configured")


class OptionsFlow(_FlowBase):
    config_entry = None
