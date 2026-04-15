import json
import queue

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hermes_link.api import create_app
from hermes_link.cli import app as cli_app
from hermes_link.control_plane import public_relay_snapshot
from hermes_link.hermes_adapter import HermesAdapter
from hermes_link.relay import verify_connect_token
from hermes_link.runtime import bootstrap_runtime, load_config, save_config, set_runtime_home
from hermes_link.security import SecurityManager
from hermes_link.storage import LinkRepository
from test_hermes_adapter import _seed_fake_hermes_home


class FakeExecutionManager:
    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.counter = 0
        self.unsubscribed = False

    def start_run(self, payload: dict) -> dict:
        self.counter += 1
        run_id = f"run_{self.counter}"
        summary = {
            "run_id": run_id,
            "session_id": payload.get("session_id") or f"sess_{self.counter}",
            "status": "running",
            "created_at": "2026-04-14T00:00:00+00:00",
            "updated_at": "2026-04-14T00:00:00+00:00",
            "input_preview": "hello",
            "continue_session": bool(payload.get("continue_session")),
            "event_count": 1,
            "final_output": None,
            "error": None,
            "usage": {},
            "cancel_requested": False,
            "cancel_reason": None,
        }
        self.runs[run_id] = summary
        return dict(summary)

    def wait_for_terminal(self, run_id: str, *, timeout_seconds: float | None = None) -> dict | None:
        summary = dict(self.runs[run_id])
        summary["status"] = "completed"
        summary["final_output"] = "done"
        summary["usage"] = {"total_tokens": 12}
        self.runs[run_id] = summary
        return dict(summary)

    def list_runs(self, *, limit: int = 20) -> list[dict]:
        return list(self.runs.values())[:limit]

    def get_run(self, run_id: str) -> dict:
        return dict(self.runs[run_id])

    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        q.put({"event": "run.started", "run_id": run_id, "session_id": self.runs[run_id]["session_id"], "sequence": 1})
        q.put({"event": "message.delta", "run_id": run_id, "session_id": self.runs[run_id]["session_id"], "sequence": 2, "delta": "hi"})
        q.put(None)
        return q

    def unsubscribe(self, run_id: str, subscription: queue.Queue) -> None:
        self.unsubscribed = True

    def cancel_run(self, run_id: str, *, reason: str = "cancelled") -> dict:
        summary = dict(self.runs[run_id])
        summary["status"] = "cancelled"
        summary["cancel_requested"] = True
        summary["cancel_reason"] = reason
        self.runs[run_id] = summary
        return dict(summary)

    def retry_run(self, run_id: str, *, timeout_seconds: float | None = None) -> dict:
        return self.start_run({"session_id": f"{self.runs[run_id]['session_id']}_retry"})


class FakeRelayManager:
    def __init__(self, config):
        self.config = config
        self.reconcile_calls: list[dict] = []
        self.reconnect_calls: list[dict] = []
        self.disconnect_calls: list[dict] = []

    def snapshot(self) -> dict:
        return public_relay_snapshot(self.config)

    async def reconcile_config(self, *, clear_credentials_state: bool = False, force_reconnect: bool = False) -> dict:
        self.reconcile_calls.append(
            {
                "clear_credentials_state": clear_credentials_state,
                "force_reconnect": force_reconnect,
            }
        )
        return self.snapshot()

    async def reconnect(self, *, clear_credentials_state: bool = False) -> dict:
        self.reconnect_calls.append({"clear_credentials_state": clear_credentials_state})
        return self.snapshot()

    async def disconnect(self, *, clear_credentials_state: bool = False) -> dict:
        self.disconnect_calls.append({"clear_credentials_state": clear_credentials_state})
        return self.snapshot()


def _claim_test_token(tmp_path, *, scopes: list[str] | None = None) -> tuple[TestClient, str]:
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session(scopes=scopes)
    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Pixel",
            "device_platform": "android",
        },
    )
    token = claim_response.json()["access_token"]["token"]
    return client, token


def _claim_test_tokens(tmp_path, *, scopes: list[str] | None = None) -> tuple[TestClient, str, str]:
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session(scopes=scopes)
    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Pixel",
            "device_platform": "android",
        },
    )
    payload = claim_response.json()
    return client, payload["access_token"]["token"], payload["refresh_token"]["token"]


def test_api_pairing_claim_and_status(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))

    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Pixel",
            "device_platform": "android",
        },
    )
    assert claim_response.status_code == 200
    token = claim_response.json()["access_token"]["token"]

    status_response = client.get(
        "/api/v1/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["link_id"] == config.link_id


def test_api_supports_auth_introspection_refresh_and_logout(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Pixel",
            "device_platform": "android",
        },
    )
    token = claim_response.json()["access_token"]["token"]
    refresh_token = claim_response.json()["refresh_token"]["token"]
    old_headers = {"Authorization": f"Bearer {token}"}

    me_response = client.get("/api/v1/auth/me", headers=old_headers)
    assert me_response.status_code == 200
    assert me_response.json()["device"]["label"] == "Pixel"
    assert "admin" in me_response.json()["scopes"]
    assert "devices:manage" in me_response.json()["scopes"]

    refresh_response = client.post("/api/v1/auth/refresh", headers=old_headers, json={"refresh_token": refresh_token})
    assert refresh_response.status_code == 200
    refreshed_token = refresh_response.json()["access_token"]["token"]
    refreshed_refresh_token = refresh_response.json()["refresh_token"]["token"]
    new_headers = {"Authorization": f"Bearer {refreshed_token}"}
    assert refreshed_refresh_token != refresh_token

    old_status_response = client.get("/api/v1/status", headers=old_headers)
    assert old_status_response.status_code == 403

    new_status_response = client.get("/api/v1/status", headers=new_headers)
    assert new_status_response.status_code == 200

    logout_response = client.post("/api/v1/auth/logout", headers=new_headers)
    assert logout_response.status_code == 200
    assert logout_response.json()["ok"] is True

    revoked_status_response = client.get("/api/v1/status", headers=new_headers)
    assert revoked_status_response.status_code == 403


def test_api_can_rotate_access_token_with_bearer_only(tmp_path):
    client, access_token, refresh_token = _claim_test_tokens(tmp_path)

    response = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {access_token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"]["token"] != access_token
    assert payload["refresh_token"] is None
    assert payload["refresh_token_expires_at"] is not None

    old_status = client.get("/api/v1/status", headers={"Authorization": f"Bearer {access_token}"})
    assert old_status.status_code == 403

    next_refresh = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {payload['access_token']['token']}"},
        json={"refresh_token": refresh_token},
    )
    assert next_refresh.status_code == 200
    assert next_refresh.json()["refresh_token"]["token"] != refresh_token


def test_api_can_refresh_session_with_refresh_token_only(tmp_path):
    client, access_token, refresh_token = _claim_test_tokens(tmp_path)

    response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access_token"]["token"] != access_token
    assert payload["refresh_token"]["token"] != refresh_token

    second = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert second.status_code == 403


def test_api_refresh_falls_back_to_refresh_token_when_bearer_is_stale(tmp_path):
    client, access_token, refresh_token = _claim_test_tokens(tmp_path)
    refreshed = client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {access_token}"})
    assert refreshed.status_code == 200

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]["token"].startswith("hlk_")


def test_api_rejects_refresh_when_bearer_and_refresh_token_belong_to_different_devices(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session_a = security.create_pairing_session()
    session_b = security.create_pairing_session()
    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))

    claim_a = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session_a.session_id,
            "code": session_a.code,
            "device_label": "Pixel A",
            "device_platform": "android",
        },
    )
    claim_b = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session_b.session_id,
            "code": session_b.code,
            "device_label": "Pixel B",
            "device_platform": "android",
        },
    )
    access_token = claim_a.json()["access_token"]["token"]
    refresh_token_b = claim_b.json()["refresh_token"]["token"]

    response = client.post(
        "/api/v1/auth/refresh",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"refresh_token": refresh_token_b},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "session_device_mismatch"


def test_api_exposes_hermes_control_plane_fallback(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    hermes_home = _seed_fake_hermes_home(tmp_path)
    config.hermes.home = str(hermes_home)
    save_config(config)

    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, load_config())
    session = security.create_pairing_session()

    monkeypatch.setattr(HermesAdapter, "_resolve_bridge_python", lambda self: None)

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "iPhone",
            "device_platform": "ios",
        },
    )
    token = claim_response.json()["access_token"]["token"]
    headers = {"Authorization": f"Bearer {token}"}

    config_response = client.get("/api/v1/hermes/config", headers=headers)
    assert config_response.status_code == 200
    assert config_response.json()["model"] == "openai/gpt-5.4"

    env_response = client.get("/api/v1/hermes/env", headers=headers)
    assert env_response.status_code == 200
    assert len(env_response.json()["vars"]) == 2

    sessions_response = client.get("/api/v1/hermes/sessions", headers=headers)
    assert sessions_response.status_code == 200
    assert sessions_response.json()["total"] == 1

    cron_response = client.get("/api/v1/hermes/cron/jobs", headers=headers)
    assert cron_response.status_code == 200
    assert cron_response.json()["jobs"][0]["id"] == "job_1"

    skills_response = client.get("/api/v1/hermes/skills", headers=headers)
    assert skills_response.status_code == 200
    assert any(skill["name"] == "demo-skill" for skill in skills_response.json()["skills"])


def test_api_localizes_missing_bearer_error_from_accept_language(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    response = client.get("/api/v1/status", headers={"Accept-Language": "zh-CN"})

    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "missing_bearer_token",
        "message": "你还没有登录，请重新连接 Hermes Link。",
    }


def test_api_applies_anonymous_rate_limit(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()
    config.security.anonymous_requests_per_minute = 1
    save_config(config)

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    first = client.get("/api/v1/bootstrap")
    second = client.get("/api/v1/bootstrap")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "rate_limited"


def test_api_does_not_treat_remote_internal_headers_as_authenticated_rate_limit(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()
    config.security.anonymous_requests_per_minute = 1
    config.security.authenticated_requests_per_minute = 10
    save_config(config)

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("203.0.113.10", 50000))
    first = client.get(
        "/api/v1/bootstrap",
        headers={
            "X-Hermes-Link-Internal-Secret": "fake",
            "X-Hermes-Link-Device-Id": "dev_fake",
        },
    )
    second = client.get(
        "/api/v1/bootstrap",
        headers={
            "X-Hermes-Link-Internal-Secret": "fake",
            "X-Hermes-Link-Device-Id": "dev_fake",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "rate_limited"


def test_api_rejects_untrusted_host_header(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    response = client.get("/healthz", headers={"host": "evil.example"})

    assert response.status_code == 400


def test_api_host_allowlist_updates_without_restart(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    client.app.state.config.network.extra_allowed_hosts = ["new-host.example"]

    response = client.get("/healthz", headers={"host": "new-host.example"})

    assert response.status_code == 200


def test_api_supports_internal_relay_device_auth(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Relay Device",
            "device_platform": "ios",
        },
    )
    device_id = claim_response.json()["device"]["device_id"]

    response = client.get(
        "/api/v1/status",
        headers={
            "X-Hermes-Link-Internal-Secret": config.service_secret,
            "X-Hermes-Link-Device-Id": device_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["link_id"] == config.link_id


def test_api_logout_invalidates_internal_relay_device_auth(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Relay Device",
            "device_platform": "ios",
        },
    )
    token = claim_response.json()["access_token"]["token"]
    device_id = claim_response.json()["device"]["device_id"]

    before = client.get(
        "/api/v1/status",
        headers={
            "X-Hermes-Link-Internal-Secret": config.service_secret,
            "X-Hermes-Link-Device-Id": device_id,
        },
    )
    logout = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    after = client.get(
        "/api/v1/status",
        headers={
            "X-Hermes-Link-Internal-Secret": config.service_secret,
            "X-Hermes-Link-Device-Id": device_id,
        },
    )

    assert before.status_code == 200
    assert logout.status_code == 200
    assert after.status_code == 403


def test_api_issues_relay_connect_tokens(tmp_path):
    client, token = _claim_test_token(tmp_path)
    config = client.app.state.config
    config.network.relay_url = "https://relay.example.com"
    config.relay.connect_signing_secret = "relay-secret"
    config.relay.proxy_base_url = f"https://relay.example.com/api/v1/relay/links/{config.link_id}/http"

    response = client.post(
        "/api/v1/relay/connect-token",
        headers={"Authorization": f"Bearer {token}"},
        json={"ttl_seconds": 900},
    )

    assert response.status_code == 200
    payload = response.json()
    decoded = verify_connect_token("relay-secret", link_id=config.link_id, token=payload["token"])

    assert payload["link_id"] == config.link_id
    assert payload["proxy_base_url"].endswith(f"/{config.link_id}/http")
    assert decoded["device_id"].startswith("dev_")


def test_api_requires_bearer_backed_session_to_issue_relay_connect_tokens(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    claim_response = client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Relay Device",
            "device_platform": "ios",
        },
    )
    device_id = claim_response.json()["device"]["device_id"]

    response = client.post(
        "/api/v1/relay/connect-token",
        headers={
            "X-Hermes-Link-Internal-Secret": config.service_secret,
            "X-Hermes-Link-Device-Id": device_id,
        },
        json={},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "bearer_token_required"


def test_api_relay_status_redacts_sensitive_credentials(tmp_path):
    client, token = _claim_test_token(tmp_path)
    config = client.app.state.config
    config.network.relay_url = "https://relay.example.com"
    config.relay.refresh_token = "refresh-secret"
    config.relay.access_token = "access-secret"
    config.relay.connect_signing_secret = "connect-secret"
    config.relay.connection_status = "connected"
    config.relay.proxy_base_url = f"https://relay.example.com/api/v1/relay/links/{config.link_id}/http"

    response = client.get("/api/v1/relay/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["connection_status"] == "connected"
    assert "refresh_token" not in payload
    assert "access_token" not in payload
    assert "connect_signing_secret" not in payload


def test_api_lists_and_cancels_pairing_sessions(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    first = security.create_pairing_session(scopes=["chat"])
    second = security.create_pairing_session(scopes=["status:read"])
    client, admin_token = _claim_test_token(tmp_path, scopes=["admin"])
    headers = {"Authorization": f"Bearer {admin_token}"}

    listed = client.get("/api/v1/pairing/sessions", headers=headers)
    assert listed.status_code == 200
    assert {session["session_id"] for session in listed.json()["sessions"]} == {first.session_id, second.session_id}

    cancelled = client.delete(f"/api/v1/pairing/sessions/{first.session_id}", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_api_pairing_status_only_exposes_claimed_device_id_with_matching_code(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    security = SecurityManager(repository, config)
    session = security.create_pairing_session()

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    client.post(
        "/api/v1/pairing/claim",
        json={
            "session_id": session.session_id,
            "code": session.code,
            "device_label": "Pixel",
            "device_platform": "android",
        },
    )

    anonymous = client.get(f"/api/v1/pairing/sessions/{session.session_id}")
    with_code = client.get(f"/api/v1/pairing/sessions/{session.session_id}", params={"code": session.code})

    assert anonymous.status_code == 200
    assert "claimed_device_id" not in anonymous.json()
    assert with_code.status_code == 200
    assert with_code.json()["claimed_device_id"].startswith("dev_")


def test_api_updates_local_link_config_and_reconciles_relay(tmp_path):
    client, admin_token = _claim_test_token(tmp_path, scopes=["admin"])
    fake_relay_manager = FakeRelayManager(client.app.state.config)
    client.app.state.relay_manager = fake_relay_manager

    response = client.post(
        "/api/v1/link/config/set",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"key": "network.relay_url", "value": "https://relay.example.com"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["changed"] is True
    assert payload["restart_required"] is False
    assert payload["relay_sync_required"] is True
    assert payload["config"]["network"]["relay_url"] == "https://relay.example.com"
    assert fake_relay_manager.reconcile_calls == [
        {
            "clear_credentials_state": True,
            "force_reconnect": True,
        }
    ]


def test_api_relay_admin_endpoints_delegate_to_manager(tmp_path):
    client, admin_token = _claim_test_token(tmp_path, scopes=["admin"])
    fake_relay_manager = FakeRelayManager(client.app.state.config)
    client.app.state.relay_manager = fake_relay_manager
    headers = {"Authorization": f"Bearer {admin_token}"}

    reconnect = client.post("/api/v1/relay/reconnect", headers=headers, json={"clear_credentials": True})
    disconnect = client.post("/api/v1/relay/disconnect", headers=headers, json={"clear_credentials": False})

    assert reconnect.status_code == 200
    assert disconnect.status_code == 200
    assert fake_relay_manager.reconnect_calls == [{"clear_credentials_state": True}]
    assert fake_relay_manager.disconnect_calls == [{"clear_credentials_state": False}]


def test_api_exposes_run_lifecycle_and_retry(tmp_path):
    client, token = _claim_test_token(tmp_path)
    fake_execution = FakeExecutionManager()
    client.app.state.execution_manager = fake_execution
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post("/api/v1/hermes/runs", headers=headers, json={"input": "hello"})
    assert create_response.status_code == 202
    run_id = create_response.json()["run_id"]

    list_response = client.get("/api/v1/hermes/runs", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["runs"][0]["run_id"] == run_id

    detail_response = client.get(f"/api/v1/hermes/runs/{run_id}", headers=headers)
    assert detail_response.status_code == 200
    assert detail_response.json()["status"] == "running"

    cancel_response = client.post(f"/api/v1/hermes/runs/{run_id}/cancel", headers=headers)
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"

    retry_response = client.post(f"/api/v1/hermes/runs/{run_id}/retry", headers=headers, json={})
    assert retry_response.status_code == 202
    assert retry_response.json()["run_id"] == "run_2"


def test_api_can_wait_for_run_completion(tmp_path):
    client, token = _claim_test_token(tmp_path)
    client.app.state.execution_manager = FakeExecutionManager()
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/hermes/runs",
        headers=headers,
        json={"input": "hello", "wait_for_completion": True, "timeout_seconds": 2},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["final_output"] == "done"


def test_api_streams_run_events(tmp_path):
    client, token = _claim_test_token(tmp_path)
    fake_execution = FakeExecutionManager()
    fake_execution.start_run({"input": "hello"})
    client.app.state.execution_manager = fake_execution
    headers = {"Authorization": f"Bearer {token}"}

    with client.stream("GET", "/api/v1/hermes/runs/run_1/events", headers=headers) as response:
        body = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in response.iter_raw())

    assert response.status_code == 200
    assert "event: run.started" in body
    assert "event: message.delta" in body
    assert fake_execution.unsubscribed is True


def test_api_requires_session_id_when_continuing_run(tmp_path):
    client, token = _claim_test_token(tmp_path)
    client.app.state.execution_manager = FakeExecutionManager()
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/hermes/runs",
        headers=headers,
        json={"input": "hello", "continue_session": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_run_request"


def test_api_applies_configured_cors_allowlist(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()
    config.network.cors_allowed_origins = ["https://console.example.com"]
    save_config(config)

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    response = client.options(
        "/api/v1/bootstrap",
        headers={
            "Origin": "https://console.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://console.example.com"
    assert "GET" in response.headers["access-control-allow-methods"]


def test_cli_help_uses_language_override(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["help"], env={"HERMES_LINK_LANG": "zh", "HERMES_LINK_HOME": str(tmp_path)})

    assert result.exit_code == 0
    assert "常用 Hermes Link 命令" in result.output


def test_cli_config_show_redacts_secrets_by_default(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()
    config.service_secret = "svc-secret"
    config.relay.refresh_token = "refresh-secret"
    config.relay.access_token = "access-secret"
    save_config(config)

    runner = CliRunner()
    redacted = runner.invoke(cli_app, ["config", "show"], env={"HERMES_LINK_HOME": str(tmp_path)})
    raw = runner.invoke(
        cli_app,
        ["config", "show", "--include-secrets"],
        env={"HERMES_LINK_HOME": str(tmp_path)},
    )

    assert redacted.exit_code == 0
    assert raw.exit_code == 0
    redacted_payload = json.loads(redacted.output)
    raw_payload = json.loads(raw.output)
    assert "service_secret" not in redacted_payload
    assert "access_token" not in redacted_payload["relay"]
    assert raw_payload["service_secret"] == "svc-secret"
    assert raw_payload["relay"]["access_token"] == "access-secret"


def test_cli_audit_list_exposes_audit_events(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()
    repository = LinkRepository(paths.db_path)
    repository.initialize()
    SecurityManager(repository, config).create_pairing_session()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["audit", "list", "--json"], env={"HERMES_LINK_HOME": str(tmp_path)})

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["events"][0]["event_type"] == "pairing.session.created"
