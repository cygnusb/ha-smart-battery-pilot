"""Config flow for Smart Battery Pilot."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
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


def _price_number(minimum: float = -1) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=1, step=0.001, unit_of_measurement="EUR/kWh", mode="box"
        )
    )


def _sugg(value: Any) -> dict[str, Any]:
    return {"suggested_value": value}


# --- step schemas (shared between config flow and options flow) -------------


def schema_prices(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRICE_ENTITY, description=_sugg(d.get(CONF_PRICE_ENTITY))): _ENTITY,
            vol.Required(
                CONF_PRICE_OFFSET,
                default=d.get(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET),
            ): _price_number(),
            vol.Required(
                CONF_FEED_IN_TARIFF,
                default=d.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF),
            ): _price_number(0),
        }
    )


def schema_battery(d: dict[str, Any]) -> vol.Schema:
    def num(minimum, maximum, step, unit=None):
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode="box"
            )
        )

    return vol.Schema(
        {
            vol.Required(CONF_SOC_ENTITY, description=_sugg(d.get(CONF_SOC_ENTITY))): _ENTITY,
            vol.Required(
                CONF_CAPACITY_KWH, default=d.get(CONF_CAPACITY_KWH, 10.0)
            ): num(1, 200, 0.1, "kWh"),
            vol.Required(
                CONF_MAX_CHARGE_POWER_W, default=d.get(CONF_MAX_CHARGE_POWER_W, 5000)
            ): num(100, 50000, 100, "W"),
            vol.Required(
                CONF_MAX_DISCHARGE_POWER_W,
                default=d.get(CONF_MAX_DISCHARGE_POWER_W, 5000),
            ): num(100, 50000, 100, "W"),
            vol.Required(
                CONF_MIN_SOC, default=d.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)
            ): num(0, 100, 1, "%"),
            vol.Required(
                CONF_MAX_SOC, default=d.get(CONF_MAX_SOC, DEFAULT_MAX_SOC)
            ): num(0, 100, 1, "%"),
            vol.Required(
                CONF_EFFICIENCY, default=d.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)
            ): num(50, 100, 1, "%"),
            vol.Optional(
                CONF_BATTERY_CHARGE_ENERGY_ENTITY,
                description=_sugg(d.get(CONF_BATTERY_CHARGE_ENERGY_ENTITY)),
            ): _ENTITY,
            vol.Optional(
                CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
                description=_sugg(d.get(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)),
            ): _ENTITY,
        }
    )


def schema_control(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SCRIPT_CHARGE, description=_sugg(d.get(CONF_SCRIPT_CHARGE))): _SCRIPT,
            vol.Required(CONF_SCRIPT_IDLE, description=_sugg(d.get(CONF_SCRIPT_IDLE))): _SCRIPT,
            vol.Required(CONF_SCRIPT_AUTO, description=_sugg(d.get(CONF_SCRIPT_AUTO))): _SCRIPT,
            vol.Optional(CONF_SCRIPT_EXPORT, description=_sugg(d.get(CONF_SCRIPT_EXPORT))): _SCRIPT,
        }
    )


def schema_consumption(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_CONSUMPTION_ENTITY, description=_sugg(d.get(CONF_CONSUMPTION_ENTITY))
            ): _ENTITY,
            vol.Optional(
                CONF_TEMPERATURE_ENTITY, description=_sugg(d.get(CONF_TEMPERATURE_ENTITY))
            ): _ENTITY,
            vol.Required(
                CONF_HAS_HEAT_PUMP, default=d.get(CONF_HAS_HEAT_PUMP, False)
            ): selector.BooleanSelector(),
        }
    )


def schema_pv(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_PV_FORECAST_TODAY, description=_sugg(d.get(CONF_PV_FORECAST_TODAY))
            ): _ENTITY,
            vol.Optional(
                CONF_PV_FORECAST_TOMORROW,
                description=_sugg(d.get(CONF_PV_FORECAST_TOMORROW)),
            ): _ENTITY,
            vol.Optional(
                CONF_PV_POWER_ENTITY, description=_sugg(d.get(CONF_PV_POWER_ENTITY))
            ): _ENTITY,
        }
    )


def schema_tuning(d: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SPREAD_THRESHOLD,
                default=d.get(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD),
            ): _price_number(0),
            vol.Required(
                CONF_DISCHARGE_MODE,
                default=d.get(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[DISCHARGE_MODE_SELF_CONSUMPTION, DISCHARGE_MODE_EXPORT],
                    translation_key="discharge_mode",
                )
            ),
            vol.Required(
                CONF_TRAINING_DAYS,
                default=d.get(CONF_TRAINING_DAYS, DEFAULT_TRAINING_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=7, max=180, step=1, mode="box")
            ),
        }
    )


STEP_FIELDS: dict[str, list[str]] = {
    "prices": [CONF_PRICE_ENTITY, CONF_PRICE_OFFSET, CONF_FEED_IN_TARIFF],
    "battery": [
        CONF_SOC_ENTITY,
        CONF_CAPACITY_KWH,
        CONF_MAX_CHARGE_POWER_W,
        CONF_MAX_DISCHARGE_POWER_W,
        CONF_MIN_SOC,
        CONF_MAX_SOC,
        CONF_EFFICIENCY,
        CONF_BATTERY_CHARGE_ENERGY_ENTITY,
        CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    ],
    "control": [CONF_SCRIPT_CHARGE, CONF_SCRIPT_IDLE, CONF_SCRIPT_AUTO, CONF_SCRIPT_EXPORT],
    "consumption": [CONF_CONSUMPTION_ENTITY, CONF_TEMPERATURE_ENTITY, CONF_HAS_HEAT_PUMP],
    "pv": [CONF_PV_FORECAST_TODAY, CONF_PV_FORECAST_TOMORROW, CONF_PV_POWER_ENTITY],
    "tuning": [CONF_SPREAD_THRESHOLD, CONF_DISCHARGE_MODE, CONF_TRAINING_DAYS],
}


def _validate_price_entity(hass, entity_id: str) -> str | None:
    """Return an error key or None."""
    state = hass.states.get(entity_id)
    if state is None:
        return "entity_not_found"
    adapter = detect_adapter(dict(state.attributes))
    if adapter is None:
        return "unsupported_price_format"
    if not adapter.parse(dict(state.attributes), dt_util.now()):
        return "no_future_prices"
    return None


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
            error = _validate_price_entity(self.hass, user_input[CONF_PRICE_ENTITY])
            if error:
                errors[CONF_PRICE_ENTITY] = error
            else:
                self._data.update(user_input)
                return await self.async_step_battery()
        return self.async_show_form(
            step_id="user", data_schema=schema_prices(self._data), errors=errors
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_MIN_SOC] >= user_input[CONF_MAX_SOC]:
                errors["base"] = "soc_range_invalid"
            else:
                # One battery, one plan: two entries steering the same
                # inverter would fight each other slot by slot.
                await self.async_set_unique_id(user_input[CONF_SOC_ENTITY])
                self._abort_if_unique_id_configured()
                self._data.update(user_input)
                return await self.async_step_control()
        return self.async_show_form(
            step_id="battery", data_schema=schema_battery(self._data), errors=errors
        )

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_consumption()
        return self.async_show_form(step_id="control", data_schema=schema_control(self._data))

    async def async_step_consumption(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_pv()
        return self.async_show_form(
            step_id="consumption", data_schema=schema_consumption(self._data)
        )

    async def async_step_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
        return self.async_show_form(step_id="pv", data_schema=schema_pv(self._data))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SBPOptionsFlow:
        return SBPOptionsFlow()


class SBPOptionsFlow(OptionsFlow):
    """Reconfigure every input (entities, scripts, tuning) after setup.

    Section steps return to the main menu and collect changes in _pending;
    only the 'apply' menu entry persists them (create_entry reloads the
    integration once, not per section).
    """

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    @property
    def _merged(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options, **self._pending}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "tuning",
                "prices",
                "battery",
                "control",
                "consumption",
                "pv",
                "apply",
            ],
        )

    async def _save_step(self, step: str, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Stash the step's fields (absent optional fields -> None), back to menu."""
        for key in STEP_FIELDS[step]:
            self._pending[key] = user_input.get(key)
        return await self.async_step_init()

    async def async_step_apply(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, **self._pending}
        )

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = _validate_price_entity(self.hass, user_input[CONF_PRICE_ENTITY])
            if error:
                errors[CONF_PRICE_ENTITY] = error
            else:
                return await self._save_step("prices", user_input)
        return self.async_show_form(
            step_id="prices", data_schema=schema_prices(self._merged), errors=errors
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_MIN_SOC] >= user_input[CONF_MAX_SOC]:
                errors["base"] = "soc_range_invalid"
            else:
                return await self._save_step("battery", user_input)
        return self.async_show_form(
            step_id="battery", data_schema=schema_battery(self._merged), errors=errors
        )

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._save_step("control", user_input)
        return self.async_show_form(step_id="control", data_schema=schema_control(self._merged))

    async def async_step_consumption(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._save_step("consumption", user_input)
        return self.async_show_form(
            step_id="consumption", data_schema=schema_consumption(self._merged)
        )

    async def async_step_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._save_step("pv", user_input)
        return self.async_show_form(step_id="pv", data_schema=schema_pv(self._merged))

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._save_step("tuning", user_input)
        return self.async_show_form(step_id="tuning", data_schema=schema_tuning(self._merged))
