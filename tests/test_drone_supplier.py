"""Exercise the unchanged, locally supplied deploy.py with fake transports.

Only locally supplied code is read. Keys are synthetic. SSH, SCP, Docker and
reboot are intercepted; this test cannot reach the physical drone.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aquahub_agent import drone as module
from aquahub_agent.drone import DroneManager
from aquahub_agent.security import RequestContext
from test_drone import App, IDENTITY, WG_BYTES, remote_payload


SUPPLIER_LOCATION = os.environ.get("AQUAHUB_SUPPLIER_BUNDLE")
SUPPLIER = Path(SUPPLIER_LOCATION) if SUPPLIER_LOCATION else None


@unittest.skipUnless(SUPPLIER is not None and (SUPPLIER / "deploy.py").is_file(), "Fournisseur local absent ; fournir AQUAHUB_SUPPLIER_BUNDLE pour cette recette.")
class SupplierContractTests(unittest.TestCase):
    def test_actual_supplier_script_private_mirror_and_exact_generated_files(self):
        with tempfile.TemporaryDirectory(prefix="aquahub-supplier-test-") as temporary:
            root = Path(temporary)
            app = App(root)
            for relative in module.KNOWN_FILES:
                destination = app.config.bundle_path / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SUPPLIER / relative, destination)
            generator = app.config.bundle_path / "mavlink-config-generator/main.py"
            hub = app.config.bundle_path / "config/mavlink/config.conf"
            hub.parent.mkdir(parents=True)
            drone = root / "drone-expected.conf"
            subprocess.run([sys.executable, "-I", "-B", str(generator), "f::1", "5", str(hub)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run([sys.executable, "-I", "-B", str(generator), "drone", "5", str(drone)], check=True, stdout=subprocess.DEVNULL)
            hub_before, inode_before = hub.read_bytes(), hub.stat().st_ino
            remote_config = drone.read_text()
            def runner(args, **kwargs):
                if args[0] == "/usr/bin/ssh" and args[-1] == module.REMOTE_READ:
                    return remote_payload(mavlink=remote_config)
                if args[0] == sys.executable and "-I" in args and "main.py" in args[-4]:
                    return module._bounded_run(args, **kwargs)
                raise AssertionError("Unexpected real command: " + repr(args))
            manager = DroneManager(app, runner=runner)
            for number in range(1, 7):
                path = manager._wg_path(number)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(WG_BYTES)
            credentials = {"options": [], "fingerprint": IDENTITY["host_key_fingerprint"],
                "host": "root@192.168.205.2", "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}}
            manager._hub_network = lambda: {"addresses": [], "routes": []}
            with patch.object(manager, "_ssh_credentials", return_value=credentials):
                preflight = manager.preflight({"number": 5}, RequestContext(os.getuid(), os.getpid()))
            self.assertTrue(preflight["allowed"], preflight)
            plan = manager.preflights[preflight["preflight_id"]]
            remote_dir, log_path = root / "remote", root / "transport.jsonl"
            remote_dir.mkdir()
            def fake_guard(plan):
                tools = plan["stage"] / "fake-bin"
                tools.mkdir(mode=0o700)
                code = '''#!/usr/bin/python3
import json,pathlib,shutil,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with pathlib.Path(LOG).open("a") as stream: stream.write(json.dumps([name,args])+"\\n")
if name=="scp":
    source,target=args[-2:]
    host,remote=target.split(":",1)
    assert host=="root@192.168.205.2"
    assert remote in ("/etc/mavlink-router/main.conf","/etc/wireguard/wg0.conf","/etc/dmx/main.config")
    filename={"/etc/mavlink-router/main.conf":"mavlink.conf","/etc/wireguard/wg0.conf":"wg0.conf","/etc/dmx/main.config":"dmx.config"}[remote]
    shutil.copyfile(source,pathlib.Path(REMOTE)/filename)
elif name=="ssh":
    assert args[-2]=="root@192.168.205.2"
    assert args[-1] in ("mkdir -p /etc/dmx","(sleep 1 ; reboot) >/dev/null 2>&1 &")
elif name=="docker":
    assert args==["compose","restart","net_setup","mavlink_router","gcs_reactor"]
else: raise AssertionError(name)
'''.replace("LOG", repr(str(log_path))).replace("REMOTE", repr(str(remote_dir)))
                for tool in ("ssh", "scp", "docker"):
                    script = tools / tool
                    script.write_text(code)
                    script.chmod(0o700)
                return tools
            with patch.object(manager, "_guard", side_effect=fake_guard):
                result = manager._deploy(plan)
            self.assertEqual(result, {"automatic_completed": 7, "manual_skipped": 5, "failed": 0})
            self.assertEqual((remote_dir / "mavlink.conf").read_bytes(), drone.read_bytes())
            self.assertEqual((remote_dir / "wg0.conf").read_bytes(), WG_BYTES)
            self.assertIn("MQTT_SERVER=192.0.2.10,192.0.2.11", (remote_dir / "dmx.config").read_text())
            self.assertEqual(hub.read_bytes(), hub_before)
            self.assertEqual(hub.stat().st_ino, inode_before)
            self.assertFalse((app.config.bundle_path / "build").exists())
            calls = [json.loads(line) for line in log_path.read_text().splitlines()]
            self.assertEqual([call[0] for call in calls], ["scp", "docker", "scp", "ssh", "scp", "ssh"])
            self.assertEqual(module.sha256_file(plan["runtime"] / "deploy.py"), module.KNOWN_FILES["deploy.py"])
            self.assertEqual(module.sha256_file(app.config.bundle_path / "deploy.py"), module.KNOWN_FILES["deploy.py"])

    def test_production_guard_refuses_unexpected_ssh_scp_and_docker_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = App(root)
            manager = DroneManager(app)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            plan = {"stage": stage, "number": 5,
                    "credentials": {"options": [], "host": "root@192.168.205.2", "env": {"PATH": "/usr/bin:/bin"}}}
            tools = manager._guard(plan)
            for tool, arguments in (("ssh", ["root@192.168.205.2", "touch /should-not-run"]),
                                    ("scp", ["/etc/passwd", "root@192.168.205.2:/etc/passwd"]),
                                    ("docker", ["system", "prune", "-af"])):
                result = subprocess.run([str(tools / tool), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
                self.assertEqual(result.returncode, 97)
                self.assertIn("refusée", result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
