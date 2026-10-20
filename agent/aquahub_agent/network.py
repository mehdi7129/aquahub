"""Compare administered addresses/routes while retaining meaningful changes.

Docker may replace an endpoint veth when recreating a bridged container. Only
its proven kernel IPv6 link-local addresses and their automatic routes are
ephemeral here. IPv4, global IPv6, bridges, VPNs and physical NICs stay visible.
Missing interface metadata makes comparison stricter, never more permissive.
"""
from __future__ import annotations

import ipaddress
import json
import re

_DOCKER_BRIDGE = re.compile(r"^(?:br-[0-9a-f]{12}|docker0)$")
_VETH_NAME = re.compile(r"^veth[0-9a-f]{5,11}$")
_ROUTE_KEYS = ("dst", "gateway", "dev", "table", "type", "prefsrc", "metric",
               "via", "nexthops", "multipath", "nhid", "encap", "from", "src")


def _kind(interface):
    info = interface.get("linkinfo") or {}
    return info.get("info_kind") if isinstance(info, dict) else None


def _is_veth(interface):
    kind = _kind(interface)
    if kind is not None:
        return kind == "veth"
    index, peer = interface.get("ifindex"), interface.get("link_index", interface.get("iflink"))
    return (bool(_VETH_NAME.fullmatch(str(interface.get("ifname", "")))) and
            type(index) is int and type(peer) is int and index != peer and
            type(interface.get("link_netnsid")) is int and interface["link_netnsid"] >= 0)


def _docker_veth_names(interfaces):
    bridges = {interface.get("ifname") for interface in interfaces
               if _DOCKER_BRIDGE.fullmatch(str(interface.get("ifname", ""))) and _kind(interface) == "bridge"}
    return {interface["ifname"] for interface in interfaces
            if (_VETH_NAME.fullmatch(str(interface.get("ifname", ""))) and _is_veth(interface) and
                interface.get("master") in bridges and
                type(interface.get("link_netnsid")) is int and interface["link_netnsid"] >= 0)}


def _kernel_link_local(item):
    if item.get("family") != "inet6" or item.get("scope") != "link" or item.get("protocol") != "kernel_ll":
        return False
    try:
        return ipaddress.IPv6Address(item["local"]).is_link_local
    except (ValueError, KeyError):
        return False


def _temporary_ipv6(item):
    if not (item.get("temporary") or "temporary" in item.get("flags", [])):
        return False
    try:
        return ipaddress.ip_address(item.get("local", "")).version == 6
    except ValueError:
        return False


def _automatic_veth_route(route, docker_veths, ignored_addresses):
    device = route.get("dev")
    if device not in docker_veths or route.get("protocol") not in ("kernel", 2):
        return False
    if any(key in route for key in ("gateway", "via", "nexthops", "multipath", "nhid", "encap", "prefsrc", "src", "from")):
        return False
    table = route.get("table", "main")
    if table not in ("main", "local", 254, 255):
        return False
    try:
        destination = ipaddress.ip_network(route.get("dst", ""), strict=False)
    except ValueError:
        return False
    if destination.version != 6:
        return False
    route_type = route.get("type", "unicast")
    if (route_type == "multicast" and table in ("local", 255) and
            destination == ipaddress.IPv6Network("ff00::/8") and route.get("metric") == 256):
        return True
    ignored = ignored_addresses.get(device, ())
    if route_type == "local" and table in ("local", 255) and destination.prefixlen == 128 and route.get("metric") == 0:
        return any(destination.network_address == address for address, _ in ignored)
    if route_type == "unicast" and table in ("main", 254) and route.get("metric") == 256:
        return any(destination == prefix for _, prefix in ignored)
    return False


def stable_network(addresses: list, routes: list) -> dict:
    docker_veths = _docker_veth_names(addresses)
    ignored_addresses = {}
    selected_addresses = []
    for interface in addresses:
        device = interface.get("ifname")
        for item in interface.get("addr_info", []):
            if device in docker_veths and _kernel_link_local(item):
                address = ipaddress.IPv6Address(item["local"])
                try:
                    prefix = ipaddress.IPv6Network(f"{address}/{item['prefixlen']}", strict=False)
                except (ValueError, KeyError):
                    pass  # Incomplete metadata is kept under observation.
                else:
                    ignored_addresses.setdefault(device, []).append((address, prefix))
                    continue
            # Keep all IPv4 and every global IPv6 on veths, including privacy
            # addresses. Existing physical-NIC temporary IPv6 policy is retained.
            if not _is_veth(interface) and _temporary_ipv6(item):
                continue
            if "local" in item:
                selected_addresses.append((device, item.get("family"), item["local"], item.get("prefixlen")))
    selected_routes = []
    for route in routes:
        if route.get("cache") or _automatic_veth_route(route, docker_veths, ignored_addresses):
            continue
        selected_routes.append(tuple((key, json.dumps(route[key], sort_keys=True)) for key in _ROUTE_KEYS if key in route))
    return {"addresses": sorted(selected_addresses), "routes": sorted(selected_routes)}
