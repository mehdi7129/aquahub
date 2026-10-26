"""WireGuard recovery fixtures: no host Docker, interface or supplier mutation."""
import base64
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from aquahub_agent import wireguard as wg
from aquahub_agent.core import Application, Config
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext, process_start_time
from test_core import FakeDocker

PRIVATE = base64.b64encode(b"p" * 32).decode()
PUBLIC = base64.b64encode(b"u" * 32).decode()
PEER = base64.b64encode(b"e" * 32).decode()
FOREIGN = base64.b64encode(b"f" * 32).decode()
RAW = f"""[Interface]
PrivateKey = {PRIVATE}
Address = 10.44.0.2/32
DNS = 192.0.2.53

[Peer]
PublicKey = {PEER}
AllowedIPs = 10.44.0.0/24
Endpoint = 198.51.100.44:51820
""".encode()


def snapshot(shape="inactive"):
    interfaces = [{"ifname": "eth-test", "linkinfo": {"info_kind": "ether"},
                   "addr_info": [{"family": "inet", "local": "192.0.2.10", "prefixlen": 24}]}]
    routes = [{"dst": "default", "gateway": "192.0.2.1", "dev": "eth-test"}]
    if shape != "absent":
        interface = {"ifname": "wg0", "flags": ["UP"], "linkinfo": {"info_kind": "wireguard"}, "addr_info": []}
        interfaces.append(interface)
        routes.append({"type": "multicast", "dst": "ff00::/8", "dev": "wg0", "table": "local", "protocol": "kernel", "metric": 256, "flags": [], "pref": "medium"})
        if shape == "active":
            interface["addr_info"].append({"family": "inet", "local": "10.44.0.2", "prefixlen": 32})
            routes += [{"dst": "10.44.0.0/24", "dev": "wg0", "scope": "link", "flags": []},
                       {"type": "local", "dst": "10.44.0.2", "dev": "wg0", "table": "local", "protocol": "kernel", "scope": "host", "prefsrc": "10.44.0.2", "flags": []}]
    return {"interfaces": interfaces, "routes": routes, "outside": {"addresses": ["physical fixture"], "routes": ["default fixture"], "rules": [[], []], "dns": "resolver fixture"}}


def kernel_state():
    return {"public": PUBLIC, "peers": {PEER: frozenset({"10.44.0.0/24"})}, "fwmark": "off", "recent": 0,
            "hashes": ["a" * 64, "b" * 64, "c" * 64, "d" * 64]}


class FixtureDocker(FakeDocker):
    def __init__(self, bundle):
        super().__init__()
        self.commands = []
        self.shape = "inactive"
        self.fail = False
        self.residual_busy = False
        self.after_change = False
        self.container = {"id": "a" * 64, "image_id": wg.IMAGE, "pid": 12345, "started_at": "2026-01-01T00:00:00Z",
                          "state": "running", "running": True, "project": "aqua-hub-bundle", "service": "wireguard",
                          "working_dir": str(bundle), "network": "host", "privileged": False, "pid_mode": "",
                          "resolv_conf": "/var/lib/docker/fixture/resolv.conf",
                          "mounts": [{"Source": "/lib/modules", "Destination": "/lib/modules", "RW": False, "Type": "bind"},
                                     {"Source": str(bundle / wg.PROFILE), "Destination": wg.MOUNT, "RW": True, "Type": "bind"}]}

    def run(self, args, **kwargs):
        self.commands.append((args.copy(), kwargs.copy()))
        if args[0] == "ps":
            return self.container["id"] + "\n"
        if args[0] == "inspect":
            return json.dumps(self.container)
        if args[0] == "exec":
            script = args[-1]
            if script == wg.OUTER:
                if self.fail:
                    raise ApiError("docker_error", "privatekey=" + PRIVATE + " supplier-stderr-must-not-leak")
                self.shape = "active"
                return "AQUAHUB_WIREGUARD_REACTIVATED\n"
            if script == wg.LOCK_PROBE:
                if self.residual_busy:
                    raise ApiError("docker_error", "busy")
                return "AQUAHUB_LOCK_FREE"
            if script == wg.READ_FIREWALL:
                return ""
            if "pubkey" in args:
                return PUBLIC + "\n"
        raise AssertionError("Unexpected fixture Docker command")


class ProfileTests(unittest.TestCase):
    def test_record_separators_match_the_existing_bash_tool(self):
        for separator in (b"\r", b"\v", b"\f", b"\x1c", b"\xc2\x85", b"\xe2\x80\xa8", b"\xe2\x80\xa9"):
            with self.subTest(separator=separator.hex()), self.assertRaises(ApiError):
                wg.parse_profile(RAW.replace(b"\nAddress", separator + b"Address"))
        with self.assertRaises(ApiError):
            wg.parse_profile(RAW.replace(b"PrivateKey = ", b"PrivateKey = \xc2\xa0"))
        self.assertEqual(wg.parse_profile(RAW.replace(b"\n", b"\r\n")).address, "10.44.0.2/32")
        self.assertEqual(wg.parse_profile("# clé de recette\n".encode() + RAW).address, "10.44.0.2/32")

    def test_exact_profile_round_trip_and_no_secret_repr(self):
        profile = wg.parse_profile(RAW)
        self.assertEqual(profile.raw, RAW)
        self.assertEqual(profile.address, "10.44.0.2/32")
        self.assertEqual(profile.peers, {PEER: frozenset({"10.44.0.0/24"})})
        self.assertNotIn(PRIVATE, repr(profile))

    def test_script_options_ambiguous_values_and_broad_contract_changes_refused(self):
        changes = [
            RAW.replace(b"DNS =", b"PostUp = touch /tmp/never\nDNS ="),
            RAW.replace(b"DNS =", b"PreDown = true\nDNS ="),
            RAW.replace(b"DNS =", b"SaveConfig = true\nDNS ="),
            RAW.replace(b"DNS =", b"Table = 123\nDNS ="),
            RAW.replace(b"DNS =", b"Address = 10.44.0.3/32\nDNS ="),
            RAW.replace(b"DNS =", b"address = 10.44.0.3/32\nDNS ="),
            RAW.replace(b"10.44.0.0/24", b"0.0.0.0/0"),
            RAW.replace(b"10.44.0.2/32", b"10.44.0.2/24"),
            RAW.replace(b"10.44.0.2/32", b"2001:db8::2/128"),
            RAW.replace(b"192.0.2.53", b"example.test;sh"),
            RAW.replace(b"198.51.100.44:51820", b"$(touch /tmp/never):51820"),
            RAW.replace(b"198.51.100.44:51820", b"10.44.0.1:51820"),
            RAW.replace(PRIVATE.encode(), b"invalid-private-key"),
            RAW + b"\x00", b"[DEFAULT]\nAddress=10.44.0.2/32\n" + RAW,
        ]
        for raw in changes:
            with self.subTest(raw_length=len(raw)), self.assertRaises(ApiError):
                wg.parse_profile(raw)

    def test_profile_is_bounded_and_links_or_specials_are_never_read(self):
        with self.assertRaises(ApiError):
            wg.parse_profile(b"x" * (wg.MAX_PROFILE + 1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "good").write_bytes(RAW)
            self.assertEqual(wg._read_under(root, "good")[0], RAW)
            (root / "link").symlink_to(root / "good")
            os.mkfifo(root / "fifo")
            for name in ("link", "fifo"):
                with self.subTest(name=name), self.assertRaises((OSError, ApiError)):
                    wg._read_under(root, name)
            with self.assertRaises(ApiError):
                wg._read_under(root, "../good")

    def test_duplicate_peers_overlaps_and_incomplete_profiles_refused(self):
        for raw in (RAW + RAW[RAW.index(b"[Peer]"):], RAW.replace(b"[Peer]", b"[Other]"), RAW[:RAW.index(b"[Peer]")],
                    RAW.replace(b"10.44.0.0/24", b"10.44.0.0/24,10.44.0.0/25")):
            with self.assertRaises(ApiError):
                wg.parse_profile(raw)


class ShapeTests(unittest.TestCase):
    def test_absent_owned_partial_and_active_are_separate(self):
        profile = wg.parse_profile(RAW)
        for name in ("absent", "inactive", "active"):
            self.assertEqual(wg.WireGuardManager._tunnel_shape(snapshot(name), profile), name)

    def test_foreign_interface_extra_ip_route_or_policy_are_refused(self):
        profile = wg.parse_profile(RAW)
        changes = []
        value = snapshot(); value["interfaces"][-1]["linkinfo"]["info_kind"] = "dummy"; changes.append(value)
        value = snapshot(); value["interfaces"][-1]["addr_info"].append({"family": "inet", "local": "10.44.0.3", "prefixlen": 32}); changes.append(value)
        value = snapshot(); value["routes"].append({"dst": "default", "dev": "wg0"}); changes.append(value)
        value = snapshot(); value["routes"].append({"dst": "10.44.0.0/24", "dev": "wg0", "table": 123}); changes.append(value)
        value = snapshot(); value["routes"][-1]["gateway"] = "192.0.2.1"; changes.append(value)
        for value in changes:
            with self.assertRaises(ApiError):
                wg.WireGuardManager._tunnel_shape(value, profile)


class ReactivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.base = Path(self.temp.name); self.bundle = self.base / "bundle"
        self.bundle.mkdir()
        self.docker = FixtureDocker(self.bundle)
        self.app = Application(Config(self.base / "rpc.sock", self.base / "state", self.bundle, no_poll=True),
                               docker=self.docker, policy=AuthPolicy(os.getuid(), privileged=False))
        self.manager = wg.register(self.app)
        self.context = RequestContext(os.getuid(), os.getpid(), process_start_time(os.getpid()))
        self.profile = wg.parse_profile(RAW)
        self.fingerprints = {wg.PROFILE: (wg._hash(RAW), (0o644, os.getuid(), os.getgid()))}
        self.patches = [patch.object(self.manager, "_attest", return_value=(self.profile, PUBLIC)),
                        patch.object(self.manager, "_host_snapshot", side_effect=lambda: snapshot(self.docker.shape)),
                        patch.object(self.manager, "_wg_state", side_effect=lambda _c: kernel_state()),
                        patch.object(self.manager, "_fingerprints", side_effect=lambda: copy.deepcopy(self.fingerprints))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        self.manager.close(); self.app.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def finish(self, job):
        for _ in range(500):
            result = self.app.store.job(job["id"])
            if result["status"] not in {"running", "queued"}:
                return result
            time.sleep(.005)
        self.fail("Fixture job did not finish")

    def call(self, method, params=None):
        return self.app.dispatch(method, params or {}, self.context)

    def helpers(self):
        return [(args, kwargs) for args, kwargs in self.docker.commands if args[0] == "exec" and args[-1] == wg.OUTER]

    def test_qualified_classic_and_containerd_image_identities_are_supported(self):
        for identity in wg.IMAGE_IDENTITIES:
            with self.subTest(identity=identity):
                self.docker.container["image_id"] = identity
                status = self.call("wireguard.status")
                self.assertEqual(status["state"], "inactive")
                self.assertTrue(status["can_reactivate"])

    def test_current_partial_tunnel_can_be_reactivated_without_container_restart_or_handshake(self):
        dto = self.call("wireguard.status")
        self.assertEqual(dto["state"], "inactive"); self.assertTrue(dto["can_reactivate"]); self.assertEqual(dto["recent_peer_count"], 0)
        result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["result"]["tunnel_active"])
        self.assertFalse(result["result"]["remote_connectivity_verified"])
        helpers = self.helpers(); self.assertEqual(len(helpers), 1)
        args, options = helpers[0]
        self.assertIn("--interactive", args)
        self.assertIn(RAW, options["input_data"])
        self.assertNotIn(PRIVATE, " ".join(args))
        self.assertIn(b"\ninactive\n", options["input_data"])
        self.assertIsNone(self.app.store.setting_get(wg.PENDING_KEY))
        self.assertFalse(any(args[0] in {"restart", "start", "stop", "compose"} for args, _ in self.docker.commands))
        audit = json.dumps({"jobs": self.app.store.jobs(), "events": self.app.store.events(), "dto": self.call("wireguard.status")})
        for secret in (PRIVATE, PEER, PUBLIC, "10.44.0.2"):
            self.assertNotIn(secret, audit)

    def test_absent_tunnel_uses_same_fixed_helper_and_active_tunnel_is_a_noop(self):
        self.docker.shape = "absent"
        self.assertEqual(self.finish(self.call("wireguard.reactivate", {"confirmed": True}))["status"], "succeeded")
        self.assertIn(b"\nabsent\n", self.helpers()[0][1]["input_data"])
        result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
        self.assertTrue(result["result"]["already_active"])
        self.assertEqual(len(self.helpers()), 1)

    def test_wrong_owner_peer_or_fwmark_is_unknown_and_never_down(self):
        for changes in ({"public": FOREIGN}, {"peers": {FOREIGN: frozenset({"10.44.0.0/24"})}}, {"fwmark": "51820"}):
            with self.subTest(changes=list(changes)):
                self.manager._wg_state.side_effect = None
                self.manager._wg_state.return_value = {**kernel_state(), **changes}
                dto = self.call("wireguard.status")
                self.assertEqual(dto["state"], "unknown"); self.assertFalse(dto["can_reactivate"])
                result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
                self.assertEqual(result["status"], "failed")
        self.assertFalse(self.helpers())

    def test_confirmation_and_operator_authorization_precede_commands(self):
        for params in ({}, {"confirmed": False}, {"confirmed": True, "path": "/etc/foreign"}):
            with self.assertRaises(ApiError):
                self.call("wireguard.reactivate", params)
        with self.assertRaises(ApiError):
            self.app.dispatch("wireguard.status", {}, RequestContext(os.getuid() + 1000, os.getpid()))
        self.assertFalse(self.docker.commands)

    def test_known_malformed_or_stopped_container_cannot_be_started(self):
        original = copy.deepcopy(self.docker.container)
        for changes in ({"image_id": "sha256:" + "f" * 64}, {"network": "bridge"}, {"privileged": True},
                        {"state": "exited", "running": False, "pid": 0}, {"working_dir": "/foreign"}):
            self.docker.container = {**copy.deepcopy(original), **changes}
            dto = self.call("wireguard.status")
            self.assertEqual(dto["state"], "unsupported"); self.assertFalse(dto["can_reactivate"])
            self.assertEqual(self.finish(self.call("wireguard.reactivate", {"confirmed": True}))["status"], "failed")
        self.assertFalse(self.helpers())

    def test_profile_race_blocks_mutation_and_outside_change_never_gets_repaired(self):
        self.fingerprints[wg.PROFILE] = ("f" * 64, ())
        self.assertEqual(self.finish(self.call("wireguard.reactivate", {"confirmed": True}))["error"]["code"], "wireguard_changed")
        self.assertFalse(self.helpers())
        self.fingerprints[wg.PROFILE] = (wg._hash(RAW), ())
        self.manager._host_snapshot.side_effect = [snapshot(), {**snapshot("active"), "outside": {"dns": "changed externally"}}]
        result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
        self.assertEqual(result["error"]["code"], "wireguard_changed")
        self.assertEqual(len(self.helpers()), 1)

    def test_failed_exec_keeps_durable_fence_and_hides_stderr(self):
        self.docker.fail = True
        result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "wireguard_unavailable")
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn("supplier-stderr", json.dumps(result))
        self.assertTrue(self.manager.is_pending())
        pending = self.app.store.setting_get(wg.PENDING_KEY)
        self.assertEqual(pending["id"], self.docker.container["id"])
        self.assertNotIn(PRIVATE, json.dumps(pending))

    def test_expired_pending_requires_lock_proof_not_just_time(self):
        pending = {"schema": 1, "intent_id": "1" * 32, **{k: self.docker.container[k] for k in ("id", "image_id", "pid", "started_at")}, "until": time.time() - 1}
        self.app.store.setting_set(wg.PENDING_KEY, pending)
        self.docker.residual_busy = True
        self.assertTrue(self.manager.is_pending())
        self.assertEqual(self.app.store.setting_get(wg.PENDING_KEY), pending)
        self.docker.residual_busy = False
        self.assertFalse(self.manager.is_pending())
        self.assertIsNone(self.app.store.setting_get(wg.PENDING_KEY))

    def test_unavailable_docker_and_corrupt_markers_leave_pending(self):
        for pending in ({"schema": 9}, {"schema": 1, "until": float("nan")}, "unknown"):
            self.app.store.setting_set(wg.PENDING_KEY, pending)
            self.assertTrue(self.manager.is_pending())
        self.app.store.setting_set(wg.PENDING_KEY, {"schema": 1, "intent_id": "1" * 32, **{k: self.docker.container[k] for k in ("id", "image_id", "pid", "started_at")}, "until": time.time() - 1})
        with patch.object(self.docker, "run", side_effect=ApiError("docker_unavailable", "fixture")):
            self.assertTrue(self.manager.is_pending())

    def test_snapshot_never_waits_for_slow_probe_and_close_reports_live_thread(self):
        started, release = threading.Event(), threading.Event()
        def slow():
            started.set(); release.wait(4); return self.manager._dto("active")
        with patch.object(self.manager, "_probe", side_effect=slow):
            start = time.monotonic(); dto = self.manager.snapshot()
            self.assertLess(time.monotonic() - start, .2)
            self.assertEqual(dto["state"], "unknown"); self.assertTrue(started.wait(1))
            self.assertFalse(self.manager.close())
            release.set(); self.manager._thread.join(1)
            self.assertTrue(self.manager.close())

    def test_pending_checker_cannot_erase_a_new_intent_created_during_its_probe(self):
        old = {"schema": 1, "intent_id": "1" * 32,
               **{k: self.docker.container[k] for k in ("id", "image_id", "pid", "started_at")}, "until": time.time() - 1}
        new = {**old, "intent_id": "2" * 32, "until": time.time() + 45}
        self.app.store.setting_set(wg.PENDING_KEY, old)
        def replace_during_proof(_container, _args, **_kwargs):
            self.app.store.setting_set(wg.PENDING_KEY, new)
            return "AQUAHUB_LOCK_FREE"
        with patch.object(self.manager, "_exec", side_effect=replace_during_proof):
            self.assertTrue(self.manager.is_pending())
        self.assertEqual(self.app.store.setting_get(wg.PENDING_KEY), new)

    def test_success_cannot_erase_a_newer_intent(self):
        original = self.manager._exec
        new = {"schema": 1, "intent_id": "2" * 32,
               **{k: self.docker.container[k] for k in ("id", "image_id", "pid", "started_at")}, "until": time.time() + 45}
        def replace_before_clear(container, args, **kwargs):
            result = original(container, args, **kwargs)
            if args[-1] == wg.LOCK_PROBE:
                self.app.store.setting_set(wg.PENDING_KEY, new)
            return result
        with patch.object(self.manager, "_exec", side_effect=replace_before_clear):
            result = self.finish(self.call("wireguard.reactivate", {"confirmed": True}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "wireguard_pending")
        self.assertEqual(self.app.store.setting_get(wg.PENDING_KEY), new)


class HelperTests(unittest.TestCase):
    def test_closed_helper_owns_expiry_timeout_lock_and_no_legacy_entrypoint(self):
        self.assertIn("/usr/bin/timeout -s TERM -k 5 20", wg.OUTER)
        self.assertIn("/usr/bin/flock -x -n", wg.OUTER)
        self.assertIn('"$(date +%s)" -le "$expires"', wg.ACTIVATE)
        self.assertLess(wg.ACTIVATE.index('expected_public" ]] || exit 72'), wg.ACTIVATE.index("wg-quick down"))
        self.assertLess(wg.ACTIVATE.index("expected_routes6"), wg.ACTIVATE.index("wg-quick down"))
        self.assertNotIn("ip route del default", wg.ACTIVATE)
        self.assertNotIn("svc-wireguard/run", wg.ACTIVATE)
        self.assertNotIn("restart", wg.ACTIVATE)
        self.assertIn("trap 'rm -f", wg.ACTIVATE)

    def helper_fixture(self, *, expired=False, foreign=False, changed=False, firewall=False, firewall_failure=False, up_failure=False,
                       truncated=False, header_cut=False):
        """Execute the real constant shell with only its external transports faked."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); tools = base / "tools"; tools.mkdir()
            state = base / "events.jsonl"
            addr = json.dumps(snapshot()["interfaces"][-1:]) + "\n"
            routes4, routes6 = "[]\n", json.dumps(snapshot()["routes"][-1:]) + "\n"
            stub = '''#!/usr/bin/python3
import hashlib,json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]
if name=='wg':
 if args==['show','wg0','public-key']:print(os.environ['FAKE_PUBLIC'])
 elif args==['show','wg0','fwmark']:print('off')
 elif args==['show','wg0','allowed-ips']:print(os.environ['FAKE_PEER']+'\\t10.44.0.0/24')
 else:raise SystemExit(91)
elif name=='ip':
 if 'address' in args:sys.stdout.write(os.environ['FAKE_ADDR'])
 elif 'route' in args:sys.stdout.write(os.environ['FAKE_ROUTES6' if '-6' in args else 'FAKE_ROUTES4'])
 else:raise SystemExit(91)
elif name=='nft':
 if os.environ['FAKE_FIREWALL_FAILURE']=='1':raise SystemExit(2)
 if os.environ['FAKE_FIREWALL']=='1':print('table ip wg-quick-wg0')
elif name in ['iptables-save','ip6tables-save']:pass
elif name=='wg-quick':
 p=pathlib.Path(args[1]); raw=p.read_bytes()
 assert p.name=='wg0.conf' and p.stat().st_mode&0o777==0o600
 assert p.parent.stat().st_mode&0o777==0o700
 with open(os.environ['FAKE_LOG'],'a') as f:f.write(json.dumps({'operation':args[0],'sha':hashlib.sha256(raw).hexdigest()})+'\\n')
 if args[0]=='up' and os.environ['FAKE_UP_FAILURE']=='1':raise SystemExit(3)
else:raise SystemExit(91)
'''
            for name in ("wg", "wg-quick", "ip", "nft", "iptables-save", "ip6tables-save"):
                path = tools / name; path.write_text(stub); path.chmod(0o700)
            script = wg.ACTIVATE.replace("/run/aquahub-wireguard.", str(base / "stage."))
            script = script.replace("/usr/bin/wg-quick", shlex.quote(str(tools / "wg-quick")))
            script = script.replace("/usr/bin/wg", shlex.quote(str(tools / "wg")))
            expires = int(time.time()) + (-1 if expired else 20)
            hashes = [wg._hash((PEER + " 10.44.0.0/24\n").encode()), wg._hash(addr.encode()), wg._hash(routes4.encode()), wg._hash(routes6.encode())]
            if changed:
                hashes[1] = "0" * 64
            body = RAW[:RAW.index(b"Endpoint =")] if truncated else RAW
            payload = (str(expires) + "\ninactive\n" + wg._hash(RAW) + "\n" + PUBLIC + "\n" + "\n".join(hashes) + "\n").encode() + body
            if header_cut:
                payload = payload[:30]
            env = {"PATH": str(tools) + ":/usr/bin:/bin", "LC_ALL": "C", "FAKE_PUBLIC": FOREIGN if foreign else PUBLIC,
                   "FAKE_PEER": PEER, "FAKE_ADDR": addr, "FAKE_ROUTES4": routes4, "FAKE_ROUTES6": routes6,
                   "FAKE_FIREWALL": str(int(firewall)), "FAKE_FIREWALL_FAILURE": str(int(firewall_failure)),
                   "FAKE_UP_FAILURE": str(int(up_failure)), "FAKE_LOG": str(state)}
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script], input=payload,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, env=env)
            events = [json.loads(line) for line in state.read_text().splitlines()] if state.exists() else []
            self.assertFalse(list(base.glob("stage.*")))
            self.assertNotIn(PRIVATE.encode(), result.stdout + result.stderr)
            return result, events

    def test_real_shell_repairs_only_after_matching_proofs_and_removes_private_copy(self):
        result, events = self.helper_fixture()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual([row["operation"] for row in events], ["down", "up"])
        self.assertEqual({row["sha"] for row in events}, {wg._hash(RAW)})
        self.assertIn(b"AQUAHUB_WIREGUARD_REACTIVATED", result.stdout)

    def test_real_shell_rejects_expiry_foreign_owner_race_and_firewall_before_down(self):
        for kwargs in ({"expired": True}, {"foreign": True}, {"changed": True}, {"firewall": True}, {"firewall_failure": True}):
            with self.subTest(kwargs=kwargs):
                result, events = self.helper_fixture(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(events)

    def test_real_shell_cleans_private_copy_when_up_fails_without_false_success(self):
        result, events = self.helper_fixture(up_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([row["operation"] for row in events], ["down", "up"])
        self.assertNotIn(b"AQUAHUB_WIREGUARD_REACTIVATED", result.stdout)

    def test_real_shell_refuses_truncated_stdin_before_any_tunnel_command(self):
        # The truncated body retains a syntactically usable WG peer but loses
        # its endpoint. Transport integrity must fail before even taking it down.
        for kwargs in ({"truncated": True}, {"header_cut": True}):
            with self.subTest(kwargs=kwargs):
                result, events = self.helper_fixture(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(events)
                self.assertNotIn(b"AQUAHUB_WIREGUARD_REACTIVATED", result.stdout)


if __name__ == "__main__":
    unittest.main()
