"""Synthetic log collection, private save boundaries and native SAVE behavior."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from aquahub_agent.core import Application, Config
from aquahub_agent.docker import SERVICES
from aquahub_agent.log_exports import LogExports, MAX_TEXT_BYTES, register
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext, safe_value
from aquahub_desktop.bridge import BridgeError
from aquahub_desktop.log_exports import (SERVICES as DESKTOP_SERVICES, build_log_export,
                                        choose_log_destination, destination_identity,
                                        export_filename, export_request, save_log_export)


class ExportDocker:
    def __init__(self):
        self.rows = [{"service": service, "id": format(number + 1, "064x")} for number, service in enumerate(SERVICES)]
        self.outputs = {row["id"]: "2026-10-21T09:00:00Z synthetic ready\n" for row in self.rows}
        self.calls = []
        self.inspections = 0
        self.error = None

    def containers(self):
        self.inspections += 1
        if self.error:
            raise self.error
        return self.rows

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert args[:3] == ["logs", "--timestamps", "--tail"]
        value = self.outputs[args[-1]]
        if callable(value):
            return value(args, kwargs)
        if isinstance(value, Exception):
            raise value
        return value


class LogCollectionTests(unittest.TestCase):
    def setUp(self):
        self.docker = ExportDocker()
        self.module = LogExports(SimpleNamespace(docker=self.docker))

    def call(self, params=None):
        return self.module.export(params or {}, None)

    def test_group_resolves_known_supplier_project_once_and_reads_only_exact_ids(self):
        snapshot = self.call()
        self.assertEqual(self.docker.inspections, 1)
        self.assertEqual(len(snapshot["entries"]), 8)
        self.assertFalse(snapshot["partial"])
        self.assertTrue(snapshot["created_at"].endswith("Z"))
        self.assertEqual([args[-1] for args, _ in self.docker.calls], [row["id"] for row in self.docker.rows])
        for args, options in self.docker.calls:
            self.assertEqual(args[3], "1000")
            self.assertTrue(options["combine"])
            self.assertLessEqual(options["timeout"], 6)

    def test_one_service_empty_is_explicit_and_does_not_read_other_containers(self):
        identifier = self.docker.rows[2]["id"]
        self.docker.outputs[identifier] = ""
        result = self.call({"service": "gcs", "tail": 25})
        self.assertEqual(len(self.docker.calls), 1)
        self.assertEqual(self.docker.calls[0][0][-1], identifier)
        self.assertEqual(result["entries"][0]["state"], "empty")
        self.assertFalse(result["partial"])

    def test_unknown_service_path_and_invalid_limits_fail_before_docker(self):
        for params in ({"service": "../../etc/passwd"}, {"service": None}, {"service": []},
                       {"path": "/tmp/never-written"}, {"tail": True}, {"tail": 0}, {"tail": 1001}, {"tail": "5"}):
            with self.subTest(params=params), self.assertRaises(ApiError):
                self.call(params)
        self.assertEqual(self.docker.inspections, 0)
        self.assertEqual(self.docker.calls, [])

    def test_redaction_precedes_byte_clipping_and_preserves_valid_utf8(self):
        self.docker.outputs[self.docker.rows[2]["id"]] = (
            "-----BEGIN PRIVATE KEY-----\n" + "private-material" * 8000 + "\n-----END PRIVATE KEY-----\n" +
            "🛥" * 15000 + "\npassword='do-not-save-me'\nhttps://operator:never-export@example.invalid/\n")
        result = self.call({"service": "gcs"})
        entry = result["entries"][0]
        self.assertEqual(entry["state"], "truncated")
        self.assertLessEqual(len(entry["text"].encode()), MAX_TEXT_BYTES)
        for secret in ("private-material", "do-not-save-me", "never-export", "�"):
            self.assertNotIn(secret, entry["text"])
        self.assertTrue(result["partial"])

    def test_individual_failure_is_partial_and_never_hides_other_service_logs(self):
        self.docker.outputs[self.docker.rows[2]["id"]] = ApiError("docker_error", "password=private-value unavailable")
        self.docker.rows[4].pop("id")
        result = self.call()
        self.assertTrue(result["partial"])
        self.assertEqual(sum(item["state"] == "available" for item in result["entries"]), 6)
        self.assertEqual(sum(item["state"] == "unavailable" for item in result["entries"]), 2)
        self.assertNotIn("private-value", json.dumps(result))

    def test_docker_outage_returns_explicit_eight_unavailable_entries(self):
        self.docker.error = ApiError("docker_unavailable", "Docker indisponible")
        result = self.call()
        self.assertTrue(result["partial"])
        self.assertTrue(all(item["state"] == "unavailable" and not item["text"] for item in result["entries"]))
        self.assertEqual(self.docker.calls, [])

    def test_large_output_retries_smaller_tail_and_keeps_truncation_visible(self):
        def output(args, _options):
            if int(args[3]) > 250:
                raise ApiError("output_limit", "Synthetic limit")
            return "remaining line\n"
        self.docker.outputs[self.docker.rows[2]["id"]] = output
        result = self.call({"service": "gcs"})
        self.assertEqual([args[3] for args, _ in self.docker.calls], ["1000", "500", "250"])
        self.assertEqual(result["tail"], 1000)
        self.assertEqual(result["entries"][0]["state"], "truncated")
        self.assertIn("remaining", result["entries"][0]["text"])

    def test_unexportable_single_line_stops_at_one_and_does_not_invent_content(self):
        self.docker.outputs[self.docker.rows[2]["id"]] = ApiError("output_limit", "Synthetic limit")
        result = self.call({"service": "gcs"})
        self.assertEqual(self.docker.calls[-1][0][3], "1")
        self.assertLessEqual(len(self.docker.calls), 10)
        self.assertEqual(result["entries"][0]["state"], "unavailable")
        self.assertTrue(result["entries"][0]["truncated"])
        self.assertEqual(result["entries"][0]["text"], "")

    def test_line_limit_reached_is_reported_as_potentially_incomplete_history(self):
        self.docker.outputs[self.docker.rows[2]["id"]] = "one\ntwo\n"
        result = self.call({"service": "gcs", "tail": 2})
        self.assertEqual(result["entries"][0]["state"], "truncated")
        self.assertIn("exhaustif", result["notice"])

    def test_collection_deadline_and_concurrent_exports_are_bounded(self):
        with patch("aquahub_agent.log_exports.time.monotonic", side_effect=[0] + [76] * 8):
            result = self.call()
        self.assertTrue(all(item["error"]["code"] == "timeout" for item in result["entries"]))
        self.assertEqual(self.docker.calls, [])
        self.module._lock.acquire()
        try:
            with self.assertRaises(ApiError) as error:
                self.call()
            self.assertEqual(error.exception.code, "busy")
        finally:
            self.module._lock.release()
        self.assertFalse(self.call()["partial"])

    def test_eight_worst_case_strings_fit_transport_after_filtering(self):
        for identifier in self.docker.outputs:
            self.docker.outputs[identifier] = '\\"\n🛥' * 20000
        result = self.call()
        packet = json.dumps(safe_value({"id": "1", "result": result}), ensure_ascii=False).encode()
        self.assertLess(len(packet), 1024 * 1024)
        self.assertTrue(all(len(item["text"].encode()) <= MAX_TEXT_BYTES for item in result["entries"]))


class LogAuthorizationTests(unittest.TestCase):
    def test_export_is_peer_authenticated_without_interactive_polkit_or_disk_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = ExportDocker()
            app = Application(Config(root / "socket", root / "state", no_poll=True), docker=docker,
                              policy=AuthPolicy(os.getuid(), privileged=True))
            self.addCleanup(app.close)
            register(app)
            before = sorted(path.name for path in (root / "state").iterdir())
            with patch("aquahub_agent.security.subprocess.run", side_effect=AssertionError("No pkcheck expected")):
                snapshot = app.dispatch("logs.export", {"service": "gcs"}, RequestContext(os.getuid(), os.getpid()))
            self.assertEqual(snapshot["entries"][0]["state"], "available")
            self.assertEqual(before, sorted(path.name for path in (root / "state").iterdir()))
            calls = len(docker.calls)
            with self.assertRaises(ApiError) as error:
                app.dispatch("logs.export", {}, RequestContext(os.getuid() + 1, os.getpid()))
            self.assertEqual(error.exception.code, "unauthorized")
            self.assertEqual(len(docker.calls), calls)


class DesktopLogFileTests(unittest.TestCase):
    def setUp(self):
        self.docker = ExportDocker()
        self.module = LogExports(SimpleNamespace(docker=self.docker))

    def test_single_log_and_group_zip_preserve_empty_partial_and_safe_paths(self):
        self.assertEqual(set(SERVICES), DESKTOP_SERVICES)
        snapshot = self.module.export({"service": "gcs"}, None)
        content, partial = build_log_export(snapshot, {"service": "gcs"})
        self.assertFalse(partial)
        self.assertIn(b"synthetic ready", content)
        self.assertIn(b"UTC", content)
        self.docker.outputs[self.docker.rows[0]["id"]] = ""
        self.docker.outputs[self.docker.rows[2]["id"]] = ApiError("timeout", "Synthetic unavailable")
        content, partial = build_log_export(self.module.export({}, None), {})
        self.assertTrue(partial)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(set(archive.namelist()), {name + ".log" for name in SERVICES} | {"resume.json"})
            self.assertTrue(all(not name.startswith("/") and ".." not in name for name in archive.namelist()))
            summary = json.loads(archive.read("resume.json"))
            self.assertTrue(summary["partial"])
            self.assertNotIn("text", summary["entries"][0])
            self.assertIn("Aucune ligne", archive.read("mosquitto.log").decode())
            self.assertIn("Synthetic unavailable", archive.read("gcs.log").decode())
            self.assertTrue(all(info.external_attr >> 16 & 0o777 == 0o600 for info in archive.infolist()))

    def test_renderer_cannot_choose_root_path_or_zip_entry_names(self):
        for params in ({"path": "/etc/never"}, {"service": "../../evil"}, {"service": None}, {"tail": 100000}):
            with self.assertRaises(BridgeError):
                export_request(params)
        snapshot = self.module.export({}, None)
        snapshot["entries"][0]["service"] = "../../evil"
        with self.assertRaises(BridgeError):
            build_log_export(snapshot, {})

    def test_malformed_or_oversized_snapshot_cannot_be_saved(self):
        snapshot = self.module.export({"service": "gcs"}, None)
        for replacement in (None, [], {"schema": 1, "entries": []}):
            with self.assertRaises(BridgeError):
                build_log_export(replacement, {"service": "gcs"})
        snapshot["entries"][0]["text"] = "x" * (MAX_TEXT_BYTES + 1)
        with self.assertRaises(BridgeError):
            build_log_export(snapshot, {"service": "gcs"})

    def test_names_are_stable_utc_and_user_destinations_are_private_atomic(self):
        instant = datetime(2026, 10, 21, 9, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(export_filename({"service": "gcs"}, instant), "aquahub-journal-gcs-20261021-090300Z.log")
        self.assertEqual(export_filename({}, instant), "aquahub-journaux-20261021-090300Z.zip")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.log"
            self.assertIsNone(destination_identity(target))
            save_log_export(target, b"first\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_bytes(), b"first\n")
            identity = destination_identity(target)
            target.chmod(0o644)
            save_log_export(target, b"replacement\n", identity)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_bytes(), b"replacement\n")
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_unconfirmed_overwrite_symlink_and_changed_destination_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.log"
            target.write_text("original")
            with self.assertRaises(BridgeError) as error:
                save_log_export(target, b"unconfirmed")
            self.assertEqual(error.exception.code, "destination_changed")
            identity = destination_identity(target)
            target.write_text("changed by another task")
            with self.assertRaises(BridgeError):
                save_log_export(target, b"must not overwrite", identity)
            link = Path(directory) / "link.log"
            link.symlink_to(target)
            for path in (link, Path(directory)):
                with self.assertRaises(BridgeError):
                    destination_identity(path)
            self.assertEqual(target.read_text(), "changed by another task")

    def test_failed_replace_removes_partial_file_and_leaves_destination_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.log"
            with patch("aquahub_desktop.log_exports.os.replace", side_effect=OSError("synthetic full disk")):
                with self.assertRaises(BridgeError) as error:
                    save_log_export(target, b"private contents")
            self.assertEqual(error.exception.code, "save_failed")
            self.assertEqual(list(Path(directory).iterdir()), [])


class Chooser:
    def __init__(self):
        self.callback = None
        self.filename = None
        self.destroyed = False
    def set_local_only(self, value): self.local_only = value
    def set_do_overwrite_confirmation(self, value): self.confirm_overwrite = value
    def set_current_name(self, value): self.current_name = value
    def add_filter(self, value): self.file_filter = value
    def connect(self, signal, callback): self.callback = callback
    def show(self): self.shown = True
    def get_filename(self): return self.filename
    def destroy(self): self.destroyed = True


class FileFilter:
    def set_name(self, name): self.name = name
    def add_pattern(self, pattern): self.pattern = pattern


class NativeLogDialogTests(unittest.TestCase):
    def fixture(self):
        chooser = Chooser()
        gtk = SimpleNamespace(FileChooserNative=SimpleNamespace(new=lambda *args: chooser),
                              FileChooserAction=SimpleNamespace(SAVE=1),
                              ResponseType=SimpleNamespace(ACCEPT=1, CANCEL=0), FileFilter=FileFilter)
        return gtk, chooser

    def test_cancel_or_missing_filename_does_not_collect_or_create_any_file(self):
        for response in (0, 1):
            gtk, chooser = self.fixture()
            events = []
            choose_log_destination(gtk, None, {}, lambda path: self.fail("must not collect"), lambda: events.append("canceled"))
            chooser.callback(chooser, response)
            self.assertEqual(events, ["canceled"])
            self.assertTrue(chooser.destroyed)
            self.assertTrue(chooser.local_only)
            self.assertTrue(chooser.confirm_overwrite)
            self.assertEqual(chooser.file_filter.pattern, "*.zip")

    def test_accept_passes_only_native_path_and_single_service_filter(self):
        gtk, chooser = self.fixture()
        paths = []
        choose_log_destination(gtk, None, {"service": "gcs"}, paths.append, lambda: self.fail("accepted"))
        chooser.filename = "/tmp/synthetic-choice.log"
        chooser.callback(chooser, 1)
        self.assertEqual(paths, [Path("/tmp/synthetic-choice.log")])
        self.assertTrue(chooser.destroyed)
        self.assertEqual(chooser.file_filter.pattern, "*.log")


@unittest.skipUnless(os.environ.get("AQUAHUB_TEST_GTK") == "1", "Banc GTK explicite sous Xvfb")
class RealGtkLogDialogTests(unittest.TestCase):
    def run_dialog(self, params, response, filename=None):
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
        ready, _args = Gtk.init_check([])
        if not ready:
            self.skipTest("Affichage GTK indisponible")
        loop = GLib.MainLoop()
        results = []
        def accepted(path):
            results.append(path)
            loop.quit()
        def canceled():
            results.append(None)
            loop.quit()
        chooser = choose_log_destination(Gtk, None, params, accepted, canceled)
        if filename:
            chooser.set_current_folder(str(filename.parent))
            chooser.set_current_name(filename.name)
        def respond():
            chooser.emit("response", response)
            return False
        def expired():
            chooser.destroy()
            loop.quit()
            return False
        response_source = GLib.timeout_add(500, respond)
        timeout_source = GLib.timeout_add(5000, expired)
        loop.run()
        if GLib.MainContext.default().find_source_by_id(timeout_source):
            GLib.source_remove(timeout_source)
        if GLib.MainContext.default().find_source_by_id(response_source):
            GLib.source_remove(response_source)
        self.assertEqual(len(results), 1, "La fenêtre native doit produire une seule réponse")
        return results[0]

    def test_real_native_cancel_has_no_destination(self):
        self.assertIsNone(self.run_dialog({}, -6))  # Gtk.ResponseType.CANCEL

    def test_real_native_accept_selects_user_path_for_private_save(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "synthetic-native.log"
            chosen = self.run_dialog({"service": "gcs"}, -3, target)  # ACCEPT
            self.assertEqual(chosen, target)
            save_log_export(chosen, b"Synthetic GTK export\n")
            self.assertEqual(chosen.stat().st_mode & 0o777, 0o600)
            self.assertEqual(chosen.read_bytes(), b"Synthetic GTK export\n")


@unittest.skipUnless(os.environ.get("AQUAHUB_TEST_GTK") == "1", "Banc WebKit explicite sous Xvfb")
class RealWebKitLogBridgeTests(unittest.TestCase):
    def test_renderer_native_backend_file_round_trip_cancel_success_and_partial(self):
        source_root = Path(__file__).resolve().parents[1]
        if not (source_root / "frontend/dist/index.html").is_file():
            self.skipTest("Construire le frontend avant le banc WebKit")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            script = folder / "exercise.js"
            report = folder / "result.json"
            script.write_text("""(async () => {
              try {
                const canceled = await window.aquahub.request('native.export_logs', {});
                const single = await window.aquahub.request('native.export_logs', {service:'gcs'});
                const group = await window.aquahub.request('native.export_logs', {});
                await window.aquahub.request('test.report', {canceled,single,group});
              } catch (error) {
                await window.aquahub.request('test.report', {failed:error.message});
              }
            })();""")
            fixture = r"""
import sys
from pathlib import Path
from types import SimpleNamespace
from aquahub_desktop import app
from aquahub_agent.log_exports import LogExports
from aquahub_agent.security import ApiError
from test_log_exports import ExportDocker
folder, frontend, script, report = map(Path, sys.argv[1:])
docker = ExportDocker()
module = LogExports(SimpleNamespace(docker=docker))
class Client:
    calls = 0
    def __init__(self, *args, **kwargs): pass
    def request(self, method, params=None, **kwargs):
        assert method == 'logs.export', method
        Client.calls += 1
        if Client.calls == 2:
            docker.outputs[docker.rows[0]['id']] = ApiError('timeout', 'Synthetic partial export')
        return module.export(params or {}, None)
choices = []
def chooser(Gtk, parent, params, accepted, canceled):
    from gi.repository import GLib
    choices.append(dict(params))
    if len(choices) == 1:
        GLib.idle_add(canceled)
    else:
        target = folder / ('single.log' if params else 'all.zip')
        GLib.idle_add(lambda: accepted(target))
app.RpcClient = Client
app.choose_log_destination = chooser
code = app.main(['--demo', '--frontend', str(frontend), '--state-dir', str(folder/'state'),
                 '--test-script', str(script), '--test-output', str(report), '--quit-after', '5'])
assert Client.calls == 2, Client.calls
assert choices == [{}, {'service':'gcs'}, {}], choices
raise SystemExit(code)
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(str(source_root / name) for name in ("agent", "desktop", "tests"))
            result = subprocess.run([sys.executable, "-c", fixture, str(folder), str(source_root / "frontend/dist"),
                                     str(script), str(report)], env=environment, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-3000:])
            self.assertTrue(report.is_file(), "La fenêtre WebKit doit terminer le parcours")
            outcome = json.loads(report.read_text())
            self.assertNotIn("failed", outcome)
            self.assertEqual(outcome["canceled"], {"saved": False, "canceled": True})
            self.assertEqual(outcome["single"], {"saved": True, "partial": False, "filename": "single.log"})
            self.assertEqual(outcome["group"], {"saved": True, "partial": True, "filename": "all.zip"})
            self.assertEqual((folder / "single.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual((folder / "all.zip").stat().st_mode & 0o777, 0o600)
            with zipfile.ZipFile(folder / "all.zip") as archive:
                self.assertEqual(len(archive.namelist()), 9)
                self.assertTrue(json.loads(archive.read("resume.json"))["partial"])


if __name__ == "__main__":
    unittest.main()
