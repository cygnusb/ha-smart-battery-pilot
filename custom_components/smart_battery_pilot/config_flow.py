"""Config flow for Smart Battery Pilot."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CAPACITY_KWH,
    CONF_CONSUMPTION_ENTITY,
    CONF_DISCHARGE_MODE,
    CONF_DRY_RUN,
    CONF_EFFICIENCY,
    CONF_FEED_IN_TARIFF,
    CONF_HAS_HEAT_PUMP,
    CONF_MAX_CHARGE_POWER_W,
    CONF_MAX_DISCHARGE_POWER_W,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PRICE_ENTITY,
    CONF_PRICE_OFFSET,
    CONF_PV_FORECAST_TODAY,
    CONF_PV_FORECAST_TOMORROW,
    CONF_PV_POWER_ENTITY,
    CONF_SCRIPT_AUTO,
    CONF_SCRIPT_CHARGE,
    CONF_SCRIPT_EXPORT,
    CONF_SCRIPT_IDLE,
    CONF_SOC_ENTITY,
    CONF_SPREAD_THRESHOLD,
    CONF_TEMPERATURE_ENTITY,
    CONF_TRAINING_DAYS,
    DEFAULT_DISCHARGE_MODE,
    DEFAULT_DRY_RUN,
    DEFAULT_EFFICIENCY,
    DEFAULT_FEED_IN_TARIFF,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_PRICE_OFFSET,
    DEFAULT_SPREAD_THRESHOLD,
    DEFAULT_TRAINING_DAYS,
    DISCHARGE_MODE_EXPORT,
    DISCHARGE_MODE_SELF_CONSUMPTION,
    DOMAIN,
)
from .price_adapters import detect_adapter

_ENTITY = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["sensor", "input_number", "number"])
)
_SCRIPT = selector.EntitySelector(selector.EntitySelectorConfig(domain="script"))
_OPTIONAL_SCRIPT = _SCRIPT


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SPREAD_THRESHOLD,
                default=defaults.get(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1, step=0.01, unit_of_measurement="EUR/kWh"
                )
            ),
            vol.Required(
                CONF_DISCHARGE_MODE,
                default=defaults.get(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        DISCHARGE_MODE_SELF_CONSUMPTION,
                        DISCHARGE_MODE_EXPORT,
                    ],
                    translation_key="discharge_mode",
                )
            ),
            vol.Required(
                CONF_PRICE_OFFSET,
                default=defaults.get(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-1, max=1, step=0.001, unit_of_measurement="EUR/kWh"
                )
            ),
            vol.Required(
                CONF_FEED_IN_TARIFF,
                default=defaults.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh"
                )
            ),
            vol.Required(
                CONF_TRAINING_DAYS,
                default=defaults.get(CONF_TRAINING_DAYS, DEFAULT_TRAINING_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=7, max=180, step=1)
            ),
        }
    )


class SBPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step setup: prices -> battery -> control -> consumption -> pv."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: price source."""
        errors: dict[str, str] = {}
        if user_input is not None:
            state = self.hass.states.get(user_input[CONF_PRICE_ENTITY])
            adapter = detect_adapter(dict(state.attributes)) if state else None
            if state is None:
                errors[CONF_PRICE_ENTITY] = "entity_not_found"
            elif adapter is None:
                errors[CONF_PRICE_ENTITY] = "unsupported_price_format"
            else:
                slots = adapter.parse(dict(state.attributes), dt_util.now())
                if not slots:
                    errors[CONF_PRICE_ENTITY] = "no_future_prices"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_battery()

        schema = vol.Schema(
            {
                vol.Required(CONF_PRICE_ENTITY): _ENTITY,
                vol.Required(CONF_PRICE_OFFSET, default=DEFAULT_PRICE_OFFSET): (
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=-1, max=1, step=0.001, unit_of_measurement="EUR/kWh"
                        )
                    )
                ),
                vol.Required(CONF_FEED_IN_TARIFF, default=DEFAULT_FEED_IN_TARIFF): (
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=1, step=0.001, unit_of_measurement="EUR/kWh"
                        )
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: battery parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_MIN_SOC] >= user_input[CONF_MAX_SOC]:
                errors["base"] = "soc_range_invalid"
            else:
                self._data.update(user_input)
                return await self.async_step_control()

        schema = vol.Schema(
            {
                vol.Required(CONF_SOC_ENTITY): _ENTITY,
                vol.Required(CONF_CAPACITY_KWH, default=10.0): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=200, step=0.1, unit_of_measurement="kWh"
                    )
                ),
                vol.Required(
                    CONF_MAX_CHARGE_POWER_W, default=5000
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=100, max=50000, step=100, unit_of_measurement="W"
                    )
                ),
                vol.Required(
                    CONF_MAX_DISCHARGE_POWER_W, default=5000
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=100, max=50000, step=100, unit_of_measurement="W"
                    )
                ),
                vol.Required(CONF_MIN_SOC, default=DEFAULT_MIN_SOC): (
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100, step=1, unit_of_measurement="%"
                        )
                    )
                ),
                vol.Required(CONF_MAX_SOC, default=DEFAULT_MAX_SOC): (
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=100, step=1, unit_of_measurement="%"
                        )
                    )
                ),
                vol.Required(CONF_EFFICIENCY, default=DEFAULT_EFFICIENCY): (
                    selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=50, max=100, step=1, unit_of_measurement="%"
                        )
                    )
                ),
            }
        )
        return self.async_show_form(step_id="battery", data_schema=schema, errors=errors)

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: control scripts."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_consumption()

        schema = vol.Schema(
            {
                vol.Required(CONF_SCRIPT_CHARGE): _SCRIPT,
                vol.Required(CONF_SCRIPT_IDLE): _SCRIPT,
                vol.Required(CONF_SCRIPT_AUTO): _SCRIPT,
                vol.Optional(CONF_SCRIPT_EXPORT): _OPTIONAL_SCRIPT,
            }
        )
        return self.async_show_form(step_id="control", data_schema=schema)

    async def async_step_consumption(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 4: consumption source for the forecast."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_pv()

        schema = vol.Schema(
            {
                vol.Required(CONF_CONSUMPTION_ENTITY): _ENTITY,
                vol.Optional(CONF_TEMPERATURE_ENTITY): _ENTITY,
                vol.Required(CONF_HAS_HEAT_PUMP, default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="consumption", data_schema=schema)

    async def async_step_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 5: optional PV forecast."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Smart Battery Pilot",
                data=self._data,
                options={
                    CONF_SPREAD_THRESHOLD: DEFAULT_SPREAD_THRESHOLD,
                    CONF_DISCHARGE_MODE: DEFAULT_DISCHARGE_MODE,
                    CONF_DRY_RUN: DEFAULT_DRY_RUN,
                    CONF_TRAINING_DAYS: DEFAULT_TRAINING_DAYS,
                },
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_PV_FORECAST_TODAY): _ENTITY,
                vol.Optional(CONF_PV_FORECAST_TOMORROW): _ENTITY,
                vol.Optional(CONF_PV_POWER_ENTITY): _ENTITY,
            }
        )
        return self.async_show_form(step_id="pv", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SBPOptionsFlow":
        return SBPOptionsFlow()


class SBPOptionsFlow(OptionsFlow):
    """Tune thresholds and modes after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})

        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_options_schema(defaults))
