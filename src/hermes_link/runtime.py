from __future__ import annotations

import json
import secrets
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any

from platformdirs import user_config_path, user_data_path, user_state_path

from hermes_link.constants import (
    APP_AUTHOR,
    APP_NAME,
    DEFAULT_DEVICE_SCOPES,
    HERMES_LINK_HOME_ENV,
    LEGACY_DEFAULT_DEVICE_SCOPES,
    PRESET_DEFAULT_DEVICE_SCOPES_V1,
)
from hermes_link.models import LinkConfig, RuntimePaths, utc_now_iso
from hermes_link.storage import LinkRepository

_runtime_home_override: Path | None = None


def _resolve_env_home() -> Path | None:
    from os import getenv

    env_value = getenv(HERMES_LINK_HOME_ENV)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return None


def _default_base_home() -> Path:
    return _resolve_env_home() or user_data_path(APP_NAME, APP_AUTHOR).resolve()


def set_runtime_home(path: Path | None) -> None:
    global _runtime_home_override
    _runtime_home_override = path.resolve() if path else None
    get_runtime_paths.cache_clear()


@lru_cache(maxsize=1)
def get_runtime_paths() -> RuntimePaths:
    env_home = _resolve_env_home()
    base_home = _runtime_home_override or env_home or _default_base_home()

    if _runtime_home_override or env_home:
        config_dir = base_home / "config"
        data_dir = base_home / "data"
        state_dir = base_home / "state"
    else:
        config_dir = user_config_path(APP_NAME, APP_AUTHOR).resolve()
        data_dir = user_data_path(APP_NAME, APP_AUTHOR).resolve()
        state_dir = user_state_path(APP_NAME, APP_AUTHOR).resolve()

    run_dir = state_dir / "run"
    logs_dir = state_dir / "logs"
    autostart_dir = state_dir / "autostart"

    windows_startup_path = None
    try:
        from os import getenv

        appdata = getenv("APPDATA")
        if appdata:
            windows_startup_path = (
                Path(appdata)
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
                / "Hermes Link.cmd"
            )
    except Exception:
        windows_startup_path = None

    return RuntimePaths(
        base_home=base_home,
        config_dir=config_dir,
        data_dir=data_dir,
        state_dir=state_dir,
        run_dir=run_dir,
        logs_dir=logs_dir,
        autostart_dir=autostart_dir,
        config_path=config_dir / "config.json",
        db_path=data_dir / "state.db",
        pid_path=run_dir / "service.pid",
        log_path=logs_dir / "hermes-link.log",
        launch_agent_path=Path.home() / "Library" / "LaunchAgents" / "me.hermespilot.hermes-link.plist",
        systemd_unit_path=Path.home() / ".config" / "systemd" / "user" / "hermes-link.service",
        windows_startup_path=windows_startup_path,
        launcher_sh_path=autostart_dir / "hermes-link.sh",
        launcher_cmd_path=autostart_dir / "hermes-link.cmd",
    )


def ensure_runtime_layout() -> RuntimePaths:
    paths = get_runtime_paths()
    for path in (
        paths.config_dir,
        paths.data_dir,
        paths.state_dir,
        paths.run_dir,
        paths.logs_dir,
        paths.autostart_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def generate_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(8)}"


def build_default_config() -> LinkConfig:
    hostname = socket.gethostname().strip() or "Hermes Link"
    return LinkConfig(
        install_id=generate_id("hlk_install_"),
        link_id=generate_id("hlk_node_"),
        display_name=hostname,
        service_secret=generate_id("hlk_srv_"),
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(path)


def load_config() -> LinkConfig:
    paths = ensure_runtime_layout()
    if not paths.config_path.exists():
        config = build_default_config()
        save_config(config)
        return config

    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    config = LinkConfig.model_validate(raw)
    should_persist = False
    current_default_scopes = tuple(config.security.default_device_scopes)
    if current_default_scopes in {
        tuple(LEGACY_DEFAULT_DEVICE_SCOPES),
        tuple(PRESET_DEFAULT_DEVICE_SCOPES_V1),
    }:
        config.security.default_device_scopes = list(DEFAULT_DEVICE_SCOPES)
        should_persist = True
    if not config.service_secret:
        config.service_secret = generate_id("hlk_srv_")
        should_persist = True
    if config.relay.relay_base_url is None and config.network.relay_url:
        config.relay.relay_base_url = config.network.relay_url
        should_persist = True
    if should_persist:
        save_config(config)
    return config


def save_config(config: LinkConfig) -> LinkConfig:
    paths = ensure_runtime_layout()
    config.updated_at = utc_now_iso()
    _atomic_write_json(paths.config_path, config.model_dump(mode="json"))
    return config


def bootstrap_runtime() -> tuple[RuntimePaths, LinkConfig]:
    paths = ensure_runtime_layout()
    config = load_config()
    LinkRepository(paths.db_path).initialize()
    return paths, config
