"""Publish a qualified public application package, never a supplier kit.

Runs in GitHub Actions with its short-lived token. Existing assets are checked,
never overwritten. A release stays draft until every expected asset is verified.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "mehdi7129/aquahub"
API = "https://api.github.com/repos/" + REPOSITORY
UPLOAD = "https://uploads.github.com/repos/" + REPOSITORY
VERSION = re.compile(r"0\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")
RUNTIME = ("agent", "desktop", "installer", "frontend/src", "frontend/package.json",
           "frontend/package-lock.json", "frontend/build.mjs", "LICENSE")


def validate_manifest(value):
    fields = {"schema", "version", "bytes", "sha256", "git_blob", "qualification_commit",
              "qualification_run", "prerelease"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int
            or value["schema"] != 1 or not isinstance(value["version"], str)
            or not VERSION.fullmatch(value["version"]) or value["prerelease"] is not True
            or type(value["bytes"]) is not int or not 0 < value["bytes"] <= 8 * 1024**2
            or type(value["qualification_run"]) is not int or value["qualification_run"] <= 0):
        raise ValueError("Invalid public pre-release manifest")
    for key, pattern in (("sha256", DIGEST), ("git_blob", SHA), ("qualification_commit", SHA)):
        if not isinstance(value[key], str) or not pattern.fullmatch(value[key]):
            raise ValueError("Invalid artifact identity")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("Authenticated redirects are forbidden")


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, *, binary=None, missing=False):
        if not path.startswith("/") or ".." in path or path.startswith("//"):
            raise ValueError("Unexpected GitHub API path")
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/vnd.github+json", "User-Agent": "AquaHub-publication",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if binary is not None:
            data = binary
            headers["Content-Type"] = "application/octet-stream"
            url = UPLOAD + path
        else:
            data = json.dumps(body).encode() if body is not None else None
            headers["Content-Type"] = "application/json"
            url = API + path
        try:
            with self.opener.open(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=30) as response:
                raw = response.read(16 * 1024**2 + 1)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"GitHub publication refused: HTTP {error.code}") from None
        if len(raw) > 16 * 1024**2:
            raise ValueError("GitHub metadata too large")
        return json.loads(raw)


def verify_asset(asset, name, data):
    if (not isinstance(asset, dict) or asset.get("name") != name or asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int or asset["size"] != len(data)
            or asset.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest()):
        raise ValueError("Release asset differs from the qualified bytes: " + name)


def find_release(api, tag):
    release = api.request("GET", "/releases/tags/" + tag, missing=True)
    if release is not None:
        return release
    rows = api.request("GET", "/releases?per_page=100")
    if not isinstance(rows, list) or len(rows) >= 100:
        raise ValueError("Cannot safely enumerate existing drafts")
    matching = [r for r in rows if r.get("tag_name") == tag]
    if len(matching) > 1:
        raise ValueError("Multiple existing drafts for this tag")
    return matching[0] if matching else None


def publish(api, manifest, target, notes, assets):
    """Create/complete one matching draft; retries never replace public assets."""
    tag = "v" + manifest["version"]
    release = find_release(api, tag)
    tag_ref = api.request("GET", "/git/ref/tags/" + tag, missing=True)
    if tag_ref and (tag_ref.get("object", {}).get("type") != "commit"
                    or tag_ref["object"].get("sha") != target):
        raise ValueError("The release tag already targets another commit")
    if release is None:
        release = api.request("POST", "/releases", {"tag_name": tag, "target_commitish": target,
            "name": "AquaHub " + manifest["version"] + " — préversion publique",
            "body": notes, "draft": True, "prerelease": True, "make_latest": "false"})
    if (type(release.get("id")) is not int or release["id"] <= 0
            or release.get("tag_name") != tag or release.get("target_commitish") != target
            or release.get("prerelease") is not True or type(release.get("draft")) is not bool
            or release.get("body") != notes):
        raise ValueError("Existing release metadata differs; it will not be changed")
    identifier = release["id"]
    rows = release.get("assets")
    if not isinstance(rows, list) or len({a.get("name") for a in rows}) != len(rows):
        raise ValueError("Ambiguous release assets")
    existing = {a["name"]: a for a in rows}
    if set(existing) - set(assets):
        raise ValueError("Unexpected release asset; refusing to alter it")
    for name, row in existing.items():
        verify_asset(row, name, assets[name])
    if not release["draft"]:
        if set(existing) != set(assets):
            raise ValueError("Published release is incomplete; refusing to modify it")
        return release
    for name, data in assets.items():
        if name not in existing:
            row = api.request("POST", f"/releases/{identifier}/assets?name=" + urllib.parse.quote(name, safe=""), binary=data)
            verify_asset(row, name, data)
    latest = api.request("GET", f"/releases/{identifier}")
    if (latest.get("id") != identifier or latest.get("tag_name") != tag
            or latest.get("target_commitish") != target or latest.get("body") != notes
            or latest.get("draft") is not True or latest.get("prerelease") is not True):
        raise ValueError("Draft metadata changed during preparation")
    rows = latest.get("assets", [])
    if len(rows) != len(assets) or {a.get("name") for a in rows} != set(assets):
        raise ValueError("Draft asset set is incomplete")
    for row in rows:
        verify_asset(row, row["name"], assets[row["name"]])
    result = api.request("PATCH", f"/releases/{identifier}", {"draft": False, "prerelease": True, "make_latest": "false"})
    if (result.get("draft") is not False or result.get("prerelease") is not True
            or result.get("target_commitish") != target or result.get("tag_name") != tag
            or result.get("body") != notes):
        raise ValueError("Publication is not confirmed")
    return result


def validate_package(data, manifest):
    if len(data) != manifest["bytes"] or hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("Artifact differs from the qualified package")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "package.deb"
        path.write_bytes(data)
        expected = {"Package": "aquahub-setup", "Version": manifest["version"],
                    "Architecture": "amd64", "X-AquaHub-Update": "application-only"}
        for key, value in expected.items():
            actual = subprocess.check_output(["dpkg-deb", "-f", str(path), key], text=True, timeout=10).strip()
            if actual != value:
                raise ValueError("Unexpected Debian field: " + key)
        raw = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(path)], timeout=15)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if member.isdir() and name in {"", "."}:
                continue
            if (not name.startswith("usr/") and name != "usr") or ".." in Path(name).parts or "offline" in Path(name).parts:
                raise ValueError("Supplier or unexpected system payload")
            if member.isdir():
                continue
            if not member.isfile() or member.size > 4 * 1024**2:
                raise ValueError("Unexpected package member")
            content = archive.extractfile(member).read()
            if (re.search(rb"/(?:home|media)/[A-Za-z0-9_.-]+/", content)
                    or re.search(rb"(?:glpat-|github_pat_|ghp_)[A-Za-z0-9_]{12,}", content)
                    or re.search(rb"(?m)^\s*(?:PrivateKey|PresharedKey)\s*=\s*[A-Za-z0-9+/]{43}=", content)
                    or b"-----BEGIN OPENSSH PRIVATE KEY-----" in content
                    or b"-----BEGIN RSA PRIVATE KEY-----" in content):
                raise ValueError("Private content must not be published")


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Publication is restricted to the source repository main branch")
    version = os.environ.get("RELEASE_VERSION", "")
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid requested pre-release version")
    target = os.environ["GITHUB_SHA"]
    if not SHA.fullmatch(target) or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != target:
        raise ValueError("Checkout does not match the publication commit")
    folder = Path("release") / ("v" + version)
    manifest = validate_manifest(json.loads((folder / "manifest.json").read_text()))
    if manifest["version"] != version:
        raise ValueError("Manifest version differs")
    subprocess.run(["git", "diff", "--exit-code", manifest["qualification_commit"], target, "--", *RUNTIME], check=True)
    api = GitHub(os.environ["GITHUB_TOKEN"])
    qualified = api.request("GET", "/actions/runs/" + str(manifest["qualification_run"]))
    if qualified.get("head_sha") != manifest["qualification_commit"] or qualified.get("conclusion") != "success":
        raise ValueError("Qualified source CI is not confirmed")
    deadline = time.monotonic() + 600
    while True:
        runs = api.request("GET", "/actions/runs?head_sha=" + target + "&event=push&per_page=20")["workflow_runs"]
        checks = [r for r in runs if r.get("path") == ".github/workflows/ci.yml" and r.get("name") == "Checks"]
        if checks and checks[0].get("status") == "completed":
            if checks[0].get("conclusion") != "success":
                raise ValueError("Publication commit CI failed")
            break
        if time.monotonic() > deadline:
            raise ValueError("Publication commit CI did not complete")
        time.sleep(5)
    blob = api.request("GET", "/git/blobs/" + manifest["git_blob"])
    if blob.get("sha") != manifest["git_blob"] or blob.get("encoding") != "base64" or blob.get("size") != manifest["bytes"]:
        raise ValueError("Qualified Git object differs")
    data = base64.b64decode("".join(blob["content"].split()), validate=True)
    validate_package(data, manifest)
    notes = (folder / "notes.md").read_text()
    installation = (folder / "INSTALLATION.txt").read_bytes()
    provenance = {**manifest, "repository": REPOSITORY, "publication_commit": target,
                  "publisher_signature": False, "format": "application-only", "platform": "Debian 13 amd64"}
    # A replay from a later documentation commit keeps the original release tag.
    previous = find_release(api, "v" + version)
    if previous:
        original = previous.get("target_commitish", "")
        if not SHA.fullmatch(original):
            raise ValueError("Existing release has an unexpected target")
        subprocess.run(["git", "diff", "--exit-code", manifest["qualification_commit"], original, "--", *RUNTIME], check=True)
        target = original
        provenance["publication_commit"] = target
    assets = {f"aquahub-application_{version}_amd64.deb": data, "INSTALLATION.txt": installation,
              "provenance.json": (json.dumps(provenance, indent=2, ensure_ascii=False) + "\n").encode()}
    assets["SHA256SUMS"] = "".join(hashlib.sha256(content).hexdigest() + "  " + name + "\n"
                                  for name, content in sorted(assets.items())).encode()
    release = publish(api, manifest, target, notes, assets)
    print("Public pre-release verified: " + release["html_url"])


if __name__ == "__main__":
    main()
