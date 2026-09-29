"""Data update coordinator: prices -> forecast -> optimizer -> plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import Any, NamedTuple

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_CHARGE,
    ACTION_EXPORT,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_DERATING,
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
    CONF_SOC_ENTITY,
    CONF_SPREAD_THRESHOLD,
    CONF_TEMPERATURE_ENTITY,
    CONF_TRAINING_DAYS,
    DEFAULT_CHARGE_DERATING,
    DEFAULT_DERATING_CURVE,
    DEFAULT_DISCHARGE_MODE,
    DEFAULT_DRY_RUN,
    DEFAULT_EFFICIENCY,
    DEFAULT_FEED_IN_TARIFF,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_PRICE_OFFSET,
    DEFAULT_SPREAD_THRESHOLD,
    DEFAULT_TRAINING_DAYS,
    DERATING_CURVE_KEYS,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    STORE_SAVE_DELAY_SECONDS,
    UPDATE_INTERVAL_MINUTES,
)
from .forecast.charge_rate import (
    SOURCE_OFF,
    ChargeRateModel,
    ChargeSample,
    Curve,
    curve_from_percentages,
)
from .forecast.consumption import ConsumptionForecaster, TrainingSample
from .forecast.pv import pv_kwh_for_slot
from .optimizer import BatteryState, InputSlot, OptimizerConfig, Plan, build_plan
from .price_adapters import detect_adapter
from .price_adapters.base import PriceSlot

_LOGGER = logging.getLogger(__name__)

RETRAIN_INTERVAL = timedelta(hours=24)
# Backoff after a training run that produced nothing. Without it a consumption
# entity that has no long-term statistics (a template sensor without state
# class, say) makes every 30-minute update re-query weeks of history and log
# the same warning, forever.
RETRAIN_RETRY_INTERVAL = timedelta(hours=6)
# Price/mode samples kept for savings accounting; see _note_conditions.
MAX_CONDITION_SAMPLES = 200

# Above this, a parsed price is not a price but a unit error - cents or öre
# read as euros. The all-time EPEX spot record is well under 1 EUR/kWh, so
# anything past this is two orders of magnitude out. Planning on such a curve
# would make every hour look like a huge arbitrage opportunity and force-charge
# the battery from the grid around the clock, so the plan is refused instead.
MAX_PLAUSIBLE_PRICE_EUR_KWH = 10.0

# Fallback daylight window when sun.sun is unavailable.
DEFAULT_SUNRISE_HOUR = 6.0
DEFAULT_SUNSET_HOUR = 21.0

# Charge factor source while derating is on but the temperature cannot be read.
SOURCE_NO_TEMPERATURE = "no_temperature"

# Observation rules for learning the cold charge limit (see the spec).
MIN_OBSERVATION = timedelta(minutes=10)
# The battery warms while it charges; cut long charges into hour-long samples
# so each is filed under a temperature close to the one it ran at.
MAX_OBSERVATION = timedelta(hours=1)
# Inverter energy meters often count in 0.1 kWh steps. A sample needs either
# enough energy for that step not to dominate, or enough time that "nothing
# went in" is a finding rather than a rounding artefact.
MIN_OBSERVATION_KWH = 0.2
RESOLUTION_EXEMPT_AFTER = timedelta(minutes=30)
TAPER_MARGIN_SOC = 5.0  # above max_soc - this, the BMS tapers because it is full
MIN_REQUEST_SHARE = 0.2  # smaller requests say nothing about the limit
SATURATION_SHARE = 0.85  # took less than this share of the request -> limited


class PriceEntityUnavailable(UpdateFailed):
    """The configured price entity has no usable state."""


class PriceAdapterMissing(UpdateFailed):
    """No adapter recognises the price entity's attribute format."""


class PriceParseError(UpdateFailed):
    """The price attributes could not be turned into slots."""


class PriceImplausible(UpdateFailed):
    """The parsed prices are far outside any real tariff - likely a unit error."""


class IntervalPrices(NamedTuple):
    """Unit prices (EUR/kWh) that applied while energy moved over a span."""

    charge: float  # what a kWh put into the battery cost
    discharge: float  # what a kWh taken out of it was worth


@dataclass(frozen=True, slots=True)
class _OpenCharge:
    """A forced charge slot being watched."""

    started: datetime
    kwh: float
    temperature: float
    requested_w: float


@dataclass(frozen=True, slots=True)
class ChargeReading:
    """Meter and SOC at one instant - the end point of a charge observation."""

    at: datetime
    kwh: float | None
    soc: float | None


@dataclass
class SBPData:
    """Result of one coordinator update."""

    plan: Plan
    valid: bool = False
    error: str | None = None
    adapter_name: str | None = None
    model_type: str = "default"
    training_samples: int = 0
    soc: float | None = None
    updated_at: datetime | None = None
    consumption_forecast_24h_kwh: float = 0.0
    pv_forecast_24h_kwh: float = 0.0
    # Actual savings accumulation (only set when energy entities are configured)
    actual_savings_eur: float | None = None
    actual_charge_kwh: float | None = None
    actual_discharge_kwh: float | None = None
    pv_power_w: float | None = None
    pv_power_entity: str | None = None
    # What the planner was fed, for the diagnostics dump: a plan is only
    # explainable next to the SOC, prices and limits it was built from.
    inputs: dict[str, Any] | None = None


class SBPCoordinator(DataUpdateCoordinator[SBPData]):
    """Coordinates price parsing, forecasting and plan optimization."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self.forecaster = ConsumptionForecaster()
        self.charge_model = ChargeRateModel(self._derating_curve())
        # Battery temperature entity already warned about as unreadable; reset
        # once it reads again, so a later outage is reported afresh.
        self._warned_battery_temperature = False
        self._open_charge: _OpenCharge | None = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._last_training: datetime | None = None
        self._last_attempt: datetime | None = None
        self._unsub_price = None
        self._adapter_name: str | None = None

        # Last action really applied to the inverter. Persisted, because
        # after a restart it is the only way to know that the battery is
        # still sitting in a forced mode we have to release.
        self._last_applied: str | None = None

        # Runtime flags controlled by the switch entities.
        self.enabled: bool = False
        self.dry_run: bool = self.opt(CONF_DRY_RUN, DEFAULT_DRY_RUN)

        # Actual savings tracking
        self._prev_charge_kwh: float | None = None
        self._prev_discharge_kwh: float | None = None
        self._prev_savings_at: datetime | None = None
        # (timestamp, import price, applied action) samples covering the span
        # since the last accumulation. The plan only ever holds future slots,
        # so the price and mode that were in force while the energy actually
        # moved cannot be recovered from it afterwards - they have to be
        # recorded as they happen.
        self._conditions: list[tuple[float, float, str | None]] = []
        # Entities already warned about an unexpected unit. The check runs on
        # every update, so without this one misconfigured meter writes the same
        # warning to the log twice an hour forever.
        self._warned_units: set[str] = set()
        self._acc_savings_eur: float = 0.0
        self._acc_charge_kwh: float = 0.0
        self._acc_discharge_kwh: float = 0.0

    # --- config helpers -------------------------------------------------------

    def conf(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, default)

    def derating_enabled(self) -> bool:
        return bool(self.conf(CONF_CHARGE_DERATING, DEFAULT_CHARGE_DERATING))

    def _derating_curve(self) -> Curve:
        return curve_from_percentages(
            [
                float(self.conf(key, default))
                for key, default in zip(DERATING_CURVE_KEYS, DEFAULT_DERATING_CURVE, strict=True)
            ]
        )

    # --- lifecycle -------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load the persisted model and subscribe to price updates."""
        stored = await self._store.async_load()
        if stored and stored.get("model"):
            try:
                self.forecaster = ConsumptionForecaster.from_dict(stored["model"])
                last = stored.get("trained_at")
                self._last_training = dt_util.parse_datetime(last) if last else None
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not restore consumption model: %s", err)

        if stored:
            self.last_applied = stored.get("last_applied")

        if stored and stored.get("savings"):
            sv = stored["savings"]
            self._acc_savings_eur = float(sv.get("savings_eur", 0.0))
            self._acc_charge_kwh = float(sv.get("charge_kwh", 0.0))
            self._acc_discharge_kwh = float(sv.get("discharge_kwh", 0.0))

        if stored and stored.get("charge_rate"):
            try:
                self.charge_model = ChargeRateModel.from_dict(
                    stored["charge_rate"], self._derating_curve()
                )
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not restore charge rate observations: %s", err)

        price_entity = self.conf(CONF_PRICE_ENTITY)
        if price_entity:
            self._unsub_price = async_track_state_change_event(
                self.hass, [price_entity], self._handle_price_update
            )

    async def async_shutdown(self) -> None:
        if self._unsub_price:
            self._unsub_price()
            self._unsub_price = None
        await super().async_shutdown()

    @callback
    def _handle_price_update(self, _event) -> None:
        """Re-plan when the price entity updates (e.g. tomorrow's prices arrive)."""
        _LOGGER.debug("Price entity changed - requesting a re-plan")
        self.hass.async_create_task(self.async_request_refresh())

    # --- update ------------------------------------------------------------------

    async def _async_update_data(self) -> SBPData:
        now = dt_util.now()
        # After a successful refresh, keep the integration loaded but mark
        # the plan invalid so the executor fails safe to auto.
        try:
            slots = self._read_price_slots(now)
        except PriceEntityUnavailable:
            if self.data is not None:
                return self._invalid_plan(now, "price_unavailable")
            raise
        except PriceAdapterMissing:
            if self.data is not None:
                return self._invalid_plan(now, "no_price_adapter")
            raise
        except PriceImplausible as err:
            # Never raised on the first refresh: the entry stays loaded so the
            # status sensor and the diagnostics dump can say what is wrong,
            # while the executor fails safe to auto on the invalid plan.
            _LOGGER.warning("Refusing to plan: %s", err)
            return self._invalid_plan(now, "implausible_prices")
        except Exception as err:
            if self.data is not None:
                return self._invalid_plan(now, "price_parse_failed")
            raise PriceParseError(f"Price parsing failed: {err}") from err

        if not slots:
            return self._invalid_plan(now, "no_price_data")

        soc = self._read_float_state(self.conf(CONF_SOC_ENTITY))
        if soc is None:
            return self._invalid_plan(now, "soc_unavailable")

        await self._maybe_retrain(now)

        temperature = self._read_float_state(self.conf(CONF_TEMPERATURE_ENTITY))
        pv_today = self._read_float_state(self.conf(CONF_PV_FORECAST_TODAY))
        pv_tomorrow = self._read_float_state(self.conf(CONF_PV_FORECAST_TOMORROW))

        sunrise_hour, sunset_hour = self._daylight_window()
        _LOGGER.debug(
            "Planning inputs: %d price slots %s .. %s via '%s', SOC %.1f%%, "
            "temperature %s, PV forecast today %s / tomorrow %s kWh, "
            "daylight %.2f-%.2f h, model %s (%d samples)",
            len(slots),
            slots[0].start.isoformat(),
            slots[-1].end.isoformat(),
            self._adapter_name,
            soc,
            temperature,
            pv_today,
            pv_tomorrow,
            sunrise_hour,
            sunset_hour,
            self.forecaster.model_type,
            self.forecaster.sample_count,
        )

        input_slots: list[InputSlot] = []
        consumption_24h = 0.0
        pv_24h = 0.0
        for slot in slots:
            consumption = self.forecaster.predict_kwh(slot.start, slot.hours, temperature)
            pv = pv_kwh_for_slot(
                slot.start,
                slot.hours,
                pv_today,
                pv_tomorrow,
                now,
                sunrise_hour=sunrise_hour,
                sunset_hour=sunset_hour,
            )
            if slot.start < now + timedelta(hours=24):
                consumption_24h += consumption
                pv_24h += pv
            input_slots.append(
                InputSlot(price_slot=slot, net_demand_kwh=consumption - pv, pv_kwh=pv)
            )

        factor, factor_source, battery_temperature = self.charge_factor()
        _LOGGER.debug(
            "Charge factor %.3f (%s) at battery temperature %s",
            factor,
            factor_source,
            battery_temperature,
        )
        battery = BatteryState(
            capacity_kwh=float(self.conf(CONF_CAPACITY_KWH, 10.0)),
            soc=soc,
            min_soc=float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC)),
            max_soc=float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC)),
            max_charge_power_w=float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000)),
            max_discharge_power_w=float(self.conf(CONF_MAX_DISCHARGE_POWER_W, 5000)),
            efficiency=float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)),
            charge_factor=factor,
        )
        config = OptimizerConfig(
            spread_threshold=float(self.conf(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD)),
            discharge_mode=self.conf(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            feed_in_tariff=float(self.conf(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF)),
            price_offset=float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET)),
        )

        plan = await self.hass.async_add_executor_job(build_plan, input_slots, battery, config)
        if "export_spread_unreachable" in plan.warnings:
            _LOGGER.warning(
                "Export mode is on but no slot's sell price beats the spread "
                "(feed-in %.3f EUR/kWh, spread %.3f EUR/kWh). Set feed-in to 0 "
                "for market-price export, or lower the spread.",
                config.feed_in_tariff,
                config.spread_threshold,
            )
        if "plan_worse_than_baseline" in plan.warnings:
            _LOGGER.warning(
                "The optimized plan came out worse than leaving the inverter "
                "alone, so this cycle runs the all-auto plan instead. This "
                "should not happen - please report it at %s",
                "https://github.com/cygnusb/ha-smart-battery-pilot/issues",
            )

        self._update_actual_savings(plan, now)

        pv_entity = self.conf(CONF_PV_POWER_ENTITY)
        pv_power = self._read_float_state(pv_entity) if pv_entity else None

        prices = [slot.price for slot in slots]
        inputs = {
            "soc": soc,
            "temperature": temperature,
            "pv_forecast_today_kwh": pv_today,
            "pv_forecast_tomorrow_kwh": pv_tomorrow,
            "daylight_hours": [round(sunrise_hour, 2), round(sunset_hour, 2)],
            "slots": len(slots),
            "horizon_start": slots[0].start.isoformat(),
            "horizon_end": slots[-1].end.isoformat(),
            "price_min": round(min(prices), 4),
            "price_max": round(max(prices), 4),
            "battery": asdict(battery),
            "config": asdict(config),
            "charge_derating": {
                "enabled": self.derating_enabled(),
                "temperature": (
                    round(battery_temperature, 2) if battery_temperature is not None else None
                ),
                "factor": round(factor, 3),
                "source": factor_source,
            },
        }
        if self.data is not None and not self.data.valid:
            _LOGGER.info("Planning works again after '%s'", self.data.error)

        return SBPData(
            plan=plan,
            valid=True,
            adapter_name=self._adapter_name,
            model_type=self.forecaster.model_type,
            training_samples=self.forecaster.sample_count,
            soc=soc,
            updated_at=now,
            consumption_forecast_24h_kwh=round(consumption_24h, 2),
            pv_forecast_24h_kwh=round(pv_24h, 2),
            pv_power_w=pv_power,
            pv_power_entity=pv_entity,
            inputs=inputs,
            **self._savings_fields(),
        )

    def _invalid_plan(self, now: datetime, error: str) -> SBPData:
        # Warn when the reason first appears; repeating it every 30 minutes
        # while an entity stays down would only bury the rest of the log.
        previous = self.data.error if self.data is not None and not self.data.valid else None
        if error != previous:
            _LOGGER.warning("No valid plan (%s) - the battery is handed back to auto mode", error)
        else:
            _LOGGER.debug("Still no valid plan (%s)", error)
        return SBPData(
            plan=Plan(), valid=False, error=error, updated_at=now, **self._savings_fields()
        )

    def _savings_fields(self) -> dict[str, float | None]:
        """The accumulated totals, for every SBPData this coordinator returns.

        They are running totals, not a property of the current plan. Leaving
        them out of the failure results would drop the two TOTAL sensors to
        `unknown` - and punch a hole in their long-term statistics - every
        time the price entity blinks.
        """
        if not self._has_energy_entities():
            return {
                "actual_savings_eur": None,
                "actual_charge_kwh": None,
                "actual_discharge_kwh": None,
            }
        return {
            "actual_savings_eur": round(self._acc_savings_eur, 3),
            "actual_charge_kwh": round(self._acc_charge_kwh, 2),
            "actual_discharge_kwh": round(self._acc_discharge_kwh, 2),
        }

    def _read_price_slots(self, now: datetime) -> list[PriceSlot]:
        entity_id = self.conf(CONF_PRICE_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unavailable", "unknown"):
            raise PriceEntityUnavailable(f"Price entity {entity_id} unavailable")
        attrs = dict(state.attributes)
        adapter = detect_adapter(attrs)
        if adapter is None:
            raise PriceAdapterMissing(f"No price adapter matches {entity_id}")
        self._adapter_name = adapter.name
        offset = float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET))
        slots = adapter.parse(attrs, now)
        _LOGGER.debug(
            "Adapter '%s' parsed %d slots from %s (attributes: %s), offset %.4f EUR/kWh",
            adapter.name,
            len(slots),
            entity_id,
            sorted(attrs),
            offset,
        )
        if offset:
            slots = [PriceSlot(start=s.start, end=s.end, price=s.price + offset) for s in slots]
        self._reject_implausible_prices(slots, entity_id, adapter.name)
        return slots

    @staticmethod
    def _reject_implausible_prices(
        slots: list[PriceSlot], entity_id: str, adapter_name: str
    ) -> None:
        """Refuse a price curve that cannot be EUR/kWh.

        The adapters convert cents and öre where the entity says so, but a
        source that labels its unit wrongly - or a generic one the catch-all
        adapter picked up - still lands here. Better no plan than a plan built
        on prices a hundred times too large.
        """
        if not slots:
            return
        worst = max(abs(slot.price) for slot in slots)
        if worst <= MAX_PLAUSIBLE_PRICE_EUR_KWH:
            return
        raise PriceImplausible(
            f"{entity_id} yields prices up to {worst:.2f} EUR/kWh via the "
            f"'{adapter_name}' adapter; expected EUR/kWh. Check the entity's "
            "unit_of_measurement (cents/öre are converted only when declared)."
        )

    def _daylight_window(self) -> tuple[float, float]:
        """Local sunrise/sunset hour from `sun.sun`, else a fixed 06-21 window.

        A December day is roughly 8 hours long, not 15 - spreading the daily
        PV total over a fixed summer window puts production in hours that are
        pitch dark, which is exactly the season this integration targets.
        """
        state = self.hass.states.get("sun.sun")
        if state is None:
            return DEFAULT_SUNRISE_HOUR, DEFAULT_SUNSET_HOUR
        rising = self._local_hour(state.attributes.get("next_rising"))
        setting = self._local_hour(state.attributes.get("next_setting"))
        if rising is None or setting is None or not 0.0 <= rising < setting <= 24.0:
            return DEFAULT_SUNRISE_HOUR, DEFAULT_SUNSET_HOUR
        return rising, setting

    @staticmethod
    def _local_hour(value: Any) -> float | None:
        """Clock hour (local, fractional) of an ISO timestamp attribute."""
        parsed = dt_util.parse_datetime(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime):
            return None
        local = dt_util.as_local(parsed)
        return local.hour + local.minute / 60.0

    def _read_float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _read_temperature_c(self, entity_id: str | None) -> float | None:
        """A temperature in °C, whatever unit the entity reports in."""
        value = self._read_float_state(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = str(state.attributes.get("unit_of_measurement") or "") if state else ""
        if unit == "°F":
            return (value - 32.0) * 5.0 / 9.0
        return value

    def charge_factor(self) -> tuple[float, str, float | None]:
        """(factor, source, battery temperature °C) for the planner."""
        if not self.derating_enabled():
            return 1.0, SOURCE_OFF, None
        entity_id = self.conf(CONF_BATTERY_TEMPERATURE_ENTITY)
        temperature = self._read_temperature_c(entity_id)
        if temperature is None:
            if not self._warned_battery_temperature:
                self._warned_battery_temperature = True
                _LOGGER.warning(
                    "Battery temperature %s is unavailable - planning with full "
                    "charge power until it reads again",
                    entity_id,
                )
            else:
                _LOGGER.debug("Battery temperature %s still unavailable", entity_id)
            return 1.0, SOURCE_NO_TEMPERATURE, None
        self._warned_battery_temperature = False
        return (
            self.charge_model.factor(temperature),
            self.charge_model.source(temperature),
            temperature,
        )

    def _read_energy_kwh(self, entity_id: str | None) -> float | None:
        """Read a cumulative energy meter, converting Wh → kWh when needed."""
        raw = self._read_float_state(entity_id)
        if raw is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = ""
        if state is not None:
            unit = str(state.attributes.get("unit_of_measurement") or "")
        if unit == "Wh":
            return raw / 1000.0
        if unit and unit not in ("kWh", "kwh") and entity_id not in self._warned_units:
            self._warned_units.add(entity_id)
            _LOGGER.warning(
                "Energy entity %s uses unit %s; expected kWh or Wh. Readings are "
                "taken as kWh; this is logged once per entity.",
                entity_id,
                unit,
            )
        return raw

    # --- training -----------------------------------------------------------------

    async def _maybe_retrain(self, now: datetime) -> None:
        if self._last_training and now - self._last_training < RETRAIN_INTERVAL:
            return
        if self._last_attempt and now - self._last_attempt < RETRAIN_RETRY_INTERVAL:
            return
        entity_id = self.conf(CONF_CONSUMPTION_ENTITY)
        if not entity_id:
            _LOGGER.debug("No consumption entity configured - skipping model training")
            return
        # Kept in memory only, so a restart always retries straight away.
        self._last_attempt = now
        days = int(self.conf(CONF_TRAINING_DAYS, DEFAULT_TRAINING_DAYS))
        start = now - timedelta(days=days)
        temp_entity = self.conf(CONF_TEMPERATURE_ENTITY)
        ids = [entity_id] + ([temp_entity] if temp_entity else [])

        try:
            stats = await self._fetch_statistics(start, now, ids)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Fetching statistics failed: %s", err)
            return

        samples = self._build_samples(stats.get(entity_id, []), stats.get(temp_entity or "", []))
        _LOGGER.debug(
            "Statistics since %s: %d consumption rows, %d temperature rows -> %d samples",
            start.isoformat(),
            len(stats.get(entity_id, [])),
            len(stats.get(temp_entity or "", [])),
            len(samples),
        )
        if not samples:
            _LOGGER.warning(
                "No statistics found for %s - consumption forecast uses defaults",
                entity_id,
            )
            return

        require_temp = bool(self.conf(CONF_HAS_HEAT_PUMP, False))
        await self.hass.async_add_executor_job(self.forecaster.train, samples, require_temp)
        self._last_training = now
        await self.async_persist()
        _LOGGER.debug(
            "Trained consumption model (%s) with %d samples",
            self.forecaster.model_type,
            len(samples),
        )

    async def _fetch_statistics(
        self, start: datetime, end: datetime, ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Hourly long-term statistics, read on the recorder's own thread."""
        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(ids),
            "hour",
            None,
            {"mean", "change"},
        )

    def _build_samples(
        self, rows: list[dict[str, Any]], temp_rows: list[dict[str, Any]]
    ) -> list[TrainingSample]:
        """Convert recorder statistics rows into training samples.

        Power sensors (W) provide `mean` -> kWh = mean/1000; energy sensors
        provide `change` (kWh or Wh) per hour.
        """
        unit = None
        entity_id = self.conf(CONF_CONSUMPTION_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state:
            unit = state.attributes.get("unit_of_measurement")

        temps: dict[Any, float] = {}
        for row in temp_rows:
            if row.get("mean") is not None:
                temps[row["start"]] = float(row["mean"])

        samples: list[TrainingSample] = []
        for row in rows:
            start = row["start"]
            if isinstance(start, (int, float)):
                start_dt = dt_util.utc_from_timestamp(start)
            else:
                start_dt = start
            start_dt = dt_util.as_local(start_dt)

            kwh: float | None = None
            if unit in ("W", "kW"):
                mean = row.get("mean")
                if mean is not None:
                    kwh = float(mean) / (1000.0 if unit == "W" else 1.0)
            else:
                change = row.get("change")
                if change is not None and float(change) >= 0:
                    kwh = float(change)
                    if unit == "Wh":
                        kwh /= 1000.0
            if kwh is None or kwh < 0 or kwh > 50:  # discard outliers
                continue
            samples.append(
                TrainingSample(start=start_dt, kwh=kwh, temperature=temps.get(row["start"]))
            )
        return samples

    # --- charge rate observation --------------------------------------------------

    def charge_reading(self, now: datetime | None = None) -> ChargeReading:
        """Snapshot for closing an observation.

        The executor takes it before calling the next action's script: that
        script may run for minutes with no charge flowing, and timing the
        sample after it returned would dilute the measured rate.
        """
        return ChargeReading(
            at=now or dt_util.now(),
            kwh=self._read_energy_kwh(self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)),
            soc=self._read_float_state(self.conf(CONF_SOC_ENTITY)),
        )

    def charge_observation(
        self,
        requested_w: float | None,
        now: datetime | None = None,
        closing: ChargeReading | None = None,
    ) -> None:
        """Close the watched charge slot and, if charging goes on, watch the next.

        Called by the executor after every decision: with the requested power
        while it really runs a charge slot, with None otherwise. The same
        request again - a coordinator refresh re-applying the slot, or the next
        slot asking for the same power - continues the running observation.
        Splitting there would cut 15-minute slots below the minimum length and
        count one 60-minute slot as several samples.
        """
        now = now or dt_util.now()
        running = self._open_charge
        if (
            running is not None
            and requested_w is not None
            and math.isclose(requested_w, running.requested_w, rel_tol=0.01)
            and now - running.started < MAX_OBSERVATION
        ):
            return
        self._close_charge_observation(closing or self.charge_reading(now))
        if requested_w is not None:
            self._open_charge_observation(requested_w, now)

    def _open_charge_observation(self, requested_w: float, now: datetime) -> None:
        if not self.derating_enabled():
            return
        meter = self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
        max_w = float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000))
        reason = None
        kwh = self._read_energy_kwh(meter) if meter else None
        temperature = self._read_temperature_c(self.conf(CONF_BATTERY_TEMPERATURE_ENTITY))
        if not meter:
            reason = "no charge meter"
        elif requested_w < MIN_REQUEST_SHARE * max_w:
            reason = f"request {requested_w:.0f} W too small"
        elif kwh is None:
            reason = "charge meter unavailable"
        elif temperature is None:
            reason = "battery temperature unavailable"
        if reason:
            _LOGGER.debug("Not observing this charge slot: %s", reason)
            return
        self._open_charge = _OpenCharge(now, kwh, temperature, requested_w)

    def _close_charge_observation(self, reading: ChargeReading) -> None:
        opened, self._open_charge = self._open_charge, None
        if opened is None:
            return
        now, kwh, soc = reading.at, reading.kwh, reading.soc
        elapsed = now - opened.started
        max_soc = float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC))
        reason = None
        if elapsed < MIN_OBSERVATION:
            reason = f"only {elapsed.total_seconds() / 60:.0f} min"
        elif kwh is None:
            reason = "charge meter unavailable"
        elif kwh < opened.kwh:
            reason = "charge meter went backwards"
        elif kwh - opened.kwh < MIN_OBSERVATION_KWH and elapsed < RESOLUTION_EXEMPT_AFTER:
            reason = f"only {kwh - opened.kwh:.2f} kWh - below meter resolution"
        elif soc is None or soc >= max_soc - TAPER_MARGIN_SOC:
            reason = f"SOC {soc} too close to max SOC {max_soc:.0f}"
        if reason:
            _LOGGER.debug("Charge observation discarded: %s", reason)
            return

        hours = elapsed.total_seconds() / 3600.0
        achieved_kw = (kwh - opened.kwh) / hours
        eta_one_way = math.sqrt(
            max(0.5, min(1.0, float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)) / 100.0))
        )
        max_kw = float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000)) / 1000.0
        sample = ChargeSample(
            temperature=opened.temperature,
            ratio=achieved_kw / (max_kw * eta_one_way),
            # Both sides of the comparison on the battery side of the inverter:
            # the meter counts what arrived, the request is what left the grid.
            saturated=achieved_kw < SATURATION_SHARE * opened.requested_w / 1000.0 * eta_one_way,
            at=now,
        )
        self.charge_model.add_sample(sample)
        _LOGGER.debug(
            "Charge observation at %.1f °C: %.2f kW of %.2f kW requested "
            "(ratio %.3f, %s) over %.0f min",
            sample.temperature,
            achieved_kw,
            opened.requested_w / 1000.0,
            sample.ratio,
            "limited" if sample.saturated else "took all",
            elapsed.total_seconds() / 60,
        )
        self.schedule_persist()

    # --- actual savings tracking --------------------------------------------------

    def _has_energy_entities(self) -> bool:
        """Both meters are needed: every published figure is a net value.

        With only one of them, discharge minus charge is just the one meter's
        total, which is not a benefit and reads as one.
        """
        return bool(
            self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
            and self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)
        )

    def _is_steering(self) -> bool:
        """True while the pilot actually drives the battery.

        Meter deltas are only the integration's doing when the master switch is
        on and dry-run is off. Counting them regardless credited the inverter's
        own self-consumption to the pilot: a battery cycling by itself reported
        several euros a day of "actual savings" from an integration that had
        not called a single script.
        """
        return self.enabled and not self.dry_run

    @property
    def last_applied(self) -> str | None:
        """Mode last really sent to the inverter, restored across restarts."""
        return self._last_applied

    @last_applied.setter
    def last_applied(self, action: str | None) -> None:
        """Record a sample on every mode change, whoever makes it.

        A switch inside a coordinator interval has to split that interval;
        otherwise the new mode is back-dated over energy that moved under the
        old one. Sampling here rather than in the executor means no caller can
        forget to do it.
        """
        changed = action != self._last_applied
        self._last_applied = action
        if changed and self.data is not None:
            self._note_conditions(self.data.plan, dt_util.now())

    @callback
    def note_conditions(self) -> None:
        """Sample at a slot boundary.

        Slots can be 15 minutes while the coordinator refreshes every 30, so
        without this a price change inside an interval would be missed and the
        whole interval billed at the price that happened to start it.
        """
        if self.data is not None:
            self._note_conditions(self.data.plan, dt_util.now())

    def _note_conditions(self, plan: Plan, now: datetime) -> None:
        """Append (time, price, action) unless nothing changed since the last one.

        Only the savings accounting reads these, and only it trims them - so
        without meters configured nothing would ever clear the list.
        """
        if not self._has_energy_entities():
            return
        sample = (now.timestamp(), self._price_for_now(plan, now), self.last_applied)
        if self._conditions and self._conditions[-1][1:] == sample[1:]:
            return
        self._conditions.append(sample)
        # Safety net: a meter stuck at unavailable stops the trimming below.
        # Keeping a day of quarter-hourly samples is ample for one interval.
        if len(self._conditions) > MAX_CONDITION_SAMPLES:
            del self._conditions[:-MAX_CONDITION_SAMPLES]

    def _trim_conditions(self, before: float) -> None:
        """Drop samples fully superseded by the one covering `before`."""
        keep = 0
        for i, (ts, _, _) in enumerate(self._conditions):
            if ts <= before:
                keep = i
            else:
                break
        del self._conditions[:keep]

    def _interval_prices(self, start: datetime, end: datetime) -> IntervalPrices:
        """Time-weighted unit prices over the span.

        Each sample holds until the next one, so a span crossing a slot
        boundary or a mode switch is split at the right instant.
        """
        if not self._conditions:
            return IntervalPrices(0.0, 0.0)

        start_ts, end_ts = start.timestamp(), end.timestamp()
        charge_w = discharge_w = total = 0.0
        for i, (ts, price, action) in enumerate(self._conditions):
            nxt = self._conditions[i + 1][0] if i + 1 < len(self._conditions) else float("inf")
            span = min(nxt, end_ts) - max(ts, start_ts)
            if span <= 0:
                continue
            charge_w += self._charge_unit_price(price, action) * span
            discharge_w += self._discharge_unit_price(price, action) * span
            total += span
        if total <= 0:
            # Degenerate span (two reads at the same instant): the freshest
            # sample is the best available answer.
            _, price, action = self._conditions[-1]
            return IntervalPrices(
                self._charge_unit_price(price, action),
                self._discharge_unit_price(price, action),
            )
        return IntervalPrices(charge_w / total, discharge_w / total)

    def _update_actual_savings(self, plan: Plan, now: datetime) -> None:
        """Read energy meter deltas and accumulate actual savings."""
        # Sample first: this closes the interval that is about to be settled
        # and opens the next one at the current price and mode.
        self._note_conditions(plan, now)
        charge_entity = self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
        discharge_entity = self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)
        # Mirror _has_energy_entities(): with one meter the accumulators would
        # fill with a one-sided total that poisons the figures for good once
        # the second meter is added later.
        if not (charge_entity and discharge_entity):
            return

        cur_charge = self._read_energy_kwh(charge_entity) if charge_entity else None
        cur_discharge = self._read_energy_kwh(discharge_entity) if discharge_entity else None

        if charge_entity and cur_charge is None:
            return
        if discharge_entity and cur_discharge is None:
            return

        delta_charge = 0.0
        delta_discharge = 0.0
        if charge_entity:
            if self._prev_charge_kwh is None:
                self._prev_charge_kwh = cur_charge
            else:
                delta_charge = max(0.0, cur_charge - self._prev_charge_kwh)
                self._prev_charge_kwh = cur_charge
        if discharge_entity:
            if self._prev_discharge_kwh is None:
                self._prev_discharge_kwh = cur_discharge
            else:
                delta_discharge = max(0.0, cur_discharge - self._prev_discharge_kwh)
                self._prev_discharge_kwh = cur_discharge

        if delta_charge == 0.0 and delta_discharge == 0.0:
            self._prev_savings_at = now
            self._trim_conditions(now.timestamp())
            return

        if not self._is_steering():
            # The meter baselines above are still advanced, so switching the
            # pilot on later books the next real delta only - not everything
            # that moved while it was off. Just the accounting pauses.
            self._prev_savings_at = now
            self._trim_conditions(now.timestamp())
            return

        self._acc_charge_kwh += delta_charge
        self._acc_discharge_kwh += delta_discharge
        interval_start = self._prev_savings_at or now
        unit = self._interval_prices(interval_start, now)
        self._acc_savings_eur += delta_discharge * unit.discharge - delta_charge * unit.charge
        self._prev_savings_at = now
        self._trim_conditions(now.timestamp())

        self.schedule_persist()

    def _price_for_now(self, plan: Plan, now: datetime) -> float:
        """Price of the plan slot covering `now`."""
        return next((slot.price for slot in plan.slots if slot.covers(now)), 0.0)

    def _export_value(self, import_price: float) -> float:
        """What a kWh handed to the grid is worth: feed-in, else market price.

        Mirrors the optimizer's `_export_sell_price`: with no feed-in tariff
        the export earns the raw market price, so the import surcharge baked
        into the slot price has to come off again.
        """
        feed_in = float(self.conf(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF))
        if feed_in > 0:
            return feed_in
        return import_price - float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET))

    def _charge_unit_price(self, import_price: float, action: str | None) -> float:
        """Grid charge costs the import price; PV charge costs the feed-in.

        `action` is the mode that was in force while the energy moved, not the
        one in force when the meter happened to be read - the executor has
        usually moved on to the next slot by then.
        """
        if action == ACTION_CHARGE:
            return import_price
        if action is None:
            # No mode on record yet: the first interval after the pilot is
            # switched on, or a restart before its first script call. Assume
            # the expensive case - valuing unknown charge at the feed-in tariff
            # would book grid energy at a fraction of what it cost.
            return import_price
        return self._export_value(import_price)

    def _discharge_unit_price(self, import_price: float, action: str | None) -> float:
        """Self-consumption avoids an import; an export slot only earns the sale.

        Crediting exported energy at the import price would book German
        household levies and taxes as income - three to four times what the
        grid actually pays for it.
        """
        if action == ACTION_EXPORT:
            return self._export_value(import_price)
        return import_price

    def _store_payload(self) -> dict[str, Any]:
        return {
            "model": self.forecaster.to_dict(),
            "trained_at": self._last_training.isoformat() if self._last_training else None,
            "last_applied": self.last_applied,
            "charge_rate": self.charge_model.to_dict(),
            "savings": {
                "savings_eur": self._acc_savings_eur,
                "charge_kwh": self._acc_charge_kwh,
                "discharge_kwh": self._acc_discharge_kwh,
            },
        }

    @callback
    def schedule_persist(self) -> None:
        """Queue a store write; batches the twice-hourly savings updates."""
        self._store.async_delay_save(self._store_payload, STORE_SAVE_DELAY_SECONDS)

    async def async_persist(self) -> None:
        """Write the store right away (model retraining, executor changes)."""
        await self._store.async_save(self._store_payload())
