from __future__ import annotations

import copy
import json
from typing import Any

from hermes_link.i18n import t
from hermes_link.models import LinkConfig
from hermes_link.network import derive_relay_proxy_url
from hermes_link.runtime import save_config

_MUTABLE_CONFIG_PREFIXES = (
    "display_name",
    "network.",
    "hermes.",
    "security.",
)
_RESTART_REQUIRED_PREFIXES = (
    "network.api_host",
    "network.api_port",
    "network.public_base_url",
    "network.extra_allowed_hosts",
    "network.cors_allowed_origins",
)
_RELAY_SYNC_PREFIXES = (
    "display_name",
    "network.allow_relay",
    "network.relay_provider",
    "network.relay_url",
)
_RELAY_CLEAR_CREDENTIAL_PREFIXES = (
    "network.allow_relay",
    "network.relay_provider",
    "network.relay_url",
)
_NORMALIZED_LIST_KEYS = {
    "network.extra_allowed_hosts",
    "network.cors_allowed_origins",
    "security.default_device_scopes",
}
_STRIPPED_OPTIONAL_STRING_KEYS = {
    "network.public_base_url",
    "network.relay_provider",
    "network.relay_url",
    "hermes.executable_path",
    "hermes.home",
    "hermes.profile",
}
_STRIPPED_REQUIRED_STRING_KEYS = {
    "display_name",
}


class ControlPlaneError(RuntimeError):
    def __init__(self, code: str, *, message: str | None = None):
        self.code = code
        self.message = message or t(f"control.{code}")
        super().__init__(self.message)


def _matches_prefix(key: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if prefix.endswith("."):
            if key.startswith(prefix):
                return True
            continue
        if key == prefix or key.startswith(f"{prefix}."):
            return True
    return False


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return []
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in candidate.split(",")]
        value = parsed

    if not isinstance(value, list):
        raise ControlPlaneError("config_value_invalid")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def parse_cli_config_value(value: str) -> Any:
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return value


def clear_relay_credentials(config: LinkConfig, *, save: bool = True) -> LinkConfig:
    config.relay.refresh_token = None
    config.relay.refresh_token_expires_at = None
    config.relay.access_token = None
    config.relay.access_token_expires_at = None
    config.relay.connect_signing_secret = None
    config.relay.control_websocket_url = None
    config.relay.proxy_base_url = None
    config.relay.last_error = None
    config.relay.connection_status = "disabled" if not config.network.allow_relay else "idle"
    if save:
        save_config(config)
    return config


def public_relay_snapshot(config: LinkConfig) -> dict[str, Any]:
    return {
        "enabled": config.network.allow_relay,
        "configured": bool(config.network.relay_url),
        "provider": config.network.relay_provider,
        "relay_base_url": config.relay.relay_base_url or config.network.relay_url,
        "proxy_base_url": config.relay.proxy_base_url or derive_relay_proxy_url(config),
        "has_refresh_token": bool(config.relay.refresh_token),
        "refresh_token_expires_at": config.relay.refresh_token_expires_at,
        "access_token_expires_at": config.relay.access_token_expires_at,
        "connection_status": config.relay.connection_status,
        "last_connected_at": config.relay.last_connected_at,
        "last_status_at": config.relay.last_status_at,
        "last_error": config.relay.last_error,
    }


def public_link_config(config: LinkConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "link_id": config.link_id,
        "display_name": config.display_name,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
        "network": config.network.model_dump(mode="json"),
        "hermes": config.hermes.model_dump(mode="json"),
        "security": config.security.model_dump(mode="json"),
        "relay": public_relay_snapshot(config),
    }


def local_link_config(config: LinkConfig, *, include_secrets: bool = False) -> dict[str, Any]:
    if include_secrets:
        return config.model_dump(mode="json")
    return public_link_config(config)


def _normalize_value_for_key(key: str, value: Any) -> Any:
    if key in _NORMALIZED_LIST_KEYS:
        return _normalize_string_list(value)

    if key in _STRIPPED_OPTIONAL_STRING_KEYS:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    if key in _STRIPPED_REQUIRED_STRING_KEYS:
        text = str(value).strip()
        if not text:
            raise ControlPlaneError("config_value_invalid")
        return text

    return value


def _resolve_container(payload: dict[str, Any], key: str) -> tuple[dict[str, Any], str]:
    parts = [part.strip() for part in key.split(".") if part.strip()]
    if not parts:
        raise ControlPlaneError("config_key_invalid")
    container = payload
    for part in parts[:-1]:
        next_value = container.get(part)
        if not isinstance(next_value, dict):
            raise ControlPlaneError("config_key_invalid")
        container = next_value
    leaf_key = parts[-1]
    if leaf_key not in container:
        raise ControlPlaneError("config_key_invalid")
    return container, leaf_key


def update_link_config_value(config: LinkConfig, key: str, value: Any) -> tuple[LinkConfig, dict[str, Any]]:
    normalized_key = key.strip()
    if not _matches_prefix(normalized_key, _MUTABLE_CONFIG_PREFIXES):
        raise ControlPlaneError("config_key_not_mutable")

    payload = copy.deepcopy(config.model_dump(mode="json"))
    container, leaf_key = _resolve_container(payload, normalized_key)
    previous_value = copy.deepcopy(container[leaf_key])
    container[leaf_key] = _normalize_value_for_key(normalized_key, value)

    try:
        updated_config = LinkConfig.model_validate(payload)
    except Exception as exc:  # pragma: no cover - pydantic provides the detail
        raise ControlPlaneError("config_value_invalid", message=str(exc)) from exc

    updated_payload = updated_config.model_dump(mode="json")
    updated_container, updated_leaf = _resolve_container(updated_payload, normalized_key)
    next_value = updated_container[updated_leaf]
    changed = previous_value != next_value
    if changed:
        save_config(updated_config)

    outcome = {
        "changed": changed,
        "key": normalized_key,
        "value": next_value,
        "restart_required": _matches_prefix(normalized_key, _RESTART_REQUIRED_PREFIXES),
        "relay_sync_required": _matches_prefix(normalized_key, _RELAY_SYNC_PREFIXES),
        "clear_relay_credentials": _matches_prefix(normalized_key, _RELAY_CLEAR_CREDENTIAL_PREFIXES),
    }
    return updated_config, outcome
