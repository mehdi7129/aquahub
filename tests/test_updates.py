"""Synthetic delivery tests. Never contacts GitHub or a Docker daemon."""
import base64
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
import urllib.request
import zipfile

from aquahub_agent.security import ApiError
from aquahub_agent import updates as module
from aquahub_agent.updates import ArchiveReader, GitLabNoRedirect, NoCredentialRedirect, UpdateManager, normalise_repository


def make_tar(path, items, gz=False):
    with tarfile.open(path, "w:gz" if gz else "w") as archive:
        for name, data in items.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class FakeDocker:
    def __init__(self):
        self.references = {service: "aqua-test/" + service + ":latest" for service in module.SERVICES}
        self.ids = {service: "sha256:" + hashlib.sha256(service.encode()).hexdigest() for service in module.SERVICES}
        self.tags = {self.references[service]: image for service, image in self.ids.items()}
        self.active = dict(self.ids)
        self.calls = []
        self.fail_next_up = False

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return self.tags[args[-1]] + "\n"
        if args[:2] == ["image", "save"]:
            Path(args[3]).write_bytes(b"private-snapshot-images")
            return ""
        if args[:2] == ["image", "load"]:
            return "loaded"
        if args[:2] == ["image", "tag"]:
            self.tags[args[3]] = args[2]
            return ""
        if args[:2] == ["container", "inspect"]:
            service = args[-1].removeprefix("container-")
            if "com.docker.compose.config-hash" in args[-2]:
                return hashlib.sha256((service + "config").encode()).hexdigest()
            return self.active[service]
        if args[0] == "compose":
            if "config" in args:
                if "--hash" in args:
                    return "\n".join(service + " " + hashlib.sha256((service + "config").encode()).hexdigest() for service in module.SERVICES)
                return json.dumps({"services": {service: {"image": ref} for service, ref in self.references.items()}})
            if "ps" in args:
                return json.dumps([{"Service": service, "ID": "container-" + service,
                    "State": "running", "Health": "healthy" if service in {"mosquitto", "dmx_fleet_controller"} else ""}
                    for service in module.SERVICES])
            if "up" in args:
                if self.fail_next_up:
                    self.fail_next_up = False
                    raise ApiError("injected_failure", "Simulated compose failure")
                self.active = {service: self.tags[reference] for service, reference in self.references.items()}
                return ""
        raise AssertionError(args)


class FakeApp:
    def __init__(self, root):
        self.config = SimpleNamespace(state_dir=root / "state", bundle_path=root / "bundle")
        self.config.state_dir.mkdir()
        self.config.bundle_path.mkdir()
        self.docker = FakeDocker()
        self.jobs = []

    def submit_job(self, kind, target, payload, work):
        self.jobs.append((kind, target, payload))
        return {"id": "job-1", "status": "succeeded", "result": work()}


class ArchiveSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_regular_tar_gzip_and_zip_hashes(self):
        for suffix in ("tar", "tar.gz", "zip"):
            with self.subTest(suffix=suffix):
                path = self.root / ("valid." + suffix)
                if suffix == "zip":
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("bundle/file.txt", "hello")
                else:
                    make_tar(path, {"bundle/file.txt": b"hello"}, gz=suffix.endswith("gz"))
                data = ArchiveReader().inspect(path)
                self.assertEqual(data["files"]["bundle/file.txt"]["sha256"], hashlib.sha256(b"hello").hexdigest())

    def test_traversal_absolute_windows_and_control_names_rejected(self):
        for name in ("../escape", "/etc/escape", "root/../../escape", "root\\escape", "C:/escape", "root/ab\ncd"):
            with self.subTest(name=name):
                path = self.root / "bad.tar"
                make_tar(path, {name: b"untrusted"})
                with self.assertRaises(ApiError):
                    ArchiveReader().inspect(path)
                self.assertFalse((self.root / "escape").exists())

    def test_symlink_hardlink_fifo_and_sparse_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            path = self.root / "bad.tar"
            with tarfile.open(path, "w") as archive:
                member = tarfile.TarInfo("bundle/link")
                member.type, member.linkname = kind, "/etc/passwd"
                archive.addfile(member)
            with self.assertRaises(ApiError):
                ArchiveReader().inspect(path)

    def test_zip_symlink_rejected(self):
        path = self.root / "bad.zip"
        member = zipfile.ZipInfo("link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, "outside")
        with self.assertRaises(ApiError):
            ArchiveReader().inspect(path)

    def test_duplicate_paths_and_file_directory_conflicts_rejected(self):
        for names in (("a", "./a"), ("a", "a/b"), ("a/b", "a")):
            path = self.root / "duplicate.tar"
            make_tar(path, {name: b"one" for name in names})
            with self.assertRaises(ApiError):
                ArchiveReader().inspect(path)

    def test_archive_size_member_count_expanded_and_ratio_limits(self):
        path = self.root / "limited.tar"
        make_tar(path, {"a": b"x" * 100, "b": b"y" * 100})
        for kwargs in ({"max_archive": 10}, {"max_members": 1}, {"max_expanded": 100}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ApiError):
                ArchiveReader(**kwargs).inspect(path)
        bomb = self.root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("zeros", b"0" * 100000)
        with self.assertRaises(ApiError):
            ArchiveReader(max_ratio=20).inspect(bomb)

    def test_symlink_input_and_corrupt_tar_rejected(self):
        path = self.root / "actual.tar"
        path.write_bytes(b"not an archive")
        linked = self.root / "link.tar"
        linked.symlink_to(path)
        for source in (linked, path):
            with self.assertRaises(ApiError):
                ArchiveReader().inspect(source)

    def test_selected_extraction_never_executes_scripts(self):
        path = self.root / "safe.tar"
        make_tar(path, {"bundle/install.sh": b"touch /tmp/DO_NOT_EXECUTE", "bundle/data": b"ok"})
        output = self.root / "stage"
        output.mkdir()
        ArchiveReader().inspect(path, output, {"bundle/data"})
        self.assertEqual((output / "bundle/data").read_bytes(), b"ok")
        self.assertFalse((output / "bundle/install.sh").exists())

    def test_oversized_pax_extension_rejected_before_body_is_read(self):
        path = self.root / "pax-bomb.tar"
        header = tarfile.TarInfo("PaxHeaders/attack")
        header.type = tarfile.XHDTYPE
        header.size = module.MAX_METADATA * 100
        path.write_bytes(header.tobuf())
        with self.assertRaises(ApiError) as error:
            ArchiveReader().inspect(path)
        self.assertEqual(error.exception.code, "archive_limit")

    def test_sparse_pax_is_rejected_before_unbounded_extent_decoding(self):
        path = self.root / "sparse.tar"
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("huge")
            member.size = 1
            member.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "999999999"}
            archive.addfile(member, io.BytesIO(b"0"))
        with self.assertRaises(ApiError) as error:
            ArchiveReader().inspect(path)
        self.assertEqual(error.exception.code, "unsafe_archive")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = FakeApp(self.root)
        self.compose = b"synthetic recognised compose fixture"
        (self.app.config.bundle_path / "docker-compose.yml").write_bytes(self.compose)
        self.hash_patch = patch.dict(module.KNOWN_FILES, {"docker-compose.yml": hashlib.sha256(self.compose).hexdigest()})
        self.hash_patch.start()
        self.manager = UpdateManager(self.app)
        self.manager._network_state = lambda: {"addresses": ["192.0.2.10/24"], "routes": []}

    def tearDown(self):
        self.hash_patch.stop()
        self.temp.cleanup()

    def delivery(self, mutate=None):
        config = b'{"architecture":"amd64","os":"linux","synthetic":"new-image"}'
        new_id = "sha256:" + hashlib.sha256(config).hexdigest()
        image_tar = self.root / "images.tar"
        make_tar(image_tar, {"manifest.json": json.dumps([{"Config": "new.json", "RepoTags": [], "Layers": ["layer.tar"]}]).encode(),
                             "new.json": config, "layer.tar": b"synthetic layer"})
        image_bytes = image_tar.read_bytes()
        desired = {**self.app.docker.ids, "dmx_fleet_controller": new_id}
        manifest = {"schema": 1, "product": "aqua-hub", "architecture": "amd64", "mode": "image-refresh-v1",
            "version": "test-2.0", "compose_sha256": hashlib.sha256(self.compose).hexdigest(),
            "data_compatibility": "unchanged", "rollback_compatible": True,
            "files": {"images.tar": hashlib.sha256(image_bytes).hexdigest()},
            "image_archives": ["images.tar"], "from_images": self.app.docker.ids,
            "to_images": desired}
        if mutate:
            mutate(manifest)
        path = self.root / "delivery.tar"
        make_tar(path, {"bundle/aquahub-manifest.json": json.dumps(manifest).encode(), "bundle/images.tar": image_bytes})
        return path, desired

    def test_manifest_update_changes_only_images_and_manual_rollback_verified(self):
        config_dir = self.app.config.bundle_path / "config"
        config_dir.mkdir()
        (config_dir / "network.conf").write_bytes(b"Address=192.0.2.5\nPrivateKey=synthetic-secret\n")
        before_config = (config_dir / "network.conf").read_bytes()
        archive, desired = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        self.assertTrue(inspected["can_apply"])
        self.assertNotIn("synthetic-secret", json.dumps(inspected))
        result = self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(self.app.docker.active, desired)
        self.assertEqual((config_dir / "network.conf").read_bytes(), before_config)
        restored = self.manager.rollback({})
        self.assertTrue(restored["result"]["restored"])
        self.assertEqual(self.app.docker.active, self.app.docker.ids)
        with self.assertRaises(ApiError):
            self.manager.rollback({})

    def test_failed_application_restores_and_verifies_previous_images(self):
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        self.app.docker.fail_next_up = True
        with self.assertRaises(ApiError) as error:
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(error.exception.code, "update_failed_restored")
        self.assertEqual(self.app.docker.active, self.app.docker.ids)

    def test_fresh_install_bundle_directory_uses_existing_project_for_update_and_rollback(self):
        self.assertEqual(self.app.config.bundle_path.name, "bundle")
        original_run = self.app.docker.run
        def project_scoped(args, **kwargs):
            if args[0] == "compose" and "ps" in args and args[1:3] != ["-p", "aqua-hub-bundle"]:
                return "[]"  # Compose otherwise observes the absent project "bundle".
            return original_run(args, **kwargs)
        with patch.object(self.app.docker, "run", side_effect=project_scoped):
            archive, _ = self.delivery()
            inspection = self.manager.inspect({"path": str(archive)})
            self.assertTrue(self.manager.apply({"inspection_id": inspection["inspection_id"]})["result"]["verified"])
            self.assertTrue(self.manager.rollback({})["result"]["verified"])
        commands = [args for args in self.app.docker.calls if args[0] == "compose"]
        self.assertTrue(all(args[1:3] == ["-p", "aqua-hub-bundle"] for args in commands))
        for operation in ("config", "ps", "up"):
            self.assertTrue(any(operation in args for args in commands))

    def test_config_changed_after_inspection_refused_before_mutation(self):
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        (self.app.config.bundle_path / ".env").write_text("BROKER_HOSTS=192.0.2.10\n")
        with self.assertRaises(ApiError) as error:
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(error.exception.code, "configuration_changed")
        self.assertFalse(any(args[:2] == ["image", "load"] for args in self.app.docker.calls))

    def test_changed_archive_refused_before_docker(self):
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        self.manager.inspections[inspected["inspection_id"]]["path"].write_bytes(b"tamper")
        with self.assertRaises(ApiError):
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(self.app.docker.calls, [])

    def test_manifest_with_changed_network_writer_is_not_applicable(self):
        archive, _ = self.delivery(lambda manifest: manifest["to_images"].update(net_setup="sha256:" + "a" * 64))
        inspected = self.manager.inspect({"path": str(archive)})
        self.assertFalse(inspected["can_apply"])
        self.assertIn("administre les IP", inspected["message"])
        self.assertEqual(self.app.docker.calls, [])

    def test_incompatible_platform_bad_hash_and_volume_migrations_rejected(self):
        mutations = [lambda m: m.update(architecture="arm64"), lambda m: m["files"].update({"images.tar": "f" * 64}),
                     lambda m: m.update(data_compatibility="migrate"), lambda m: m.update(rollback_compatible=False)]
        for mutate in mutations:
            archive, _ = self.delivery(mutate)
            inspected = self.manager.inspect({"path": str(archive)})
            self.assertFalse(inspected["can_apply"])

    def test_unknown_and_expired_inspection_refused(self):
        with self.assertRaises(ApiError):
            self.manager.apply({"inspection_id": "unknown"})
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        self.manager.inspections[inspected["inspection_id"]]["created"] = 0
        with self.assertRaises(ApiError):
            self.manager.apply({"inspection_id": inspected["inspection_id"]})

    def test_legacy_recognition_uses_script_hashes_never_executes(self):
        fixtures = {name: ("synthetic " + name).encode() for name in module.KNOWN_FILES}
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in fixtures.items()}
        fixtures["aqua-hub-images-amd64.tar"] = b"synthetic image archive"
        path = self.root / "legacy.tar"
        make_tar(path, {"aqua-hub-bundle/" + name: data for name, data in fixtures.items()})
        with patch.dict(module.KNOWN_FILES, hashes):
            inspected = self.manager.inspect({"path": str(path)})
        self.assertTrue(inspected["compatible"])
        self.assertFalse(inspected["can_apply"])
        with self.assertRaises(ApiError):
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(self.app.docker.calls, [])

    def test_image_archive_cannot_inject_unrelated_tag(self):
        path = self.root / "image.tar"
        config = b"{}"
        expected = "sha256:" + hashlib.sha256(config).hexdigest()
        make_tar(path, {"manifest.json": json.dumps([{"Config": "image.json", "RepoTags": ["unrelated/system:latest"], "Layers": []}]).encode(),
                        "image.json": config})
        with self.assertRaises(ApiError):
            self.manager._validate_image_tar(path, {expected}, {"aqua-test/dmx:latest"})

    def test_snapshot_is_private_and_rollback_detects_corruption(self):
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        self.manager.apply({"inspection_id": inspected["inspection_id"]})
        snapshot = self.manager.root / inspected["inspection_id"] / "snapshot"
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((snapshot / "previous-images.tar").stat().st_mode), 0o600)
        (snapshot / "previous-images.tar").write_bytes(b"tampered")
        with self.assertRaises(ApiError) as error:
            self.manager.rollback({})
        self.assertEqual(error.exception.code, "rollback_corrupt")

    def test_shared_image_reference_cannot_request_conflicting_service_images(self):
        self.app.docker.references["gcs_reactor"] = self.app.docker.references["gcs"]
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        with self.assertRaises(ApiError) as error:
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(error.exception.code, "image_alias_conflict")
        self.assertFalse(any(args[:2] == ["image", "load"] for args in self.app.docker.calls))

    def test_config_hash_mismatch_is_rejected_before_loading_images(self):
        archive, _ = self.delivery()
        inspected = self.manager.inspect({"path": str(archive)})
        original_run = self.app.docker.run
        def run(args, **kwargs):
            if args[:2] == ["container", "inspect"] and "com.docker.compose.config-hash" in args[-2]:
                return "f" * 64
            return original_run(args, **kwargs)
        with patch.object(self.app.docker, "run", side_effect=run), self.assertRaises(ApiError) as error:
            self.manager.apply({"inspection_id": inspected["inspection_id"]})
        self.assertEqual(error.exception.code, "configuration_mismatch")
        self.assertFalse(any(args[:2] == ["image", "load"] for args in self.app.docker.calls))

    def test_corrupt_rollback_metadata_is_explicit_and_never_loads_an_image(self):
        (self.manager.root / "last-rollback.json").write_text("not json")
        with self.assertRaises(ApiError) as error:
            self.manager.rollback({})
        self.assertEqual(error.exception.code, "rollback_unavailable")
        self.assertEqual(self.app.docker.calls, [])

    def test_duplicate_manifest_keys_are_rejected(self):
        path = self.root / "duplicate-manifest.tar"
        make_tar(path, {"aquahub-manifest.json": b'{"schema":1,"schema":2}'})
        with self.assertRaises(ApiError) as error:
            self.manager.inspect({"path": str(path)})
        self.assertEqual(error.exception.code, "invalid_manifest")


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = FakeApp(self.root)
        self.manager = UpdateManager(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_repository_normalisation_and_rejected_sources(self):
        self.assertEqual(normalise_repository("https://github.com/Owner/repo.git/"), "https://github.com/Owner/repo")
        for url in ("http://github.com/a/b", "https://evil.example/a/b", "https://github.com/a/b/tree/main", "https://secret@github.com/a/b", "https://github.com/a/b?token=secret"):
            with self.subTest(url=url), self.assertRaises(ApiError):
                normalise_repository(url)

    def test_token_saved_only_in_private_agent_file(self):
        result = self.manager.settings({"repository_url": "https://github.com/test/bundle", "token": "synthetic-not-real-token"})
        self.assertTrue(result["token_configured"])
        self.assertNotIn("synthetic-not-real-token", json.dumps(result))
        self.assertEqual(stat.S_IMODE((self.manager.root / "github.json").stat().st_mode), 0o600)

    def test_no_repository_is_explicitly_unconfigured(self):
        result = self.manager.check({})
        self.assertFalse(result["configured"])
        self.assertEqual(result["releases"], [])

    def test_only_published_compatible_assets_with_digest_downloadable(self):
        self.manager.settings({"repository_url": "https://github.com/test/bundle"})
        fixture = [{"id": 1, "tag_name": "v2", "assets": [
            {"id": 10, "name": "aqua-hub-amd64.tar.gz", "size": 100, "digest": "sha256:" + "a" * 64},
            {"id": 11, "name": "source-amd64.zip", "size": 100, "digest": "sha256:" + "b" * 64},
            {"id": 12, "name": "aqua-hub-amd64.zip", "size": 100}]},
            {"id": 2, "draft": True, "assets": []}, {"id": 3, "prerelease": True, "assets": []}]
        with patch.object(self.manager, "_github", return_value=fixture):
            result = self.manager.check({})
        self.assertEqual(len(result["releases"]), 1)
        self.assertEqual([a["downloadable"] for a in result["releases"][0]["assets"]], [True, False, False])
        self.assertEqual(set(self.manager.releases), {(1, 10)})

    def test_api_pagination_is_bounded(self):
        self.manager.settings({"repository_url": "https://github.com/test/bundle"})
        with patch.object(self.manager, "_github", return_value=[{}] * 20) as get:
            self.manager.check({})
        self.assertEqual(get.call_count, 3)

    def test_credentials_not_forwarded_on_storage_redirect(self):
        handler = NoCredentialRedirect()
        request = urllib.request.Request("https://api.github.com/repos/test/bundle/releases/assets/1")
        request.add_header("Authorization", "Bearer synthetic-token")
        request.add_unredirected_header("Authorization", "Bearer synthetic-token")
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/path")
        self.assertNotIn("Authorization", redirected.headers)
        self.assertNotIn("Authorization", redirected.unredirected_hdrs)
        for target in ("http://release-assets.githubusercontent.com/path", "https://evil.example/path"):
            with self.assertRaises(ApiError):
                handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_bad_download_digest_does_not_offer_application(self):
        self.manager.settings({"repository_url": "https://github.com/test/bundle"})
        self.manager.releases[(1, 2)] = {"id": 2, "size": 3, "sha256": "f" * 64, "repository": "test/bundle", "created": module.time.time()}
        with patch.object(self.manager, "_github", return_value=io.BytesIO(b"bad")):
            with self.assertRaises(ApiError) as error:
                self.manager.download({"release_id": 1, "asset_id": 2})
        self.assertEqual(error.exception.code, "download_integrity")
        self.assertEqual(self.manager.inspections, {})
        self.assertEqual(self.app.docker.calls, [])

    def test_source_saved_by_shared_settings_is_used_without_private_token_file(self):
        class Store:
            def setting_get(self, key, default):
                self.key = key
                return "https://github.com/test/bundle.git"
        self.app.store = Store()
        with patch.object(self.manager, "_github", return_value=[]) as get:
            result = self.manager.check({})
        self.assertEqual(result["repository_url"], "https://github.com/test/bundle")
        self.assertEqual(self.app.store.key, "github_url")
        self.assertIn("/repos/test/bundle/releases?", get.call_args.args[0])

    def test_nested_gitlab_repository_normalises_and_web_or_credential_urls_are_refused(self):
        self.assertEqual(normalise_repository("https://gitlab.com/group/subgroup/project.git/"),
                         "https://gitlab.com/group/subgroup/project")
        for url in ("https://gitlab.com/group/project/-/tree/main", "https://secret@gitlab.com/group/project",
                    "https://gitlab.com/group/project?private_token=secret", "https://gitlab.com/group/%2fproject",
                    "https://other.gitlab.com/group/project", "http://gitlab.com/group/project"):
            with self.subTest(url=url), self.assertRaises(ApiError):
                normalise_repository(url)

    def test_gitlab_lists_links_without_following_unverified_download_targets(self):
        self.manager.settings({"repository_url": "https://gitlab.com/group/sub/project"})
        fixture = [{"tag_name": "v1", "name": "First", "assets": {"links": [
            {"id": 7, "name": "hub-amd64.tar.gz", "url": "http://127.0.0.1/private"},
            {"id": 8, "name": "source-amd64.zip", "url": "https://gitlab.com/source.zip"}]}}]
        with patch.object(self.manager, "_gitlab", return_value=fixture) as get:
            result = self.manager.check({})
        self.assertEqual(get.call_count, 1)
        self.assertIn("group%2Fsub%2Fproject/releases?", get.call_args.args[0])
        self.assertEqual(result["provider"], "gitlab")
        self.assertEqual([a["downloadable"] for a in result["releases"][0]["assets"]], [False, False])
        self.assertNotIn("127.0.0.1", json.dumps(result))
        self.assertFalse(self.manager.releases)

    def test_gitlab_api_uses_private_header_and_never_follows_redirects(self):
        self.manager.settings({"repository_url": "https://gitlab.com/group/project", "token": "synthetic-private-token"})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        self.manager.gitlab_opener = SimpleNamespace(open=unittest.mock.Mock(return_value=response))
        self.assertEqual(self.manager._gitlab("https://gitlab.com/api/v4/projects/group%2Fproject/releases"), [])
        request = self.manager.gitlab_opener.open.call_args.args[0]
        self.assertEqual(request.unredirected_hdrs["Private-token"], "synthetic-private-token")
        self.assertFalse(request.headers.get("Private-token"))
        for target in ("https://gitlab.com/api/v4/projects/1", "https://storage.googleapis.com/file", "http://127.0.0.1/"):
            with self.subTest(target=target), self.assertRaises(ApiError):
                GitLabNoRedirect().redirect_request(request, None, 302, "Found", {}, target)

    def test_gitlab_pagination_and_api_targets_are_bounded(self):
        self.manager.settings({"repository_url": "https://gitlab.com/group/project"})
        with patch.object(self.manager, "_gitlab", return_value=[{}] * 20) as get:
            self.manager.check({})
        self.assertEqual(get.call_count, 3)
        for url in ("https://gitlab.com.evil.test/api/v4/projects/1", "https://gitlab.com:444/api/v4/projects/1",
                    "https://secret@gitlab.com/api/v4/projects/1", "https://gitlab.com/group/project"):
            with self.subTest(url=url), self.assertRaises(ApiError):
                self.manager._gitlab(url)

    def test_switching_provider_never_sends_the_other_providers_token(self):
        for first, second, api in (("github", "gitlab", "https://gitlab.com/api/v4/projects/a%2Fb/releases"),
                                   ("gitlab", "github", "https://api.github.com/repos/a/b/releases")):
            with self.subTest(provider=second):
                self.manager.settings({"repository_url": f"https://{first}.com/a/b", "token": "synthetic-token"})
                changed = self.manager.settings({"repository_url": f"https://{second}.com/a/b"})
                self.assertFalse(changed["token_configured"])
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.return_value = b"[]"
                response.read.return_value = b"[]"
                opener = SimpleNamespace(open=unittest.mock.Mock(return_value=response))
                if second == "gitlab":
                    self.manager.gitlab_opener = opener
                    self.manager._gitlab(api)
                else:
                    self.manager.opener = opener
                    self.manager._github(api)
                request = opener.open.call_args.args[0]
                self.assertFalse(any(k.lower() in ("authorization", "private-token") for k in request.unredirected_hdrs))


if __name__ == "__main__":
    unittest.main()
