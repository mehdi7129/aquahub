"""Byte-derived image identities across classic Docker and containerd stores."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from aquahub_agent import image_identity
from aquahub_agent.image_identity import ImageIdentityError, archive_identities, archive_image_records, canonical_reference
from aquahub_agent.updates import UpdateManager
from aquahub_agent.security import ApiError


MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
REF = "aqua-test/fleet:latest"


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Fixture:
    def __init__(self):
        self.files = {}
        self.config = packed({"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": []}})

    def blob(self, content, media):
        digest = hashlib.sha256(content).hexdigest()
        self.files["blobs/sha256/" + digest] = content
        return {"digest": "sha256:" + digest, "size": len(content), "mediaType": media}

    def image(self, config=None):
        config_descriptor = self.blob(config or self.config, CONFIG)
        layer = self.blob(b"synthetic image layer", LAYER)
        manifest = {"schemaVersion": 2, "mediaType": MANIFEST, "config": config_descriptor, "layers": [layer]}
        descriptor = self.blob(packed(manifest), MANIFEST)
        return descriptor, config_descriptor, layer

    def oci(self, *, index=False, hybrid=True, tags=True, foreign_child=False):
        manifest, config, layer = self.image()
        top = manifest
        if index:
            native = {**manifest, "platform": {"os": "linux", "architecture": "amd64"}}
            children = [native]
            if foreign_child:
                children.append({"digest": "sha256:" + "f" * 64, "size": 100, "mediaType": MANIFEST,
                                 "platform": {"os": "linux", "architecture": "arm64"}})
            top = self.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": children}), INDEX)
        descriptor = dict(top)
        if tags:
            descriptor["annotations"] = {"io.containerd.image.name": "docker.io/" + REF,
                                         "org.opencontainers.image.ref.name": "latest"}
        self.files["index.json"] = packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [descriptor]})
        self.files["oci-layout"] = packed({"imageLayoutVersion": "1.0.0"})
        if hybrid:
            self.files["manifest.json"] = packed([{"Config": "blobs/sha256/" + config["digest"][7:],
                "RepoTags": [REF] if tags else [], "Layers": ["blobs/sha256/" + layer["digest"][7:]]}])
        return manifest["digest"], config["digest"], top["digest"]

    def classic(self, tags=True):
        digest = hashlib.sha256(self.config).hexdigest()
        self.files[digest + ".json"] = self.config
        self.files["layer.tar"] = b"synthetic layer"
        self.files["manifest.json"] = packed([{"Config": digest + ".json", "RepoTags": [REF] if tags else [], "Layers": ["layer.tar"]}])
        return "sha256:" + digest

    def write(self, path):
        with tarfile.open(path, "w") as archive:
            for name, data in self.files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path


class ImageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "images.tar"

    def tearDown(self):
        self.temp.cleanup()

    def test_canonical_references_cover_docker_hub_library_registry_and_digest(self):
        self.assertEqual(canonical_reference("eclipse-mosquitto:2.0"), "docker.io/library/eclipse-mosquitto:2.0")
        self.assertEqual(canonical_reference("aqua-hub/gcs"), "docker.io/aqua-hub/gcs:latest")
        self.assertEqual(canonical_reference("ghcr.io/team/image:v2"), "ghcr.io/team/image:v2")
        self.assertEqual(canonical_reference("index.docker.io/library/alpine:3"), "docker.io/library/alpine:3")
        for value in ("https://docker.io/a/b", "user:secret@registry.io/a", "../image", "Upper/Case:v1", "sha256:" + "a" * 64):
            with self.subTest(value=value), self.assertRaises(ImageIdentityError):
                canonical_reference(value)

    def test_classic_identity_is_exact_config_bytes_not_filename_claim(self):
        fixture = Fixture()
        expected = fixture.classic()
        proof = archive_identities(fixture.write(self.path))["docker.io/" + REF]
        self.assertEqual(proof["identities"], [expected])
        self.assertEqual(proof["config_digest"], expected)
        self.assertEqual(proof["platform"], {"os": "linux", "architecture": "amd64"})
        self.assertEqual(proof["archive_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_oci_hybrid_proves_both_config_and_manifest_store_identities(self):
        fixture = Fixture()
        manifest, config, _ = fixture.oci()
        proof = archive_identities(fixture.write(self.path))["docker.io/" + REF]
        self.assertEqual(set(proof["identities"]), {config, manifest})
        self.assertNotEqual(config, manifest)
        self.assertEqual(proof["manifest_digests"], [manifest])
        self.assertEqual(proof["index_digests"], [])
        for runtime_id in (config, manifest):
            actual = UpdateManager._validate_image_tar(None, self.path, {runtime_id}, {REF})
            self.assertEqual(actual, {runtime_id})

    def test_nested_image_index_digest_is_proven_but_root_catalogue_digest_is_not(self):
        fixture = Fixture()
        manifest, config, index = fixture.oci(index=True, foreign_child=True)
        catalogue = "sha256:" + hashlib.sha256(fixture.files["index.json"]).hexdigest()
        proof = archive_identities(fixture.write(self.path))["docker.io/" + REF]
        self.assertEqual(set(proof["identities"]), {config, manifest, index})
        self.assertEqual(proof["index_digests"], [index])
        self.assertNotIn(catalogue, proof["identities"])

    def test_shared_index_dag_is_expanded_once_without_skipping_descriptor_validation(self):
        fixture = Fixture()
        descriptor, config, _ = fixture.image()
        indexes = []
        for _ in range(7):
            descriptor = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX,
                                             "manifests": [dict(descriptor) for _ in range(64)]}), INDEX)
            indexes.append(descriptor["digest"])
        descriptor["annotations"] = {"io.containerd.image.name": REF}
        fixture.files["index.json"] = packed({"schemaVersion": 2, "manifests": [descriptor]})
        fixture.write(self.path)
        # Without memoization this small archive expands 64**7 branches.
        with patch.object(image_identity, "_json", wraps=image_identity._json) as parse:
            proof = archive_identities(self.path)["docker.io/" + REF]
        self.assertEqual(proof["config_digest"], config["digest"])
        self.assertEqual(set(proof["index_digests"]), set(indexes))
        self.assertLessEqual(parse.call_count, 10)

        root = json.loads(fixture.files["index.json"])
        invalid = dict(root["manifests"][0])
        invalid["size"] += 1
        root["manifests"].append(invalid)  # Same cached digest, false descriptor.
        fixture.files["index.json"] = packed(root)
        fixture.write(self.path)
        with self.assertRaisesRegex(ImageIdentityError, "taille"):
            archive_identities(self.path)

    def test_shared_dag_still_obeys_depth_and_descriptor_visit_limits(self):
        fixture = Fixture()
        image, _, _ = fixture.image()
        shallow = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [image]}), INDEX)
        deep = shallow
        for _ in range(8):
            deep = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [deep]}), INDEX)
        fixture.files["index.json"] = packed({"schemaVersion": 2, "manifests": [shallow, deep]})
        fixture.write(self.path)
        with self.assertRaisesRegex(ImageIdentityError, "profonde"):
            archive_identities(self.path)
        index = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [image] * 32}), INDEX)
        fixture.files["index.json"] = packed({"schemaVersion": 2, "manifests": [index]})
        fixture.write(self.path)
        with patch.object(image_identity, "_MAX_DESCRIPTORS", 16):
            with self.assertRaisesRegex(ImageIdentityError, "Parcours"):
                archive_identities(self.path)

    def test_oci_only_and_untagged_save_are_supported_without_inventing_a_reference(self):
        fixture = Fixture()
        manifest, config, _ = fixture.oci(hybrid=False, tags=False)
        fixture.write(self.path)
        self.assertEqual(archive_identities(self.path), {})
        records = archive_image_records(self.path)
        self.assertEqual(records[0]["references"], [])
        self.assertEqual(set(records[0]["identities"]), {config, manifest})

    def test_corrupt_blob_or_incorrect_descriptor_size_is_rejected(self):
        for corrupt_size in (False, True):
            fixture = Fixture()
            manifest, _, _ = fixture.oci()
            if corrupt_size:
                catalogue = json.loads(fixture.files["index.json"])
                catalogue["manifests"][0]["size"] += 1
                fixture.files["index.json"] = packed(catalogue)
            else:
                fixture.files["blobs/sha256/" + manifest[7:]] += b" "
            fixture.write(self.path)
            with self.assertRaises(ImageIdentityError):
                archive_identities(self.path)

    def test_unproven_declared_identity_is_never_accepted(self):
        fixture = Fixture()
        fixture.oci()
        catalogue = json.loads(fixture.files["index.json"])
        catalogue["identities"] = ["sha256:" + "f" * 64]
        fixture.files["index.json"] = packed(catalogue)
        fixture.write(self.path)
        proof = archive_identities(self.path)["docker.io/" + REF]
        self.assertNotIn("sha256:" + "f" * 64, proof["identities"])
        with self.assertRaises(ApiError):
            UpdateManager._validate_image_tar(None, self.path, {"sha256:" + "f" * 64}, {REF})

    def test_missing_native_blob_ambiguous_native_index_and_wrong_platform_rejected(self):
        fixture = Fixture()
        _, config, _ = fixture.oci()
        fixture.files.pop("blobs/sha256/" + config[7:])
        fixture.write(self.path)
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)
        fixture = Fixture()
        first, _, _ = fixture.image()
        second, _, _ = fixture.image(packed({"os": "linux", "architecture": "amd64", "different": True}))
        index = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [first, second]}), INDEX)
        fixture.files["index.json"] = packed({"schemaVersion": 2, "manifests": [index]})
        fixture.write(self.path)
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)
        fixture = Fixture()
        fixture.config = packed({"os": "linux", "architecture": "arm64"})
        fixture.classic()
        fixture.write(self.path)
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)

    def test_nested_unrelated_tag_is_visible_and_update_refuses_it(self):
        fixture = Fixture()
        manifest, _, _ = fixture.image()
        manifest["annotations"] = {"io.containerd.image.name": "unrelated/critical:latest"}
        index = fixture.blob(packed({"schemaVersion": 2, "mediaType": INDEX, "manifests": [manifest]}), INDEX)
        index["annotations"] = {"io.containerd.image.name": REF}
        fixture.files["index.json"] = packed({"schemaVersion": 2, "manifests": [index]})
        fixture.write(self.path)
        proof = archive_image_records(self.path)[0]
        self.assertIn("docker.io/unrelated/critical:latest", proof["references"])
        with self.assertRaises(ApiError):
            UpdateManager._validate_image_tar(None, self.path, set(proof["identities"]), {REF})

    def test_foreign_root_entry_and_legacy_hidden_tags_are_rejected(self):
        fixture = Fixture()
        fixture.oci()
        root = json.loads(fixture.files["index.json"])
        root["manifests"].append({"digest": "sha256:" + "f" * 64, "mediaType": MANIFEST, "size": 10,
                                 "platform": {"os": "linux", "architecture": "arm64"},
                                 "annotations": {"io.containerd.image.name": "unrelated/image:v1"}})
        fixture.files["index.json"] = packed(root)
        fixture.write(self.path)
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)
        fixture = Fixture()
        fixture.classic()
        fixture.files["repositories"] = packed({"unrelated/image": {"latest": "legacy-layer"}})
        fixture.write(self.path)
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)

    def test_links_and_oversized_extension_headers_are_rejected_before_allocation(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with tarfile.open(self.path, "w") as archive:
                member = tarfile.TarInfo("index.json")
                member.type, member.linkname = kind, "/etc/passwd"
                archive.addfile(member)
            with self.assertRaises(ImageIdentityError):
                archive_identities(self.path)
        member = tarfile.TarInfo("PaxHeaders")
        member.type, member.size = tarfile.XHDTYPE, 100 * 1024**2
        self.path.write_bytes(member.tobuf())
        with self.assertRaises(ImageIdentityError):
            archive_identities(self.path)


if __name__ == "__main__":
    unittest.main()
