from hermes_link.network import allowed_host_patterns, build_topology_snapshot
from hermes_link.runtime import bootstrap_runtime, load_config, save_config, set_runtime_home


def test_topology_marks_direct_paths_unavailable_when_bound_to_localhost(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.api_host = "127.0.0.1"
    config.network.public_base_url = "https://node.example.com"
    save_config(config)

    topology = build_topology_snapshot(load_config())

    assert topology.lan_direct.status == "unavailable"
    assert topology.lan_direct.reason == "bind_localhost_only"
    assert topology.public_direct.status == "unavailable"
    assert topology.public_direct.reason == "bind_localhost_only"


def test_topology_allows_http_for_public_direct(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.api_host = "0.0.0.0"
    config.network.public_base_url = "http://node.example.com"
    save_config(config)

    topology = build_topology_snapshot(load_config())

    assert topology.public_direct.status == "ready"
    assert topology.public_direct.urls == ["http://node.example.com"]


def test_allowed_hosts_include_public_domain_and_configured_extras(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.public_base_url = "https://node.example.com"
    config.network.extra_allowed_hosts = ["app.example.com"]
    save_config(config)

    monkeypatch.setattr("hermes_link.network.list_lan_ipv4_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr("hermes_link.network.list_lan_ipv6_addresses", lambda: ["fd00::20"])

    hosts = allowed_host_patterns(load_config())

    assert "127.0.0.1" in hosts
    assert "192.168.1.20" in hosts
    assert "fd00::20" in hosts
    assert "node.example.com" in hosts
    assert "app.example.com" in hosts


def test_topology_reports_ipv6_lan_urls_when_bound_to_ipv6(tmp_path, monkeypatch):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.api_host = "::"
    save_config(config)

    monkeypatch.setattr("hermes_link.network.list_lan_ipv6_addresses", lambda: ["fd00::20", "2001:db8::20"])

    topology = build_topology_snapshot(load_config())

    assert topology.lan_direct.status == "ready"
    assert topology.lan_direct.urls == [
        "http://[fd00::20]:47211",
        "http://[2001:db8::20]:47211",
    ]


def test_topology_derives_relay_proxy_url_before_connection(tmp_path):
    set_runtime_home(tmp_path)
    bootstrap_runtime()

    config = load_config()
    config.network.relay_url = "https://relay.example.com"
    config.relay.proxy_base_url = None
    save_config(config)

    topology = build_topology_snapshot(load_config())

    assert topology.relay.status == "configured"
    assert topology.relay.urls == [f"https://relay.example.com/api/v1/relay/links/{config.link_id}/http"]
