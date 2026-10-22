"""Synthetic Aqua_Drone_5 tests. No connection to the physical drone."""
import base64
from contextlib import contextmanager, nullcontext
from collections import deque
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aquahub_agent import drone as module
from aquahub_agent.drone import DroneManager, drone_address, drone_number, network_fields, parse_remote, stable_network
from aquahub_agent.core import Application, Config
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext, process_start_time
from test_core import FakeDocker


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

    def operation_guard(self):
        return nullcontext()

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

    def test_hub_coverage_checks_complete_sections_and_preserves_extra_fleet(self):
        required = ("[General]\nDebugLogLevel=info\nMavlinkDialect=auto\nDeDuplicationPeriod=300\n"
                    "[UdpEndpoint aqua_1_ipv6]\nMode=server_connected\nAddress=[fd00:100::1]\n"
                    "Port=14555\nClientAddress=[f::1]\nClientPort=14550\nAllowSrcSysIn=255\n"
                    "AllowSrcSysOut=1\nSendGroup=aqua_1\n"
                    "[UdpEndpoint gcs_multicast]\nMode=server\nAddress=[::]\nPort=14556\n")
        installed = required + "[UdpEndpoint aqua_2_ipv4_1]\nMode=server\nAddress=192.168.101.2\nPort=14558\n"
        self.assertTrue(module.hub_covers_generated(installed, required))
        self.assertTrue(module.hub_covers_generated(installed.replace("[f::1]", "[000f:0000:0000:0000:0000:0000:0000:0001]"), required))
        for before, after in (("Port=14555", "Port=14554"), ("ClientPort=14550", "ClientPort=14551"),
                              ("Mode=server_connected", "Mode=server"), ("AllowSrcSysIn=255", "AllowSrcSysIn=1"),
                              ("AllowSrcSysOut=1", "AllowSrcSysOut=2"), ("MavlinkDialect=auto", "MavlinkDialect=common"),
                              ("DeDuplicationPeriod=300", "DeDuplicationPeriod=0"),
                              ("Port=14556", "Port=14557"), ("SendGroup=aqua_1", "SendGroup=aqua_2"),
                              ("ClientAddress=[f::1]", "ClientAddress=[f::2]"),
                              ("Address=[fd00:100::1]", "Address=[fd00:100::2]"),
                              ("Port=14555", "pOrT=14555"),
                              ("Port=14555", "Port=14555\nAllowMsgIdOut=0")):
            with self.subTest(change=before):
                self.assertFalse(module.hub_covers_generated(installed.replace(before, after), required))
        self.assertFalse(module.hub_covers_generated(required.partition("[UdpEndpoint gcs_multicast]")[0], required))
        for ambiguous in (required + "[General]\nOther=1\n", required.replace("Port=14555", "Port=14555\nPort=14555"),
                          required + "[general]\nOther=1\n", required.replace("Port=14555", "Port=14555\npOrT=14555"),
                          required + "[udpendpoint aqua_1_ipv6]\nAddress=[fd00:100::1]\n",
                          "[DEFAULT]\nMode=server\n" + required, "[DEFAULT]\n" + required,
                          required.replace("Address=[::]", "Address=[[::]]")):
            with self.subTest(config=ambiguous), self.assertRaises(ApiError) as caught:
                module.hub_covers_generated(ambiguous, required)
            self.assertEqual(caught.exception.code, "invalid_config")

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

    @contextmanager
    def registered_app(self, *, privileged=False):
        application = Application(Config(self.root / "socket", self.root / "guarded-state",
            bundle_path=self.app.config.bundle_path, no_poll=True), docker=FakeDocker(),
            policy=AuthPolicy(os.getuid(), privileged=privileged))
        manager = module.register(application)
        manager.runner = self.runner
        manager._hub_network = self.manager._hub_network
        try:
            with patch.object(manager, "_ssh_credentials", return_value={
                "options": [], "fingerprint": IDENTITY["host_key_fingerprint"],
                "host": "root@192.168.205.2", "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}}):
                yield application, manager
        finally:
            application.close()

    def test_registered_preflight_never_prompts_but_other_admin_actions_stay_protected(self):
        context = RequestContext(os.getuid(), os.getpid(), process_start_time(os.getpid()))
        with self.registered_app(privileged=True) as (application, _):
            with patch("aquahub_agent.security.subprocess.run") as pkcheck:
                pkcheck.return_value.returncode = 1
                result = application.dispatch("drones.preflight", {"number": 5}, context)
                self.assertTrue(result["allowed"], result)
                pkcheck.assert_not_called()
                for method, params in (
                    ("drones.enroll", {"number": 5, "confirmed": True}),
                    ("drones.ssh_settings", {}),
                    ("service.restart", {"service": "mavlink_router"}),
                    ("settings.save", {"theme": "dark"}),
                ):
                    with self.assertRaises(ApiError) as caught:
                        application.dispatch(method, params, context)
                    self.assertEqual(caught.exception.code, "unauthorized")
                self.assertEqual(pkcheck.call_count, 4)
                self.assertTrue(all(call.args[0][0] == "/usr/bin/pkcheck" for call in pkcheck.call_args_list))
            self.assertEqual(application.store.jobs(), [])

    def test_registered_preflight_rejects_another_uid_before_ssh_or_staging(self):
        with self.registered_app(privileged=True) as (application, manager):
            before = list(manager.root.iterdir())
            with patch("aquahub_agent.security.subprocess.run") as pkcheck:
                with self.assertRaises(ApiError) as caught:
                    application.dispatch("drones.preflight", {"number": 5}, RequestContext(os.getuid() + 1, os.getpid()))
                self.assertEqual(caught.exception.code, "unauthorized")
                pkcheck.assert_not_called()
            self.assertEqual(self.runner.calls, [])
            self.assertEqual(list(manager.root.iterdir()), before)

    def test_registered_preflight_is_bounded_read_only_remote_and_does_not_enroll(self):
        with self.registered_app() as (application, manager):
            original = (self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes()
            result = application.dispatch("drones.preflight", {"number": 5}, self.context)
            self.assertTrue(result["allowed"], result)
            remote_calls = [(args, kwargs) for args, kwargs in self.runner.calls if args[0].endswith("ssh")]
            self.assertEqual(len(remote_calls), 1)
            args, kwargs = remote_calls[0]
            self.assertEqual(args, ["/usr/bin/ssh", "root@192.168.205.2", module.REMOTE_READ])
            self.assertEqual(kwargs["timeout"], 25)
            self.assertFalse(any(args[0].endswith("scp") or "reboot" in args[-1] for args, _ in self.runner.calls))
            self.assertFalse((manager.root / "inventory.json").exists())
            self.assertFalse((manager.root / "hosts").exists())
            self.assertEqual((self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes(), original)
            self.assertEqual(application.store.jobs(), [])

    def test_registered_preflight_excludes_jobs_second_preflight_and_external_maintenance(self):
        entered, release = threading.Event(), threading.Event()
        outcome = []
        with self.registered_app() as (application, manager):
            def blocking_runner(args, **kwargs):
                if args[-1] == module.REMOTE_READ:
                    entered.set()
                    self.assertTrue(release.wait(3))
                return self.runner(args, **kwargs)
            manager.runner = blocking_runner
            def check():
                outcome.append(application.dispatch("drones.preflight", {"number": 5}, self.context))
            thread = threading.Thread(target=check)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(ApiError) as caught:
                    application.submit_job("update", "aqua-hub", {}, lambda: self.fail("Must not update"))
                self.assertEqual(caught.exception.code, "busy")
                with self.assertRaises(ApiError) as caught:
                    application.dispatch("drones.preflight", {"number": 6}, self.context)
                self.assertEqual(caught.exception.code, "busy")
                with application._maintenance_path.open("r+") as descriptor:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(outcome[0]["allowed"], outcome)
            self.assertFalse(application._mutation_lock.locked())
            with application._maintenance_path.open("r+") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_registered_preflight_refuses_pending_update_external_maintenance_and_unresolved_job(self):
        with self.registered_app() as (application, _):
            application.app_updates = SimpleNamespace(installation_pending=Mock(return_value=True))
            with self.assertRaises(ApiError) as caught:
                application.dispatch("drones.preflight", {"number": 5}, self.context)
            self.assertEqual(caught.exception.code, "maintenance")
            # Pending installation may appear between dispatch and reservation.
            application.app_updates.installation_pending = Mock(side_effect=[False, True])
            with self.assertRaises(ApiError) as caught:
                application.dispatch("drones.preflight", {"number": 5}, self.context)
            self.assertEqual(caught.exception.code, "maintenance")
            application.app_updates.installation_pending = Mock(return_value=False)
            with application._maintenance_path.open("r+") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ApiError) as caught:
                    application.dispatch("drones.preflight", {"number": 5}, self.context)
                self.assertEqual(caught.exception.code, "maintenance")
            application.store.create_job("legacy", "aqua-hub", {})
            with self.assertRaises(ApiError) as caught:
                application.dispatch("drones.preflight", {"number": 5}, self.context)
            self.assertEqual(caught.exception.code, "busy")
            self.assertEqual(self.runner.calls, [])
            self.assertFalse(application._mutation_lock.locked())

    def test_registered_preflight_releases_locks_after_failure_and_unexpected_error(self):
        with self.registered_app() as (application, manager):
            for error in (ApiError("ssh_failed", "synthetic failure"), RuntimeError("synthetic unexpected failure")):
                manager.runner = Mock(side_effect=error)
                if isinstance(error, ApiError):
                    result = application.dispatch("drones.preflight", {"number": 5}, self.context)
                    self.assertFalse(result["allowed"])
                    self.assertEqual(result["code"], "ssh_failed")
                else:
                    with self.assertRaises(RuntimeError):
                        application.dispatch("drones.preflight", {"number": 5}, self.context)
                self.assertFalse(application._mutation_lock.locked())
                with application._maintenance_path.open("r+") as descriptor:
                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

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

    def test_existing_larger_fleet_is_preserved_without_sending_files(self):
        (self.app.config.bundle_path / "config/mavlink/config.conf").write_text(hub_config(6))
        result = self.preflight()
        self.assertTrue(result["allowed"], result)
        plan = self.manager.preflights[result["preflight_id"]]
        self.assertNotEqual(plan["expected_hub_sha"], plan["generated_hub_sha"])
        self.assertEqual((self.app.config.bundle_path / "config/mavlink/config.conf").read_text(), hub_config(6))
        self.assertFalse(any(args[0].endswith("scp") for args, _ in self.runner.calls))

    def test_fleet_targets_can_be_checked_in_any_order_but_extension_is_refused(self):
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        original = hub.read_bytes()
        for number in (5, 3, 1, 4, 2):
            self.runner.remote_kwargs["mavlink"] = drone_config(number)
            result = self.manager.preflight({"number": number}, self.context)
            self.assertTrue(result["allowed"], result)
            self.assertEqual(result["number"], number)
            self.assertEqual(hub.read_bytes(), original)
        result = self.manager.preflight({"number": 6}, self.context)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["code"], "ip_change_forbidden")
        self.assertIn("Aqua_Drone_6", result["message"])
        self.assertIn("extension", result["message"])
        self.assertEqual(hub.read_bytes(), original)
        self.assertFalse(any(args[0].endswith("scp") for args, _ in self.runner.calls))

    def test_hub_change_between_semantic_check_and_fingerprint_is_refused(self):
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        real_hashes = self.manager._hashes
        def changed_hashes(number):
            hub.write_text(hub_config().replace("192.168.101.5", "192.168.120.5"))
            return real_hashes(number)
        with patch.object(self.manager, "_hashes", side_effect=changed_hashes):
            result = self.preflight()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["code"], "configuration_changed")
        self.assertEqual(self.manager.preflights, {})
        self.assertFalse(any(args[0].endswith("scp") for args, _ in self.runner.calls))

    def test_hub_fifo_is_refused_without_waiting_for_a_writer(self):
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        hub.unlink()
        os.mkfifo(hub, 0o600)
        code = '''from pathlib import Path
import sys
from types import SimpleNamespace
from aquahub_agent.drone import DroneManager
from aquahub_agent.security import ApiError
manager = DroneManager.__new__(DroneManager)
manager.app = SimpleNamespace(config=SimpleNamespace(bundle_path=Path(sys.argv[1])))
try:
    manager._read_hub()
except ApiError as exc:
    print(exc.code)
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.app.config.bundle_path)],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "unsafe_path")

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

    @staticmethod
    def wait_registered_job(application, job_id):
        with application._threads_lock:
            workers = list(application._threads)
        for worker in workers:
            worker.join(3)
            if worker.is_alive():
                raise AssertionError("Synthetic configuration did not finish")
        return application.store.job(job_id)

    def test_registered_configuration_requires_preflight_confirmation_target_and_operator(self):
        with self.registered_app(privileged=True) as (application, manager):
            with patch("aquahub_agent.security.subprocess.run") as pkcheck:
                result = application.dispatch("drones.preflight", {"number": 5}, self.context)
                params = {"number": 5, "preflight_id": result["preflight_id"], "confirmed_identity": result["identity"]}
                calls_before = len(self.runner.calls)
                for update, code in (({"preflight_id": None}, "invalid_params"),
                                     ({"preflight_id": "unknown-preflight"}, "preflight_expired"),
                                     ({"confirmed_identity": None}, "identity_confirmation_required"),
                                     ({"number": 4}, "target_mismatch")):
                    with self.subTest(update=update), self.assertRaises(ApiError) as caught:
                        application.dispatch("drones.configure", {**params, **update}, self.context)
                    self.assertEqual(caught.exception.code, code)
                with self.assertRaises(ApiError) as caught:
                    application.dispatch("drones.configure", params, RequestContext(os.getuid() + 1, os.getpid()))
                self.assertEqual(caught.exception.code, "unauthorized")
                manager.preflights[result["preflight_id"]]["uid"] = os.getuid() + 1
                with self.assertRaises(ApiError) as caught:
                    application.dispatch("drones.configure", params, self.context)
                self.assertEqual(caught.exception.code, "target_mismatch")
                pkcheck.assert_not_called()
                self.assertEqual(len(self.runner.calls), calls_before)
                self.assertEqual(application.store.jobs(), [])

    def test_registered_configuration_keeps_maintenance_and_busy_fences_without_prompt(self):
        with self.registered_app(privileged=True) as (application, manager):
            with patch("aquahub_agent.security.subprocess.run") as pkcheck:
                result = application.dispatch("drones.preflight", {"number": 5}, self.context)
                params = {"number": 5, "preflight_id": result["preflight_id"], "confirmed_identity": result["identity"]}
                for pending in (Mock(return_value=True), Mock(side_effect=[False, True])):
                    application.app_updates = SimpleNamespace(installation_pending=pending)
                    with self.assertRaises(ApiError) as caught:
                        application.dispatch("drones.configure", params, self.context)
                    self.assertEqual(caught.exception.code, "maintenance")
                application.app_updates.installation_pending = Mock(return_value=False)
                with application._maintenance_path.open("r+") as descriptor:
                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with self.assertRaises(ApiError) as caught:
                        application.dispatch("drones.configure", params, self.context)
                    self.assertEqual(caught.exception.code, "maintenance")
                with application.operation_guard():
                    with self.assertRaises(ApiError) as caught:
                        application.dispatch("drones.configure", params, self.context)
                    self.assertEqual(caught.exception.code, "busy")
                pkcheck.assert_not_called()
                self.assertFalse(manager.preflights[result["preflight_id"]]["used"])
                self.assertEqual(application.store.jobs(), [])
                self.assertFalse(application._mutation_lock.locked())

    def test_registered_configuration_completes_and_persists_without_admin_prompt(self):
        entered, release = threading.Event(), threading.Event()
        with self.registered_app(privileged=True) as (application, manager):
            with patch("aquahub_agent.security.subprocess.run") as pkcheck:
                result = application.dispatch("drones.preflight", {"number": 5}, self.context)
                second = application.dispatch("drones.preflight", {"number": 5}, self.context)
                params = {"number": 5, "preflight_id": result["preflight_id"], "confirmed_identity": result["identity"]}
                second_params = {**params, "preflight_id": second["preflight_id"]}
                states = deque((False, True))
                manager._reachable = lambda number: states.popleft() if states else True
                original_hub = (self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes()
                def supplier(plan):
                    entered.set()
                    self.assertTrue(release.wait(3))
                    self.runner.rebooted = True
                    return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
                with patch.object(manager, "_deploy_pty", side_effect=supplier) as deploy, patch.object(module.time, "sleep"):
                    job = application.dispatch("drones.configure", params, self.context)
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertEqual(application.store.active_job()["id"], job["id"])
                        with self.assertRaises(ApiError) as caught:
                            application.dispatch("drones.configure", second_params, self.context)
                        self.assertEqual(caught.exception.code, "busy")
                        with self.assertRaises(ApiError) as caught:
                            application.dispatch("drones.configure", params, self.context)
                        self.assertEqual(caught.exception.code, "preflight_expired")
                    finally:
                        release.set()
                    terminal = self.wait_registered_job(application, job["id"])
                deploy.assert_called_once()
                pkcheck.assert_not_called()
                self.assertEqual(terminal["status"], "succeeded", terminal)
                self.assertEqual(terminal["kind"], "configure_drone")
                self.assertEqual(terminal["target"], "Aqua_Drone_5")
                self.assertTrue(terminal["result"]["verified"])
                self.assertTrue(terminal["result"]["reboot_verified"])
                self.assertTrue(terminal["result"]["ip_unchanged"])
                self.assertEqual(terminal["progress"]["percent"], 100)
                self.assertFalse(terminal["progress"]["indeterminate"])
                self.assertEqual(len(application.store.jobs()), 1)
                self.assertIsNone(application.store.active_job())
                self.assertTrue(manager.list()["drones"][4]["configured"])
                plan = manager.preflights[result["preflight_id"]]
                self.assertTrue((plan["stage"] / "drone-before.tar.gz").is_file())
                self.assertEqual(stat.S_IMODE((plan["stage"] / "drone-before.tar.gz").stat().st_mode), 0o600)
                self.assertEqual((self.app.config.bundle_path / "config/mavlink/config.conf").read_bytes(), original_hub)
                self.assertEqual((plan["runtime"] / "deploy.py").read_bytes(),
                                 (self.app.config.bundle_path / "deploy.py").read_bytes())
        with self.registered_app(privileged=True) as (restarted, manager):
            self.assertEqual(restarted.store.job(job["id"]), terminal)
            self.assertTrue(manager.list()["drones"][4]["configured"])
            self.assertEqual(manager.list()["drones"][4]["identity"], IDENTITY)

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

    def test_lower_target_success_verifies_original_hub_not_private_generated_subset(self):
        number = 3
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        original, original_inode = hub.read_bytes(), hub.stat().st_ino
        self.runner.remote_kwargs["mavlink"] = drone_config(number)
        result = self.manager.preflight({"number": number}, self.context)
        self.assertTrue(result["allowed"], result)
        plan = self.manager.preflights[result["preflight_id"]]
        self.assertNotEqual(plan["generated_hub_sha"], plan["expected_hub_sha"])
        states = deque((False, True))
        self.manager._reachable = lambda number: states.popleft() if states else True
        def deploy(plan):
            self.runner.rebooted = True
            return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
        with patch.object(self.manager, "_deploy", side_effect=deploy), patch.object(module.time, "sleep"):
            output = self.manager.configure({"number": number, "preflight_id": result["preflight_id"],
                                             "confirmed_identity": result["identity"]}, self.context)
        self.assertTrue(output["result"]["verified"])
        self.assertEqual(output["result"]["number"], number)
        self.assertEqual(hub.read_bytes(), original)
        self.assertEqual(hub.stat().st_ino, original_inode)
        self.assertEqual([entry["number"] for entry in self.manager.list()["drones"] if entry["configured"]], [number])

    def test_supplier_private_hub_mismatch_is_refused_without_touching_active_hub(self):
        result = self.preflight()
        plan = self.manager.preflights[result["preflight_id"]]
        hub = self.app.config.bundle_path / "config/mavlink/config.conf"
        original = hub.read_bytes()
        def deploy(plan):
            private_hub = plan["runtime"] / "config/mavlink/config.conf"
            self.assertFalse(private_hub.is_symlink())
            self.assertEqual(stat.S_IMODE(private_hub.stat().st_mode), 0o600)
            private_hub.write_text(hub_config(1))
            return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
        with patch.object(self.manager, "_deploy_pty", side_effect=deploy), self.assertRaises(ApiError) as caught:
            self.manager._deploy(plan)
        self.assertEqual(caught.exception.code, "postcheck_failed")
        self.assertEqual(hub.read_bytes(), original)

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
