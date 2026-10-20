"""Docker endpoint churn cannot hide administered IP/route changes."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aquahub_agent.drone import stable_network
from aquahub_agent.updates import UpdateManager


BRIDGE = "br-deadbeefcafe"


def fixture(name="vethabc1234", address="fe80::100"):
    addresses = [
        {"ifname": "eth0", "ifindex": 2, "addr_info": [
            {"family": "inet", "local": "192.0.2.10", "prefixlen": 24},
            {"family": "inet6", "local": "2001:db8::10", "prefixlen": 64},
            {"family": "inet6", "local": "fe80::10", "prefixlen": 64, "scope": "link", "protocol": "kernel_ll"}]},
        {"ifname": "wg0", "ifindex": 8, "linkinfo": {"info_kind": "wireguard"}, "addr_info": [
            {"family": "inet", "local": "198.51.100.5", "prefixlen": 32}]},
        {"ifname": BRIDGE, "ifindex": 5, "linkinfo": {"info_kind": "bridge"}, "addr_info": [
            {"family": "inet", "local": "172.18.0.1", "prefixlen": 16},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64, "scope": "link", "protocol": "kernel_ll"}]},
        {"ifname": name, "ifindex": 12, "link_index": 2, "master": BRIDGE, "link_netnsid": 0,
         "linkinfo": {"info_kind": "veth"}, "addr_info": [
            {"family": "inet6", "local": address, "prefixlen": 64, "scope": "link", "protocol": "kernel_ll"}]},
    ]
    routes = [
        {"dst": "default", "dev": "eth0", "gateway": "192.0.2.1", "protocol": "dhcp", "metric": 100},
        {"dst": "198.51.100.0/24", "dev": "wg0", "scope": "link"},
        {"dst": "fe80::/64", "dev": "eth0", "protocol": "kernel", "metric": 1024},
        {"dst": "172.18.0.0/16", "dev": BRIDGE, "protocol": "kernel", "prefsrc": "172.18.0.1"},
        {"dst": "fe80::/64", "dev": name, "protocol": "kernel", "metric": 256},
        {"dst": address, "dev": name, "type": "local", "table": "local", "protocol": "kernel", "metric": 0},
        {"dst": "ff00::/8", "dev": name, "type": "multicast", "table": "local", "protocol": "kernel", "metric": 256},
    ]
    return addresses, routes


def update_snapshot(addresses, routes):
    # Exercise the actual UpdateManager observation/serialization adapter too.
    responses = [SimpleNamespace(stdout=json.dumps(value).encode()) for value in (addresses, routes)]
    with patch("aquahub_agent.updates.subprocess.run", side_effect=responses) as run:
        snapshot = UpdateManager._network_state(None)
    if run.call_args_list[0].args[0][:4] != ["/usr/sbin/ip", "-j", "-d", "address"]:
        raise AssertionError("Detailed interface metadata is required for Docker identification")
    return snapshot


class DockerVethNormalisationTests(unittest.TestCase):
    def assertChanged(self, before, after):
        self.assertNotEqual(stable_network(*before), stable_network(*after))
        self.assertNotEqual(update_snapshot(*before), update_snapshot(*after))

    def test_only_proven_automatic_veth_link_local_and_three_routes_are_excluded(self):
        before, after = fixture(), fixture("vethdef5678", "fe80::200")
        self.assertEqual(stable_network(*before), stable_network(*after))
        self.assertEqual(update_snapshot(*before), update_snapshot(*after))
        result = stable_network(*before)
        self.assertEqual(len(result["addresses"]), 6)  # physical/VPN/bridge unchanged
        self.assertEqual(len(result["routes"]), 4)
        self.assertTrue(any(row[0] == BRIDGE and row[2] == "fe80::1" for row in result["addresses"]))

    def test_missing_or_contradictory_metadata_never_broadens_the_exception(self):
        changes = [lambda a: a[-1].pop("link_netnsid"),
                   lambda a: a[-1].update(master="other-bridge"),
                   lambda a: a[-1].update(linkinfo={"info_kind": "dummy"}),
                   lambda a: a[-1].update(ifname="vpn0"),
                   lambda a: a[2].pop("linkinfo")]
        for change in changes:
            with self.subTest(change=change):
                before, after = fixture(), fixture("vethdef5678", "fe80::200")
                change(before[0])
                change(after[0])
                self.assertChanged(before, after)

    def test_peer_index_fallback_still_requires_docker_bridge_and_netns(self):
        before, after = fixture(), fixture("vethdef5678", "fe80::200")
        for addresses, _ in (before, after):
            addresses[-1].pop("linkinfo")
        self.assertEqual(stable_network(*before), stable_network(*after))
        after[0][-1]["link_index"] = after[0][-1]["ifindex"]
        self.assertChanged(before, after)

    def test_every_physical_vpn_and_bridge_address_change_is_detected(self):
        for interface, address in ((0, 0), (0, 1), (0, 2), (1, 0), (2, 0), (2, 1)):
            before = fixture()
            after = deepcopy(before)
            entry = after[0][interface]["addr_info"][address]
            entry["local"] = "192.0.2.99" if entry["family"] == "inet" else "2001:db8::99"
            with self.subTest(interface=interface, address=address):
                self.assertChanged(before, after)

    def test_all_ipv4_and_global_ipv6_on_veth_remain_monitored_even_temporary(self):
        for entry in ({"family": "inet", "local": "172.18.1.5", "prefixlen": 16},
                      {"family": "inet", "local": "169.254.1.5", "prefixlen": 16, "temporary": True},
                      {"family": "inet6", "local": "2001:db8::5", "prefixlen": 64},
                      {"family": "inet6", "local": "2001:db8::5", "prefixlen": 64, "temporary": True}):
            before = fixture()
            after = deepcopy(before)
            after[0][-1]["addr_info"].append(entry)
            with self.subTest(entry=entry):
                self.assertChanged(before, after)

    def test_static_or_unproven_link_local_assignment_is_not_excluded(self):
        for protocol in ("static", None):
            before, after = fixture(), fixture("vethdef5678", "fe80::200")
            for addresses, _ in (before, after):
                if protocol is None:
                    addresses[-1]["addr_info"][0].pop("protocol")
                else:
                    addresses[-1]["addr_info"][0]["protocol"] = protocol
            self.assertChanged(before, after)

    def test_ipv4_global_ipv6_static_custom_metric_and_gateway_routes_are_kept(self):
        routes = [
            {"dst": "172.18.99.0/24", "dev": "vethabc1234", "protocol": "kernel", "metric": 256},
            {"dst": "2001:db8:99::/64", "dev": "vethabc1234", "protocol": "kernel", "metric": 256},
            {"dst": "fe80::/64", "dev": "vethabc1234", "protocol": "static", "metric": 256},
            {"dst": "fe80::/64", "dev": "vethabc1234", "protocol": "kernel", "metric": 1234},
            {"dst": "fe80::/64", "dev": "vethabc1234", "protocol": "kernel", "metric": 256, "gateway": "fe80::99"},
            {"dst": "fe80::/64", "dev": "vethabc1234", "protocol": "kernel", "metric": 256, "table": 100},
            {"dst": "fe80::9999", "dev": "vethabc1234", "protocol": "kernel", "metric": 0, "table": "local", "type": "local"},
        ]
        for route in routes:
            before = fixture()
            after = deepcopy(before)
            after[1].append(route)
            with self.subTest(route=route):
                self.assertChanged(before, after)

    def test_physical_and_vpn_routes_are_never_filtered(self):
        for route in ({"dst": "192.0.2.0/24", "dev": "eth0", "protocol": "kernel"},
                      {"dst": "2001:db8::/64", "dev": "wg0", "protocol": "kernel"},
                      {"dst": "ff00::/8", "dev": BRIDGE, "protocol": "kernel", "table": "local", "type": "multicast", "metric": 256}):
            before = fixture()
            after = deepcopy(before)
            after[1].append(route)
            self.assertChanged(before, after)

    def test_complex_route_parameters_are_preserved_with_canonical_json(self):
        parameters = {
            "via": {"family": "inet6", "addr": "2001:db8::1"},
            "nexthops": [{"gateway": "192.0.2.1", "dev": "eth0", "weight": 1}],
            "multipath": [{"via": {"family": "inet6", "addr": "2001:db8::2"}, "dev": "wg0"}],
            "nhid": 25,
            "encap": {"type": "seg6", "segs": ["2001:db8:1::1", "2001:db8:2::1"]},
            "from": "2001:db8:3::/64",
            "src": "192.0.2.10",
        }
        for key, value in parameters.items():
            with self.subTest(parameter=key):
                before = fixture()
                before[1].append({"dst": "2001:db8:99::/64", "dev": "wg0", key: value})
                same = deepcopy(before)
                # JSON key ordering does not represent a route change.
                same[1][-1] = json.loads(json.dumps(same[1][-1], sort_keys=True))
                self.assertEqual(stable_network(*before), stable_network(*same))
                self.assertEqual(update_snapshot(*before), update_snapshot(*same))
                after = deepcopy(before)
                replacement = deepcopy(value)
                if isinstance(replacement, dict):
                    replacement["qa_changed"] = True
                elif isinstance(replacement, list):
                    replacement[0]["qa_changed"] = True
                elif isinstance(replacement, int):
                    replacement += 1
                else:
                    replacement += "0"
                after[1][-1][key] = replacement
                self.assertChanged(before, after)


if __name__ == "__main__":
    unittest.main()
