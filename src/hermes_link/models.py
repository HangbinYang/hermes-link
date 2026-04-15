from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from hermes_link.constants import DEFAULT_API_HOST, DEFAULT_API_PORT, DEFAULT_DEVICE_SCOPES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


class NetworkConfig(BaseModel):
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    allow_lan_direct: bool = True
    allow_public_direct: bool = True
    public_base_url: str | None = None
    extra_allowed_hosts: list[str] = Field(default_factory=list)
    cors_allowed_origins: list[str] = Field(default_factory=list)
    allow_relay: bool = True
    relay_provider: str | None = "cloudflare"
    relay_url: str | None = None


class HermesConfig(BaseModel):
    executable_path: str | None = None
    home: str | None = None
    profile: str | None = None


class SecurityConfig(BaseModel):
    pairing_code_ttl_minutes: int = 10
    access_token_ttl_minutes: int = 24 * 60
    refresh_token_ttl_days: int = 90
    max_active_pairing_sessions: int = 5
    anonymous_requests_per_minute: int = 120
    authenticated_requests_per_minute: int = 600
    pairing_claim_requests_per_minute: int = 12
    default_device_scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_DEVICE_SCOPES))


class RelayRuntimeConfig(BaseModel):
    refresh_token: str | None = None
    refresh_token_expires_at: str | None = None
    access_token: str | None = None
    access_token_expires_at: str | None = None
    connect_signing_secret: str | None = None
    relay_base_url: str | None = None
    control_websocket_url: str | None = None
    proxy_base_url: str | None = None
    connection_status: Literal["disabled", "idle", "bootstrapping", "connecting", "connected", "degraded", "error"] = "idle"
    last_connected_at: str | None = None
    last_status_at: str | None = None
    last_error: str | None = None


class LinkConfig(BaseModel):
    schema_version: int = 1
    install_id: str
    link_id: str
    display_name: str
    service_secret: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    relay: RelayRuntimeConfig = Field(default_factory=RelayRuntimeConfig)


class RuntimePaths(BaseModel):
    base_home: Path
    config_dir: Path
    data_dir: Path
    state_dir: Path
    run_dir: Path
    logs_dir: Path
    autostart_dir: Path
    config_path: Path
    db_path: Path
    pid_path: Path
    log_path: Path
    launch_agent_path: Path | None = None
    systemd_unit_path: Path | None = None
    windows_startup_path: Path | None = None
    launcher_sh_path: Path
    launcher_cmd_path: Path


class PairingSession(BaseModel):
    session_id: str
    code: str
    created_at: str
    expires_at: str
    status: Literal["pending", "claimed", "expired", "cancelled"] = "pending"
    scopes: list[str] = Field(default_factory=list)
    note: str | None = None
    claimed_at: str | None = None
    claimed_device_id: str | None = None


class DeviceRecord(BaseModel):
    device_id: str
    label: str
    platform: str
    scopes: list[str] = Field(default_factory=list)
    created_at: str
    last_seen_at: str | None = None
    status: Literal["active", "revoked"] = "active"


class AccessTokenRecord(BaseModel):
    token_id: str
    token_prefix: str
    token_hash: str
    device_id: str
    scopes: list[str] = Field(default_factory=list)
    created_at: str
    expires_at: str
    revoked_at: str | None = None


class IssuedAccessToken(BaseModel):
    token_id: str
    token: str
    token_prefix: str
    device_id: str
    scopes: list[str] = Field(default_factory=list)
    created_at: str
    expires_at: str


class RefreshTokenRecord(BaseModel):
    refresh_token_id: str
    token_hash: str
    device_id: str
    created_at: str
    expires_at: str
    revoked_at: str | None = None


class IssuedRefreshToken(BaseModel):
    refresh_token_id: str
    token: str
    device_id: str
    created_at: str
    expires_at: str


class AuthenticatedDevice(BaseModel):
    device: DeviceRecord
    token_id: str | None = None
    token_expires_at: str | None = None
    refresh_token_id: str | None = None
    refresh_token_expires_at: str | None = None


class AuditEvent(BaseModel):
    event_id: int
    event_type: str
    occurred_at: str
    actor_type: str
    actor_id: str | None = None
    detail: dict = Field(default_factory=dict)


class HermesDiscovery(BaseModel):
    found: bool
    executable_path: str | None = None
    hermes_home: str | None = None
    config_path: str | None = None
    env_path: str | None = None
    version: str | None = None
    dashboard_supported: bool = False
    api_server_env_supported: bool = False
    source: str | None = None
    message: str | None = None


class ConnectionPathSnapshot(BaseModel):
    mode: Literal["lan_direct", "public_direct", "relay"]
    status: Literal["ready", "configured", "disabled", "unavailable", "planned"]
    urls: list[str] = Field(default_factory=list)
    reason: str | None = None
    message: str | None = None


class TopologySnapshot(BaseModel):
    lan_direct: ConnectionPathSnapshot
    public_direct: ConnectionPathSnapshot
    relay: ConnectionPathSnapshot


class ServiceStatus(BaseModel):
    running: bool
    pid: int | None = None
    health_url: str | None = None
    log_path: str
    autostart_enabled: bool = False
    autostart_method: str | None = None


class RelayStatusSnapshot(BaseModel):
    enabled: bool
    configured: bool
    provider: str | None = None
    relay_base_url: str | None = None
    proxy_base_url: str | None = None
    has_refresh_token: bool = False
    refresh_token_expires_at: str | None = None
    access_token_expires_at: str | None = None
    connection_status: Literal["disabled", "idle", "bootstrapping", "connecting", "connected", "degraded", "error"] = "idle"
    last_connected_at: str | None = None
    last_status_at: str | None = None
    last_error: str | None = None


class StatusSnapshot(BaseModel):
    version: str
    link_id: str
    display_name: str
    install_id: str
    config_path: str
    db_path: str
    service: ServiceStatus
    hermes: HermesDiscovery
    topology: TopologySnapshot
    relay: RelayStatusSnapshot
    paired_devices: int
    pending_pairing_sessions: int


class DoctorCheck(BaseModel):
    level: Literal["ok", "warning", "error"]
    code: str
    message: str
    hint: str | None = None


class DoctorReport(BaseModel):
    summary: Literal["ok", "warning", "error"]
    checks: list[DoctorCheck] = Field(default_factory=list)
