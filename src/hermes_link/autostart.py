from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from hermes_link.constants import DARWIN_AUTOSTART_LABEL, LINUX_AUTOSTART_UNIT
from hermes_link.i18n import t
from hermes_link.runtime import ensure_runtime_layout, get_runtime_paths


def _write_launcher_scripts() -> None:
    paths = ensure_runtime_layout()
    launcher_sh = (
        "#!/bin/sh\n"
        f"exec '{sys.executable}' -m hermes_link run\n"
    )
    paths.launcher_sh_path.write_text(launcher_sh, encoding="utf-8")
    paths.launcher_sh_path.chmod(paths.launcher_sh_path.stat().st_mode | stat.S_IXUSR)

    launcher_cmd = (
        "@echo off\r\n"
        f"cd /d \"{paths.base_home}\"\r\n"
        f"\"{sys.executable}\" -m hermes_link run\r\n"
    )
    paths.launcher_cmd_path.write_text(launcher_cmd, encoding="utf-8")


def _run_command(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        message = (result.stderr or result.stdout or "").strip()
        return result.returncode, message
    except FileNotFoundError as exc:
        return 127, str(exc)


def _darwin_enable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.launch_agent_path is None:
        return False, t("autostart.launch_agent_unavailable")

    _write_launcher_scripts()
    paths.launch_agent_path.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{DARWIN_AUTOSTART_LABEL}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
      <string>{paths.launcher_sh_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{paths.base_home}</string>
  </dict>
</plist>
"""
    paths.launch_agent_path.write_text(plist, encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    _run_command(["launchctl", "bootout", domain, str(paths.launch_agent_path)])
    code, message = _run_command(["launchctl", "bootstrap", domain, str(paths.launch_agent_path)])
    if code == 0:
        return True, t("autostart.launchd")
    return False, message or t("autostart.launchctl_failed")


def _darwin_disable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.launch_agent_path is None:
        return False, t("autostart.launch_agent_unavailable")
    domain = f"gui/{os.getuid()}"
    _run_command(["launchctl", "bootout", domain, str(paths.launch_agent_path)])
    paths.launch_agent_path.unlink(missing_ok=True)
    return True, t("autostart.launchd_removed")


def _linux_enable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.systemd_unit_path is None:
        return False, t("autostart.systemd_unit_unavailable")

    _write_launcher_scripts()
    paths.systemd_unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit = (
        "[Unit]\n"
        "Description=Hermes Link\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={paths.launcher_sh_path}\n"
        f"WorkingDirectory={paths.base_home}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    paths.systemd_unit_path.write_text(unit, encoding="utf-8")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", LINUX_AUTOSTART_UNIT],
    ):
        code, message = _run_command(command)
        if code != 0:
            return False, message or t("autostart.systemctl_failed")
    return True, t("autostart.systemd")


def _linux_disable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.systemd_unit_path is None:
        return False, t("autostart.systemd_unit_unavailable")
    _run_command(["systemctl", "--user", "disable", "--now", LINUX_AUTOSTART_UNIT])
    paths.systemd_unit_path.unlink(missing_ok=True)
    _run_command(["systemctl", "--user", "daemon-reload"])
    return True, t("autostart.systemd_removed")


def _windows_enable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.windows_startup_path is None:
        return False, t("autostart.windows_startup_unavailable")
    _write_launcher_scripts()
    paths.windows_startup_path.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        "@echo off\r\n"
        f"start \"\" /min \"{paths.launcher_cmd_path}\"\r\n"
    )
    paths.windows_startup_path.write_text(contents, encoding="utf-8")
    return True, t("autostart.windows_startup")


def _windows_disable() -> tuple[bool, str]:
    paths = ensure_runtime_layout()
    if paths.windows_startup_path is None:
        return False, t("autostart.windows_startup_unavailable")
    paths.windows_startup_path.unlink(missing_ok=True)
    return True, t("autostart.windows_startup_removed")


def enable_autostart() -> tuple[bool, str]:
    if sys.platform == "darwin":
        return _darwin_enable()
    if sys.platform.startswith("linux"):
        return _linux_enable()
    if sys.platform == "win32":
        return _windows_enable()
    return False, t("autostart.not_implemented", platform=sys.platform)


def disable_autostart() -> tuple[bool, str]:
    if sys.platform == "darwin":
        return _darwin_disable()
    if sys.platform.startswith("linux"):
        return _linux_disable()
    if sys.platform == "win32":
        return _windows_disable()
    return False, t("autostart.not_implemented", platform=sys.platform)


def get_autostart_status() -> dict[str, str | bool | None]:
    paths = ensure_runtime_layout()
    if sys.platform == "darwin" and paths.launch_agent_path is not None:
        enabled = paths.launch_agent_path.exists()
        return {"enabled": enabled, "method": t("autostart.launchd") if enabled else None}
    if sys.platform.startswith("linux") and paths.systemd_unit_path is not None:
        enabled = paths.systemd_unit_path.exists()
        return {"enabled": enabled, "method": t("autostart.systemd") if enabled else None}
    if sys.platform == "win32" and paths.windows_startup_path is not None:
        enabled = paths.windows_startup_path.exists()
        return {"enabled": enabled, "method": t("autostart.windows_startup") if enabled else None}
    return {"enabled": False, "method": None}
