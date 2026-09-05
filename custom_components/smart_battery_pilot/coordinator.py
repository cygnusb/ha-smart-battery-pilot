"""Data update coordinator: prices -> forecast -> optimizer -> plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
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
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    STORE_SAVE_DELAY_SECONDS,
    UPDATE_INTERVAL_MINUTES,
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

# Fallback daylight window when sun.sun is unavailable.
DEFAULT_SUNRISE_HOUR = 6.0
DEFAULT_SUNSET_HOUR = 21.0


class PriceEntityUnavailable(UpdateFailed):
    """The configured price entity has no usable state."""


class PriceAdapterMissing(UpdateFailed):
    """No adapter recognises the price entity's attribute format."""


class PriceParseError(UpdateFailed):
    """The price attributes could not be turned into slots."""


class IntervalPrices(NamedTuple):
    """Unit prices (EUR/kWh) that applied while energy moved over a span."""

    charge: float  # what a kWh put into the battery cost
    discharge: float  # what a kWh taken out of it was worth


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
        self._acc_savings_eur: float = 0.0
        self._acc_charge_kwh: float = 0.0
        self._acc_discharge_kwh: float = 0.0

    # --- config helpers -------------------------------------------------------

    def conf(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, default))

    def opt(self, key: str, default: Any = None) -> Any:
        return self.entry.options.get(key, default)

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

        battery = BatteryState(
            capacity_kwh=float(self.conf(CONF_CAPACITY_KWH, 10.0)),
            soc=soc,
            min_soc=float(self.conf(CONF_MIN_SOC, DEFAULT_MIN_SOC)),
            max_soc=float(self.conf(CONF_MAX_SOC, DEFAULT_MAX_SOC)),
            max_charge_power_w=float(self.conf(CONF_MAX_CHARGE_POWER_W, 5000)),
            max_discharge_power_w=float(self.conf(CONF_MAX_DISCHARGE_POWER_W, 5000)),
            efficiency=float(self.conf(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)),
        )
        config = OptimizerConfig(
            spread_threshold=float(
                self.conf(CONF_SPREAD_THRESHOLD, DEFAULT_SPREAD_THRESHOLD)
            ),
            discharge_mode=self.conf(CONF_DISCHARGE_MODE, DEFAULT_DISCHARGE_MODE),
            feed_in_tariff=float(self.conf(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF)),
            price_offset=float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET)),
        )

        plan = await self.hass.async_add_executor_job(
            build_plan, input_slots, battery, config
        )
        if "export_spread_unreachable" in plan.warnings:
            _LOGGER.warning(
                "Export mode is on but no slot's sell price beats the spread "
                "(feed-in %.3f EUR/kWh, spread %.3f EUR/kWh). Set feed-in to 0 "
                "for market-price export, or lower the spread.",
                config.feed_in_tariff,
                config.spread_threshold,
            )

        self._update_actual_savings(plan, now)

        pv_entity = self.conf(CONF_PV_POWER_ENTITY)
        pv_power = self._read_float_state(pv_entity) if pv_entity else None

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
            **self._savings_fields(),
        )

    def _invalid_plan(self, now: datetime, error: str) -> SBPData:
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
        if offset:
            slots = [
                PriceSlot(start=s.start, end=s.end, price=s.price + offset)
                for s in slots
            ]
        return slots

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
        if unit and unit not in ("kWh", "kwh"):
            _LOGGER.warning(
                "Energy entity %s uses unit %s; expected kWh or Wh",
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
        if not samples:
            _LOGGER.warning(
                "No statistics found for %s - consumption forecast uses defaults",
                entity_id,
            )
            return

        require_temp = bool(self.conf(CONF_HAS_HEAT_PUMP, False))
        await self.hass.async_add_executor_job(
            self.forecaster.train, samples, require_temp
        )
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
            nxt = (
                self._conditions[i + 1][0]
                if i + 1 < len(self._conditions)
                else float("inf")
            )
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

        self._acc_charge_kwh += delta_charge
        self._acc_discharge_kwh += delta_discharge
        interval_start = self._prev_savings_at or now
        unit = self._interval_prices(interval_start, now)
        self._acc_savings_eur += (
            delta_discharge * unit.discharge - delta_charge * unit.charge
        )
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
