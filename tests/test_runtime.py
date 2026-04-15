import json

from hermes_link.constants import LEGACY_DEFAULT_DEVICE_SCOPES, PRESET_DEFAULT_DEVICE_SCOPES_V1
from hermes_link.runtime import bootstrap_runtime, get_runtime_paths, load_config, set_runtime_home


def test_bootstrap_runtime_creates_config_and_database(tmp_path):
    set_runtime_home(tmp_path)
    paths, config = bootstrap_runtime()

    assert paths.config_path.exists()
    assert paths.db_path.exists()
    assert config.link_id.startswith("hlk_node_")
    assert get_runtime_paths().base_home == tmp_path


def test_runtime_home_env_redirects_all_runtime_dirs(tmp_path, monkeypatch):
    set_runtime_home(None)
    monkeypatch.setenv("HERMES_LINK_HOME", str(tmp_path))

    paths, _ = bootstrap_runtime()

    assert paths.config_path.parent == tmp_path / "config"
    assert paths.db_path.parent == tmp_path / "data"
    assert paths.log_path.parent == tmp_path / "state" / "logs"


def test_load_config_migrates_legacy_default_device_scopes(tmp_path):
    set_runtime_home(tmp_path)
    paths, _ = bootstrap_runtime()

    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    raw["security"]["default_device_scopes"] = list(LEGACY_DEFAULT_DEVICE_SCOPES)
    paths.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    config = load_config()

    assert "admin" in config.security.default_device_scopes
    assert "devices:manage" in config.security.default_device_scopes


def test_load_config_migrates_v1_default_device_scopes(tmp_path):
    set_runtime_home(tmp_path)
    paths, _ = bootstrap_runtime()

    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    raw["security"]["default_device_scopes"] = list(PRESET_DEFAULT_DEVICE_SCOPES_V1)
    paths.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    config = load_config()

    assert "admin" in config.security.default_device_scopes
    assert "devices:manage" in config.security.default_device_scopes


def test_load_config_persists_backfilled_relay_base_url(tmp_path):
    set_runtime_home(tmp_path)
    paths, _ = bootstrap_runtime()

    raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
    raw["network"]["relay_url"] = "https://relay.example.com"
    raw["relay"]["relay_base_url"] = None
    paths.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    config = load_config()
    persisted = json.loads(paths.config_path.read_text(encoding="utf-8"))

    assert config.relay.relay_base_url == "https://relay.example.com"
    assert persisted["relay"]["relay_base_url"] == "https://relay.example.com"
