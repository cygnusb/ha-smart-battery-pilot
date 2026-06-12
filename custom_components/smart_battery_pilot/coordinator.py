"""Data update coordinator: prices -> forecast -> optimizer -> plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    UPDATE_INTERVAL_MINUTES,
)
from .forecast.consumption import ConsumptionForecaster, TrainingSample
from .forecast.pv import pv_kwh_for_slot
from .optimizer import BatteryState, InputSlot, OptimizerConfig, Plan, build_plan
from .price_adapters import detect_adapter
from .price_adapters.base import PriceSlot

_LOGGER = logging.getLogger(__name__)

RETRAIN_INTERVAL = timedelta(hours=24)


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
        self._unsub_price = None

        # Runtime flags controlled by the switch entities.
        self.enabled: bool = False
        self.dry_run: bool = self.opt(CONF_DRY_RUN, DEFAULT_DRY_RUN)

        # Actual savings tracking
        self._prev_charge_kwh: float | None = None
        self._prev_discharge_kwh: float | None = None
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
        try:
            slots = self._read_price_slots(now)
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001 - surface as invalid plan
            raise UpdateFailed(f"Price parsing failed: {err}") from err

        if not slots:
            return SBPData(plan=Plan(), valid=False, error="no_price_data", updated_at=now)

        soc = self._read_float_state(self.conf(CONF_SOC_ENTITY))
        if soc is None:
            return SBPData(plan=Plan(), valid=False, error="soc_unavailable", updated_at=now)

        await self._maybe_retrain(now)

        temperature = self._read_float_state(self.conf(CONF_TEMPERATURE_ENTITY))
        pv_today = self._read_float_state(self.conf(CONF_PV_FORECAST_TODAY))
        pv_tomorrow = self._read_float_state(self.conf(CONF_PV_FORECAST_TOMORROW))

        input_slots: list[InputSlot] = []
        consumption_24h = 0.0
        pv_24h = 0.0
        for slot in slots:
            consumption = self.forecaster.predict_kwh(slot.start, slot.hours, temperature)
            pv = pv_kwh_for_slot(slot.start, slot.hours, pv_today, pv_tomorrow, now)
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
        )

        plan = await self.hass.async_add_executor_job(
            build_plan, input_slots, battery, config
        )

        self._update_actual_savings(plan, now)

        actual_savings = self._acc_savings_eur if self._has_energy_entities() else None
        actual_charge = self._acc_charge_kwh if self._has_energy_entities() else None
        actual_discharge = self._acc_discharge_kwh if self._has_energy_entities() else None

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
            actual_savings_eur=round(actual_savings, 3) if actual_savings is not None else None,
            actual_charge_kwh=round(actual_charge, 2) if actual_charge is not None else None,
            actual_discharge_kwh=round(actual_discharge, 2) if actual_discharge is not None else None,
        )

    _adapter_name: str | None = None

    def _read_price_slots(self, now: datetime) -> list[PriceSlot]:
        entity_id = self.conf(CONF_PRICE_ENTITY)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in ("unavailable", "unknown"):
            raise UpdateFailed(f"Price entity {entity_id} unavailable")
        attrs = dict(state.attributes)
        adapter = detect_adapter(attrs)
        if adapter is None:
            raise UpdateFailed(f"No price adapter matches {entity_id}")
        self._adapter_name = adapter.name
        offset = float(self.conf(CONF_PRICE_OFFSET, DEFAULT_PRICE_OFFSET))
        slots = adapter.parse(attrs, now)
        if offset:
            slots = [
                PriceSlot(start=s.start, end=s.end, price=s.price + offset)
                for s in slots
            ]
        return slots

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

    # --- training -----------------------------------------------------------------

    async def _maybe_retrain(self, now: datetime) -> None:
        if self._last_training and now - self._last_training < RETRAIN_INTERVAL:
            return
        entity_id = self.conf(CONF_CONSUMPTION_ENTITY)
        if not entity_id:
            return
        days = int(self.conf(CONF_TRAINING_DAYS, DEFAULT_TRAINING_DAYS))
        start = now - timedelta(days=days)
        temp_entity = self.conf(CONF_TEMPERATURE_ENTITY)
        ids = [entity_id] + ([temp_entity] if temp_entity else [])

        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                now,
                set(ids),
                "hour",
                None,
                {"mean", "change"},
            )
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

        await self.hass.async_add_executor_job(self.forecaster.train, samples)
        self._last_training = now
        await self._persist_store(now)
        _LOGGER.debug(
            "Trained consumption model (%s) with %d samples",
            self.forecaster.model_type,
            len(samples),
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
        return bool(
            self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
            or self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)
        )

    def _update_actual_savings(self, plan: Plan, now: datetime) -> None:
        """Read energy meter deltas and accumulate actual savings."""
        charge_entity = self.conf(CONF_BATTERY_CHARGE_ENERGY_ENTITY)
        discharge_entity = self.conf(CONF_BATTERY_DISCHARGE_ENERGY_ENTITY)
        if not charge_entity and not discharge_entity:
            return

        cur_charge = self._read_float_state(charge_entity) or 0.0
        cur_discharge = self._read_float_state(discharge_entity) or 0.0

        if self._prev_charge_kwh is None or self._prev_discharge_kwh is None:
            # First reading — just store baseline, don't accumulate yet
            self._prev_charge_kwh = cur_charge
            self._prev_discharge_kwh = cur_discharge
            return

        delta_charge = max(0.0, cur_charge - self._prev_charge_kwh)
        delta_discharge = max(0.0, cur_discharge - self._prev_discharge_kwh)
        self._prev_charge_kwh = cur_charge
        self._prev_discharge_kwh = cur_discharge

        if delta_charge == 0.0 and delta_discharge == 0.0:
            return

        # Derive representative prices from past plan slots
        avg_charge_price = self._avg_price_for_action(plan, "charge", now)
        avg_discharge_price = self._avg_price_for_action_discharged(plan, now)

        savings = delta_discharge * avg_discharge_price - delta_charge * avg_charge_price
        self._acc_savings_eur += savings
        self._acc_charge_kwh += delta_charge
        self._acc_discharge_kwh += delta_discharge

        self.hass.async_create_task(self._persist_store(now))

    def _avg_price_for_action(self, plan: Plan, action: str, now: datetime) -> float:
        """Average price of slots with the given action in the plan horizon."""
        slots = [s for s in plan.slots if s.action == action]
        if not slots:
            return 0.0
        return sum(s.price for s in slots) / len(slots)

    def _avg_price_for_action_discharged(self, plan: Plan, now: datetime) -> float:
        """Average price during auto/export slots (where battery discharges)."""
        slots = [s for s in plan.slots if s.action in ("auto", "export")]
        if not slots:
            # Fallback: average of all slots
            if plan.slots:
                return sum(s.price for s in plan.slots) / len(plan.slots)
            return 0.0
        return sum(s.price for s in slots) / len(slots)

    async def _persist_store(self, now: datetime) -> None:
        await self._store.async_save(
            {
                "model": self.forecaster.to_dict(),
                "trained_at": self._last_training.isoformat() if self._last_training else None,
                "savings": {
                    "savings_eur": self._acc_savings_eur,
                    "charge_kwh": self._acc_charge_kwh,
                    "discharge_kwh": self._acc_discharge_kwh,
                },
            }
        )
