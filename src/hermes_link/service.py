from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import uvicorn

from hermes_link import __version__
from hermes_link.autostart import get_autostart_status
from hermes_link.control_plane import public_relay_snapshot
from hermes_link.hermes import discover_hermes_installation
from hermes_link.i18n import t
from hermes_link.models import DoctorCheck, DoctorReport, LinkConfig, RelayStatusSnapshot, ServiceStatus, StatusSnapshot
from hermes_link.network import build_topology_snapshot
from hermes_link.runtime import ensure_runtime_layout, get_runtime_paths, load_config
from hermes_link.storage import LinkRepository


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def configure_logging() -> None:
    paths = ensure_runtime_layout()
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return

    root.setLevel(logging.INFO)
    handler = RotatingFileHandler(paths.log_path, maxBytes=2 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


def _read_pid() -> int | None:
    paths = get_runtime_paths()
    if not paths.pid_path.exists():
        return None
    try:
        payload = json.loads(paths.pid_path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid"))
        return pid if pid > 0 else None
    except Exception:
        return None


def _write_pid(pid: int, *, host: str, port: int) -> None:
    paths = ensure_runtime_layout()
    payload = {
        "pid": pid,
        "host": host,
        "port": port,
        "started_at": time.time(),
        "version": __version__,
    }
    paths.pid_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clear_pid() -> None:
    get_runtime_paths().pid_path.unlink(missing_ok=True)


def is_process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_running_pid() -> int | None:
    pid = _read_pid()
    if is_process_running(pid):
        return pid
    if pid is not None:
        _clear_pid()
    return None


def health_url(config: LinkConfig) -> str:
    return f"http://127.0.0.1:{config.network.api_port}/healthz"


def wait_for_service_ready(config: LinkConfig, *, timeout_seconds: float = 10.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pid = read_running_pid()
        if pid and is_process_running(pid):
            try:
                response = httpx.get(health_url(config), timeout=0.8)
                if response.status_code == 200:
                    return True
            except Exception:
                pass
        time.sleep(0.2)
    return False


def start_background_service(config: LinkConfig) -> int:
    running_pid = read_running_pid()
    if running_pid:
        return running_pid

    paths = ensure_runtime_layout()
    log_handle = paths.log_path.open("ab")
    command = [sys.executable, "-m", "hermes_link", "run"]
    if config.network.api_host:
        command.extend(["--host", config.network.api_host])
    if config.network.api_port:
        command.extend(["--port", str(config.network.api_port)])

    kwargs: dict = {
        "cwd": str(paths.base_home),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    log_handle.close()
    if not wait_for_service_ready(config):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
            else:
                os.kill(process.pid, signal.SIGTERM)
        finally:
            raise RuntimeError("service_failed_to_start")
    return process.pid


def stop_background_service() -> bool:
    pid = read_running_pid()
    if not pid:
        return False

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    else:
        os.kill(pid, signal.SIGTERM)

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not is_process_running(pid):
            _clear_pid()
            return True
        time.sleep(0.1)

    if sys.platform != "win32":
        os.kill(pid, signal.SIGKILL)
    _clear_pid()
    return True


def run_foreground_service(config: LinkConfig, *, host: str | None = None, port: int | None = None) -> None:
    configure_logging()
    resolved_host = host or config.network.api_host
    resolved_port = port or config.network.api_port
    _write_pid(os.getpid(), host=resolved_host, port=resolved_port)
    try:
        uvicorn.run(
            "hermes_link.api:create_app",
            host=resolved_host,
            port=resolved_port,
            factory=True,
            log_level="info",
        )
    finally:
        _clear_pid()


def collect_status_snapshot(config: LinkConfig | None = None) -> StatusSnapshot:
    config = config or load_config()
    paths = ensure_runtime_layout()
    repository = LinkRepository(paths.db_path)
    repository.initialize()

    pid = read_running_pid()
    autostart = get_autostart_status()
    service_status = ServiceStatus(
        running=bool(pid),
        pid=pid,
        health_url=health_url(config),
        log_path=str(paths.log_path),
        autostart_enabled=bool(autostart.get("enabled")),
        autostart_method=autostart.get("method"),
    )
    return StatusSnapshot(
        version=__version__,
        link_id=config.link_id,
        display_name=config.display_name,
        install_id=config.install_id,
        config_path=str(paths.config_path),
        db_path=str(paths.db_path),
        service=service_status,
        hermes=discover_hermes_installation(config),
        topology=build_topology_snapshot(config),
        relay=RelayStatusSnapshot.model_validate(public_relay_snapshot(config)),
        paired_devices=repository.count_active_devices(),
        pending_pairing_sessions=repository.count_pending_pairings(),
    )


def collect_doctor_report(config: LinkConfig | None = None) -> DoctorReport:
    snapshot = collect_status_snapshot(config)
    active_config = config or load_config()
    checks: list[DoctorCheck] = []

    if snapshot.service.running:
        checks.append(DoctorCheck(level="ok", code="service_running", message=t("doctor.service_running")))
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="service_not_running",
                message=t("doctor.service_not_running"),
                hint=t("doctor.service_not_running_hint"),
            )
        )

    if snapshot.hermes.found:
        checks.append(DoctorCheck(level="ok", code="hermes_detected", message=t("doctor.hermes_detected")))
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="hermes_missing",
                message=t("doctor.hermes_missing"),
                hint=t("doctor.hermes_missing_hint"),
            )
        )

    if snapshot.topology.lan_direct.status == "ready":
        checks.append(DoctorCheck(level="ok", code="lan_ready", message=t("doctor.lan_ready")))
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="lan_not_ready",
                message=snapshot.topology.lan_direct.message or t("doctor.lan_not_ready"),
                hint=t("doctor.lan_not_ready_hint"),
            )
        )

    if snapshot.topology.public_direct.status in {"configured", "ready"}:
        checks.append(DoctorCheck(level="ok", code="public_direct_configured", message=t("doctor.public_direct_configured")))
    elif snapshot.topology.public_direct.status == "disabled":
        checks.append(DoctorCheck(level="ok", code="public_direct_disabled", message=t("doctor.public_direct_disabled")))
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="public_direct_unconfigured",
                message=snapshot.topology.public_direct.message or t("doctor.public_direct_unconfigured"),
                hint=t("doctor.public_direct_unconfigured_hint"),
            )
        )

    if snapshot.topology.relay.status == "ready" and snapshot.relay.connection_status == "connected":
        checks.append(DoctorCheck(level="ok", code="relay_connected", message=t("doctor.relay_connected")))
    elif snapshot.topology.relay.status in {"configured", "ready"} and snapshot.relay.connection_status == "degraded":
        checks.append(
            DoctorCheck(
                level="warning",
                code="relay_degraded",
                message=t("doctor.relay_degraded"),
                hint=snapshot.relay.last_error or t("doctor.relay_degraded_hint"),
            )
        )
    elif snapshot.topology.relay.status in {"configured", "ready"}:
        checks.append(
            DoctorCheck(
                level="warning",
                code="relay_not_connected",
                message=t("doctor.relay_not_connected"),
                hint=t("doctor.relay_not_connected_hint"),
            )
        )
    elif snapshot.topology.relay.status == "disabled":
        checks.append(DoctorCheck(level="ok", code="relay_disabled", message=t("doctor.relay_disabled")))
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="relay_not_configured",
                message=snapshot.topology.relay.message or t("doctor.relay_not_configured"),
                hint=t("doctor.relay_not_configured_hint"),
            )
        )

    if snapshot.topology.relay.status in {"configured", "ready"}:
        if not snapshot.relay.has_refresh_token:
            checks.append(
                DoctorCheck(
                    level="warning",
                    code="relay_refresh_missing",
                    message=t("doctor.relay_refresh_missing"),
                    hint=t("doctor.relay_refresh_missing_hint"),
                )
            )
        else:
            refresh_expires_at = _parse_iso_datetime(snapshot.relay.refresh_token_expires_at)
            if refresh_expires_at and refresh_expires_at <= datetime.now(timezone.utc) + timedelta(days=7):
                checks.append(
                    DoctorCheck(
                        level="warning",
                        code="relay_refresh_expiring",
                        message=t("doctor.relay_refresh_expiring"),
                        hint=t("doctor.relay_refresh_expiring_hint", value=snapshot.relay.refresh_token_expires_at),
                    )
                )

    if snapshot.paired_devices > 0:
        checks.append(
            DoctorCheck(
                level="ok",
                code="device_paired",
                message=t("doctor.device_paired", count=snapshot.paired_devices),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                level="warning",
                code="no_paired_devices",
                message=t("doctor.no_paired_devices"),
                hint=t("doctor.no_paired_devices_hint"),
            )
        )

    if active_config.security.refresh_token_ttl_days < 30:
        checks.append(
            DoctorCheck(
                level="warning",
                code="device_refresh_ttl_short",
                message=t("doctor.device_refresh_ttl_short"),
                hint=t("doctor.device_refresh_ttl_short_hint", value=active_config.security.refresh_token_ttl_days),
            )
        )

    summary = "ok"
    if any(check.level == "error" for check in checks):
        summary = "error"
    elif any(check.level == "warning" for check in checks):
        summary = "warning"
    return DoctorReport(summary=summary, checks=checks)
