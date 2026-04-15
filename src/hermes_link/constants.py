APP_NAME = "hermes-link"
APP_AUTHOR = "HermesPilot"
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 47211
HERMES_LINK_HOME_ENV = "HERMES_LINK_HOME"

DARWIN_AUTOSTART_LABEL = "me.hermespilot.hermes-link"
LINUX_AUTOSTART_UNIT = "hermes-link.service"
WINDOWS_AUTOSTART_NAME = "Hermes Link"

PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE_LENGTH = 8

LEGACY_DEFAULT_DEVICE_SCOPES = [
    "chat",
    "status:read",
    "diagnostics:read",
    "devices:manage",
    "admin",
]

PRESET_DEFAULT_DEVICE_SCOPES_V1 = [
    "chat",
    "status:read",
    "diagnostics:read",
    "config:read",
    "env:read",
    "sessions:read",
    "sessions:write",
    "logs:read",
    "analytics:read",
    "cron:read",
    "cron:write",
]

DEFAULT_DEVICE_SCOPES = [
    "chat",
    "status:read",
    "diagnostics:read",
    "admin",
    "devices:manage",
    "config:read",
    "env:read",
    "sessions:read",
    "sessions:write",
    "logs:read",
    "analytics:read",
    "cron:read",
    "cron:write",
]
