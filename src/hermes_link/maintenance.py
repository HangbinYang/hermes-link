from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hermes_link import __version__
from hermes_link.autostart import disable_autostart
from hermes_link.runtime import ensure_runtime_layout, get_runtime_paths
from hermes_link.service import stop_background_service


def get_installation_metadata() -> dict[str, Any]:
    editable = False
    direct_url = None
    location = None
    try:
        distribution = importlib.metadata.distribution("hermes-link")
        location = str(distribution.locate_file(""))
        direct_url_raw = distribution.read_text("direct_url.json")
        if direct_url_raw:
            direct_url = json.loads(direct_url_raw)
            editable = bool((direct_url.get("dir_info") or {}).get("editable"))
    except importlib.metadata.PackageNotFoundError:
        pass
    return {
        "version": __version__,
        "python": sys.executable,
        "editable": editable,
        "location": location,
        "direct_url": direct_url,
    }


def update_installation(package_spec: str | None = None) -> dict[str, Any]:
    metadata = get_installation_metadata()
    target = package_spec or "hermes-link"
    command = [sys.executable, "-m", "pip", "install", "--upgrade", target]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        rollback = None
        if metadata["version"] and not metadata["editable"]:
            rollback = subprocess.run(
                [sys.executable, "-m", "pip", "install", f"hermes-link=={metadata['version']}"],
                capture_output=True,
                text=True,
                check=False,
            )
        return {
            "ok": False,
            "command": command,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "rollback_ok": bool(rollback and rollback.returncode == 0),
        }
    return {
        "ok": True,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "before": metadata,
        "after": get_installation_metadata(),
    }


def uninstall_installation(*, remove_data: bool = False) -> dict[str, Any]:
    paths = ensure_runtime_layout()
    stop_background_service()
    disable_autostart()

    removed_paths: list[str] = []
    if remove_data:
        for path in (paths.config_dir, paths.data_dir, paths.state_dir):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                removed_paths.append(str(path))

    command = [sys.executable, "-m", "pip", "uninstall", "-y", "hermes-link"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "ok": completed.returncode == 0,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "removed_paths": removed_paths,
        "runtime_home": str(get_runtime_paths().base_home),
    }
