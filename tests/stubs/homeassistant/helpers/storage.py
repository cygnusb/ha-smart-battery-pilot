class Store:
    def __init__(self, hass, version, key):
        self._data = None
        self.delayed_saves = 0
        self.removed = False

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data

    def async_delay_save(self, data_func, delay=0):
        """Mirror HA: remember the payload, flush it later (here: at once)."""
        self.delayed_saves += 1
        self._data = data_func()

    async def async_remove(self):
        self._data = None
        self.removed = True
