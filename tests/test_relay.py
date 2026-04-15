import asyncio
import contextlib

import httpx

from hermes_link.relay import RelayManager
from hermes_link.runtime import bootstrap_runtime, load_config, save_config, set_runtime_home
from hermes_link.storage import LinkRepository


class FakeStreamingResponse:
    def __init__(self, chunks: list[bytes]):
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "application/json"})
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def test_relay_manager_start_persists_disabled_state(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    config.network.allow_relay = False
    save_config(config)
    manager = RelayManager(config, repository)

    async def run_test():
        await manager.start()
        await manager.shutdown()

    asyncio.run(run_test())

    reloaded = load_config()
    assert reloaded.relay.connection_status == "disabled"
    assert reloaded.relay.access_token is None
    assert reloaded.relay.proxy_base_url is None


def test_relay_manager_closes_streaming_local_response(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    manager = RelayManager(config, repository)
    response = FakeStreamingResponse([b'{"ok":true}'])
    frames: list[tuple[str, dict]] = []

    async def fake_send(request, *, stream):
        assert stream is True
        assert str(request.url) == f"http://127.0.0.1:{config.network.api_port}/api/v1/status"
        return response

    async def fake_send_frame(frame_type: str, payload: dict):
        frames.append((frame_type, payload))

    monkeypatch.setattr(manager._get_local_client(), "send", fake_send)
    monkeypatch.setattr(manager, "_send_frame", fake_send_frame)

    async def run_test():
        await manager._handle_http_request(
            {
                "requestId": "req_1",
                "method": "GET",
                "path": "/api/v1/status",
                "headers": {"authorization": "Bearer device-token"},
            }
        )
        await manager.shutdown()

    asyncio.run(run_test())

    assert response.closed is True
    assert [frame_type for frame_type, _ in frames] == [
        "http.response.start",
        "http.response.chunk",
        "http.response.end",
    ]


def test_relay_manager_preserves_degraded_status_during_reconnect_backoff(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    config.network.relay_url = "https://relay.example.com"
    save_config(config)
    manager = RelayManager(config, repository)

    async def failing_credentials():
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "ensure_access_credentials", failing_credentials)

    async def run_test():
        task = asyncio.create_task(manager._run_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if manager._server_client is not None:
            await manager._server_client.aclose()
        if manager._local_client is not None:
            await manager._local_client.aclose()

    asyncio.run(run_test())

    assert manager.config.relay.connection_status == "degraded"
    assert manager.config.relay.last_error == "boom"


def test_relay_manager_can_restart_after_stop(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    config.network.relay_url = "https://relay.example.com"
    save_config(config)
    manager = RelayManager(config, repository)
    run_markers: list[str] = []

    async def fake_run_loop():
        run_markers.append("run")
        await manager._stop_event.wait()

    monkeypatch.setattr(manager, "_run_loop", fake_run_loop)

    async def run_test():
        await manager.start()
        await asyncio.sleep(0)
        await manager.stop()
        await manager.start()
        await asyncio.sleep(0)
        await manager.shutdown()

    asyncio.run(run_test())

    assert run_markers == ["run", "run"]


def test_relay_manager_rejects_non_api_paths(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    manager = RelayManager(config, repository)
    frames: list[tuple[str, dict]] = []

    async def fake_send_frame(frame_type: str, payload: dict):
        frames.append((frame_type, payload))

    async def fail_send(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("local proxy should not be called for rejected paths")

    monkeypatch.setattr(manager, "_send_frame", fake_send_frame)
    monkeypatch.setattr(manager._get_local_client(), "send", fail_send)

    async def run_test():
        await manager._handle_http_request(
            {
                "requestId": "req_1",
                "method": "GET",
                "path": "/docs",
                "headers": {"authorization": "Bearer device-token"},
            }
        )
        await manager.shutdown()

    asyncio.run(run_test())

    assert frames == [
        (
            "http.response.error",
            {
                "requestId": "req_1",
                "code": "relay_request_path_invalid",
                "message": "This request cannot be forwarded through Relay.",
            },
        )
    ]


def test_relay_manager_rejects_unsupported_http_methods(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    manager = RelayManager(config, repository)
    frames: list[tuple[str, dict]] = []

    async def fake_send_frame(frame_type: str, payload: dict):
        frames.append((frame_type, payload))

    async def fail_send(*args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("local proxy should not be called for rejected methods")

    monkeypatch.setattr(manager, "_send_frame", fake_send_frame)
    monkeypatch.setattr(manager._get_local_client(), "send", fail_send)

    async def run_test():
        await manager._handle_http_request(
            {
                "requestId": "req_2",
                "method": "TRACE",
                "path": "/api/v1/status",
                "headers": {"authorization": "Bearer device-token"},
            }
        )
        await manager.shutdown()

    asyncio.run(run_test())

    assert frames == [
        (
            "http.response.error",
            {
                "requestId": "req_2",
                "code": "relay_request_method_invalid",
                "message": "Relay request used an unsupported HTTP method.",
            },
        )
    ]


def test_relay_manager_allows_refresh_token_only_route_without_forwarded_auth(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    manager = RelayManager(config, repository)
    response = FakeStreamingResponse([b'{"ok":true}'])
    frames: list[tuple[str, dict]] = []

    async def fake_send(request, *, stream):
        assert stream is True
        assert request.method == "POST"
        assert str(request.url) == f"http://127.0.0.1:{config.network.api_port}/api/v1/auth/refresh"
        assert "authorization" not in request.headers
        return response

    async def fake_send_frame(frame_type: str, payload: dict):
        frames.append((frame_type, payload))

    monkeypatch.setattr(manager._get_local_client(), "send", fake_send)
    monkeypatch.setattr(manager, "_send_frame", fake_send_frame)

    async def run_test():
        await manager._handle_http_request(
            {
                "requestId": "req_refresh",
                "method": "POST",
                "path": "/api/v1/auth/refresh",
                "headers": {"content-type": "application/json"},
                "bodyBase64": "e30=",
            }
        )
        await manager.shutdown()

    asyncio.run(run_test())

    assert response.closed is True
    assert [frame_type for frame_type, _ in frames] == [
        "http.response.start",
        "http.response.chunk",
        "http.response.end",
    ]


def test_relay_manager_bootstrap_reports_network_snapshot(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    config.network.api_host = "::"
    config.network.public_base_url = "http://node.example.com:47211"
    config.network.relay_url = "https://relay.example.com"
    save_config(config)
    manager = RelayManager(config, repository)
    captured: dict = {}

    monkeypatch.setattr("hermes_link.network.list_lan_ipv6_addresses", lambda: ["fd00::20"])

    async def fake_post_json(path, body, *, bearer_token=None):
        captured["path"] = path
        captured["body"] = body
        captured["bearer_token"] = bearer_token
        return {
            "link": {
                "relayBaseUrl": "https://relay.example.com",
                "controlWebsocketUrl": f"wss://relay.example.com/api/v1/relay/control/{config.link_id}/ws",
                "proxyBaseUrl": f"https://relay.example.com/api/v1/relay/links/{config.link_id}/http",
            },
            "credentials": {
                "refreshToken": "refresh-token",
                "refreshTokenExpiresAt": "2099-01-01T00:00:00+00:00",
                "accessToken": "access-token",
                "accessTokenExpiresAt": "2099-01-01T01:00:00+00:00",
                "connectSigningSecret": "connect-secret",
            },
        }

    monkeypatch.setattr(manager, "_post_json", fake_post_json)

    async def run_test():
        await manager.ensure_access_credentials()
        await manager.shutdown()

    asyncio.run(run_test())

    assert captured["path"] == "/api/v1/relay/bootstrap"
    assert captured["bearer_token"] is None
    assert captured["body"]["publicBaseUrl"] == "http://node.example.com:47211"
    assert captured["body"]["publicEndpoints"] == ["http://node.example.com:47211"]
    assert captured["body"]["lanEndpoints"] == ["http://[fd00::20]:47211"]
