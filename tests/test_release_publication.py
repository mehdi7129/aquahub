"""Public release state transitions on an in-memory GitHub; no network/token."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("aquahub_release_publication", ROOT / "release/publish.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)
TARGET = "a" * 40
NOTES = "Synthetic public pre-release notes."
ASSETS = {"application.deb": b"synthetic-package", "SHA256SUMS": b"synthetic-checksum"}


def asset(name, data):
    return {"name": name, "size": len(data), "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest()}


class FakeGitHub:
    def __init__(self, release=None):
        self.release = copy.deepcopy(release)
        self.ref = None
        self.events = []
        self.corrupt_upload = False

    def request(self, method, path, body=None, *, binary=None, missing=False):
        self.events.append((method, path))
        if method == "GET" and path.startswith("/releases/tags/"):
            # Published-by-tag endpoints need not expose an unfinished draft.
            return copy.deepcopy(self.release) if self.release and not self.release["draft"] else None
        if method == "GET" and path == "/releases?per_page=100":
            return [copy.deepcopy(self.release)] if self.release else []
        if method == "GET" and path.startswith("/git/ref/tags/"):
            return copy.deepcopy(self.ref)
        if method == "POST" and path == "/releases":
            assert self.release is None
            self.release = {**copy.deepcopy(body), "id": 17, "assets": [],
                            "html_url": "https://github.com/mehdi7129/aquahub/releases/tag/v0.2.7"}
            return copy.deepcopy(self.release)
        if method == "POST" and path.startswith("/releases/17/assets?"):
            name = parse_qs(urlsplit(path).query)["name"][0]
            row = asset(name, binary)
            if self.corrupt_upload:
                row["digest"] = "sha256:" + "0" * 64
            self.release["assets"].append(row)
            return copy.deepcopy(row)
        if method == "GET" and path == "/releases/17":
            return copy.deepcopy(self.release)
        if method == "PATCH" and path == "/releases/17":
            self.release.update(body)
            self.ref = {"object": {"type": "commit", "sha": self.release["target_commitish"]}}
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def mutations(self):
        return [e for e in self.events if e[0] != "GET"]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "release/v0.2.7/manifest.json").read_text())

    def release(self, *, draft=False, assets=None):
        return {"id": 17, "tag_name": "v0.2.7", "target_commitish": TARGET,
                "body": NOTES, "draft": draft, "prerelease": True,
                "assets": [asset(n, d) for n, d in (ASSETS if assets is None else assets).items()],
                "html_url": "https://github.com/mehdi7129/aquahub/releases/tag/v0.2.7"}

    def test_manifest_rejects_private_kit_or_unqualified_identity(self):
        self.assertEqual(publication.validate_manifest(self.manifest), self.manifest)
        for change in ({"prerelease": False}, {"version": "../private"}, {"version": "0.02.7"},
                       {"version": "1.0.0"}, {"bytes": True}, {"qualification_run": 0},
                       {"qualification_commit": "main"}, {"sha256": "bad"}, {"payload": "private-kit"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication.validate_manifest({**self.manifest, **change})

    def test_verified_assets_are_uploaded_while_draft_then_published(self):
        api = FakeGitHub()
        result = publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
        self.assertFalse(result["draft"])
        self.assertTrue(result["prerelease"])
        writes = api.mutations()
        self.assertEqual([m for m, _ in writes], ["POST", "POST", "POST", "PATCH"])
        self.assertEqual(writes[-1][1], "/releases/17")

    def test_upload_digest_mismatch_never_publishes_the_draft(self):
        api = FakeGitHub()
        api.corrupt_upload = True
        with self.assertRaises(ValueError):
            publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
        self.assertTrue(api.release["draft"])
        self.assertFalse(any(method == "PATCH" for method, _ in api.events))

    def test_published_matching_release_is_an_idempotent_read(self):
        api = FakeGitHub(self.release())
        api.ref = {"object": {"type": "commit", "sha": TARGET}}
        result = publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
        self.assertFalse(result["draft"])
        self.assertEqual(api.mutations(), [])

    def test_existing_asset_changes_or_extra_assets_are_never_overwritten(self):
        for rows in ([{**asset("application.deb", ASSETS["application.deb"]), "size": 0}],
                     [asset("foreign.txt", b"keep")]):
            existing = self.release(draft=True, assets={})
            existing["assets"] = rows
            api = FakeGitHub(existing)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
            self.assertEqual(api.mutations(), [])

    def test_existing_tag_with_another_target_is_preserved(self):
        api = FakeGitHub()
        api.ref = {"object": {"type": "commit", "sha": "b" * 40}}
        with self.assertRaises(ValueError):
            publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
        self.assertEqual(api.mutations(), [])

    def test_incomplete_matching_draft_only_uploads_missing_assets(self):
        api = FakeGitHub(self.release(draft=True, assets={"application.deb": ASSETS["application.deb"]}))
        result = publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
        self.assertFalse(result["draft"])
        writes = api.mutations()
        self.assertEqual(len(writes), 2)
        self.assertIn("SHA256SUMS", writes[0][1])
        self.assertEqual(writes[1], ("PATCH", "/releases/17"))

    def test_changed_release_metadata_and_partial_published_release_are_refused(self):
        for change in ({"body": "Changed notes"}, {"prerelease": False}, {"assets": []}):
            api = FakeGitHub({**self.release(), **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication.publish(api, self.manifest, TARGET, NOTES, ASSETS)
            self.assertEqual(api.mutations(), [])


if __name__ == "__main__":
    unittest.main()
