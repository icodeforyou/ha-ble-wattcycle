"""Constants for the WattCycle BLE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ha_ble_wattcycle"

# Config entry keys
CONF_ADDRESS: Final = "address"
CONF_DEVICE_TYPE: Final = "device_type"
CONF_USE_HILINK_AUTH: Final = "use_hilink_auth"

# Options
CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
CONF_QUIET_LOGGING: Final = "quiet_logging"
DEFAULT_QUIET_LOGGING: Final = False

# Connection tuning
CONNECT_TIMEOUT: Final = 20.0
COMMAND_TIMEOUT: Final = 8.0
PROBE_TIMEOUT: Final = 4.0  # per request variant while probing the frame format
MAX_CONNECT_ATTEMPTS: Final = 4

# Services
SERVICE_SEND_RAW: Final = "send_raw"
ATTR_DATA: Final = "data"

MANUFACTURER: Final = "WattCycle (reverse-engineered)"
