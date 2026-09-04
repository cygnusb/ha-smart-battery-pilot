"""Stub of homeassistant.config_entries."""


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


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)

    def async_show_form(self, **kwargs):
        return ConfigFlowResult(type="form", **kwargs)

    def async_create_entry(self, **kwargs):
        return ConfigFlowResult(type="create_entry", **kwargs)


class OptionsFlow:
    def async_show_form(self, **kwargs):
        return ConfigFlowResult(type="form", **kwargs)

    def async_create_entry(self, **kwargs):
        return ConfigFlowResult(type="create_entry", **kwargs)
