"""Stub of homeassistant.helpers.selector."""


class _Config(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class _Selector:
    def __init__(self, config=None):
        self.config = config

    def __call__(self, value):
        return value


EntitySelectorConfig = _Config
NumberSelectorConfig = _Config
SelectSelectorConfig = _Config

EntitySelector = _Selector
NumberSelector = _Selector
SelectSelector = _Selector
BooleanSelector = _Selector
