import pytest

from hermes_link.control_plane import ControlPlaneError, public_link_config, public_relay_snapshot, update_link_config_value
from hermes_link.runtime import bootstrap_runtime, set_runtime_home


def test_public_link_config_redacts_runtime_secrets(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()
    config.service_secret = "svc-secret"
    config.relay.refresh_token = "refresh-secret"
    config.relay.access_token = "access-secret"
    config.relay.connect_signing_secret = "connect-secret"

    payload = public_link_config(config)
    relay_snapshot = public_relay_snapshot(config)

    assert "service_secret" not in payload
    assert "access_token" not in payload["relay"]
    assert "refresh_token" not in payload["relay"]
    assert "connect_signing_secret" not in payload["relay"]
    assert "access_token" not in relay_snapshot
    assert relay_snapshot["has_refresh_token"] is True


def test_update_link_config_flags_relay_sync_and_restart_boundaries(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()

    updated, relay_outcome = update_link_config_value(config, "network.relay_url", "https://relay.example.com")
    _, public_outcome = update_link_config_value(updated, "network.public_base_url", "https://node.example.com")

    assert updated.network.relay_url == "https://relay.example.com"
    assert relay_outcome["relay_sync_required"] is True
    assert relay_outcome["clear_relay_credentials"] is True
    assert relay_outcome["restart_required"] is False
    assert public_outcome["restart_required"] is True


def test_update_link_config_rejects_immutable_keys(tmp_path):
    set_runtime_home(tmp_path)
    _, config = bootstrap_runtime()

    with pytest.raises(ControlPlaneError) as exc_info:
        update_link_config_value(config, "service_secret", "new-secret")

    assert exc_info.value.code == "config_key_not_mutable"
