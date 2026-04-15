from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import websockets

from hermes_link.control_plane import clear_relay_credentials, public_relay_snapshot
from hermes_link.i18n import t
from hermes_link.models import AuthenticatedDevice, LinkConfig, utc_now_iso
from hermes_link.network import build_topology_snapshot
from hermes_link.runtime import save_config
from hermes_link.storage import LinkRepository

logger = logging.getLogger("hermes_link.relay")

INTERNAL_SECRET_HEADER = "x-hermes-link-internal-secret"
INTERNAL_DEVICE_ID_HEADER = "x-hermes-link-device-id"
INTERNAL_ROUTE_HEADER = "x-hermes-link-route"
RELAY_CONNECT_TOKEN_HEADER = "x-hermes-link-relay-connect-token"

RELAY_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60
RELAY_HEARTBEAT_SECONDS = 20
RELAY_RECONNECT_BASE_SECONDS = 3
RELAY_RECONNECT_MAX_SECONDS = 60
RELAY_CONNECT_TOKEN_DEFAULT_TTL_SECONDS = 30 * 60
RELAY_CONNECT_TOKEN_MAX_TTL_SECONDS = 60 * 60

RELAY_CONNECT_TOKEN_ISSUER = "hermes-link"
RELAY_CONNECT_TOKEN_AUDIENCE = "hermespilot-relay"
RELAY_CONNECT_TOKEN_TYPE = "hermes_link_connect"
ALLOWED_RELAY_HTTP_METHODS = {
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
}
ALLOWED_RELAY_PATH_PREFIXES = ("/api/v1/", "/healthz")


class RelayError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or t(f"relay.{code}")
        super().__init__(self.message)


def _normalize_non_empty_string(value: Any, max_length: int = 4096) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:max_length]


def _base64url_encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode_bytes(value: str) -> bytes:
    normalized = _normalize_non_empty_string(value)
    if not normalized:
        raise RelayError("relay_connect_token_invalid")
    padding = "=" * ((4 - len(normalized) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(f"{normalized}{padding}".encode("ascii"))
    except Exception as exc:  # pragma: no cover - defensive
        raise RelayError("relay_connect_token_invalid") from exc


def _base64_encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _base64_decode_bytes(value: str) -> bytes:
    normalized = _normalize_non_empty_string(value)
    if not normalized:
        return b""
    return base64.b64decode(normalized.encode("ascii"))


def _encode_json_base64url(value: dict[str, Any]) -> str:
    return _base64url_encode_bytes(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _decode_json_base64url(value: str) -> dict[str, Any]:
    try:
        return json.loads(_base64url_decode_bytes(value).decode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise RelayError("relay_connect_token_invalid") from exc


def _sign_hs256(secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return _base64url_encode_bytes(digest)


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _build_frame(frame_type: str, payload: dict[str, Any] | None = None) -> str:
    return json.dumps({"v": 1, "type": frame_type, "payload": payload or {}}, separators=(",", ":"))


def _normalize_http_method(value: Any) -> str:
    method = _normalize_non_empty_string(value, 16)
    if not method:
        return "GET"
    normalized = method.upper()
    if normalized not in ALLOWED_RELAY_HTTP_METHODS:
        raise RelayError("relay_request_method_invalid")
    return normalized


def _normalize_http_path(value: Any) -> str:
    path = _normalize_non_empty_string(value, 4096) or "/"
    if not path.startswith("/") or path.startswith("//"):
        raise RelayError("relay_request_path_invalid")
    if path == "/healthz" or path.startswith("/api/v1/"):
        return path
    raise RelayError("relay_request_path_invalid")


def _sanitize_request_headers(headers: dict[str, Any]) -> dict[str, str]:
    next_headers: dict[str, str] = {}
    for raw_name, raw_value in (headers or {}).items():
        name = _normalize_non_empty_string(raw_name, 128)
        value = _normalize_non_empty_string(raw_value, 4096)
        if not name or not value:
            continue
        lowered = name.lower()
        if lowered in {
            "connection",
            "content-length",
            "host",
            INTERNAL_SECRET_HEADER,
            INTERNAL_DEVICE_ID_HEADER,
            INTERNAL_ROUTE_HEADER,
            RELAY_CONNECT_TOKEN_HEADER,
            "transfer-encoding",
            "upgrade",
        }:
            continue
        next_headers[lowered] = value
    return next_headers


def _sanitize_response_headers(headers: httpx.Headers) -> dict[str, str]:
    next_headers: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = _normalize_non_empty_string(raw_name, 128)
        value = _normalize_non_empty_string(raw_value, 4096)
        if not name or not value:
            continue
        lowered = name.lower()
        if lowered in {"connection", "content-length", "transfer-encoding", "upgrade"}:
            continue
        next_headers[lowered] = value
    return next_headers


def _relay_request_can_skip_app_auth(method: str, path: str) -> bool:
    return method == "POST" and path == "/api/v1/auth/refresh"


def create_connect_token(secret: str, *, link_id: str, device_id: str, ttl_seconds: int = RELAY_CONNECT_TOKEN_DEFAULT_TTL_SECONDS) -> tuple[str, str]:
    # The relay connect token is a short-lived app-facing credential.
    # It is scoped to one Hermes Link instance and one paired device.
    issued_at = int(_utc_now().timestamp())
    expires_at = issued_at + ttl_seconds
    payload = {
        "iss": RELAY_CONNECT_TOKEN_ISSUER,
        "aud": RELAY_CONNECT_TOKEN_AUDIENCE,
        "token_type": RELAY_CONNECT_TOKEN_TYPE,
        "sub": link_id,
        "link_id": link_id,
        "device_id": device_id,
        "iat": issued_at,
        "exp": expires_at,
        "jti": hashlib.sha256(f"{link_id}:{device_id}:{issued_at}".encode("utf-8")).hexdigest()[:24],
    }
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _encode_json_base64url(header)
    encoded_payload = _encode_json_base64url(payload)
    signing_input = f"{encoded_header}.{encoded_payload}"
    signature = _sign_hs256(secret, signing_input)
    return f"{signing_input}.{signature}", datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()


def verify_connect_token(secret: str, *, link_id: str, token: str) -> dict[str, Any]:
    # The worker only knows the link id, so every link-scoped claim is checked
    # here before we trust the embedded device identity.
    segments = token.split(".")
    if len(segments) != 3:
        raise RelayError("relay_connect_token_invalid")
    encoded_header, encoded_payload, signature = segments
    header = _decode_json_base64url(encoded_header)
    payload = _decode_json_base64url(encoded_payload)
    if header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise RelayError("relay_connect_token_invalid")
    expected_signature = _sign_hs256(secret, f"{encoded_header}.{encoded_payload}")
    if not _constant_time_equal(signature, expected_signature):
        raise RelayError("relay_connect_token_invalid")
    if (
        payload.get("iss") != RELAY_CONNECT_TOKEN_ISSUER
        or payload.get("aud") != RELAY_CONNECT_TOKEN_AUDIENCE
        or payload.get("token_type") != RELAY_CONNECT_TOKEN_TYPE
        or payload.get("sub") != link_id
        or payload.get("link_id") != link_id
    ):
        raise RelayError("relay_connect_token_invalid")
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= int(_utc_now().timestamp()):
        raise RelayError("relay_connect_token_expired")
    return payload


class RelayManager:
    def __init__(self, config: LinkConfig, repository: LinkRepository):
        self.config = config
        self.repository = repository
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._control_socket = None
        self._heartbeat_task: asyncio.Task | None = None
        self._server_client: httpx.AsyncClient | None = None
        self._local_client: httpx.AsyncClient | None = None
        self._inflight_proxy_tasks: set[asyncio.Task] = set()

    def _get_server_client(self) -> httpx.AsyncClient:
        if self._server_client is None:
            self._server_client = httpx.AsyncClient(timeout=20.0)
        return self._server_client

    def _get_local_client(self) -> httpx.AsyncClient:
        if self._local_client is None:
            self._local_client = httpx.AsyncClient(timeout=None)
        return self._local_client

    async def start(self) -> None:
        if not self.config.network.allow_relay or not self.config.network.relay_url:
            clear_relay_credentials(self.config, save=False)
            self.config.relay.relay_base_url = _normalize_non_empty_string(self.config.network.relay_url, 2048)
            save_config(self.config)
            return
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="hermes-link-relay")

    async def stop(self, *, close_clients: bool = False, clear_credentials_state: bool = False) -> None:
        self._stop_event.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._control_socket is not None:
            with contextlib.suppress(Exception):
                await self._control_socket.close()
        for task in list(self._inflight_proxy_tasks):
            task.cancel()
        if clear_credentials_state:
            clear_relay_credentials(self.config, save=False)
        if close_clients:
            if self._server_client is not None:
                await self._server_client.aclose()
                self._server_client = None
            if self._local_client is not None:
                await self._local_client.aclose()
                self._local_client = None
        self.config.relay.connection_status = "disabled" if not self.config.network.allow_relay else "idle"
        self._task = None
        save_config(self.config)

    async def shutdown(self) -> None:
        await self.stop(close_clients=True)

    def snapshot(self) -> dict[str, Any]:
        return public_relay_snapshot(self.config)

    async def disconnect(self, *, clear_credentials_state: bool = False) -> dict[str, Any]:
        await self.stop(clear_credentials_state=clear_credentials_state)
        return self.snapshot()

    async def reconnect(self, *, clear_credentials_state: bool = False) -> dict[str, Any]:
        await self.stop(clear_credentials_state=clear_credentials_state)
        await self.start()
        return self.snapshot()

    async def reconcile_config(self, *, clear_credentials_state: bool = False, force_reconnect: bool = False) -> dict[str, Any]:
        if not self.config.network.allow_relay or not self.config.network.relay_url:
            await self.stop(clear_credentials_state=True)
            return self.snapshot()
        if clear_credentials_state or force_reconnect:
            return await self.reconnect(clear_credentials_state=clear_credentials_state)
        await self.start()
        return self.snapshot()

    async def issue_connect_token(self, actor: AuthenticatedDevice, *, ttl_seconds: int | None = None) -> dict[str, Any]:
        if not self.config.network.allow_relay or not self.config.network.relay_url:
            raise RelayError("relay_not_ready")
        secret = _normalize_non_empty_string(self.config.relay.connect_signing_secret)
        proxy_base_url = _normalize_non_empty_string(self.config.relay.proxy_base_url)
        if not secret or not proxy_base_url:
            await self.ensure_access_credentials()
            secret = _normalize_non_empty_string(self.config.relay.connect_signing_secret)
            proxy_base_url = _normalize_non_empty_string(self.config.relay.proxy_base_url)
        if not secret or not proxy_base_url:
            raise RelayError("relay_not_ready")
        ttl = ttl_seconds or RELAY_CONNECT_TOKEN_DEFAULT_TTL_SECONDS
        ttl = max(60, min(ttl, RELAY_CONNECT_TOKEN_MAX_TTL_SECONDS))
        token, expires_at = create_connect_token(
            secret,
            link_id=self.config.link_id,
            device_id=actor.device.device_id,
            ttl_seconds=ttl,
        )
        return {
            "token": token,
            "expires_at": expires_at,
            "link_id": self.config.link_id,
            "relay_base_url": self.config.relay.relay_base_url,
            "proxy_base_url": proxy_base_url,
        }

    async def ensure_access_credentials(self) -> None:
        async with self._lock:
            relay_base_url = _normalize_non_empty_string(self.config.network.relay_url, 2048)
            if self.config.relay.relay_base_url != relay_base_url:
                self.config.relay.relay_base_url = relay_base_url
                save_config(self.config)
            if self._has_valid_access_token():
                return
            if self.config.relay.refresh_token:
                try:
                    payload = await self._post_json(
                        "/api/v1/relay/access-token",
                        {"refreshToken": self.config.relay.refresh_token},
                    )
                except RelayError as exc:
                    if exc.code in {
                        "RELAY_REFRESH_TOKEN_INVALID",
                        "RELAY_REFRESH_TOKEN_REVOKED",
                        "RELAY_REFRESH_TOKEN_EXPIRED",
                    }:
                        self._clear_relay_credentials()
                    else:
                        raise
                else:
                    self._apply_server_payload(payload)
                    return
            payload = await self._post_json(
                "/api/v1/relay/bootstrap",
                {
                    "linkId": self.config.link_id,
                    "installId": self.config.install_id,
                    "displayName": self.config.display_name,
                    "hostname": socket.gethostname().strip() or self.config.display_name,
                    "platform": "python",
                    **self._build_network_snapshot_payload(),
                },
            )
            self._apply_server_payload(payload)

    def _has_valid_access_token(self) -> bool:
        access_token = _normalize_non_empty_string(self.config.relay.access_token)
        expires_at = _parse_iso_datetime(self.config.relay.access_token_expires_at)
        if not access_token or not expires_at:
            return False
        return expires_at > (_utc_now() + timedelta(seconds=RELAY_ACCESS_TOKEN_REFRESH_MARGIN_SECONDS))

    def _clear_relay_credentials(self) -> None:
        clear_relay_credentials(self.config)

    def _build_network_snapshot_payload(self) -> dict[str, Any]:
        topology = build_topology_snapshot(self.config)
        return {
            "publicBaseUrl": self.config.network.public_base_url,
            "publicEndpoints": topology.public_direct.urls or None,
            "lanEndpoints": topology.lan_direct.urls or None,
        }

    def _apply_server_payload(self, payload: dict[str, Any]) -> None:
        credentials = payload.get("credentials") if isinstance(payload, dict) else None
        link = payload.get("link") if isinstance(payload, dict) else None
        if not isinstance(credentials, dict) or not isinstance(link, dict):
            raise RelayError("relay_server_payload_invalid")
        self.config.relay.refresh_token = _normalize_non_empty_string(
            credentials.get("refreshToken"),
            4096,
        ) or self.config.relay.refresh_token
        self.config.relay.refresh_token_expires_at = (
            _normalize_non_empty_string(credentials.get("refreshTokenExpiresAt"), 128)
            or self.config.relay.refresh_token_expires_at
        )
        self.config.relay.access_token = _normalize_non_empty_string(credentials.get("accessToken"), 4096)
        self.config.relay.access_token_expires_at = _normalize_non_empty_string(credentials.get("accessTokenExpiresAt"), 128)
        self.config.relay.connect_signing_secret = _normalize_non_empty_string(credentials.get("connectSigningSecret"), 4096)
        self.config.relay.relay_base_url = _normalize_non_empty_string(link.get("relayBaseUrl"), 2048) or self.config.network.relay_url
        self.config.relay.control_websocket_url = _normalize_non_empty_string(link.get("controlWebsocketUrl"), 2048)
        self.config.relay.proxy_base_url = _normalize_non_empty_string(link.get("proxyBaseUrl"), 2048)
        save_config(self.config)

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        relay_base_url = _normalize_non_empty_string(self.config.network.relay_url, 2048)
        if not relay_base_url:
            raise RelayError("relay_not_configured")
        url = f"{relay_base_url.rstrip('/')}{path}"
        headers = {"content-type": "application/json"}
        if bearer_token:
            headers["authorization"] = f"Bearer {bearer_token}"
        try:
            response = await self._get_server_client().post(url, json=body, headers=headers)
        except Exception as exc:  # pragma: no cover - network failure
            raise RelayError("relay_server_unreachable", str(exc)) from exc
        payload: dict[str, Any] | None = None
        try:
            payload = response.json() if response.content else None
        except Exception:
            payload = None
        if response.is_success:
            return payload or {}
        error = payload.get("error") if isinstance(payload, dict) else None
        raise RelayError(
            _normalize_non_empty_string(error.get("code") if isinstance(error, dict) else None, 128)
            or "relay_server_request_failed",
            _normalize_non_empty_string(error.get("message") if isinstance(error, dict) else None, 1024)
            or f"Relay server request failed with HTTP {response.status_code}.",
        )

    async def _report_status(self, *, connection_status: str, relay_connected: bool, last_error: str | None = None) -> None:
        self.config.relay.connection_status = connection_status
        self.config.relay.last_error = last_error
        if not self.config.relay.access_token or not self.config.network.relay_url:
            save_config(self.config)
            return
        body = {
            "connectionStatus": connection_status,
            "relayConnected": relay_connected,
            "lastError": last_error,
            **self._build_network_snapshot_payload(),
        }
        try:
            payload = await self._post_json(
                "/api/v1/relay/status",
                body,
                bearer_token=self.config.relay.access_token,
            )
            link = payload.get("link") if isinstance(payload, dict) else None
            if isinstance(link, dict):
                self.config.relay.proxy_base_url = _normalize_non_empty_string(link.get("proxyBaseUrl"), 2048)
                self.config.relay.control_websocket_url = _normalize_non_empty_string(link.get("controlWebsocketUrl"), 2048)
            self.config.relay.last_status_at = utc_now_iso()
            save_config(self.config)
        except RelayError as exc:
            self.config.relay.connection_status = "degraded"
            self.config.relay.last_error = exc.message
            save_config(self.config)

    async def _run_loop(self) -> None:
        reconnect_delay = RELAY_RECONNECT_BASE_SECONDS
        while not self._stop_event.is_set():
            final_status = "idle"
            try:
                await self.ensure_access_credentials()
                control_url = _normalize_non_empty_string(self.config.relay.control_websocket_url, 2048)
                if not control_url:
                    raise RelayError("relay_not_ready")
                self.config.relay.connection_status = "connecting"
                self.config.relay.last_error = None
                save_config(self.config)
                async with websockets.connect(
                    control_url,
                    additional_headers={"Authorization": f"Bearer {self.config.relay.access_token}"},
                    ping_interval=25,
                    ping_timeout=20,
                    max_size=10 * 1024 * 1024,
                ) as websocket:
                    self._control_socket = websocket
                    self.config.relay.connection_status = "connected"
                    self.config.relay.last_connected_at = utc_now_iso()
                    self.config.relay.last_error = None
                    save_config(self.config)
                    await self._report_status(connection_status="connected", relay_connected=True, last_error=None)
                    reconnect_delay = RELAY_RECONNECT_BASE_SECONDS
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="hermes-link-relay-heartbeat")
                    async for raw_message in websocket:
                        await self._handle_control_message(raw_message)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - runtime network behavior
                message = str(exc)
                logger.warning("relay_loop_error", extra={"error_message": message})
                final_status = "degraded"
                self.config.relay.connection_status = "degraded"
                self.config.relay.last_error = message
                save_config(self.config)
                await self._report_status(connection_status="degraded", relay_connected=False, last_error=message)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, RELAY_RECONNECT_MAX_SECONDS)
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None
                self._control_socket = None
                if self._stop_event.is_set():
                    final_status = "disabled" if not self.config.network.allow_relay else "idle"
                if self.config.relay.connection_status != final_status:
                    self.config.relay.connection_status = final_status
                    save_config(self.config)

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(RELAY_HEARTBEAT_SECONDS)
            try:
                await self.ensure_access_credentials()
                await self._report_status(connection_status="connected", relay_connected=True, last_error=None)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("relay_heartbeat_failed", extra={"error_message": str(exc)})

    async def _handle_control_message(self, raw_message: str | bytes) -> None:
        try:
            message = json.loads(raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message)
        except json.JSONDecodeError:
            return
        frame_type = message.get("type")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if frame_type == "ping":
            await self._send_frame("pong", {})
            return
        if frame_type != "http.request":
            return
        task = asyncio.create_task(self._handle_http_request(payload), name="hermes-link-relay-http-request")
        self._inflight_proxy_tasks.add(task)
        task.add_done_callback(self._inflight_proxy_tasks.discard)

    async def _handle_http_request(self, payload: dict[str, Any]) -> None:
        request_id = _normalize_non_empty_string(payload.get("requestId"), 128)
        if not request_id:
            return

        response: httpx.Response | None = None
        try:
            method = _normalize_http_method(payload.get("method"))
            path = _normalize_http_path(payload.get("path"))
            connect_token = _normalize_non_empty_string(payload.get("connectToken"), 4096)
            headers = _sanitize_request_headers(payload.get("headers") or {})
            device = None
            if connect_token:
                # Relay connect tokens let the public relay path impersonate a
                # previously paired device without exposing its bearer token.
                secret = _normalize_non_empty_string(self.config.relay.connect_signing_secret, 4096)
                if not secret:
                    raise RelayError("relay_connect_token_unavailable")
                token_payload = verify_connect_token(secret, link_id=self.config.link_id, token=connect_token)
                device = self.repository.get_active_device(str(token_payload.get("device_id")))
                if device is None:
                    raise RelayError("internal_device_not_active")
                headers.pop("authorization", None)
                headers[INTERNAL_SECRET_HEADER] = self.config.service_secret
                headers[INTERNAL_DEVICE_ID_HEADER] = device.device_id
                headers[INTERNAL_ROUTE_HEADER] = "relay"
            elif "authorization" not in headers and not _relay_request_can_skip_app_auth(method, path):
                raise RelayError("relay_app_auth_required")

            content = _base64_decode_bytes(str(payload.get("bodyBase64") or ""))
            local_url = f"http://127.0.0.1:{self.config.network.api_port}{path}"
            request = self._get_local_client().build_request(method, local_url, headers=headers, content=content)
            # Stream the local FastAPI response back over the control websocket
            # so large bodies do not need to be buffered in memory.
            response = await self._get_local_client().send(request, stream=True)
            await self._send_frame(
                "http.response.start",
                {
                    "requestId": request_id,
                    "status": response.status_code,
                    "headers": _sanitize_response_headers(response.headers),
                },
            )
            async for chunk in response.aiter_bytes():
                if chunk:
                    await self._send_frame(
                        "http.response.chunk",
                        {
                            "requestId": request_id,
                            "bodyBase64": _base64_encode_bytes(chunk),
                        },
                    )
            await self._send_frame("http.response.end", {"requestId": request_id})
        except RelayError as exc:
            await self._send_frame(
                "http.response.error",
                {"requestId": request_id, "code": exc.code, "message": exc.message},
            )
        except Exception as exc:  # pragma: no cover - defensive
            await self._send_frame(
                "http.response.error",
                {"requestId": request_id, "code": "relay_local_proxy_failed", "message": str(exc)},
            )
        finally:
            if response is not None:
                await response.aclose()

    async def _send_frame(self, frame_type: str, payload: dict[str, Any]) -> None:
        if self._control_socket is None:
            raise RelayError("relay_control_offline")
        await self._control_socket.send(_build_frame(frame_type, payload))
