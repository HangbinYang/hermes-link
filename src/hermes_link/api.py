from __future__ import annotations

from contextlib import asynccontextmanager
import json
import queue
import time
from typing import Any

import asyncio
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, StreamingResponse

from hermes_link import __version__
from hermes_link.control_plane import ControlPlaneError, public_link_config, update_link_config_value
from hermes_link.execution import ExecutionError, HermesExecutionManager
from hermes_link.hermes import discover_hermes_installation
from hermes_link.hermes_adapter import HermesAdapter, HermesAdapterError
from hermes_link.i18n import pop_language, push_language, t
from hermes_link.models import AuthenticatedDevice, LinkConfig
from hermes_link.network import allowed_host_patterns, build_topology_snapshot, is_loopback_host, preferred_pairing_urls
from hermes_link.rate_limit import InMemoryRateLimiter, RateLimitExceeded
from hermes_link.relay import INTERNAL_DEVICE_ID_HEADER, INTERNAL_SECRET_HEADER, RelayError, RelayManager
from hermes_link.runtime import bootstrap_runtime
from hermes_link.security import SecurityError, SecurityManager, normalize_pairing_code
from hermes_link.service import collect_status_snapshot, configure_logging
from hermes_link.storage import LinkRepository


class PairingClaimBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32)
    device_label: str = Field(min_length=1, max_length=128)
    device_platform: str = Field(default="unknown", min_length=1, max_length=64)


class RawConfigBody(BaseModel):
    yaml_text: str


class ConfigSetBody(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    value: str


class LinkConfigSetBody(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    value: Any


class EnvSetBody(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    value: str


class EnvDeleteBody(BaseModel):
    key: str = Field(min_length=1, max_length=256)


class CronCreateBody(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"
    skills: list[str] = Field(default_factory=list)


class CronUpdateBody(BaseModel):
    updates: dict


class SkillToggleBody(BaseModel):
    name: str
    enabled: bool


class BackupCreateBody(BaseModel):
    output_path: str | None = None


class BackupRestoreBody(BaseModel):
    archive_path: str
    force: bool = False


class RunCreateBody(BaseModel):
    input: str | list[str | dict]
    instructions: str | None = None
    conversation_history: list[dict] = Field(default_factory=list)
    session_id: str | None = None
    continue_session: bool = False
    wait_for_completion: bool = False
    timeout_seconds: float | None = None


class RunRetryBody(BaseModel):
    wait_for_completion: bool = False
    timeout_seconds: float | None = None


class RelayConnectTokenBody(BaseModel):
    ttl_seconds: int | None = None


class RelayOperationBody(BaseModel):
    clear_credentials: bool = False


class AuthRefreshBody(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=1, max_length=4096)


def _raise_http_from_adapter(exc: HermesAdapterError) -> None:
    status_code = 400
    if exc.code in {"hermes_not_found", "hermes_home_missing", "sessions_db_missing"}:
        status_code = 404
    if exc.code in {"session_not_found", "cron_job_not_found", "backup_not_found"}:
        status_code = 404
    if exc.code in {"hermes_config_invalid", "invalid_schedule", "backup_restore_conflict"}:
        status_code = 400
    raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


def _raise_http_from_execution(exc: ExecutionError) -> None:
    status_code = 400
    if exc.code in {"run_not_found"}:
        status_code = 404
    elif exc.code in {"run_not_active"}:
        status_code = 409
    elif exc.code in {"execution_unavailable"}:
        status_code = 503
    raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


def create_app() -> FastAPI:
    configure_logging()
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    relay_manager = RelayManager(config, repository)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await relay_manager.start()
        try:
            yield
        finally:
            await relay_manager.shutdown()

    app = FastAPI(
        title="Hermes Link",
        version=__version__,
        description=t("app.description"),
        lifespan=lifespan,
    )
    if config.network.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.network.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept-Language", "X-Hermes-Link-Relay-Connect-Token"],
            expose_headers=["Retry-After"],
        )
    app.state.config = config
    app.state.repository = repository
    app.state.hermes_adapter = HermesAdapter(config)
    app.state.execution_manager = HermesExecutionManager(app.state.hermes_adapter)
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.relay_manager = relay_manager

    host_cache = {
        "computed_at": 0.0,
        "signature": None,
        "allowed_hosts": set(),
    }

    def current_allowed_hosts(active_config: LinkConfig) -> set[str]:
        signature = (
            active_config.network.api_host,
            active_config.network.public_base_url,
            tuple(active_config.network.extra_allowed_hosts),
        )
        now = time.monotonic()
        if (
            host_cache["signature"] == signature
            and now - host_cache["computed_at"] < 5.0
            and host_cache["allowed_hosts"]
        ):
            return host_cache["allowed_hosts"]

        allowed = {pattern.lower() for pattern in allowed_host_patterns(active_config)}
        host_cache["signature"] = signature
        host_cache["computed_at"] = now
        host_cache["allowed_hosts"] = allowed
        return allowed

    def is_trusted_host(host_header: str | None, active_config: LinkConfig) -> bool:
        if not host_header:
            return True
        raw_host = host_header.strip()
        if not raw_host:
            return True
        host = raw_host
        if raw_host.startswith("["):
            closing = raw_host.find("]")
            host = raw_host[1:closing] if closing > 0 else raw_host
        elif ":" in raw_host:
            host = raw_host.split(":", 1)[0]
        return host.lower() in current_allowed_hosts(active_config)

    @app.middleware("http")
    async def apply_request_language(request: Request, call_next):
        token = push_language(request.headers.get("Accept-Language"))
        try:
            active_config = request.app.state.config
            if not is_trusted_host(request.headers.get("host"), active_config):
                return JSONResponse(
                    status_code=400,
                    content={"detail": {"code": "untrusted_host", "message": t("api.error.untrusted_host")}},
                )
            if request.url.path != "/healthz":
                client_host = request.client.host if request.client else "unknown"
                auth_header = request.headers.get("Authorization", "")
                internal_secret = request.headers.get(INTERNAL_SECRET_HEADER, "")
                internal_device_id = request.headers.get(INTERNAL_DEVICE_ID_HEADER, "")
                is_internal_loopback_request = bool(
                    internal_secret
                    and internal_device_id
                    and is_loopback_host(client_host)
                )
                if request.url.path == "/api/v1/pairing/claim":
                    bucket = "pairing_claim"
                    limit = active_config.security.pairing_claim_requests_per_minute
                elif is_internal_loopback_request:
                    bucket = "authenticated"
                    limit = active_config.security.authenticated_requests_per_minute
                elif auth_header.startswith("Bearer "):
                    bucket = "authenticated"
                    limit = active_config.security.authenticated_requests_per_minute
                else:
                    bucket = "anonymous"
                    limit = active_config.security.anonymous_requests_per_minute

                try:
                    request.app.state.rate_limiter.check(key=f"{bucket}:{client_host}", limit=limit)
                except RateLimitExceeded as exc:
                    detail = {
                        "code": "rate_limited",
                        "message": t("api.error.rate_limited", seconds=exc.retry_after_seconds),
                        "retry_after_seconds": exc.retry_after_seconds,
                    }
                    return JSONResponse(
                        status_code=429,
                        content={"detail": detail},
                        headers={"Retry-After": str(exc.retry_after_seconds)},
                    )
            return await call_next(request)
        finally:
            pop_language(token)

    def get_config_from_app(request: Request) -> LinkConfig:
        return request.app.state.config

    def get_repository_from_app(request: Request) -> LinkRepository:
        return request.app.state.repository

    def get_adapter_from_app(request: Request) -> HermesAdapter:
        return request.app.state.hermes_adapter

    def get_execution_manager_from_app(request: Request) -> HermesExecutionManager:
        return request.app.state.execution_manager

    def get_relay_manager_from_app(request: Request) -> RelayManager:
        return request.app.state.relay_manager

    def replace_runtime_config(request: Request, updated_config: LinkConfig) -> HermesAdapter:
        adapter = HermesAdapter(updated_config)
        request.app.state.config = updated_config
        request.app.state.hermes_adapter = adapter
        if hasattr(request.app.state.execution_manager, "adapter"):
            request.app.state.execution_manager.adapter = adapter
        if hasattr(request.app.state.relay_manager, "config"):
            request.app.state.relay_manager.config = updated_config
        return adapter

    def get_security_manager(
        config: LinkConfig = Depends(get_config_from_app),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> SecurityManager:
        return SecurityManager(repository, config)

    def require_scopes(*scopes: str):
        def dependency(
            request: Request,
            authorization: str | None = Header(None),
            internal_secret: str | None = Header(None, alias="X-Hermes-Link-Internal-Secret"),
            internal_device_id: str | None = Header(None, alias="X-Hermes-Link-Device-Id"),
            security_manager: SecurityManager = Depends(get_security_manager),
        ) -> AuthenticatedDevice:
            if internal_secret and internal_device_id:
                try:
                    client_host = request.client.host if request.client else None
                    return security_manager.authenticate_internal_device(
                        service_secret=internal_secret,
                        device_id=internal_device_id,
                        client_host=client_host,
                        required_scopes=list(scopes),
                    )
                except SecurityError as exc:
                    raise HTTPException(status_code=403, detail={"code": exc.code, "message": exc.message}) from exc
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(
                    status_code=401,
                    detail={"code": "missing_bearer_token", "message": t("api.error.missing_bearer")},
                )
            token = authorization.removeprefix("Bearer ").strip()
            try:
                return security_manager.authenticate_bearer(token, required_scopes=list(scopes))
            except SecurityError as exc:
                raise HTTPException(status_code=403, detail={"code": exc.code, "message": exc.message}) from exc

        return dependency

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/bootstrap")
    def bootstrap_info(config: LinkConfig = Depends(get_config_from_app)) -> dict:
        topology = build_topology_snapshot(config)
        return {
            "version": __version__,
            "link_id": config.link_id,
            "display_name": config.display_name,
            "pairing_supported": True,
            "preferred_pairing_urls": preferred_pairing_urls(topology),
            "topology": topology.model_dump(mode="json"),
        }

    @app.get("/api/v1/auth/me")
    def auth_me(
        actor: AuthenticatedDevice = Depends(require_scopes()),
        config: LinkConfig = Depends(get_config_from_app),
    ) -> dict:
        return {
            "link_id": config.link_id,
            "display_name": config.display_name,
            "device": actor.device.model_dump(mode="json"),
            "token_id": actor.token_id,
            "token_expires_at": actor.token_expires_at,
            "refresh_token_id": actor.refresh_token_id,
            "refresh_token_expires_at": actor.refresh_token_expires_at,
            "session_renewable": actor.refresh_token_expires_at is not None,
            "scopes": actor.device.scopes,
        }

    @app.post("/api/v1/auth/refresh")
    def auth_refresh(
        request: Request,
        body: AuthRefreshBody | None = Body(default=None),
        security_manager: SecurityManager = Depends(get_security_manager),
    ) -> dict:
        try:
            authorization = request.headers.get("Authorization", "")
            issued_refresh = None
            refresh_token_expires_at = None
            if authorization.startswith("Bearer "):
                try:
                    actor = security_manager.authenticate_bearer(authorization.removeprefix("Bearer ").strip())
                    if body and body.refresh_token:
                        _, issued_access, issued_refresh = security_manager.refresh_device_session(
                            authenticated=actor,
                            refresh_token=body.refresh_token,
                        )
                        refresh_token_expires_at = issued_refresh.expires_at
                    else:
                        issued_access = security_manager.rotate_access_token(actor)
                        refresh_token_expires_at = actor.refresh_token_expires_at
                except SecurityError as exc:
                    if exc.code == "access_token_invalid" and body and body.refresh_token:
                        refreshed_actor, issued_access, issued_refresh = security_manager.refresh_device_session(
                            refresh_token=body.refresh_token
                        )
                        refresh_token_expires_at = refreshed_actor.refresh_token_expires_at
                    else:
                        raise
            elif body and body.refresh_token:
                refreshed_actor, issued_access, issued_refresh = security_manager.refresh_device_session(
                    refresh_token=body.refresh_token
                )
                refresh_token_expires_at = refreshed_actor.refresh_token_expires_at
            else:
                raise HTTPException(
                    status_code=401,
                    detail={"code": "session_refresh_missing", "message": t("api.error.session_refresh_missing")},
                )
        except SecurityError as exc:
            raise HTTPException(status_code=403, detail={"code": exc.code, "message": exc.message}) from exc
        return {
            "access_token": issued_access.model_dump(mode="json"),
            "refresh_token": issued_refresh.model_dump(mode="json") if issued_refresh else None,
            "refresh_token_expires_at": refresh_token_expires_at,
        }

    @app.post("/api/v1/auth/logout")
    def auth_logout(
        actor: AuthenticatedDevice = Depends(require_scopes()),
        security_manager: SecurityManager = Depends(get_security_manager),
    ) -> dict:
        try:
            revoked = security_manager.revoke_device_session(actor)
        except SecurityError as exc:
            raise HTTPException(status_code=403, detail={"code": exc.code, "message": exc.message}) from exc
        return {"ok": revoked, "token_id": actor.token_id, "refresh_token_id": actor.refresh_token_id}

    @app.get("/api/v1/status")
    def status(_: AuthenticatedDevice = Depends(require_scopes("status:read"))) -> dict:
        return collect_status_snapshot().model_dump(mode="json")

    @app.get("/api/v1/link/config")
    def link_config(
        _: AuthenticatedDevice = Depends(require_scopes("admin")),
        config: LinkConfig = Depends(get_config_from_app),
    ) -> dict:
        return public_link_config(config)

    @app.get("/api/v1/hermes/discovery")
    def hermes_discovery(
        _: AuthenticatedDevice = Depends(require_scopes("status:read")),
        config: LinkConfig = Depends(get_config_from_app),
    ) -> dict:
        return discover_hermes_installation(config).model_dump(mode="json")

    @app.get("/api/v1/topology")
    def topology(_: AuthenticatedDevice = Depends(require_scopes("status:read")), config: LinkConfig = Depends(get_config_from_app)) -> dict:
        return build_topology_snapshot(config).model_dump(mode="json")

    @app.get("/api/v1/relay/status")
    def relay_status(
        _: AuthenticatedDevice = Depends(require_scopes("status:read")),
        relay_manager: RelayManager = Depends(get_relay_manager_from_app),
    ) -> dict:
        return relay_manager.snapshot()

    @app.post("/api/v1/relay/reconnect")
    async def relay_reconnect(
        body: RelayOperationBody,
        actor: AuthenticatedDevice = Depends(require_scopes("admin")),
        relay_manager: RelayManager = Depends(get_relay_manager_from_app),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        snapshot = await relay_manager.reconnect(clear_credentials_state=body.clear_credentials)
        repository.append_audit_event(
            "relay.reconnect.requested",
            actor_type="remote_device",
            actor_id=actor.device.device_id,
            detail={"clear_credentials": body.clear_credentials},
        )
        return snapshot

    @app.post("/api/v1/relay/disconnect")
    async def relay_disconnect(
        body: RelayOperationBody,
        actor: AuthenticatedDevice = Depends(require_scopes("admin")),
        relay_manager: RelayManager = Depends(get_relay_manager_from_app),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        snapshot = await relay_manager.disconnect(clear_credentials_state=body.clear_credentials)
        repository.append_audit_event(
            "relay.disconnect.requested",
            actor_type="remote_device",
            actor_id=actor.device.device_id,
            detail={"clear_credentials": body.clear_credentials},
        )
        return snapshot

    @app.post("/api/v1/relay/connect-token")
    async def relay_connect_token(
        body: RelayConnectTokenBody,
        actor: AuthenticatedDevice = Depends(require_scopes()),
        relay_manager: RelayManager = Depends(get_relay_manager_from_app),
    ) -> dict:
        if actor.token_id is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "bearer_token_required", "message": t("api.error.bearer_token_required")},
            )
        try:
            return await relay_manager.issue_connect_token(actor, ttl_seconds=body.ttl_seconds)
        except RelayError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc

    @app.post("/api/v1/link/config/set")
    async def set_link_config(
        request: Request,
        body: LinkConfigSetBody,
        actor: AuthenticatedDevice = Depends(require_scopes("admin")),
        config: LinkConfig = Depends(get_config_from_app),
        relay_manager: RelayManager = Depends(get_relay_manager_from_app),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        try:
            updated_config, outcome = update_link_config_value(config, body.key, body.value)
        except ControlPlaneError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

        replace_runtime_config(request, updated_config)
        relay_snapshot = (
            await relay_manager.reconcile_config(
                clear_credentials_state=outcome["clear_relay_credentials"],
                force_reconnect=outcome["relay_sync_required"],
            )
            if outcome["changed"] and outcome["relay_sync_required"]
            else relay_manager.snapshot()
        )
        if outcome["changed"]:
            repository.append_audit_event(
                "link.config.updated",
                actor_type="remote_device",
                actor_id=actor.device.device_id,
                detail={
                    "key": body.key,
                    "restart_required": outcome["restart_required"],
                    "relay_sync_required": outcome["relay_sync_required"],
                },
            )
        return outcome | {"config": public_link_config(updated_config), "relay": relay_snapshot}

    @app.get("/api/v1/pairing/sessions")
    def list_pairing_sessions(
        _: AuthenticatedDevice = Depends(require_scopes("admin")),
        repository: LinkRepository = Depends(get_repository_from_app),
        include_non_pending: bool = False,
    ) -> dict:
        repository.expire_stale_pairings()
        sessions = repository.list_pairing_sessions(include_non_pending=include_non_pending)
        return {"sessions": [session.model_dump(mode="json") for session in sessions]}

    @app.get("/api/v1/pairing/sessions/{session_id}")
    def pairing_session(
        session_id: str,
        code: str | None = Query(default=None, min_length=1, max_length=32),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        repository.expire_stale_pairings()
        session = repository.get_pairing_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "pairing_session_not_found", "message": t("api.error.pairing_session_not_found")},
            )
        payload = {
            "session_id": session.session_id,
            "status": session.status,
            "expires_at": session.expires_at,
        }
        if code and normalize_pairing_code(code) == session.code:
            payload["claimed_device_id"] = session.claimed_device_id
        return payload

    @app.delete("/api/v1/pairing/sessions/{session_id}")
    def cancel_pairing_session(
        session_id: str,
        actor: AuthenticatedDevice = Depends(require_scopes("admin")),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        session = repository.cancel_pairing_session(session_id)
        if session is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "pairing_session_not_found", "message": t("api.error.pairing_session_not_found")},
            )
        if session.status != "cancelled":
            raise HTTPException(
                status_code=409,
                detail={"code": "pairing_session_not_pending", "message": t("security.pairing_session_not_pending")},
            )
        repository.append_audit_event(
            "pairing.session.cancelled",
            actor_type="remote_device",
            actor_id=actor.device.device_id,
            detail={"session_id": session_id},
        )
        return session.model_dump(mode="json")

    @app.post("/api/v1/pairing/claim")
    def claim_pairing(
        body: PairingClaimBody,
        security_manager: SecurityManager = Depends(get_security_manager),
    ) -> dict:
        try:
            session, authenticated, issued, issued_refresh = security_manager.claim_pairing_session(
                session_id=body.session_id,
                code=body.code,
                device_label=body.device_label,
                device_platform=body.device_platform,
            )
        except SecurityError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
        return {
            "pairing_session": session.model_dump(mode="json"),
            "device": authenticated.device.model_dump(mode="json"),
            "access_token": issued.model_dump(mode="json"),
            "refresh_token": issued_refresh.model_dump(mode="json"),
        }

    @app.get("/api/v1/devices")
    def list_devices(
        _: AuthenticatedDevice = Depends(require_scopes("devices:manage")),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        return {"devices": [device.model_dump(mode="json") for device in repository.list_devices()]}

    @app.delete("/api/v1/devices/{device_id}")
    def revoke_device(
        device_id: str,
        actor: AuthenticatedDevice = Depends(require_scopes("devices:manage")),
        repository: LinkRepository = Depends(get_repository_from_app),
    ) -> dict:
        revoked = repository.revoke_device(device_id)
        if revoked:
            repository.append_audit_event(
                "device.revoked",
                actor_type="remote_device",
                actor_id=actor.device.device_id,
                detail={"revoked_device_id": device_id},
            )
        return {"ok": revoked, "device_id": device_id}

    @app.get("/api/v1/audit")
    def list_audit_events(
        _: AuthenticatedDevice = Depends(require_scopes("admin")),
        repository: LinkRepository = Depends(get_repository_from_app),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict:
        return {"events": [event.model_dump(mode="json") for event in repository.list_audit_events(limit=limit)]}

    @app.get("/api/v1/hermes/config")
    def get_hermes_config(
        _: AuthenticatedDevice = Depends(require_scopes("config:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.get_config()
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/config/set")
    def set_hermes_config(
        body: ConfigSetBody,
        _: AuthenticatedDevice = Depends(require_scopes("config:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.set_config_value(body.key, body.value)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/config/raw")
    def get_hermes_config_raw(
        _: AuthenticatedDevice = Depends(require_scopes("config:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"yaml": adapter.get_config_raw()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.put("/api/v1/hermes/config/raw")
    def set_hermes_config_raw(
        body: RawConfigBody,
        _: AuthenticatedDevice = Depends(require_scopes("config:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"ok": True, "config": adapter.save_config_raw(body.yaml_text)}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/env")
    def get_hermes_env(
        _: AuthenticatedDevice = Depends(require_scopes("env:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"vars": adapter.list_env_vars()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.put("/api/v1/hermes/env")
    def set_hermes_env(
        body: EnvSetBody,
        _: AuthenticatedDevice = Depends(require_scopes("env:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.set_env_value(body.key, body.value)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.delete("/api/v1/hermes/env")
    def delete_hermes_env(
        body: EnvDeleteBody,
        _: AuthenticatedDevice = Depends(require_scopes("env:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.delete_env_value(body.key)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/providers")
    def list_provider_status(
        _: AuthenticatedDevice = Depends(require_scopes("env:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"providers": adapter.get_provider_auth_status()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/sessions")
    def list_hermes_sessions(
        _: AuthenticatedDevice = Depends(require_scopes("sessions:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10000),
    ) -> dict:
        try:
            return adapter.list_sessions(limit=limit, offset=offset)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/sessions/search")
    def search_hermes_sessions(
        q: str,
        _: AuthenticatedDevice = Depends(require_scopes("sessions:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict:
        try:
            return adapter.search_sessions(q, limit=limit)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/sessions/{session_id}")
    def get_hermes_session(
        session_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("sessions:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.get_session(session_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/sessions/{session_id}/messages")
    def get_hermes_session_messages(
        session_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("sessions:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.get_session_messages(session_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.delete("/api/v1/hermes/sessions/{session_id}")
    def delete_hermes_session(
        session_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("sessions:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.delete_session(session_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/logs")
    def get_hermes_logs(
        _: AuthenticatedDevice = Depends(require_scopes("logs:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
        file: str = "agent",
        lines: int = Query(default=100, ge=1, le=1000),
        search: str | None = None,
    ) -> dict:
        try:
            return adapter.list_logs(file=file, lines=lines, search=search)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/analytics/usage")
    def get_hermes_analytics(
        _: AuthenticatedDevice = Depends(require_scopes("analytics:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
        days: int = Query(default=30, ge=1, le=365),
    ) -> dict:
        try:
            return adapter.get_usage_analytics(days=days)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/cron/jobs")
    def list_hermes_cron_jobs(
        _: AuthenticatedDevice = Depends(require_scopes("cron:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"jobs": adapter.list_cron_jobs()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/cron/jobs/{job_id}")
    def get_hermes_cron_job(
        job_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("cron:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.get_cron_job(job_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/cron/jobs")
    def create_hermes_cron_job(
        body: CronCreateBody,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.create_cron_job(
                prompt=body.prompt,
                schedule=body.schedule,
                name=body.name,
                deliver=body.deliver,
                skills=body.skills,
            )
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.put("/api/v1/hermes/cron/jobs/{job_id}")
    def update_hermes_cron_job(
        job_id: str,
        body: CronUpdateBody,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.update_cron_job(job_id, body.updates)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/cron/jobs/{job_id}/pause")
    def pause_hermes_cron_job(
        job_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.pause_cron_job(job_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/cron/jobs/{job_id}/resume")
    def resume_hermes_cron_job(
        job_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.resume_cron_job(job_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/cron/jobs/{job_id}/trigger")
    def trigger_hermes_cron_job(
        job_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.trigger_cron_job(job_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.delete("/api/v1/hermes/cron/jobs/{job_id}")
    def delete_hermes_cron_job(
        job_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("cron:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.delete_cron_job(job_id)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/skills")
    def get_hermes_skills(
        _: AuthenticatedDevice = Depends(require_scopes("status:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"skills": adapter.list_skills()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.put("/api/v1/hermes/skills/toggle")
    def toggle_hermes_skill(
        body: SkillToggleBody,
        _: AuthenticatedDevice = Depends(require_scopes("config:write")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.toggle_skill(body.name, enabled=body.enabled)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/toolsets")
    def get_hermes_toolsets(
        _: AuthenticatedDevice = Depends(require_scopes("status:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return {"toolsets": adapter.list_toolsets()}
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.get("/api/v1/hermes/profiles")
    def get_hermes_profiles(
        _: AuthenticatedDevice = Depends(require_scopes("status:read")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.list_profiles()
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/backup")
    def create_hermes_backup(
        body: BackupCreateBody,
        _: AuthenticatedDevice = Depends(require_scopes("admin")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.create_backup(body.output_path)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/backup/restore")
    def restore_hermes_backup(
        body: BackupRestoreBody,
        _: AuthenticatedDevice = Depends(require_scopes("admin")),
        adapter: HermesAdapter = Depends(get_adapter_from_app),
    ) -> dict:
        try:
            return adapter.restore_backup(body.archive_path, force=body.force)
        except HermesAdapterError as exc:
            _raise_http_from_adapter(exc)

    @app.post("/api/v1/hermes/runs", response_model=None)
    def create_hermes_run(
        body: RunCreateBody,
        actor: AuthenticatedDevice = Depends(require_scopes("chat")),
        repository: LinkRepository = Depends(get_repository_from_app),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
    ):
        if body.continue_session and not body.session_id:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_run_request", "message": t("api.error.run_continue_requires_session")},
            )
        payload = body.model_dump(mode="json")
        try:
            summary = execution_manager.start_run(payload)
            repository.append_audit_event(
                "hermes.run.started",
                actor_type="remote_device",
                actor_id=actor.device.device_id,
                detail={
                    "run_id": summary["run_id"],
                    "session_id": summary["session_id"],
                    "continue_session": body.continue_session,
                },
            )
            if body.wait_for_completion:
                completed = execution_manager.wait_for_terminal(
                    summary["run_id"],
                    timeout_seconds=body.timeout_seconds,
                )
                if completed is not None:
                    return completed
            return JSONResponse(status_code=202, content=summary)
        except ExecutionError as exc:
            _raise_http_from_execution(exc)

    @app.get("/api/v1/hermes/runs")
    def list_hermes_runs(
        _: AuthenticatedDevice = Depends(require_scopes("chat")),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict:
        return {"runs": execution_manager.list_runs(limit=limit)}

    @app.get("/api/v1/hermes/runs/{run_id}")
    def get_hermes_run(
        run_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("chat")),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
    ) -> dict:
        try:
            return execution_manager.get_run(run_id)
        except ExecutionError as exc:
            _raise_http_from_execution(exc)

    @app.get("/api/v1/hermes/runs/{run_id}/events")
    def stream_hermes_run_events(
        run_id: str,
        _: AuthenticatedDevice = Depends(require_scopes("chat")),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
    ) -> StreamingResponse:
        try:
            subscription = execution_manager.subscribe(run_id)
        except ExecutionError as exc:
            _raise_http_from_execution(exc)

        async def iterator():
            try:
                while True:
                    try:
                        item = await asyncio.to_thread(subscription.get, True, 15.0)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        yield ": stream closed\n\n"
                        break
                    event_name = str(item.get("event") or "message")
                    payload = json.dumps(item, ensure_ascii=False)
                    yield f"event: {event_name}\ndata: {payload}\n\n"
            finally:
                execution_manager.unsubscribe(run_id, subscription)

        return StreamingResponse(
            iterator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/hermes/runs/{run_id}/cancel")
    def cancel_hermes_run(
        run_id: str,
        actor: AuthenticatedDevice = Depends(require_scopes("chat")),
        repository: LinkRepository = Depends(get_repository_from_app),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
    ) -> dict:
        try:
            summary = execution_manager.cancel_run(run_id)
        except ExecutionError as exc:
            _raise_http_from_execution(exc)
        repository.append_audit_event(
            "hermes.run.cancel_requested",
            actor_type="remote_device",
            actor_id=actor.device.device_id,
            detail={"run_id": run_id, "session_id": summary["session_id"]},
        )
        return summary

    @app.post("/api/v1/hermes/runs/{run_id}/retry", response_model=None)
    def retry_hermes_run(
        run_id: str,
        body: RunRetryBody,
        actor: AuthenticatedDevice = Depends(require_scopes("chat")),
        repository: LinkRepository = Depends(get_repository_from_app),
        execution_manager: HermesExecutionManager = Depends(get_execution_manager_from_app),
    ):
        try:
            summary = execution_manager.retry_run(run_id, timeout_seconds=body.timeout_seconds)
            repository.append_audit_event(
                "hermes.run.retried",
                actor_type="remote_device",
                actor_id=actor.device.device_id,
                detail={"source_run_id": run_id, "retry_run_id": summary["run_id"], "session_id": summary["session_id"]},
            )
            if body.wait_for_completion:
                completed = execution_manager.wait_for_terminal(
                    summary["run_id"],
                    timeout_seconds=body.timeout_seconds,
                )
                if completed is not None:
                    return completed
            return JSONResponse(status_code=202, content=summary)
        except ExecutionError as exc:
            _raise_http_from_execution(exc)

    return app
