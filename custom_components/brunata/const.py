"""Constants for the Brunata integration."""

from datetime import timedelta

DOMAIN = "brunata"

BASE_URL = "https://nutzerportal.brunata-muenchen.de"
SAP_CLIENT = "201"

# The portal only publishes monthly values; the README asks to avoid aggressive polling.
UPDATE_INTERVAL = timedelta(hours=12)

UNIT_CLASSES = {"m³": "volume", "kWh": "energy", "MWh": "energy"}

STATISTIC_NAMES = {
    "cold_water": "Brunata cold water",
    "hot_water": "Brunata hot water",
    "heating": "Brunata heating",
}
