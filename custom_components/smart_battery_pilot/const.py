"""Constants for the Smart Battery Pilot integration."""

from __future__ import annotations

DOMAIN = "smart_battery_pilot"

# --- Config entry keys (data) ---
# Step: prices
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_OFFSET = "price_offset"  # EUR/kWh added on top of market price
CONF_FEED_IN_TARIFF = "feed_in_tariff"  # EUR/kWh earned when exporting

# Step: battery
CONF_SOC_ENTITY = "soc_entity"
CONF_BATTERY_CHARGE_ENERGY_ENTITY = "battery_charge_energy_entity"
CONF_BATTERY_DISCHARGE_ENERGY_ENTITY = "battery_discharge_energy_entity"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_MAX_CHARGE_POWER_W = "max_charge_power_w"
CONF_MAX_DISCHARGE_POWER_W = "max_discharge_power_w"
CONF_MIN_SOC = "min_soc"
CONF_MAX_SOC = "max_soc"
CONF_EFFICIENCY = "efficiency"  # roundtrip efficiency in percent

# Step: control scripts
CONF_SCRIPT_CHARGE = "script_charge"
CONF_SCRIPT_IDLE = "script_idle"
CONF_SCRIPT_AUTO = "script_auto"
CONF_SCRIPT_EXPORT = "script_export"

# Step: consumption
CONF_CONSUMPTION_ENTITY = "consumption_entity"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_HAS_HEAT_PUMP = "has_heat_pump"

# Step: PV (optional)
CONF_PV_FORECAST_TODAY = "pv_forecast_today"
CONF_PV_FORECAST_TOMORROW = "pv_forecast_tomorrow"
CONF_PV_POWER_ENTITY = "pv_power_entity"

# Options
CONF_SPREAD_THRESHOLD = "spread_threshold"  # EUR/kWh min price spread
CONF_DISCHARGE_MODE = "discharge_mode"
CONF_DRY_RUN = "dry_run"
CONF_TRAINING_DAYS = "training_days"

DISCHARGE_MODE_SELF_CONSUMPTION = "self_consumption"
DISCHARGE_MODE_EXPORT = "export"

# --- Defaults ---
DEFAULT_PRICE_OFFSET = 0.0
DEFAULT_FEED_IN_TARIFF = 0.08  # typical German residential feed-in tariff
DEFAULT_MIN_SOC = 10
DEFAULT_MAX_SOC = 95
DEFAULT_EFFICIENCY = 90
DEFAULT_SPREAD_THRESHOLD = 0.20
DEFAULT_DISCHARGE_MODE = DISCHARGE_MODE_SELF_CONSUMPTION
DEFAULT_DRY_RUN = True
DEFAULT_TRAINING_DAYS = 60

# --- Plan actions ---
ACTION_CHARGE = "charge"
ACTION_AUTO = "auto"
ACTION_IDLE = "idle"
ACTION_EXPORT = "export"

# --- Misc ---
ATTR_SLOTS = "slots"
SERVICE_REPLAN = "replan"
UPDATE_INTERVAL_MINUTES = 30
STORAGE_KEY = f"{DOMAIN}.model"
STORAGE_VERSION = 1

FRONTEND_SCRIPT_URL = f"/{DOMAIN}/smart-battery-pilot-card.js"
