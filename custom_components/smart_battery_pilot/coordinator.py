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
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_AUTO,
    ACTION_CHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    CONF_BACKUP_RESERVE,
    CONF_BACKUP_RESERVE_ENTITY,
    CONF_BATTERY_CHARGE_ENERGY_ENTITY,
    CONF_BATTERY_DISCHARGE_ENERGY_ENTITY,
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_DERATING,
    CONF_CONSUMPTION_ENTITY,
    CONF_DISCHARGE_MODE,
    CONF_DRY_RUN,
    CONF_EFFICIENCY,
    CONF_EV_BATTERY_POLICY,
    CONF_EV_POWER_ENTITIES,
    CONF_EV_SOURCE,
    CONF_EV_THRESHOLD_W,
    CONF_EVCC_URL,
    CONF_EXTERNAL_CONTROL_ENTITY,
    CONF_FEED_IN_TARIFF,
    CONF_GRID_IMPORT_LIMIT_W,
    CONF_HAS_HEAT_PUMP,
    CONF_MAX_CHARGE_POWER_W,
    CONF_MAX_DISCHARGE_POWER_W,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PRICE_ENTITY,
    CONF_PRICE_OFFSET,
    CONF_PV_FORECAST_ENTRIES,
    CONF_PV_FORECAST_TODAY,
    CONF_PV_FORECAST_TOMORROW,
    CONF_PV_POWER_ENTITY,
    CONF_RESERVE_BLOCK_DISCHARGE,
    CONF_RESERVE_REFILL_HOURS,
    CONF_SOC_ENTITY,
    CONF_SPREAD_THRESHOLD,
    CONF_TEMPERATURE_ENTITY,
    CONF_TRAINING_DAYS,
    CURRENCY_DOCS_URL,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_CHARGE_DERATING,
    DEFAULT_DERATING_CURVE,
    DEFAULT_DISCHARGE_MODE,
    DEFAULT_DRY_RUN,
    DEFAULT_EFFICIENCY,
    DEFAULT_EV_BATTERY_POLICY,
    DEFAULT_EV_SOURCE,
    DEFAULT_EV_THRESHOLD_W,
    DEFAULT_FEED_IN_TARIFF,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_PRICE_OFFSET,
    DEFAULT_RESERVE_BLOCK_DISCHARGE,
    DEFAULT_RESERVE_REFILL_HOURS,
    DEFAULT_SPREAD_THRESHOLD,
    DEFAULT_TRAINING_DAYS,
    DERATING_CURVE_KEYS,
    DOMAIN,
    EV_POLICY_BLOCK,
    EV_POLICY_EXTERNAL,
    EV_SOURCE_EVCC,
    HEALTH_DOCS_URL,
    STORAGE_KEY,
    STORAGE_VERSION,
    STORE_SAVE_DELAY_SECONDS,
    UPDATE_INTERVAL_MINUTES,
)
from .currency import currency_from_attrs, currency_issue_id, hass_currency, price_scale
from .ev_sessions import (
    LIMIT_OFFSET_TOLERANCE,
    MIN_SESSION_KWH,
    EvForecast,
    derive,
    limit_on_price_basis,
    per_slot,
)
from .evcc import EvccError, async_fetch_plans, async_fetch_state, normalize_url
from .forecast.charge_rate import (
    SOURCE_OFF,
    ChargeRateModel,
    ChargeSample,
    Curve,
    curve_from_percentages,
)
from .forecast.consumption import ConsumptionForecaster, TrainingSample
from .forecast.pv import HourlyPv, pv_kwh_for_slot
from .health import ALL_CHECKS, HealthMonitor, Observation, issue_id
from .optimizer import BatteryState, InputSlot, OptimizerConfig, Plan, PlanSlot, build_plan
from .price_adapters import detect_adapter
from .price_adapters.base import PriceSlot
from .pv_sources import async_fetch_hourly_pv
from .savings_ledger import PilotLedger

_LOGGER = logging.getLogger(__name__)

RETRAIN_INTERVAL = timedelta(hours=24)
# Backoff after a training run that produced nothing. Without it a consumption
# entity that has no long-term statistics (a template sensor without state
# class, say) makes every 30-minute update re-query weeks of history and log
# the same warning, forever.
RETRAIN_RETRY_INTERVAL = timedelta(hours=6)
# Price/mode samples kept for savings accounting; see _note_conditions.
MAX_CONDITION_SAMPLES = 200

# evcc is polled this often while it is the EV source; a re-plan follows only
# when the expected sessions changed. A fetch younger than EVCC_MAX_AGE is
# reused by a refresh instead of asking evcc again.
EVCC_POLL_INTERVAL = timedelta(seconds=60)
EVCC_MAX_AGE = timedelta(minutes=5)

# Above this, a parsed price is not a price but a unit error - cents or öre
# read as euros. The all-time EPEX spot record is well under 1 EUR/kWh, so
# anything past this is two orders of magnitude out. Planning on such a curve
# would make every hour look like a huge arbitrage opportunity and force-charge
# the battery from the grid around the clock, so the plan is refused instead.
# In euros; other currencies scale it by currency.price_scale.
MAX_PLAUSIBLE_PRICE_EUR_KWH = 10.0

# Fallback daylight window when sun.sun is unavailable.
DEFAULT_SUNRISE_HOUR = 6.0
DEFAULT_SUNSET_HOUR = 21.0

# Charge factor source while derating is on but the temperature cannot be read.
SOURCE_NO_TEMPERATURE = "no_temperature"

# States that mean "another controller has the battery": on for binary
# sensors, helpers and switches, plus evcc's battery mode codes. A list of
# active states, not of idle ones - integrations translate their states
# (evcc's readable mode sensor says "Unbekannt" in German), and a state nobody
# anticipated must not pause the pilot for good.
EXTERNAL_ACTIVE_STATES = frozenset({"on", "true", "hold", "charge", "holdcharge"})

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

# From here on a low charge rate is not the cold. A factor learned up here must
# not hide a battery that simply never takes what it is asked for.
COLD_BELOW_C = 15.0


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
class _LedgerMark:
    """Meters, mode and price at the start of a pilot-ledger segment."""

    at: datetime
    charge_kwh: float
    discharge_kwh: float
    action: str | None
    price: float
    demand_kw: float  # forecast net demand rate of the slot, for idle holds
    # The battery is held for a car (EV block or another controller), not
    # for arbitrage - nothing the pilot gets credit for.
    ev_hold: bool = False


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
    battery_gross_eur: float | None = None
    pilot_savings_eur: float | None = None
    pv_power_w: float | None = None
    pv_power_entity: str | None = None
    # What the planner was fed, for the diagnostics dump: a plan is only
    # explainable next to the SOC, prices and limits it was built from.
    inputs: dict[str, Any] | None = None
    # Planned EV sessions; ev_source stays None while no EV source is set.
    ev_source: str | None = None
    ev_sessions: list[dict[str, Any]] | None = None
    ev_kwh_total: float = 0.0
    # evcc's cheap-price limit on this plan's price basis, or None when the
    # two price series are not a constant offset apart.
    ev_price_limit: float | None = None
    # The same limit as evcc states it, against evcc's own prices.
    ev_price_limit_evcc: float | None = None
    # "ok" while evcc answers, "offline" otherwise; with each loadpoint's
    # state (no names), so the card can say why nothing is planned.
    ev_status: str | None = None
    ev_loadpoints: list[dict[str, Any]] | None = None
    # What each plan slot's EV energy comes from (plan, smart_cost, now,
    # charging), aligned with plan.slots.
    ev_slot_sources: list[str | None] | None = None


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
        # Hourly PV entries that delivered nothing last cycle, so a lasting
        # outage is reported once rather than every refresh.
        self._pv_hourly_failed: list[str] = []
        # Read once: Home Assistant's currency changing under a running entry
        # would relabel totals that were counted in the old one. A reload
        # picks up a new setting.
        self.currency = hass_currency(hass)
        if price_scale(self.currency) is None:
            _LOGGER.warning(
                "Currency %s has no known price scale: prices are used as they "
                "come, but the check for implausible prices is off and "
                "defaults are euro-sized - review the spread and feed-in.",
                self.currency,
            )
        # What the price entity itself declares, for diagnostics, and the
        # mismatch already warned about.
        self._price_currency: str | None = None
        self._warned_currency_mismatch: str | None = None
        self._unsub_reserve = None
        # Reserve entity already warned about as unreadable; reset once it reads.
        self._warned_reserve_entity = False
        # Reserve (whole %) last handed to a control script, persisted: after a
        # restart only a changed reserve has to reach the inverter again.
        self.last_reserve_sent: int | None = None

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
        # Discharge valued at the grid price only - no PV opportunity cost.
        self._acc_gross_eur: float = 0.0
        # What the pilot itself saved; see savings_ledger.
        self.pilot_ledger = PilotLedger()
        self._ledger_mark: _LedgerMark | None = None
        self._acc_charge_kwh: float = 0.0
        self._acc_discharge_kwh: float = 0.0

        # Planned EV sessions from evcc: the last good fetch (time, state,
        # plan power per loadpoint), why the last one failed, and the session
        # signature the current plan was built with.
        self._evcc_fetch: tuple[datetime, dict[str, Any], dict[int, dict[str, Any]]] | None = None
        self._evcc_error: str | None = None
        self._ev_signature: tuple | None = None
        self._unsub_evcc_poll = None
        self._unsub_started = None
        self._unsub_soc = None
        # Set by the executor while the battery is held for a car.
        self._ev_hold_active = False

        # What the plan counts on against what really happens. Its repairs stay
        # untouched until the switches restored their state: with the defaults
        # of a fresh start, a repair the user chose to ignore would be deleted
        # and come back.
        self.health = HealthMonitor()
        self._health_started = False

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
            self.last_reserve_sent = stored.get("last_reserve_sent")

        if stored and stored.get("savings"):
            sv = stored["savings"]
            self._acc_savings_eur = float(sv.get("savings_eur", 0.0))
            self._acc_gross_eur = float(sv.get("gross_eur", 0.0))

        if stored and stored.get("pilot_ledger"):
            self.pilot_ledger = PilotLedger.from_dict(stored["pilot_ledger"])
            self._acc_charge_kwh = float(sv.get("charge_kwh", 0.0))
            self._acc_discharge_kwh = float(sv.get("discharge_kwh", 0.0))

        if stored and stored.get("charge_rate"):
            try:
                self.charge_model = ChargeRateModel.from_dict(
                    stored["charge_rate"], self._derating_curve()
                )
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not restore charge rate observations: %s", err)

        if stored and stored.get("health"):
            try:
                self.health = HealthMonitor.from_dict(stored["health"])
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not restore the consistency checks: %s", err)

        price_entity = self.conf(CONF_PRICE_ENTITY)
        if price_entity:
            self._unsub_price = async_track_state_change_event(
                self.hass, [price_entity], self._handle_price_update
            )
        reserve_entity = self.conf(CONF_BACKUP_RESERVE_ENTITY)
        if reserve_entity:
            self._unsub_reserve = async_track_state_change_event(
                self.hass, [reserve_entity], self._handle_reserve_update
            )
        soc_entity = self.conf(CONF_SOC_ENTITY)
        if soc_entity:
            self._unsub_soc = async_track_state_change_event(
                self.hass, [soc_entity], self._handle_soc_update
            )
        if self.ev_source() == EV_SOURCE_EVCC:
            self._unsub_evcc_poll = async_track_time_interval(
                self.hass, self._async_poll_evcc, EVCC_POLL_INTERVAL
            )
        if self._pv_forecast_entries() and not self.hass.is_running:
            # Forecast integrations often finish loading after us; the first
            # plan then runs on the daily sensors. Plan again once they are up.
            self._unsub_started = async_at_started(self.hass, self._handle_started)

    async def async_shutdown(self) -> None:
        if self._unsub_price:
            self._unsub_price()
            self._unsub_price = None
        if self._unsub_reserve:
            self._unsub_reserve()
            self._unsub_reserve = None
        if self._unsub_soc:
            self._unsub_soc()
            self._unsub_soc = None
        if self._unsub_evcc_poll:
            self._unsub_evcc_poll()
            self._unsub_evcc_poll = None
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None
        await super().async_shutdown()

    @callback
    def _handle_soc_update(self, _event) -> None:
        """Plan as soon as the SOC reads, when that is all the plan waited for.

        Right after a restart the battery integration often comes up after
        this one; without this the pilot sat on "no SOC" until the next
        30-minute refresh.
        """
        if (
            self.data is not None
            and not self.data.valid
            and self.data.error == "soc_unavailable"
            and self.live_soc() is not None
        ):
            _LOGGER.debug("SOC readable again - requesting a re-plan")
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _handle_reserve_update(self, _event) -> None:
        """The reserve floor moved: re-plan, and the executor re-sends it."""
        _LOGGER.debug("Reserve entity changed - requesting a re-plan")
        self.hass.async_create_task(self.async_request_refresh())

    async def _handle_started(self, _hass) -> None:
        self._unsub_started = None
        await self.async_request_refresh()

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
        pv_hourly = await self._hourly_pv()

        sunrise_hour, sunset_hour = self._daylight_window()
        factor, factor_source, battery_temperature = self.charge_factor()
        reserve, reserve_source, reserve_entity_value = self.reserve_state()
        refill_hours = float(self.conf(CONF_RESERVE_REFILL_HOURS, DEFAULT_RESERVE_REFILL_HOURS))
        _LOGGER.debug(
            "Planning inputs: %d price slots %s .. %s via '%s', SOC %.1f%%, "
            "temperature %s, PV forecast today %s / tomorrow %s kWh, "
            "daylight %.2f-%.2f h, model %s (%d samples), "
            "charge factor %.3f (%s) at battery %s °C, reserve %s %% (%s)",
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
            factor,
            factor_source,
            battery_temperature,
            reserve,
            reserve_source,
        )

        ev_forecast = await self._ev_forecast(now)
        ev_per_slot = (
            per_slot(ev_forecast.windows, [(s.start, s.end) for s in slots])
            if ev_forecast is not None
            else [(0.0, None)] * len(slots)
        )
        input_slots: list[InputSlot] = []
        consumption_24h = 0.0
        pv_24h = 0.0
        hourly_slots = 0
        for index, slot in enumerate(slots):
            # Hour of day and weekday are read in local time: a price source
            # that stamps its slots in UTC would otherwise shift the
            # consumption profile and the PV curve by the UTC offset.
            local_start = slot.start.astimezone(now.tzinfo)
            consumption = self.forecaster.predict_kwh(local_start, slot.hours, temperature)
            if pv_hourly is not None and pv_hourly.covers(slot.start, slot.end):
                pv = pv_hourly.kwh(slot.start, slot.end)
                hourly_slots += 1
            else:
                pv = pv_kwh_for_slot(
                    local_start,
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
            ev_kwh = ev_per_slot[index][0]
            input_slots.append(
                InputSlot(
                    price_slot=slot,
                    net_demand_kwh=consumption - pv,
                    pv_kwh=pv,
                    ev_kwh=ev_kwh,
                    battery_blocked=self._ev_blocks_battery(ev_kwh, slot.hours, ev_forecast),
                )
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
            reserve_soc=reserve,
        )
        config = OptimizerConfig(
            spread_threshold=float(self.conf(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD)),
            discharge_mode=self.conf(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            feed_in_tariff=float(self.conf(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF)),
            price_offset=float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET)),
            reserve_refill_hours=refill_hours,
            ev_no_grid_charge=ev_forecast is not None and self.ev_policy() == EV_POLICY_EXTERNAL,
            grid_import_limit_w=self._grid_import_limit_w(),
        )

        plan = await self.hass.async_add_executor_job(build_plan, input_slots, battery, config)
        if "export_spread_unreachable" in plan.warnings:
            _LOGGER.warning(
                "Export mode is on but no slot's sell price beats the spread "
                "(feed-in %.3f %s/kWh, spread %.3f %s/kWh). Set feed-in to 0 "
                "for market-price export, or lower the spread.",
                config.feed_in_tariff,
                self.currency,
                config.spread_threshold,
                self.currency,
            )
        if "reserve_refill_incomplete" in plan.warnings:
            # Once per episode: it stays true for every refresh until the
            # battery catches up, and a warning twice an hour buries the log.
            previous = self.data.plan.warnings if self.data is not None else []
            _LOGGER.log(
                logging.DEBUG if "reserve_refill_incomplete" in previous else logging.WARNING,
                "The backup reserve of %.0f %% cannot be refilled within %.0f h at the "
                "current charge limits; charging what fits.",
                reserve,
                refill_hours,
            )
        if "plan_worse_than_baseline" in plan.warnings:
            _LOGGER.warning(
                "The optimized plan came out worse than leaving the inverter "
                "alone, so this cycle runs the all-auto plan instead. This "
                "should not happen - please report it at %s",
                "https://github.com/cygnusb/ha-smart-battery-pilot/issues",
            )

        self._update_actual_savings(plan, now)
        self.health.plan_result(now, None)
        self.sync_health_issues()

        pv_entity = self.conf(CONF_PV_POWER_ENTITY)
        pv_power = self._read_float_state(pv_entity) if pv_entity else None

        prices = [slot.price for slot in slots]
        inputs = {
            "soc": soc,
            "temperature": temperature,
            "pv_forecast_today_kwh": pv_today,
            "pv_forecast_tomorrow_kwh": pv_tomorrow,
            "pv_hourly": {
                "entries": self._pv_forecast_entries(),
                "failed": list(self._pv_hourly_failed),
                "slots": hourly_slots,
            },
            "daylight_hours": [round(sunrise_hour, 2), round(sunset_hour, 2)],
            "slots": len(slots),
            "horizon_start": slots[0].start.isoformat(),
            "horizon_end": slots[-1].end.isoformat(),
            "currency": self.currency,
            "price_currency": self._price_currency,
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
            "reserve": {
                "soc": reserve,
                "source": reserve_source,
                "entity_value": reserve_entity_value,
                "refill_hours": refill_hours,
                "block": self.reserve_block_enabled(),
            },
            "ev": self.ev_inputs(ev_forecast),
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
            **self._ev_fields(ev_forecast, slots, ev_per_slot),
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
        self.health.plan_result(now, error)
        self.sync_health_issues()
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
                "battery_gross_eur": None,
                "pilot_savings_eur": None,
            }
        return {
            "battery_gross_eur": round(self._acc_gross_eur, 3),
            "pilot_savings_eur": round(self.pilot_ledger.savings_eur, 3),
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
            "Adapter '%s' parsed %d slots from %s (attributes: %s), offset %.4f %s/kWh",
            adapter.name,
            len(slots),
            entity_id,
            sorted(attrs),
            offset,
            self.currency,
        )
        self._check_price_currency(attrs, entity_id)
        if offset:
            slots = [PriceSlot(start=s.start, end=s.end, price=s.price + offset) for s in slots]
        self._reject_implausible_prices(slots, entity_id, adapter.name)
        return slots

    def _check_price_currency(self, attrs: dict[str, Any], entity_id: str) -> None:
        """Raise a repair when the price entity counts in another currency.

        Nothing is converted, so a curve in SEK under a Home Assistant set to
        EUR plans correctly but is labelled - and sized by every default and
        bound - as euros. Planning goes on; a log line alone went unseen, so
        the mismatch also sits under Settings > Repairs until it is resolved.
        """
        declared = currency_from_attrs(attrs)
        self._price_currency = declared
        if declared is None or declared == self.currency:
            self._warned_currency_mismatch = None
            # Unconditionally: an issue raised before a restart outlives the
            # flag that remembered it.
            ir.async_delete_issue(self.hass, DOMAIN, currency_issue_id(self.entry.entry_id))
            return
        if declared == self._warned_currency_mismatch:
            return
        self._warned_currency_mismatch = declared
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            currency_issue_id(self.entry.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="currency_mismatch",
            translation_placeholders={
                "entity": entity_id,
                "sensor_currency": declared,
                "ha_currency": self.currency,
            },
            learn_more_url=CURRENCY_DOCS_URL,
        )
        _LOGGER.warning(
            "%s delivers prices in %s, but Home Assistant's currency is %s. Prices "
            "are not converted: set the currency under Settings > System > General "
            "to the one your tariff is billed in, then reload the integration.",
            entity_id,
            declared,
            self.currency,
        )

    def _reject_implausible_prices(
        self, slots: list[PriceSlot], entity_id: str, adapter_name: str
    ) -> None:
        """Refuse a price curve that cannot be a price per kWh.

        The adapters convert cents and öre where the entity says so, but a
        source that labels its unit wrongly - or a generic one the catch-all
        adapter picked up - still lands here. Better no plan than a plan built
        on prices a hundred times too large. A currency without a known scale
        has no bound to check against.
        """
        scale = price_scale(self.currency)
        if not slots or scale is None:
            return
        worst = max(abs(slot.price) for slot in slots)
        limit = MAX_PLAUSIBLE_PRICE_EUR_KWH * scale
        if worst <= limit:
            return
        raise PriceImplausible(
            f"{entity_id} yields prices up to {worst:.2f} {self.currency}/kWh via the "
            f"'{adapter_name}' adapter; more than {limit:.0f} {self.currency}/kWh is "
            "not a real tariff. Check the entity's unit_of_measurement (cents/öre are "
            "converted only when declared) and Home Assistant's currency setting."
        )

    def _pv_forecast_entries(self) -> list[str]:
        return [e for e in self.conf(CONF_PV_FORECAST_ENTRIES) or [] if isinstance(e, str)]

    async def _hourly_pv(self) -> HourlyPv | None:
        """The hourly PV curve of the configured forecast entries, if usable.

        None means "plan with the daily sensors": nothing is configured, or
        one of the entries delivered no curve this cycle.
        """
        entry_ids = self._pv_forecast_entries()
        if not entry_ids:
            self._pv_hourly_failed = []
            return None
        fetch = await async_fetch_hourly_pv(self.hass, entry_ids)
        if fetch.failed and not self.hass.is_running:
            # Still starting: the forecast integration may simply not be
            # loaded yet. _handle_started plans again once it is.
            _LOGGER.debug("Hourly solar forecast not ready yet: %s", ", ".join(fetch.failed))
            return None
        if fetch.failed and fetch.failed != self._pv_hourly_failed:
            _LOGGER.warning(
                "No hourly solar forecast from config entr%s %s - planning with "
                "the daily PV forecast sensors until it is back",
                "y" if len(fetch.failed) == 1 else "ies",
                ", ".join(fetch.failed),
            )
        elif not fetch.failed and self._pv_hourly_failed:
            _LOGGER.info("Hourly solar forecast is back")
        self._pv_hourly_failed = fetch.failed
        return fetch.pv

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
            value = float(state.state)
        except ValueError:
            return None
        # "nan" and "inf" parse, and would poison every min/max downstream -
        # a nan reserve clamped to max_soc charged the battery full.
        return value if math.isfinite(value) else None

    def live_soc(self) -> float | None:
        """The SOC entity right now, not as of the last refresh."""
        return self._read_float_state(self.conf(CONF_SOC_ENTITY))

    def reserve_block_enabled(self) -> bool:
        return bool(self.conf(CONF_RESERVE_BLOCK_DISCHARGE, DEFAULT_RESERVE_BLOCK_DISCHARGE))

    def reserve_state(self) -> tuple[float | None, str, float | None]:
        """(active reserve % or None, source, raw entity value).

        The entity, while it reads a number, replaces the fixed value; either
        is clamped to the SOC window, and a reserve at or below min_soc is off.
        """
        min_soc = float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        max_soc = float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC))
        entity_id = self.conf(CONF_BACKUP_RESERVE_ENTITY)
        entity_value = self._read_float_state(entity_id) if entity_id else None
        if entity_id and entity_value is None:
            if not self._warned_reserve_entity:
                self._warned_reserve_entity = True
                _LOGGER.warning(
                    "Reserve entity %s is unavailable - using the fixed reserve "
                    "until it reads again",
                    entity_id,
                )
            else:
                _LOGGER.debug("Reserve entity %s still unavailable", entity_id)
        elif entity_value is not None:
            self._warned_reserve_entity = False

        if entity_value is not None:
            value, source = entity_value, "entity"
        else:
            value, source = float(self.conf(CONF_BACKUP_RESERVE, DEFAULT_BACKUP_RESERVE)), "fixed"
        clamped = max(min_soc, min(max_soc, value))
        if clamped != value:
            _LOGGER.debug("Reserve %.1f %% clamped to the SOC window: %.1f %%", value, clamped)
        if clamped <= min_soc:
            return None, "off", entity_value
        return clamped, source, entity_value

    def reserve_for_scripts(self) -> int:
        """reserve_soc for every script call: the reserve, or min_soc when off."""
        reserve, _, _ = self.reserve_state()
        if reserve is None:
            reserve = float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        return round(reserve)

    # --- EV charging and other controllers ------------------------------------------

    def ev_power_entities(self) -> list[str]:
        entities = self.conf(CONF_EV_POWER_ENTITIES) or []
        if isinstance(entities, str):
            entities = [entities]
        return [entity_id for entity_id in entities if entity_id]

    def ev_policy(self) -> str:
        return self.conf(CONF_EV_BATTERY_POLICY) or DEFAULT_EV_BATTERY_POLICY

    def ev_threshold_w(self) -> float:
        return float(self.conf(CONF_EV_THRESHOLD_W) or DEFAULT_EV_THRESHOLD_W)

    def _read_power_w(self, entity_id: str) -> float | None:
        """A power sensor in W, whatever of W or kW it reports in."""
        value = self._read_float_state(entity_id)
        if value is None:
            return None
        state = self.hass.states.get(entity_id)
        unit = str(state.attributes.get("unit_of_measurement") or "") if state else ""
        return value * 1000.0 if unit == "kW" else value

    def ev_power_w(self) -> float | None:
        """What all configured wallboxes draw right now, or None without a reading."""
        readings = [self._read_power_w(entity_id) for entity_id in self.ev_power_entities()]
        known = [max(0.0, value) for value in readings if value is not None]
        return sum(known) if known else None

    def external_control_active(self) -> bool:
        """True while another controller (evcc, an automation ...) has the battery.

        An entity that is unavailable counts as inactive: the pilot would rather
        steer than leave the battery to a controller nobody can see.
        """
        entity_id = self.conf(CONF_EXTERNAL_CONTROL_ENTITY)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return str(state.state).strip().lower() in EXTERNAL_ACTIVE_STATES

    def ev_inputs(self, forecast: EvForecast | None = None) -> dict[str, Any]:
        """The EV section of the diagnostics and sensor attributes."""
        power = self.ev_power_w()
        inputs: dict[str, Any] = {
            "policy": self.ev_policy(),
            "power_entities": self.ev_power_entities(),
            "power_w": round(power) if power is not None else None,
            "threshold_w": self.ev_threshold_w(),
            "external_control_entity": self.conf(CONF_EXTERNAL_CONTROL_ENTITY),
            "external_control_active": self.external_control_active(),
            "source": self.ev_source(),
        }
        if self.ev_source() == EV_SOURCE_EVCC:
            fetched = self._evcc_fetch
            inputs["evcc"] = {
                "status": "ok" if forecast is not None else "offline",
                "error": self._evcc_error,
                "fetched_at": fetched[0].isoformat() if fetched else None,
                "version": fetched[1].get("version") if fetched else None,
                "battery_discharge_control": forecast.battery_discharge_control
                if forecast
                else None,
                "price_limit": forecast.price_limit if forecast else None,
                # No vehicle names or loadpoint titles: dumps get posted publicly.
                "loadpoints": forecast.loadpoints if forecast else [],
                "sessions": len(forecast.sessions) if forecast else 0,
            }
        return inputs

    def _grid_import_limit_w(self) -> float | None:
        value = self.conf(CONF_GRID_IMPORT_LIMIT_W)
        try:
            limit = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
        return limit if limit and limit > 0 else None

    def ev_source(self) -> str:
        return self.conf(CONF_EV_SOURCE) or DEFAULT_EV_SOURCE

    def _evcc_url(self) -> str | None:
        url = self.conf(CONF_EVCC_URL)
        return normalize_url(str(url)) if url else None

    async def _async_fetch_evcc(self, now: datetime) -> bool:
        """Fetch evcc's state; False (and the reason kept) when it failed."""
        url = self._evcc_url()
        if not url:
            self._evcc_error = "no_url"
            return False
        try:
            state = await async_fetch_state(self.hass, url)
            plans = await async_fetch_plans(self.hass, url, state)
        except EvccError as err:
            if self._evcc_error is None:
                _LOGGER.warning(
                    "evcc is not readable (%s) - planning without EV sessions until it is", err
                )
            else:
                _LOGGER.debug("evcc still not readable: %s", err)
            self._evcc_error = str(err)
            return False
        if self._evcc_error is not None:
            _LOGGER.info("evcc is readable again")
        self._evcc_error = None
        self._evcc_fetch = (now, state, plans)
        return True

    async def _ev_forecast(self, now: datetime) -> EvForecast | None:
        """Planned EV sessions, or None without a (readable) EV source."""
        if self.ev_source() != EV_SOURCE_EVCC:
            return None
        fresh = self._evcc_fetch is not None and now - self._evcc_fetch[0] < EVCC_MAX_AGE
        if not fresh and not await self._async_fetch_evcc(now):
            return None
        _, state, plans = self._evcc_fetch
        return derive(state, plans, now)

    def _ev_blocks_battery(self, ev_kwh: float, hours: float, forecast: EvForecast | None) -> bool:
        """Whether the EV policy will hold the battery in a planned EV slot.

        Mirrors what the live mechanisms do, or plan and inverter disagree:
        the wallbox block engages above its threshold only, and evcc holds
        whatever the power - if it is allowed to hold at all.
        """
        if forecast is None or ev_kwh < MIN_SESSION_KWH:
            return False
        policy = self.ev_policy()
        if policy == EV_POLICY_BLOCK:
            return hours > 0 and ev_kwh / hours * 1000.0 > self.ev_threshold_w()
        if policy == EV_POLICY_EXTERNAL:
            return forecast.battery_discharge_control is not False
        return False

    def _ev_fields(
        self,
        forecast: EvForecast | None,
        slots: list[PriceSlot],
        ev_per_slot: list[tuple[float, str | None]],
    ) -> dict[str, Any]:
        """Plan-level EV attributes; empty without an EV source."""
        if self.ev_source() != EV_SOURCE_EVCC:
            return {}
        sessions = []
        for session in forecast.sessions if forecast else []:
            sessions.append(
                {
                    "start": session.windows[0].start.isoformat(),
                    "end": session.windows[-1].end.isoformat(),
                    "energy_kwh": round(session.energy_kwh, 2),
                    "source": session.source,
                }
            )
        limit = None
        if forecast is not None and forecast.price_limit is not None:
            limit = limit_on_price_basis(
                forecast.price_limit,
                forecast.rates,
                [(s.start, s.end, s.price) for s in slots],
                tolerance=LIMIT_OFFSET_TOLERANCE * (price_scale(self.currency) or 1.0),
            )
        self._ev_signature = self._signature(slots)
        return {
            "ev_source": EV_SOURCE_EVCC,
            "ev_sessions": sessions,
            "ev_kwh_total": round(sum(kwh for kwh, _ in ev_per_slot), 2),
            "ev_price_limit": limit,
            "ev_price_limit_evcc": forecast.price_limit if forecast is not None else None,
            "ev_status": "ok" if forecast is not None else "offline",
            "ev_loadpoints": [
                {
                    "connected": lp["connected"],
                    "charging": lp["charging"],
                    "mode": lp["mode"],
                    "vehicle_soc": lp["vehicle_soc"],
                    "limit_soc": lp["effective_limit_soc"],
                }
                for lp in (forecast.loadpoints if forecast is not None else [])
            ],
            "ev_slot_sources": [source for _, source in ev_per_slot],
        }

    def _signature(self, slots: list[Any]) -> tuple:
        """The expected EV energy per slot, stable while a session runs.

        Windows keep their real start here, so the slot in progress counts
        in full instead of shrinking with every poll.
        """
        # A failed fetch plans without sessions; the last good one must not
        # bring them back, or every poll would ask for another re-plan.
        if self._evcc_fetch is None or self._evcc_error is not None or not slots:
            return ()
        _, state, plans = self._evcc_fetch
        forecast = derive(state, plans, dt_util.now(), trim=False)
        bounds = [(s.start, s.end) for s in slots]
        return tuple(round(kwh, 1) for kwh, _ in per_slot(forecast.windows, bounds))

    async def _async_poll_evcc(self, _now: datetime | None = None) -> None:
        """Re-plan when evcc's expected sessions changed since the plan was made."""
        if self.data is None or not self.data.valid:
            return
        if not await self._async_fetch_evcc(dt_util.now()):
            if self._ev_signature:
                self._ev_signature = ()
                self.hass.async_create_task(self.async_request_refresh())
            return
        signature = self._signature(self.data.plan.slots)
        if signature != self._ev_signature:
            _LOGGER.debug("evcc sessions changed - requesting a re-plan")
            self.hass.async_create_task(self.async_request_refresh())

    @property
    def ev_hold_active(self) -> bool:
        """The executor holds the battery for a car right now."""
        return self._ev_hold_active

    @ev_hold_active.setter
    def ev_hold_active(self, active: bool) -> None:
        # A change splits the ledger segment, like a mode change does.
        changed = active != self._ev_hold_active
        self._ev_hold_active = active
        if changed and self.data is not None:
            self._note_conditions(self.data.plan, dt_util.now())

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
        ev_entities = self.ev_power_entities()
        ids = [entity_id] + ([temp_entity] if temp_entity else []) + ev_entities

        try:
            stats = await self._fetch_statistics(start, now, ids)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Fetching statistics failed: %s", err)
            return

        ev_rows = {ev: stats.get(ev, []) for ev in ev_entities}
        for ev, rows in ev_rows.items():
            if not rows:
                _LOGGER.warning(
                    "No statistics found for wallbox sensor %s - its energy stays in "
                    "the consumption history. It needs a state_class to be recorded.",
                    ev,
                )
        samples = self._build_samples(
            stats.get(entity_id, []), stats.get(temp_entity or "", []), ev_rows
        )
        _LOGGER.debug(
            "Statistics since %s: %d consumption rows, %d temperature rows, "
            "%d EV rows -> %d samples",
            start.isoformat(),
            len(stats.get(entity_id, [])),
            len(stats.get(temp_entity or "", [])),
            sum(len(rows) for rows in ev_rows.values()),
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

    def _unit(self, entity_id: str | None) -> str | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.attributes.get("unit_of_measurement") if state else None

    @staticmethod
    def _row_kwh(row: dict[str, Any], unit: str | None) -> float | None:
        """One hourly statistics row as kWh.

        Power sensors (W, kW) provide `mean`; energy sensors provide `change`
        (kWh or Wh) per hour.
        """
        if unit in ("W", "kW"):
            mean = row.get("mean")
            if mean is None:
                return None
            return float(mean) / (1000.0 if unit == "W" else 1.0)
        change = row.get("change")
        if change is None or float(change) < 0:
            return None
        return float(change) / (1000.0 if unit == "Wh" else 1.0)

    def _build_samples(
        self,
        rows: list[dict[str, Any]],
        temp_rows: list[dict[str, Any]],
        ev_rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> list[TrainingSample]:
        """Convert recorder statistics rows into training samples.

        A whole-house meter counts the wallbox too, and a single charging
        session outweighs a day of household load. Whatever the configured EV
        sensors drew in an hour is taken off that hour, so the model learns the
        house alone - EV sessions follow the car, not the clock.
        """
        unit = self._unit(self.conf(CONF_CONSUMPTION_ENTITY))

        temps: dict[Any, float] = {}
        for row in temp_rows:
            if row.get("mean") is not None:
                temps[row["start"]] = float(row["mean"])

        ev_kwh: dict[Any, float] = {}
        for ev_entity, entity_rows in (ev_rows or {}).items():
            ev_unit = self._unit(ev_entity)
            for row in entity_rows:
                kwh = self._row_kwh(row, ev_unit)
                if kwh is not None and kwh > 0:
                    ev_kwh[row["start"]] = ev_kwh.get(row["start"], 0.0) + kwh

        samples: list[TrainingSample] = []
        for row in rows:
            start = row["start"]
            if isinstance(start, (int, float)):
                start_dt = dt_util.utc_from_timestamp(start)
            else:
                start_dt = start
            start_dt = dt_util.as_local(start_dt)

            kwh = self._row_kwh(row, unit)
            if kwh is None or kwh < 0 or kwh > 50:  # discard outliers
                continue
            if row["start"] in ev_kwh:
                # Meter resolution and averaging can leave the EV share a
                # little above the house total; the house never draws < 0.
                kwh = max(0.0, kwh - ev_kwh[row["start"]])
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

    # --- consistency checks ---------------------------------------------------------

    @callback
    def async_start_health(self) -> None:
        """The switches are restored: repairs may follow the checks from now on."""
        self._health_started = True
        self.sync_health_issues()

    @callback
    def note_script_result(self, action: str, entity_id: str | None, success: bool) -> None:
        """The executor reports every script call."""
        self.health.script_result(action, entity_id, success)
        self.sync_health_issues()
        self.schedule_persist()

    @callback
    def sync_health_issues(self) -> None:
        """Mirror the failed checks into Settings > Repairs."""
        if not self._health_started:
            return
        shown = self.health.issues(enabled=self.enabled, dry_run=self.dry_run)
        for check in ALL_CHECKS:
            key = issue_id(self.entry.entry_id, check)
            if check not in shown:
                ir.async_delete_issue(self.hass, DOMAIN, key)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                key,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=check,
                translation_placeholders=shown[check],
                learn_more_url=HEALTH_DOCS_URL,
            )

    def _observe_health(self, plan: Plan, now: datetime) -> None:
        slot = next((s for s in plan.slots if s.covers(now)), None)
        steering = self._is_steering()
        ev_power = self.ev_power_w()
        factor, _, temperature = self.charge_factor()
        reserve, _, _ = self.reserve_state()
        efficiency = float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)) / 100.0
        self.health.observe(
            Observation(
                at=now,
                day=dt_util.as_local(now).date().isoformat(),
                steering=steering,
                action=self.last_applied if steering else None,
                power_w=slot.power_w if slot else 0.0,
                soc=self.live_soc(),
                charge_kwh=self._read_energy_kwh(self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)),
                discharge_kwh=self._read_energy_kwh(
                    self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)
                ),
                pv_w=self._live_pv_w(),
                load_w=self._house_load_w(slot),
                no_pv_expected=slot is not None and slot.pv_kwh <= 0,
                ev_active=(ev_power is not None and ev_power > self.ev_threshold_w())
                or (slot is not None and slot.ev_kwh > 0),
                external=self.external_control_active(),
                pv_today_kwh=self._pv_today_for_health(now),
                capacity_kwh=float(self.conf(CONF_CAPACITY_KWH, 10.0)),
                floor_soc=(
                    reserve
                    if reserve is not None
                    else float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC))
                ),
                max_soc=float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC)),
                eta_one_way=math.sqrt(max(0.5, min(1.0, efficiency))),
                max_charge_w=float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000)),
                max_discharge_w=float(self.conf(CONF_MAX_DISCHARGE_POWER_W, 5000)),
                charge_factor=(
                    factor if temperature is not None and temperature < COLD_BELOW_C else 1.0
                ),
            )
        )
        self.schedule_persist()

    def _live_pv_w(self) -> float | None:
        entity_id = self.conf(CONF_PV_POWER_ENTITY)
        return self._read_power_w(entity_id) if entity_id else None

    def _house_load_w(self, slot: PlanSlot | None) -> float | None:
        """The house right now; the forecast if its sensor only counts energy."""
        entity_id = self.conf(CONF_CONSUMPTION_ENTITY)
        if entity_id and self._unit(entity_id) in ("W", "kW"):
            load = self._read_power_w(entity_id)
            return None if load is None else max(0.0, load - (self.ev_power_w() or 0.0))
        if slot is None:
            return None
        hours = (slot.end - slot.start).total_seconds() / 3600.0
        return max(0.0, slot.net_demand_kwh + slot.pv_kwh) / hours * 1000.0 if hours > 0 else None

    def _pv_today_for_health(self, now: datetime) -> float | None:
        """The daily forecast sensor while the sun is up, if the plan relies on it."""
        if self._pv_forecast_entries():
            return None
        sunrise, sunset = self._daylight_window()
        local = dt_util.as_local(now)
        if not sunrise <= local.hour + local.minute / 60.0 < sunset:
            return None
        return self._read_float_state(self.conf(CONF_PV_FORECAST_TODAY))

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
        self._observe_health(plan, now)
        if not self._has_energy_entities():
            return
        self._ledger_step(plan, now)
        sample = (now.timestamp(), self._price_for_now(plan, now), self._effective_action())
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

        # The battery benefit is the battery's worth, pilot or not - with the
        # pilot off, the inverter simply runs in its own (auto) mode. What the
        # pilot itself adds is the ledger's business.
        self._acc_charge_kwh += delta_charge
        self._acc_discharge_kwh += delta_discharge
        interval_start = self._prev_savings_at or now
        unit = self._interval_prices(interval_start, now)
        self._acc_savings_eur += delta_discharge * unit.discharge - delta_charge * unit.charge
        self._acc_gross_eur += delta_discharge * unit.discharge
        self._prev_savings_at = now
        self._trim_conditions(now.timestamp())

        self.schedule_persist()

    def _effective_action(self) -> str | None:
        """The mode the inverter is in: the pilot's, or auto while it is off."""
        return self.last_applied if self._is_steering() else ACTION_AUTO

    def _stored_kwh(self) -> float | None:
        """Energy in the battery above min SOC, from the live SOC."""
        soc = self._read_float_state(self.conf(CONF_SOC_ENTITY))
        if soc is None:
            return None
        min_soc = float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC))
        return max(0.0, (soc - min_soc) / 100.0 * float(self.conf(CONF_CAPACITY_KWH, 10.0)))

    def _ledger_step(self, plan: Plan, now: datetime) -> None:
        """Book the segment since the last mark on the pilot ledger.

        Runs at every slot boundary, mode change and refresh, so each segment
        has one mode and one price: grid charge in a charge slot and demand
        held back in an idle slot become lots; any discharge spends them.
        """
        charge = self._read_energy_kwh(self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY))
        discharge = self._read_energy_kwh(self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY))
        if charge is None or discharge is None:
            self._ledger_mark = None
            return
        slot = next((s for s in plan.slots if s.covers(now)), None)
        hours = (slot.end - slot.start).total_seconds() / 3600.0 if slot else 0.0
        mark = _LedgerMark(
            at=now,
            charge_kwh=charge,
            discharge_kwh=discharge,
            action=self._effective_action(),
            price=slot.price if slot else 0.0,
            demand_kw=max(0.0, slot.net_demand_kwh) / hours if slot and hours > 0 else 0.0,
            ev_hold=self._ev_hold_active,
        )
        prev, self._ledger_mark = self._ledger_mark, mark
        if prev is None:
            return
        d_charge = charge - prev.charge_kwh
        d_discharge = discharge - prev.discharge_kwh
        if d_charge < 0 or d_discharge < 0:
            _LOGGER.debug("Energy meter went backwards - pilot ledger skips this segment")
            return
        elapsed_h = max(0.0, (now - prev.at).total_seconds() / 3600.0)
        eta = math.sqrt(
            max(0.5, min(1.0, float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)) / 100.0))
        )
        stored = self._stored_kwh()
        if prev.action == ACTION_CHARGE and d_charge > 0:
            self.pilot_ledger.book_charge(d_charge, prev.price, eta)
        elif (
            prev.action == ACTION_IDLE and elapsed_h > 0 and stored is not None and not prev.ev_hold
        ):
            self.pilot_ledger.book_hold(prev.demand_kw * elapsed_h, prev.price, eta, stored)
        if d_discharge > 0:
            value = self._export_value(prev.price) if prev.action == ACTION_EXPORT else prev.price
            gain = self.pilot_ledger.book_discharge(d_discharge, value, eta)
            if gain:
                _LOGGER.debug(
                    "Pilot ledger: %.3f kWh discharged at %.4f -> %+.4f EUR",
                    d_discharge,
                    value,
                    gain,
                )
        if stored is not None:
            self.pilot_ledger.cap(stored)
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
            "last_reserve_sent": self.last_reserve_sent,
            "charge_rate": self.charge_model.to_dict(),
            "pilot_ledger": self.pilot_ledger.to_dict(),
            "health": self.health.to_dict(),
            "savings": {
                "savings_eur": self._acc_savings_eur,
                "gross_eur": self._acc_gross_eur,
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
