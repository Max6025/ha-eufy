"""Konstanten fuer die Eufy Max Integration."""

from __future__ import annotations

DOMAIN = "eufy_max"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3000

# Hoechste Schema-Version, die dieser Client versteht. Beim Verbinden wird
# min(server_max, MAX_SCHEMA_VERSION) ausgehandelt.
MAX_SCHEMA_VERSION = 21

CONF_HOST = "host"
CONF_PORT = "port"
CONF_RTSP_FIRST = "rtsp_first"
CONF_SNAPSHOT_FROM_STREAM = "snapshot_from_stream"

DEFAULT_RTSP_FIRST = True
DEFAULT_SNAPSHOT_FROM_STREAM = True

# Reconnect-Verhalten
RECONNECT_MIN_DELAY = 2
RECONNECT_MAX_DELAY = 60
HEARTBEAT_INTERVAL = 30
COMMAND_TIMEOUT = 30

PLATFORMS = [
    "alarm_control_panel",
    "binary_sensor",
    "button",
    "camera",
    "light",
    "number",
    "select",
    "sensor",
    "switch",
]

SIGNAL_DEVICE_UPDATE = f"{DOMAIN}_device_update"
SIGNAL_CONNECTION_STATE = f"{DOMAIN}_connection_state"

# Properties, die niemals als eigene Entity auftauchen sollen.
IGNORED_PROPERTIES = {
    "name",
    "model",
    "serialNumber",
    "type",
    "hardwareVersion",
    "softwareVersion",
    "stationSerialNumber",
    "picture",
    "pictureUrl",
    "rtspStreamUrl",
}

# Properties, die eine eigene Entity-Klasse bekommen statt der generischen.
LIGHT_PROPERTIES = {"light"}
CAMERA_ENABLED_PROPERTY = "enabled"
RTSP_PROPERTY = "rtspStream"
RTSP_URL_PROPERTY = "rtspStreamUrl"

# Geraetetypen, die als Kamera behandelt werden.
CAMERA_DEVICE_TYPES = {
    0, 1, 2, 3, 4, 5, 7, 8, 9, 14, 15, 16, 18, 19, 23, 24, 30, 31,
    34, 35, 37, 38, 39, 44, 45, 46, 47, 48, 50, 51, 52, 60, 61, 62,
    87, 88, 90, 91, 93, 94, 100, 101, 102, 104, 110, 131, 132, 133,
}

SERVICE_SET_CAPTCHA = "set_captcha"
SERVICE_SET_VERIFY_CODE = "set_verify_code"
SERVICE_SET_PROPERTY = "set_property"
SERVICE_PTZ = "ptz"
SERVICE_RECONNECT = "reconnect"

ATTR_CAPTCHA_ID = "captcha_id"
ATTR_CAPTCHA = "captcha"
ATTR_VERIFY_CODE = "verify_code"
ATTR_PROPERTY = "property"
ATTR_VALUE = "value"
ATTR_DIRECTION = "direction"

PTZ_DIRECTIONS = {
    "rotate360": 0,
    "left": 1,
    "right": 2,
    "up": 3,
    "down": 4,
}

# ----------------------------------------------------------------------
# Zentraler Livestream-Controller
# ----------------------------------------------------------------------

# Standarddauer, wie lange die Kameras nach dem Buttondruck streamen (Sekunden)
DEFAULT_STREAM_DURATION = 120
MIN_STREAM_DURATION = 10
MAX_STREAM_DURATION = 1800

SIGNAL_STREAM_STATE = f"{DOMAIN}_stream_state"

HUB_IDENTIFIER = "eufy_max_hub"

SERVICE_START_STREAM = "start_stream"
SERVICE_STOP_STREAM = "stop_stream"
ATTR_DURATION = "duration"


# ----------------------------------------------------------------------
# Alarm Panel / Guard Modes
# ----------------------------------------------------------------------

# Guard-Mode-Werte aus eufy-security-client
GUARD_AWAY = 0
GUARD_HOME = 1
GUARD_SCHEDULE = 2
GUARD_CUSTOM1 = 3
GUARD_CUSTOM2 = 4
GUARD_CUSTOM3 = 5
GUARD_OFF = 6
GUARD_GEO = 47
GUARD_DISARMED = 63

GUARD_MODE_PROPERTY = "guardMode"
CURRENT_MODE_PROPERTY = "currentMode"
MOTION_DETECTION_PROPERTY = "motionDetection"

SERVICE_SET_GUARD_MODE = "set_guard_mode"
ATTR_GUARD_MODE = "guard_mode"

GUARD_MODE_NAMES = {
    GUARD_AWAY: "abwesend",
    GUARD_HOME: "zuhause",
    GUARD_SCHEDULE: "zeitplan",
    GUARD_CUSTOM1: "eigener_modus_1",
    GUARD_CUSTOM2: "eigener_modus_2",
    GUARD_CUSTOM3: "eigener_modus_3",
    GUARD_OFF: "aus",
    GUARD_GEO: "geofence",
    GUARD_DISARMED: "unscharf",
}
