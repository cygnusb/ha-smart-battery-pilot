class UpdateFailed(Exception):
    pass


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self._listeners = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def async_request_refresh(self):
        self.data = await self._async_update_data()
        for listener in list(self._listeners):
            listener()

    async def async_config_entry_first_refresh(self):
        await self.async_request_refresh()

    async def async_shutdown(self):
        pass


class CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def async_added_to_hass(self):
        pass

    def async_write_ha_state(self):
        pass
