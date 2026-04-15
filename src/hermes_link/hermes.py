from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hermes_link.i18n import t
from hermes_link.models import HermesDiscovery, LinkConfig


def _native_hermes_root() -> Path:
    return Path.home() / ".hermes"


def _resolve_default_hermes_root() -> Path:
    native_home = _native_hermes_root()
    env_home = os.getenv("HERMES_HOME", "").strip()
    if not env_home:
        return native_home

    env_path = Path(env_home).expanduser()
    try:
        env_path.resolve().relative_to(native_home.resolve())
        return native_home
    except ValueError:
        pass

    if env_path.parent.name == "profiles":
        return env_path.parent.parent

    return env_path


def _resolve_profile_home(root: Path, profile_name: str) -> Path:
    if profile_name == "default":
        return root
    return root / "profiles" / profile_name


def _read_active_profile(root: Path) -> str | None:
    path = root / "active_profile"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return None
    return value or "default"


def _resolve_hermes_home(config: LinkConfig) -> Path | None:
    configured_home = (config.hermes.home or "").strip()
    if configured_home:
        return Path(configured_home).expanduser()

    root = _resolve_default_hermes_root()
    configured_profile = (config.hermes.profile or "").strip()
    if configured_profile:
        return _resolve_profile_home(root, configured_profile)

    env_home = os.getenv("HERMES_HOME", "").strip()
    if env_home:
        env_path = Path(env_home).expanduser()
        if env_path.parent.name == "profiles":
            return env_path

    active_profile = _read_active_profile(root)
    if active_profile and active_profile != "default":
        return _resolve_profile_home(root, active_profile)

    if env_home:
        return Path(env_home).expanduser()

    return root if root.exists() else None


def _probe_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return None

    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else None


def discover_hermes_installation(config: LinkConfig) -> HermesDiscovery:
    configured_exec = config.hermes.executable_path
    executable = configured_exec or shutil.which("hermes")
    hermes_home = _resolve_hermes_home(config)

    config_path = hermes_home / "config.yaml" if hermes_home else None
    env_path = hermes_home / ".env" if hermes_home else None
    version = _probe_version(executable) if executable else None
    found = bool(executable or hermes_home)

    source = None
    if configured_exec:
        source = "config"
    elif executable:
        source = "PATH"
    elif hermes_home:
        source = "filesystem"

    message = None
    if not found:
        message = t("hermes.not_detected")
    elif not executable:
        message = t("hermes.home_without_cli")

    return HermesDiscovery(
        found=found,
        executable_path=str(executable) if executable else None,
        hermes_home=str(hermes_home) if hermes_home else None,
        config_path=str(config_path) if config_path and config_path.exists() else None,
        env_path=str(env_path) if env_path and env_path.exists() else None,
        version=version,
        dashboard_supported=bool(executable),
        api_server_env_supported=bool(executable),
        source=source,
        message=message,
    )
