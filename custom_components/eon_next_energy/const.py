"""Constants for E.ON Next Energy Data."""

from datetime import timedelta

DOMAIN = "eon_next_energy"

CONF_EMAIL = "email"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_HISTORY_DAYS = "history_days"
CONF_ELECTRICITY_OFFPEAK_RATE = "electricity_offpeak_rate"
CONF_ELECTRICITY_PEAK_RATE = "electricity_peak_rate"
CONF_ELECTRICITY_STANDING_CHARGE = "electricity_standing_charge"
CONF_GAS_RATE = "gas_rate"
CONF_GAS_STANDING_CHARGE = "gas_standing_charge"

DEFAULT_HISTORY_DAYS = 30
DEFAULT_ELECTRICITY_OFFPEAK_RATE = 0.069
DEFAULT_ELECTRICITY_PEAK_RATE = 0.3367
DEFAULT_ELECTRICITY_STANDING_CHARGE = 0.60
DEFAULT_GAS_RATE = 0.0812
DEFAULT_GAS_STANDING_CHARGE = 0.2916

CORRECTION_LOOKBACK_DAYS = 14

UPDATE_INTERVAL = timedelta(hours=6)
API_URL = "https://api.eonnext-kraken.energy/v1/graphql/"
