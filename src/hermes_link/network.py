from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from hermes_link.i18n import t
from hermes_link.models import ConnectionPathSnapshot, LinkConfig, TopologySnapshot

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_UNSPECIFIED_HOSTS = {"0.0.0.0", "::", "[::]"}
_IPV4_PROBE_TARGET = ("8.8.8.8", 80)
_IPV6_PROBE_TARGET = ("2001:4860:4860::8888", 80, 0, 0)


def _normalize_host(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized


def is_loopback_host(value: str | None) -> bool:
    normalized = _normalize_host(value)
    if normalized in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_unspecified_host(value: str | None) -> bool:
    normalized = _normalize_host(value)
    if normalized in _UNSPECIFIED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_unspecified
    except ValueError:
        return False


def allows_direct_inbound(config: LinkConfig) -> bool:
    host = _normalize_host(config.network.api_host)
    if not host:
        return False
    if is_loopback_host(host):
        return False
    return True


def parse_public_base_url(public_base_url: str | None):
    if not public_base_url:
        return None
    parsed = urlparse(public_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed


def derive_relay_proxy_url(config: LinkConfig) -> str | None:
    relay_base = parse_public_base_url(config.relay.relay_base_url or config.network.relay_url)
    if not relay_base:
        return None
    base_path = (relay_base.path or "").rstrip("/")
    proxy_path = f"{base_path}/api/v1/relay/links/{config.link_id}/http" if base_path else f"/api/v1/relay/links/{config.link_id}/http"
    base = relay_base._replace(path=proxy_path, params="", query="", fragment="")
    return base.geturl().rstrip("/")


def _normalize_ip_candidate(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if "%" in normalized:
        normalized = normalized.split("%", 1)[0]
    return normalized


def _sorted_ip_strings(values: set[str]) -> list[str]:
    return sorted(
        values,
        key=lambda item: (
            ipaddress.ip_address(item).version,
            ipaddress.ip_address(item).packed,
        ),
    )


def _filter_usable_lan_addresses(addresses: set[str]) -> list[str]:
    filtered: set[str] = set()
    for raw_address in addresses:
        normalized = _normalize_ip_candidate(raw_address)
        if not normalized:
            continue
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_link_local or parsed.is_multicast:
            continue
        filtered.add(parsed.compressed)
    return _sorted_ip_strings(filtered)


def _lan_listener_urls(config: LinkConfig, lan_ips: list[str]) -> list[str]:
    bind_host = _normalize_host(config.network.api_host)
    if is_unspecified_host(bind_host):
        return _build_urls(lan_ips, config.network.api_port)
    if bind_host and not is_loopback_host(bind_host):
        try:
            ip = ipaddress.ip_address(bind_host)
        except ValueError:
            return _build_urls(lan_ips, config.network.api_port)
        return _build_urls([ip.compressed], config.network.api_port)
    return []


def list_lan_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(_IPV4_PROBE_TARGET)
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for candidate in {hostname, socket.getfqdn()}:
            if not candidate:
                continue
            for family, _, _, _, sockaddr in socket.getaddrinfo(candidate, None, socket.AF_INET):
                if family == socket.AF_INET:
                    addresses.add(sockaddr[0])
    except OSError:
        pass

    return _filter_usable_lan_addresses(addresses)


def list_lan_ipv6_addresses() -> list[str]:
    addresses: set[str] = set()

    if not socket.has_ipv6:
        return []

    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.connect(_IPV6_PROBE_TARGET)
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for candidate in {hostname, socket.getfqdn()}:
            if not candidate:
                continue
            for family, _, _, _, sockaddr in socket.getaddrinfo(candidate, None, socket.AF_INET6):
                if family == socket.AF_INET6:
                    addresses.add(sockaddr[0])
    except OSError:
        pass

    return _filter_usable_lan_addresses(addresses)


def list_lan_listener_urls(config: LinkConfig) -> list[str]:
    if not config.network.allow_lan_direct:
        return []
    if not allows_direct_inbound(config):
        return []

    bind_host = _normalize_host(config.network.api_host)
    if bind_host == "0.0.0.0":
        return _build_urls(list_lan_ipv4_addresses(), config.network.api_port)
    if bind_host in {"::", "[::]"}:
        return _build_urls(list_lan_ipv6_addresses(), config.network.api_port)

    try:
        bind_ip = ipaddress.ip_address(bind_host)
    except ValueError:
        lan_ips = list_lan_ipv4_addresses() + list_lan_ipv6_addresses()
        return _lan_listener_urls(config, lan_ips)

    if isinstance(bind_ip, ipaddress.IPv4Address):
        return _build_urls([bind_ip.compressed], config.network.api_port)
    return _build_urls([bind_ip.compressed], config.network.api_port)


def _build_urls(hosts: list[str], port: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        normalized = _normalize_ip_candidate(host)
        if not normalized:
            continue
        try:
            ip = ipaddress.ip_address(normalized)
        except ValueError:
            url = f"http://{normalized}:{port}"
        else:
            url = f"http://[{ip.compressed}]:{port}" if isinstance(ip, ipaddress.IPv6Address) else f"http://{ip.compressed}:{port}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def allowed_host_patterns(config: LinkConfig) -> list[str]:
    hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
    }

    bind_host = _normalize_host(config.network.api_host)
    if bind_host and not is_unspecified_host(bind_host):
        hosts.add(bind_host)

    for hostname in {socket.gethostname().strip(), socket.getfqdn().strip()}:
        if hostname:
            hosts.add(hostname.lower())

    for address in list_lan_ipv4_addresses():
        hosts.add(address)
    for address in list_lan_ipv6_addresses():
        hosts.add(address)

    public_url = parse_public_base_url(config.network.public_base_url)
    if public_url and public_url.hostname:
        hosts.add(public_url.hostname.lower())

    for host in config.network.extra_allowed_hosts:
        normalized = _normalize_host(host)
        if normalized:
            hosts.add(normalized)

    return sorted(hosts)


def build_topology_snapshot(config: LinkConfig) -> TopologySnapshot:
    direct_bind = allows_direct_inbound(config)
    lan_urls = list_lan_listener_urls(config)

    if not config.network.allow_lan_direct:
        lan = ConnectionPathSnapshot(
            mode="lan_direct",
            status="disabled",
            reason="lan_direct_disabled",
            message=t("network.lan_disabled"),
        )
    elif not direct_bind:
        lan = ConnectionPathSnapshot(
            mode="lan_direct",
            status="unavailable",
            reason="bind_localhost_only",
            message=t("network.bind_localhost_only"),
        )
    elif lan_urls:
        lan = ConnectionPathSnapshot(
            mode="lan_direct",
            status="ready",
            urls=lan_urls,
            message=t("network.lan_available"),
        )
    else:
        lan = ConnectionPathSnapshot(
            mode="lan_direct",
            status="unavailable",
            reason="no_lan_address",
            message=t("network.no_lan_address"),
        )

    if not config.network.allow_public_direct:
        public_direct = ConnectionPathSnapshot(
            mode="public_direct",
            status="disabled",
            reason="public_direct_disabled",
            message=t("network.public_disabled"),
        )
    elif not parse_public_base_url(config.network.public_base_url):
        public_direct = ConnectionPathSnapshot(
            mode="public_direct",
            status="unavailable",
            reason="public_base_url_invalid",
            message=t("network.public_invalid"),
        )
    elif not direct_bind:
        public_direct = ConnectionPathSnapshot(
            mode="public_direct",
            status="unavailable",
            reason="bind_localhost_only",
            message=t("network.bind_localhost_only"),
        )
    elif config.network.public_base_url:
        public_direct = ConnectionPathSnapshot(
            mode="public_direct",
            status="ready",
            urls=[config.network.public_base_url],
            message=t("network.public_configured"),
        )
    else:
        public_direct = ConnectionPathSnapshot(
            mode="public_direct",
            status="unavailable",
            reason="public_base_url_missing",
            message=t("network.public_missing"),
        )

    if not config.network.allow_relay:
        relay = ConnectionPathSnapshot(
            mode="relay",
            status="disabled",
            reason="relay_disabled",
            message=t("network.relay_disabled"),
        )
    elif config.relay.proxy_base_url and config.relay.connection_status == "connected":
        relay = ConnectionPathSnapshot(
            mode="relay",
            status="ready",
            urls=[config.relay.proxy_base_url],
            message=t("network.relay_ready"),
        )
    elif config.network.relay_url:
        relay_proxy_url = config.relay.proxy_base_url or derive_relay_proxy_url(config)
        relay = ConnectionPathSnapshot(
            mode="relay",
            status="configured",
            urls=[relay_proxy_url] if relay_proxy_url else [config.network.relay_url],
            message=t("network.relay_configured"),
        )
    else:
        relay = ConnectionPathSnapshot(
            mode="relay",
            status="planned",
            reason="relay_not_configured",
            message=t("network.relay_not_configured"),
        )

    return TopologySnapshot(
        lan_direct=lan,
        public_direct=public_direct,
        relay=relay,
    )


def preferred_pairing_urls(topology: TopologySnapshot) -> list[str]:
    urls: list[str] = []
    for path in (topology.lan_direct, topology.public_direct, topology.relay):
        urls.extend(path.urls)
    return urls
