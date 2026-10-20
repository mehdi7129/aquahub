"""Synthetic Aqua_Drone_5 tests. No connection to the physical drone."""
import base64
from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aquahub_agent import drone as module
from aquahub_agent.drone import DroneManager, drone_address, drone_number, network_fields, parse_remote, stable_network
from aquahub_agent.security import ApiError, RequestContext


IDENTITY = {"machine_id": "a" * 32, "host_key_fingerprint": "SHA256:synthetic-approved-host-fingerprint"}
WG_BYTES = b"# synthetic fixture, no actual private key\nAddress=192.0.2.5/32\n"


def hub_config(count=5):
    return "\n".join(f"[UdpEndpoint aqua_{number}_ipv4_1]\nAddress = 192.168.101.{number}\nClientAddress=[f::1]\nGroup=aqua_{number}\n" for number in range(1, count + 1))


def drone_config(number=5):
    return f"[UdpEndpoint ipv4_1]\nAddress=192.168.101.{number}\nGroup=aqua_{number}\n\n[UdpEndpoint qground]\nAddress=0.0.0.0\n"


def remote_payload(*, boot="11111111-1111-1111-1111-111111111111", machine="a" * 32,
                   mavlink=None, wg_hash=None, mqtt="192.0.2.10,192.0.2.11", services=None):
    data = {"MACHINE": machine, "BOOT": boot, "MAVLINK": mavlink or drone_config(),
            "WG_HASH": (wg_hash or hashlib.sha256(WG_BYTES).hexdigest()) + "  /etc/wireguard/wg0.conf",
            "MQTT": "MQTT_SERVER=" + mqtt,
            "ADDR": json.dumps([{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.168.205.2", "prefixlen": 24}]}]),
            "ROUTE": json.dumps([{"dst": "192.168.205.0/24", "dev": "eth0"}]),
            "SERVICES": services or "mavlink-router.service loaded active running MAVLink\nwg0.service loaded active exited VPN\nmqtt-bridge.service loaded active running MQTT\ndmx-scheduler.service loaded active running DMX"}
    return "".join("__AQ_" + key + "__\n" + value + "\n" for key, value in data.items()) + "__AQ_END__\n"


def backup_archive():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in {"etc/mavlink-router/main.conf": drone_config().encode(),
                           "etc/wireguard/wg0.conf": WG_BYTES,
                           "etc/dmx/main.config": b"MQTT_SERVER=192.0.2.10,192.0.2.11\n"}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class Runner:
    def __init__(self):
        self.calls = []
        self.rebooted = False
        self.remote_kwargs = {}

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0].endswith("ssh"):
            if args[-1].startswith("tar -czf"):
                return backup_archive()
            if args[-1] == module.REMOTE_READ:
                return remote_payload(boot="22222222-2222-2222-2222-222222222222" if self.rebooted else "11111111-1111-1111-1111-111111111111", **self.remote_kwargs)
        if "-I" in args and "main.py" in args[-4]:
            content = drone_config(int(args[-2])) if args[-3] == "drone" else hub_config(int(args[-2]))
            Path(args[-1]).write_text(content)
            return "synthetic generator completed"
        raise AssertionError(args)


class App:
    def __init__(self, root):
        self.config = SimpleNamespace(state_dir=root / "state", bundle_path=root / "bundle")
        self.config.state_dir.mkdir()
        self.config.bundle_path.mkdir()
        self.jobs = []
        self.progress = []

    def report_progress(self, progress):
        self.progress.append(progress)

    def submit_job(self, kind, target, payload, work):
        self.jobs.append((kind, target, payload))
        return {"id": "job-test", "status": "succeeded", "result": work()}


class DroneParsingTests(unittest.TestCase):
    def test_number_and_literal_bundle_destination(self):
        self.assertEqual(drone_address(5), "192.168.205.2")
        self.assertEqual(drone_address(6), "192.168.206.2")
        for value in (0, 7, 10, 253, True, "5", -1, None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                drone_number(value)

    def test_network_comparison_ignores_formatting_but_detects_ip_and_group(self):
        original = drone_config()
        self.assertEqual(network_fields(original), network_fields(original.replace("Address=", "Address = ")))
        self.assertNotEqual(network_fields(original), network_fields(drone_config(6)))
        self.assertNotEqual(network_fields(original), network_fields(original.replace("aqua_5", "aqua_4")))
        for invalid in ("not a config", "[Endpoint]\nAddress=garbage\n", "[Endpoint]\nPort=42\n"):
            with self.assertRaises(ApiError):
                network_fields(invalid)

    def test_read_only_remote_snapshot_parsing(self):
        value = parse_remote(remote_payload())
        self.assertEqual(value["machine_id"], IDENTITY["machine_id"])
        self.assertEqual(value["mqtt"], "192.0.2.10,192.0.2.11")
        self.assertIn("wg0.service", value["services"])
        self.assertEqual(value["network"]["addresses"][0][2], "192.168.205.2")

    def test_missing_identity_invalid_mqtt_and_incomplete_snapshot_refused(self):
        for payload in (remote_payload(machine=""), remote_payload(machine="0" * 32),
                        remote_payload(mqtt="$(untrusted-command)"), remote_payload().replace("__AQ_ADDR__", "__AQ_MISSING__")):
            with self.assertRaises(ApiError):
                parse_remote(payload)

    def test_stable_network_ignores_temporary_ipv6_and_route_expiry(self):
        a = [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.0.2.5", "prefixlen": 24},
             {"local": "2001:db8::1", "temporary": True}]}]
        b = [{"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.0.2.5", "prefixlen": 24},
             {"local": "2001:db8::2", "flags": ["temporary"]}]}]
        self.assertEqual(stable_network(a, [{"dst": "default", "expires": 5}]), stable_network(b, [{"dst": "default", "expires": 3}]))


class DroneManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(self.root)
        self.runner = Runner()
        self.manager = DroneManager(self.app, runner=self.runner)
        self.context = RequestContext(os.getuid(), os.getpid())
        bundle = self.app.config.bundle_path
        for name in module.KNOWN_FILES:
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic recognised fixture " + name)
        for number in range(1, 7):
            path = self.manager._wg_path(number)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(WG_BYTES)
        hub = bundle / "config/mavlink/config.conf"
        hub.parent.mkdir(parents=True)
        hub.write_text(hub_config())
        hashes = {relative: module.sha256_file(bundle / relative) for relative in module.KNOWN_FILES}
        self.hash_patch = patch.dict(module.KNOWN_FILES, hashes)
        self.hash_patch.start()
        self.credential_patch = patch.object(self.manager, "_ssh_credentials", return_value={
            "options": [], "fingerprint": IDENTITY["host_key_fingerprint"], "host": "root@192.168.205.2",
            "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}})
        self.credential_patch.start()
        self.manager._hub_network = lambda: {"addresses": [["test0", "inet", "192.0.2.10", 24]], "routes": []}

    def tearDown(self):
        self.credential_patch.stop()
        self.hash_patch.stop()
        self.temp.cleanup()

    def preflight(self):
        return self.manager.preflight({"number": 5}, self.context)

    def configure(self, preflight):
        return self.manager.configure({"number": 5, "preflight_id": preflight["preflight_id"],
                                       "confirmed_identity": preflight["identity"]}, self.context)

    def test_inventory_is_six_supported_unknown_unconfigured_devices(self):
        result = self.manager.list()
        self.assertEqual(len(result["drones"]), 6)
        self.assertTrue(all(drone["supported"] for drone in result["drones"]))
        self.assertTrue(all(drone["reachable"] is None for drone in result["drones"]))
        self.assertTrue(all(not drone["identity_verified"] and not drone["configured"] for drone in result["drones"]))
        self.assertEqual(result["drones"][4]["name"], "Aqua_Drone_5")
        checklist = result["drones"][4]["manual_checklist"]
        self.assertEqual(len(checklist), 5)
        self.assertEqual(len({check["id"] for check in checklist}), 5)
        self.assertIn("MAV_SYS_ID = 5", checklist[2]["details"])
        self.assertIn("sans modifier", checklist[0]["details"])
        self.assertIn("sans modifier", checklist[1]["details"])
        self.assertIn("listener esc_status", checklist[4]["details"])
        self.assertIn("MAV_SYS_ID = 6", result["drones"][5]["manual_checklist"][2]["details"])

    def test_scan_only_six_known_ssh_addresses_and_presence_is_not_identity(self):
        calls = []
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): return None
        def connector(target, timeout):
            calls.append((target, timeout))
            if target[0] != "192.168.205.2":
                raise OSError("offline")
            return Connection()
        self.manager.connector = connector
        result = self.manager.scan()
        self.assertEqual({target for target, _ in calls}, {(drone_address(n), 22) for n in range(1, 7)})
        self.assertTrue(result["drones"][4]["reachable"])
        self.assertFalse(result["drones"][4]["identity_verified"])
        self.manager.observed[5]["checked"] = 0
        self.assertIsNone(self.manager.list()["drones"][4]["reachable"])

    def test_preflight_generates_in_private_stage_and_preserves_existing_mqtt(self):
        original = (self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes()
        result = self.preflight()
        self.assertTrue(result["allowed"], result)
        self.assertEqual(result["identity"], IDENTITY)
        self.assertTrue(result["needs_identity_confirmation"])
        self.assertEqual(result["manual_checklist"], self.manager.list()["drones"][4]["manual_checklist"])
        plan = self.manager.preflights[result["preflight_id"]]
        self.assertEqual(stat.S_IMODE(plan["stage"].stat().st_mode), 0o700)
        self.assertEqual(plan["remote"]["mqtt"], "192.0.2.10,192.0.2.11")
        self.assertEqual((self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes(), original)
        self.assertEqual(self.app.jobs, [])
        self.assertTrue(all("-I" in args for args, _ in self.runner.calls if args[-1].endswith(".conf")))

    def test_fleet_reduction_refused_without_sending_files(self):
        (self.app.config.bundle_path / "config/mavlink/config.conf").write_text(hub_config(6))
        result = self.preflight()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["code"], "fleet_reduction")
        self.assertFalse(any(args[0].endswith("scp") for args, _ in self.runner.calls))

    def test_hub_and_drone_network_changes_refused(self):
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        hub.write_text(hub_config().replace("192.168.101.5", "192.168.120.5"))
        self.assertEqual(self.preflight()["code"], "ip_change_forbidden")
        hub.write_text(hub_config())
        self.runner.remote_kwargs["mavlink"] = drone_config(4)
        self.assertEqual(self.preflight()["code"], "ip_change_forbidden")

    def test_wireguard_mismatch_refused(self):
        self.runner.remote_kwargs["wg_hash"] = "f" * 64
        self.assertEqual(self.preflight()["code"], "wireguard_mismatch")

    def test_changed_script_refused_before_any_ssh(self):
        (self.app.config.bundle_path / "deploy.py").write_text("unrecognised script")
        result = self.preflight()
        self.assertEqual(result["code"], "unsupported_bundle")
        self.assertEqual(self.runner.calls, [])

    def test_previously_associated_identity_change_refused(self):
        module._atomic_json(self.manager.root / "inventory.json", {"5": {"identity": {**IDENTITY, "machine_id": "b" * 32}}})
        result = self.preflight()
        self.assertEqual(result["code"], "identity_mismatch")

    def test_unapproved_ssh_identity_is_an_explicit_failed_preflight(self):
        self.credential_patch.stop()
        self.credential_patch = patch.object(self.manager, "_ssh_credentials", side_effect=ApiError("ssh_identity_untrusted", "Clé non approuvée"))
        self.credential_patch.start()
        result = self.preflight()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["code"], "ssh_identity_untrusted")

    def test_configuration_requires_confirmation_correct_target_and_operator(self):
        result = self.preflight()
        params = {"number": 5, "preflight_id": result["preflight_id"], "confirmed_identity": result["identity"]}
        for update in ({"confirmed_identity": None}, {"number": 4}):
            with self.assertRaises(ApiError):
                self.manager.configure({**params, **update}, self.context)
        with self.assertRaises(ApiError):
            self.manager.configure(params, RequestContext(os.getuid() + 1, os.getpid()))
        self.assertEqual(self.app.jobs, [])

    def test_expired_preflight_and_file_changes_refused(self):
        result = self.preflight()
        plan = self.manager.preflights[result["preflight_id"]]
        plan["created"] = 0
        with self.assertRaises(ApiError) as error:
            self.configure(result)
        self.assertEqual(error.exception.code, "preflight_expired")
        result = self.preflight()
        (self.app.config.bundle_path / ".env").write_text("CHANGED=1\n")
        with self.assertRaises(ApiError) as error:
            self.configure(result)
        self.assertEqual(error.exception.code, "configuration_changed")

    def test_target_changed_since_preflight_refused_before_backup_and_deploy(self):
        result = self.preflight()
        self.runner.remote_kwargs["machine"] = "b" * 32
        with self.assertRaises(ApiError) as error:
            self.configure(result)
        self.assertEqual(error.exception.code, "target_changed")
        self.assertFalse(any(args[-1].startswith("tar -czf") for args, _ in self.runner.calls))

    def test_success_requires_observed_reboot_private_backup_and_postchecks(self):
        result = self.preflight()
        states = deque((False, True))
        self.manager._reachable = lambda number: states.popleft() if states else True
        def deploy(plan):
            self.runner.rebooted = True
            return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
        with patch.object(self.manager, "_deploy", side_effect=deploy), patch.object(module.time, "sleep"):
            output = self.configure(result)
        self.assertTrue(output["result"]["reboot_verified"])
        self.assertTrue(output["result"]["ip_unchanged"])
        self.assertEqual(output["result"]["manual_skipped"], 5)
        self.assertIn("Aqua_Drone_5", output["result"]["message"])
        self.assertEqual(len(output["result"]["manual_checklist"]), 5)
        progress = self.app.progress
        self.assertEqual([value["completed"] for value in progress], [0, 1, 2, 9, 10, 10, 11, 12])
        self.assertTrue(all(value["percent"] < 100 for value in progress[:-1]))
        self.assertEqual(progress[-1]["percent"], 100)
        self.assertFalse(progress[-1]["indeterminate"])
        self.assertIn("Aqua_Drone_5", progress[-1]["label"])
        backup = self.manager.preflights[result["preflight_id"]]["stage"] / "drone-before.tar.gz"
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        inventory = self.manager.list()["drones"][4]
        self.assertTrue(inventory["configured"] and inventory["identity_verified"])
        with self.assertRaises(ApiError):
            self.configure(result)

    def test_corrupt_backup_refused_before_deployment(self):
        result = self.preflight()
        original_runner = self.manager.runner
        def runner(args, **kwargs):
            return b"not an archive" if args[-1].startswith("tar -czf") else original_runner(args, **kwargs)
        self.manager.runner = runner
        with patch.object(self.manager, "_deploy") as deploy:
            with self.assertRaises(ApiError):
                self.configure(result)
        deploy.assert_not_called()

    def test_early_ssh_after_reboot_waits_for_wireguard_routes_and_dmx(self):
        result = self.preflight()
        states = deque((False, True))
        self.manager._reachable = lambda number: states.popleft() if states else True
        original_remote = self.manager._remote
        post_reads = []
        def remote(credentials):
            snapshot = original_remote(credentials)
            if self.runner.rebooted:
                post_reads.append(1)
                if len(post_reads) == 1:
                    snapshot["network"] = {"addresses": [], "routes": []}
                    snapshot["services"] = ["mavlink-router.service"]
            return snapshot
        def deploy(plan):
            self.runner.rebooted = True
            return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
        with patch.object(self.manager, "_remote", side_effect=remote), patch.object(self.manager, "_deploy", side_effect=deploy), patch.object(module.time, "sleep"):
            output = self.configure(result)
        self.assertTrue(output["result"]["verified"])
        self.assertEqual(len(post_reads), 3)  # one incomplete, two fully ready

    def test_persistent_network_difference_is_never_accepted(self):
        result = self.preflight()
        states = deque((False, True))
        self.manager._reachable = lambda number: states.popleft() if states else True
        original_remote = self.manager._remote
        def remote(credentials):
            snapshot = original_remote(credentials)
            if self.runner.rebooted:
                snapshot["network"] = {"addresses": [], "routes": []}
            return snapshot
        def deploy(plan):
            self.runner.rebooted = True
            return {}
        with patch.object(self.manager, "_remote", side_effect=remote), patch.object(self.manager, "_deploy", side_effect=deploy), patch.object(module, "REBOOT_TIMEOUT", .02), patch.object(module.time, "sleep"):
            with self.assertRaises(ApiError) as error:
                self.configure(result)
        self.assertEqual(error.exception.code, "reboot_unverified")
        self.assertIn("IP et routes du drone", error.exception.message)
        self.assertFalse(self.manager.list()["drones"][4]["configured"])
        self.assertLess(self.app.progress[-1]["percent"], 100)
        self.assertTrue(self.app.progress[-1]["indeterminate"])

    def test_supplier_progress_requires_exact_success_lines_in_order(self):
        preflight = self.preflight()
        plan = self.manager.preflights[preflight["preflight_id"]]
        self.manager._supplier_progress(plan, "[hub] config.conf mavlink-router (IPv6 GCS + N drones)\n")
        self.assertEqual(self.app.progress, [])
        first = "  ✔ Hub — IPv6 GCS fixée (f::1) (fait)\n"
        second = "  ✔ [hub] config.conf mavlink-router (IPv6 GCS + N drones) (fait)\n"
        self.manager._supplier_progress(plan, second)
        self.assertEqual(self.app.progress, [])  # no out-of-order inference
        self.manager._supplier_progress(plan, first + second)
        self.assertEqual([value["completed"] for value in self.app.progress], [3, 4])
        self.manager._supplier_progress(plan, first + second)
        self.assertEqual(len(self.app.progress), 2)  # cumulative output does not replay milestones
        self.manager._supplier_progress(plan, first + second + "  ✔ [aqua 4] mavlink.conf → drone (fait)\n")
        self.assertEqual(len(self.app.progress), 2)  # another drone never advances this target

    def test_changed_ssh_key_never_silently_reenrolled(self):
        key1 = base64.b64encode(b"synthetic-public-key-one").decode()
        key2 = base64.b64encode(b"synthetic-public-key-two").decode()
        with patch.object(self.manager, "runner", return_value=f"192.168.205.2 ssh-ed25519 {key1}\n"):
            seen = self.manager.identity({"number": 5})
            self.assertFalse(seen["trusted"])
            with self.assertRaises(ApiError):
                self.manager.enroll({"number": 5, "host_key_fingerprint": seen["host_key_fingerprint"], "confirmed": False})
            self.assertTrue(self.manager.enroll({"number": 5, "host_key_fingerprint": seen["host_key_fingerprint"], "confirmed": True})["enrolled"])
        with patch.object(self.manager, "runner", return_value=f"192.168.205.2 ssh-ed25519 {key2}\n"):
            changed = self.manager.identity({"number": 5})
            with self.assertRaises(ApiError) as error:
                self.manager.enroll({"number": 5, "host_key_fingerprint": changed["host_key_fingerprint"], "confirmed": True})
        self.assertEqual(error.exception.code, "identity_mismatch")

    def test_none_auth_is_explicit_and_settings_remain_private(self):
        self.assertFalse(self.manager.ssh_settings({})["allow_none_auth"])
        result = self.manager.ssh_settings({"allow_none_auth": True})
        self.assertTrue(result["allow_none_auth"])
        self.assertEqual(stat.S_IMODE((self.manager.root / "ssh.json").stat().st_mode), 0o600)
        with self.assertRaises(ApiError):
            self.manager.ssh_settings({"allow_none_auth": "yes"})

    def test_pseudo_terminal_never_answers_yes_to_hardware_checkboxes(self):
        result = self.preflight()
        plan = self.manager.preflights[result["preflight_id"]]
        script = self.app.config.bundle_path / "deploy.py"
        inputs = self.root / "answers.json"
        prompt_strings = [
            "Aquadrones à configurer (ex: 1-3 ou 1,3,5) : ",
            "  [ ] [aqua 5] RUTC50 : LAN interface IP = 192.168.205.1/24, protocol static  (o/n) : ",
            "  [ ] [aqua 5] Next hop 192.168.205.1 → IP aquadrone sur réseau GCS  (o/n) : ",
            "  [ ] [aqua 5] Vérifier les paramètres — fait  (o/n) : ",
            "  [ ] [aqua 5] Relever les versions Heron — fait  (o/n) : ",
            "  IP(s) broker MQTT — CSV pour la redondance (ex: 10.0.0.1,10.0.0.2) [198.51.100.88] : ",
            "  [ ] [aqua 5] Vérifier le status (QGC → MAVLink Console / nsh) — fait  (o/n) : "]
        script.write_text("import json, pathlib\nanswers=[]\n" +
            "\n".join("answers.append(input(" + repr(prompt) + "))" for prompt in prompt_strings) +
            "\npathlib.Path(" + repr(str(inputs)) + ").write_text(json.dumps(answers))\nprint('7 fait · 5 ignoré · 0 échec')\n")
        with patch.dict(module.KNOWN_FILES, {"deploy.py": module.sha256_file(script)}):
            output = self.manager._deploy(plan)
        self.assertEqual(output["manual_skipped"], 5)
        self.assertEqual(json.loads(inputs.read_text()), ["5", "n", "n", "n", "n", "192.0.2.10,192.0.2.11", "n"])

    def test_pseudo_terminal_unexpected_failure_aborts_no_retry(self):
        result = self.preflight()
        plan = self.manager.preflights[result["preflight_id"]]
        script = self.app.config.bundle_path / "deploy.py"
        script.write_text("input('Aquadrones à configurer (ex: 1-3 ou 1,3,5) : ')\nprint('✘ échec : synthetic failure',flush=True)\ninput('  (r)éessayer / (s)kip : ')\n")
        with patch.dict(module.KNOWN_FILES, {"deploy.py": module.sha256_file(script)}), patch.object(module, "DEPLOY_TIMEOUT", 2), self.assertRaises(ApiError) as error:
            self.manager._deploy(plan)
        self.assertEqual(error.exception.code, "deployment_partial")

    def test_pseudo_terminal_unknown_prompt_is_bounded_and_not_answered(self):
        result = self.preflight()
        plan = self.manager.preflights[result["preflight_id"]]
        (self.app.config.bundle_path / "deploy.py").write_text("input('Unexpected confirmation: ')\n")
        script = self.app.config.bundle_path / "deploy.py"
        with patch.dict(module.KNOWN_FILES, {"deploy.py": module.sha256_file(script)}), patch.object(module, "DEPLOY_TIMEOUT", .2), self.assertRaises(ApiError) as error:
            self.manager._deploy(plan)
        self.assertEqual(error.exception.code, "deployment_timeout")


if __name__ == "__main__":
    unittest.main()
