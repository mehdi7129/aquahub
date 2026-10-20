"""Public app-release updates, using real Debian parsing and isolated state.

No HTTP request, Docker operation, service launch or package installation can
escape these fixtures. The supplier connector and its credentials are separate.
"""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from aquahub_agent import app_updates as module
from aquahub_agent.app_updates import API, REPOSITORY, AppUpdates, version_tuple
from aquahub_agent.core import Application, Config
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext
from aquahub_agent.updates import _atomic_json
from test_core import FakeDocker


class Response(io.BytesIO):
    def __init__(self, data, *, length=None):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": str(length)}


class InterruptedResponse(Response):
    def read(self, length=-1):
        if self.tell():
            raise urllib.error.URLError("fixture: connection interrupted")
        return super().read(min(length, 3))


class BrokenResponse(Response):
    def __init__(self, error):
        super().__init__(b"")
        self.error = error

    def read(self, length=-1):
        raise self.error


class Opener:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def metadata(self, url, value):
        self.routes[url] = lambda: Response(json.dumps(value).encode())

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if request.full_url not in self.routes:
            raise AssertionError("Unexpected network request: " + request.full_url)
        response = self.routes[request.full_url]
        if isinstance(response, Exception):
            raise response
        return response()


def release(data=b"synthetic deb", version="9.9.9", *, release_id=40, asset_id=80):
    name = f"aquahub-application_{version}_amd64.deb"
    return {"id": release_id, "tag_name": "v" + version, "draft": False,
            "prerelease": False, "body": "Version d’essai", "published_at": "2026-01-01T00:00:00Z",
            "assets": [{"id": asset_id, "name": name, "size": len(data),
                        "state": "uploaded", "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "url": API + "/releases/assets/" + str(asset_id),
                        "browser_download_url": REPOSITORY + "/releases/download/v" + version + "/" + name}]}


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aquahub-app-update-test-")
        self.root = Path(self.temp.name)
        self.app = Application(Config(self.root / "socket", self.root / "state", no_poll=True),
                               docker=FakeDocker(), policy=AuthPolicy(os.getuid(), privileged=False))
        self.opener = Opener()
        self.launcher = Mock()
        self.runner = Mock(side_effect=self.package_field)
        self.manager = AppUpdates(self.app, opener=self.opener, runner=self.runner, launcher=self.launcher)
        self.app.app_updates = self.manager
        for name in ("status", "check", "download", "install"):
            self.app.register_handler("app_updates." + name, getattr(self.manager, name), mutation=name in {"download", "install"})
        self.context = RequestContext(os.getuid(), os.getpid())
        self.version_patch = patch.object(module, "__version__", "1.0.0")
        self.version_patch.start()

    def tearDown(self):
        self.app.close()
        self.version_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def package_field(args, **kwargs):
        if args[:2] != ["/usr/bin/dpkg-deb", "--field"]:
            raise AssertionError("Unexpected system command: " + repr(args))
        fields = {"Package": "aquahub-setup", "Version": "9.9.9", "Architecture": "amd64",
                  "X-AquaHub-Update": "application-only"}
        return SimpleNamespace(returncode=0, stdout=fields[args[-1]] + "\n", stderr="")

    def call(self, method, params=None):
        return self.app.dispatch(method, params or {}, self.context)

    def expect(self, code, method, params=None):
        with self.assertRaises(ApiError) as caught:
            self.call(method, params)
        self.assertEqual(caught.exception.code, code)

    def wait(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.app.store.job(job["id"])
            if result["status"] not in {"queued", "running"} and not self.app._mutation_lock.locked():
                return result
            time.sleep(.005)
        self.fail("Background job did not finish")

    def prepare(self, data=b"synthetic deb", *, item=None, response=None):
        item = item or release(data)
        self.opener.metadata(API + "/releases?per_page=20", [item])
        self.opener.metadata(API + "/releases/" + str(item["id"]), item)
        self.opener.routes[item["assets"][0]["url"]] = response or (lambda: Response(data, length=len(data)))
        self.call("app_updates.check")
        return item

    def download(self, item):
        return self.wait(self.call("app_updates.download", {"release_id": item["id"], "asset_id": item["assets"][0]["id"]}))

    def stage(self):
        item = self.prepare()
        job = self.download(item)
        self.assertEqual(job["status"], "succeeded", job)
        return item

    def test_fixed_public_channel_never_reads_or_transmits_supplier_credentials(self):
        supplier = self.app.config.state_dir / "updates"
        supplier.mkdir()
        _atomic_json(supplier / "github.json", {"token": "synthetic-supplier-secret", "token_provider": "gitlab"})
        self.app.store.setting_set("github_url", "https://gitlab.com/private/example")
        self.opener.metadata(API + "/releases?per_page=20", [])
        result = self.call("app_updates.check")
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertFalse(result["available"])
        self.assertIn("commit Git", result["message"])
        self.assertEqual(len(self.opener.calls), 1)
        request, timeout = self.opener.calls[0]
        self.assertEqual(request.full_url, API + "/releases?per_page=20")
        self.assertEqual(timeout, 30)
        headers = {key.lower(): value for key, value in {**request.headers, **request.unredirected_hdrs}.items()}
        self.assertFalse({"authorization", "private-token"} & set(headers))
        self.assertNotIn("synthetic-supplier-secret", json.dumps(headers))
        for params in ({"url": "https://evil.example/package"}, {"repository": "https://github.com/other/repo"}, {"token": "new-secret"}):
            self.expect("invalid_params", "app_updates.check", params)
        self.assertEqual(len(self.opener.calls), 1)

    def test_stable_newest_semantic_version_selected_not_latest_date_or_list_order(self):
        old = release(version="0.9.0", release_id=1)
        newer = release(version="2.9.0", release_id=2)
        highest = release(version="2.10.0", release_id=3)
        highest["published_at"] = "2020-01-01T00:00:00Z"
        draft = release(version="99.0.0", release_id=4); draft["draft"] = True
        prerelease = release(version="98.0.0", release_id=5); prerelease["prerelease"] = True
        self.opener.metadata(API + "/releases?per_page=20", [draft, newer, old, highest, prerelease])
        result = self.call("app_updates.check")
        self.assertEqual(result["release"]["version"], "2.10.0")
        self.assertNotIn("_url", result["release"])
        self.assertEqual(version_tuple("2.10.0"), (2, 10, 0))
        for bad in ("1.2", "01.2.3", "1.2.3-beta", "1.2.3\nDepends: bad", True, None):
            with self.subTest(version=bad), self.assertRaises(ApiError):
                version_tuple(bad)

    def test_unverified_source_full_kit_duplicate_and_arbitrary_assets_are_rejected(self):
        invalid = []
        for field, value in (("digest", None), ("digest", "sha256:" + "A" * 64), ("state", "new"),
                             ("size", module.MAX_PACKAGE + 1), ("size", True), ("id", True),
                             ("url", "https://evil.example/package"),
                             ("browser_download_url", "https://github.com/other/repo/releases/download/v9.9.9/package"),
                             ("name", "aquahub-setup_9.9.9_amd64.deb"),
                             ("name", "aquahub-application_9.9.9_arm64.deb"), ("name", "source.zip")):
            item = release(); item["assets"][0][field] = value; invalid.append(item)
        duplicate = release(); duplicate["assets"].append(dict(duplicate["assets"][0])); invalid.append(duplicate)
        for item in invalid:
            with self.subTest(asset=item["assets"][0]):
                self.assertIsNone(AppUpdates._candidate(item))
        self.opener.metadata(API + "/releases?per_page=20", invalid)
        self.assertFalse(self.call("app_updates.check")["available"])

    def test_metadata_is_bounded_and_duplicate_json_keys_rejected(self):
        url = API + "/releases?per_page=20"
        for value in ({"unexpected": []}, [{}] * 21):
            self.opener.metadata(url, value)
            self.expect("app_update_metadata", "app_updates.check")
        for raw in (b'{"same":1,"same":2}', b"[" + b" " * module.MAX_METADATA + b"]"):
            self.opener.routes[url] = lambda raw=raw: Response(raw)
            self.expect("app_update_metadata", "app_updates.check")

    def test_metadata_body_interruptions_are_actionable_network_errors(self):
        url = API + "/releases?per_page=20"
        for error in (urllib.error.URLError("fixture disconnected"), TimeoutError("fixture timeout"),
                      http.client.IncompleteRead(b"partial", 20)):
            with self.subTest(error=type(error).__name__):
                self.opener.routes[url] = lambda error=error: BrokenResponse(error)
                self.expect("app_update_network", "app_updates.check")

    def test_stale_unknown_and_mismatched_download_request_never_downloads(self):
        self.expect("app_update_expired", "app_updates.download", {"release_id": 40, "asset_id": 80})
        item = self.prepare()
        for params in ({"release_id": 41, "asset_id": 80}, {"release_id": 40, "asset_id": True},
                       {"release_id": 40, "asset_id": 81}, {"release_id": "40", "asset_id": 80}):
            self.expect("app_update_expired", "app_updates.download", params)
        self.manager.checked_at -= module.TTL + 1
        self.expect("app_update_expired", "app_updates.download", {"release_id": item["id"], "asset_id": 80})
        self.assertEqual(len(self.opener.calls), 1)

    def test_release_changed_since_check_is_refused_before_binary_download(self):
        item = self.prepare()
        changed = release(b"different package")
        self.opener.metadata(API + "/releases/40", changed)
        job = self.download(item)
        self.assertEqual(job["error"]["code"], "app_update_changed")
        self.assertFalse((self.manager.root / "ready.json").exists())
        self.assertFalse(any("/releases/assets/" in request.full_url for request, _ in self.opener.calls))

    def test_streaming_success_persists_verified_package_and_observed_progress(self):
        data = b"fixture package bytes" * 65536
        item = self.prepare(data)
        job = self.download(item)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["progress"]["completed"], len(data))
        self.assertEqual(job["progress"]["percent"], 100)
        package = self.manager.root / "package.deb"
        self.assertEqual(package.read_bytes(), data)
        self.assertEqual(package.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.manager.root.glob("*.part")))
        self.assertTrue(self.call("app_updates.status")["download"]["ready"])
        self.assertEqual(len(self.runner.call_args_list), 4)
        self.launcher.assert_not_called()
        self.assertEqual(self.app.docker.actions, [])

    def test_truncated_oversized_wrong_digest_or_content_length_downloads_never_stage(self):
        data = b"expected package"
        for received, length in ((data[:-1], len(data)), (data + b"extra", len(data)),
                                 (b"wrong!!! package", len(data)), (data, "unknown"), (data, len(data) + 1)):
            with self.subTest(length=length, size=len(received)):
                item = self.prepare(data, response=lambda received=received, length=length: Response(received, length=length))
                job = self.download(item)
                self.assertEqual(job["error"]["code"], "app_update_integrity", job)
                self.assertFalse((self.manager.root / "ready.json").exists())
                self.assertFalse((self.manager.root / "package.deb").exists())
                self.assertFalse(list(self.manager.root.glob("*.part")))

    def test_interrupted_stream_is_removed_and_failure_is_actionable(self):
        item = self.prepare(response=lambda: InterruptedResponse(b"synthetic deb"))
        job = self.download(item)
        self.assertEqual(job["error"]["code"], "app_update_network")
        self.assertLess(job["progress"]["percent"], 100)
        self.assertFalse((self.manager.root / "ready.json").exists())
        self.assertFalse(list(self.manager.root.glob("*.part")))

    def test_interrupted_chunked_stream_never_becomes_internal_error_or_ready_package(self):
        item = self.prepare(response=lambda: BrokenResponse(http.client.IncompleteRead(b"partial", 20)))
        job = self.download(item)
        self.assertEqual(job["error"]["code"], "app_update_network", job)
        self.assertFalse((self.manager.root / "ready.json").exists())
        self.assertFalse(list(self.manager.root.glob("*.part")))

    def test_insufficient_disk_space_prevents_binary_download(self):
        item = self.prepare()
        with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            job = self.download(item)
        self.assertEqual(job["error"]["code"], "disk_space")
        self.assertFalse(any("/releases/assets/" in request.full_url for request, _ in self.opener.calls))

    @unittest.skipUnless(shutil.which("dpkg-deb"), "Debian package tool absent")
    def test_real_debian_fields_accept_only_application_package_not_full_kit(self):
        self.manager.runner = subprocess.run
        base = {"Package": "aquahub-setup", "Version": "9.9.9", "Architecture": "amd64",
                "X-AquaHub-Update": "application-only"}
        for changed in ({}, {"Package": "other-package"}, {"Version": "9.9.8"},
                        {"Architecture": "all"}, {"X-AquaHub-Update": "full-kit"}, {"X-AquaHub-Update": None}):
            with self.subTest(changed=changed):
                staging = self.root / "deb-staging"
                shutil.rmtree(staging, ignore_errors=True)
                (staging / "DEBIAN").mkdir(parents=True)
                fields = {**base, **changed}
                control = "".join(key + ": " + value + "\n" for key, value in fields.items() if value is not None)
                (staging / "DEBIAN/control").write_text(control + "Maintainer: AquaHub QA <qa@example.invalid>\nDescription: Synthetic application package\n")
                package = self.root / "field-test.deb"
                subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(staging), str(package)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                package.chmod(0o600)
                size = package.stat().st_size
                digest = hashlib.sha256(package.read_bytes()).hexdigest()
                if changed:
                    with self.assertRaises(ApiError) as caught:
                        self.manager._package(package, "9.9.9", digest, size)
                    self.assertEqual(caught.exception.code, "app_update_package")
                else:
                    self.manager._package(package, "9.9.9", digest, size)

    def test_invalid_package_fields_or_symlink_or_permissions_never_stage(self):
        for field, wrong in (("Package", "other"), ("Version", "9.9.8"), ("Architecture", "arm64"), ("X-AquaHub-Update", "")):
            with self.subTest(field=field):
                def fields(args, field=field, wrong=wrong, **kwargs):
                    return SimpleNamespace(returncode=0, stdout=wrong if args[-1] == field else self.package_field(args).stdout)
                self.runner.side_effect = fields
                item = self.prepare()
                job = self.download(item)
                self.assertEqual(job["error"]["code"], "app_update_package")
                self.assertFalse((self.manager.root / "ready.json").exists())
        self.runner.side_effect = self.package_field
        package = self.root / "private.deb"
        package.write_bytes(b"synthetic deb"); package.chmod(0o622)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        with self.assertRaises(ApiError):
            self.manager._package(package, "9.9.9", digest, package.stat().st_size)
        link = self.root / "linked.deb"; link.symlink_to(package)
        with self.assertRaises(ApiError):
            self.manager._package(link, "9.9.9", digest, package.stat().st_size)

    def test_install_requires_confirmation_matching_ready_version_and_no_active_job(self):
        self.expect("confirmation_required", "app_updates.install", {"version": "9.9.9"})
        self.expect("app_update_not_ready", "app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.stage()
        self.expect("app_update_not_ready", "app_updates.install", {"version": "9.9.8", "confirmed": True})
        started, finish = threading.Event(), threading.Event()
        job = self.app.submit_job("test.blocking", "test", {}, lambda: (started.set(), finish.wait(2), {})[-1])
        self.assertTrue(started.wait(1))
        try:
            self.expect("busy", "app_updates.install", {"version": "9.9.9", "confirmed": True})
            self.launcher.assert_not_called()
        finally:
            finish.set(); self.wait(job)

    def test_install_respects_external_package_lock_and_unfinished_persisted_jobs(self):
        self.stage()
        with self.app._maintenance_path.open("r+") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.expect("maintenance", "app_updates.install", {"version": "9.9.9", "confirmed": True})
            self.assertFalse(self.app._mutation_lock.locked())
        job, _ = self.app.store.create_job("configure_drone", "Aqua_Drone_5", {})
        self.expect("busy", "app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.assertFalse(self.app._mutation_lock.locked())
        self.launcher.assert_not_called()
        self.app.store.set_job(job["id"], "interrupted")

    def test_install_revalidates_remote_release_and_local_bytes_before_launch(self):
        self.stage()
        changed = release(b"replaced upstream package")
        self.opener.metadata(API + "/releases/40", changed)
        self.expect("app_update_changed", "app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.launcher.assert_not_called()
        self.opener.metadata(API + "/releases/40", release())
        package = self.manager.root / "package.deb"
        package.write_bytes(b"altered local")
        self.expect("app_update_integrity", "app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.assertFalse((self.manager.root / "pending.json").exists())

    def test_install_is_independent_scheduled_once_and_blocks_other_mutations(self):
        self.stage()
        before = len(self.app.store.jobs())
        result = self.call("app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.assertTrue(result["scheduled"])
        self.assertEqual(result["installation"]["status"], "scheduled")
        self.assertEqual(len(self.app.store.jobs()), before)  # never a worker that kills its own installer
        self.launcher.assert_called_once_with()
        pending = self.manager._read("pending.json")
        self.assertEqual(pending["version"], "9.9.9")
        again = self.call("app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.assertTrue(again["scheduled"])
        self.launcher.assert_called_once()
        self.assertTrue(self.manager.installation_pending())
        for method, params in (("service.restart", {"service": "gcs"}), ("settings.save", {"theme": "dark"}),
                               ("app_updates.download", {"release_id": 40, "asset_id": 80})):
            self.expect("maintenance", method, params)
        self.expect("maintenance", "app_updates.install", {"version": "10.0.0", "confirmed": True})
        self.assertEqual(self.call("app_updates.status")["installation"]["status"], "scheduled")
        self.assertEqual(self.call("settings.get")["theme"], "system")
        self.assertFalse(self.app.docker.actions)

    def test_pending_guard_is_rechecked_when_a_previously_authorized_handler_submits_its_job(self):
        self.stage()
        passed_guard, resume = threading.Event(), threading.Event()
        executed = threading.Event()
        outcome = []
        def delayed(params, context):
            passed_guard.set()
            if not resume.wait(2):
                raise AssertionError("Test synchronization expired")
            return self.app.submit_job("test.delayed", "test", {}, lambda: executed.set())
        self.app.register_handler("test.delayed", delayed, mutation=True)
        def invoke():
            try:
                outcome.append(self.call("test.delayed"))
            except ApiError as error:
                outcome.append({"error": error.code})
        request = threading.Thread(target=invoke)
        request.start()
        self.assertTrue(passed_guard.wait(1))
        try:
            scheduled = self.call("app_updates.install", {"version": "9.9.9", "confirmed": True})
            self.assertTrue(scheduled["scheduled"])
        finally:
            resume.set()
            request.join(2)
        self.assertFalse(request.is_alive())
        self.assertEqual(outcome, [{"error": "maintenance"}])
        self.assertFalse(executed.is_set())
        self.assertFalse(self.app._mutation_lock.locked())

    def test_launch_failure_is_durable_does_not_leave_mutations_blocked(self):
        self.stage()
        self.launcher.side_effect = ApiError("app_update_installation", "fixture launch failure")
        self.expect("app_update_installation", "app_updates.install", {"version": "9.9.9", "confirmed": True})
        self.assertFalse(self.manager.installation_pending())
        self.assertEqual(self.call("app_updates.status")["installation"]["status"], "failed")
        self.assertEqual(self.call("settings.save", {"theme": "dark"})["theme"], "dark")
        self.assertFalse(self.app._mutation_lock.locked())

    def test_system_launch_accepts_only_installed_root_stage_and_fixed_helper(self):
        with self.assertRaises(ApiError) as caught:
            self.manager._launch()
        self.assertEqual(caught.exception.code, "app_update_installation")
        self.runner.assert_not_called()
        self.manager.runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        with patch.object(module.os, "geteuid", return_value=0), patch.object(module, "STAGE", self.manager.root):
            self.manager._launch()
        command = self.manager.runner.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/systemd-run", "--unit=aquahub-application-upgrade", "--collect",
            "--property=Type=exec", "/usr/bin/python3", "/usr/lib/aquahub/application-upgrade.py"])
        self.expect("invalid_params", "app_updates.install", {"version": "9.9.9", "confirmed": True, "path": "/tmp/user-selected.deb"})

    def test_public_api_failures_are_reported_without_exposing_response_body(self):
        url = API + "/releases?per_page=20"
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.opener.routes[url] = urllib.error.HTTPError(url, status, "fixture-private-response", {}, io.BytesIO(b"fixture-secret"))
                with self.assertRaises(ApiError) as caught:
                    self.call("app_updates.check")
                self.assertEqual(caught.exception.code, "app_update_source")
                self.assertNotIn("fixture-private", caught.exception.message)
                self.assertNotIn("fixture-secret", caught.exception.message)

    def pending_state(self, *, status="scheduled", age=60):
        _atomic_json(self.manager.root / "status.json", {"status": status, "version": "9.9.9",
            "message": "Synthetic pending installation", "updated_at": time.time() - age})

    @staticmethod
    def recovery_runner(*, active=False, version="9.9.9", error=None):
        def run(args, **kwargs):
            if error:
                raise error
            if args[0] == "/usr/bin/systemctl":
                return SimpleNamespace(returncode=0 if active else 3, stdout="", stderr="")
            if args[0] == "/usr/bin/dpkg-query":
                return SimpleNamespace(returncode=0, stdout="install ok installed\t" + version + "\n", stderr="")
            raise AssertionError("Unexpected recovery command: " + repr(args))
        return Mock(side_effect=run)

    def test_stale_pending_recovers_success_only_when_runtime_and_debian_versions_match(self):
        self.pending_state()
        self.manager.runner = self.recovery_runner()
        with patch.object(module, "STAGE", self.manager.root), patch.object(module, "__version__", "9.9.9"):
            result = self.call("app_updates.status")
            self.assertEqual(result["installation"]["status"], "installed")
            self.assertFalse(self.manager.installation_pending())
        commands = [call.args[0] for call in self.manager.runner.call_args_list]
        self.assertEqual(commands[0], ["/usr/bin/systemctl", "is-active", "--quiet", "aquahub-application-upgrade.service"])
        self.assertEqual(commands[1][0], "/usr/bin/dpkg-query")
        self.assertEqual(commands[1][-1], "aquahub-setup")
        self.launcher.assert_not_called()
        self.assertEqual(self.app.store.jobs(), [])

    def test_stale_pending_with_stopped_installer_and_mismatched_versions_becomes_failed(self):
        for runtime, debian in (("1.0.0", "9.9.9"), ("9.9.9", "1.0.0")):
            with self.subTest(runtime=runtime, debian=debian):
                self.pending_state(status="installing")
                self.manager.runner = self.recovery_runner(version=debian)
                with patch.object(module, "STAGE", self.manager.root), patch.object(module, "__version__", runtime):
                    result = self.call("app_updates.status")
                    self.assertEqual(result["installation"]["status"], "failed")
                    self.assertFalse(self.manager.installation_pending())
                self.launcher.assert_not_called()

    def test_stale_pending_keeps_mutations_blocked_while_installer_is_active(self):
        self.pending_state(status="installing")
        self.manager.runner = self.recovery_runner(active=True)
        with patch.object(module, "STAGE", self.manager.root):
            result = self.call("app_updates.status")
            self.assertEqual(result["installation"]["status"], "installing")
            self.assertTrue(self.manager.installation_pending())
            self.expect("maintenance", "settings.save", {"theme": "dark"})
        self.assertTrue(self.manager.runner.called)
        self.assertTrue(all(call.args[0][0] == "/usr/bin/systemctl" for call in self.manager.runner.call_args_list))
        self.launcher.assert_not_called()

    def test_young_or_nonproduction_pending_never_runs_recovery_commands(self):
        self.pending_state(age=1)
        with patch.object(module, "STAGE", self.manager.root):
            self.assertTrue(self.manager.installation_pending())
        self.pending_state(age=60)
        self.assertTrue(self.manager.installation_pending())  # private dev state is not the production stage
        self.runner.assert_not_called()
        self.launcher.assert_not_called()

    def test_recovery_probe_failure_does_not_claim_success_or_unlock_mutations(self):
        self.pending_state()
        self.manager.runner = self.recovery_runner(error=OSError("fixture: systemctl unavailable"))
        with patch.object(module, "STAGE", self.manager.root):
            result = self.call("app_updates.status")
            self.assertEqual(result["installation"]["status"], "scheduled")
            self.assertTrue(self.manager.installation_pending())
            self.expect("maintenance", "settings.save", {"theme": "dark"})
        self.launcher.assert_not_called()

    def test_protected_status_cannot_be_replaced_by_symlink_or_writable_json(self):
        target = self.root / "outside.json"; target.write_text('{"status":"scheduled"}')
        status = self.manager.root / "status.json"; status.symlink_to(target)
        self.expect("unsafe_path", "app_updates.status")
        status.unlink(); status.write_text('{"status":"scheduled"}'); status.chmod(0o666)
        self.expect("unsafe_path", "app_updates.status")

    def test_restarting_manager_removes_only_owned_partial_downloads(self):
        orphan = self.manager.root / ("download-" + "a" * 32 + ".part")
        other = self.manager.root / "download-operator.part"
        outside = self.root / "unrelated"
        outside.write_bytes(b"keep")
        link = self.manager.root / ("download-" + "b" * 32 + ".part")
        orphan.write_bytes(b"incomplete")
        other.write_bytes(b"keep")
        link.symlink_to(outside)
        AppUpdates(self.app, opener=self.opener, runner=self.runner, launcher=self.launcher)
        self.assertFalse(orphan.exists())
        self.assertTrue(other.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
