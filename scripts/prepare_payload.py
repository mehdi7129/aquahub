#!/usr/bin/env python3
"""Prepare a pinned offline test kit, outside the source repository.

This build-workstation command downloads authenticated Debian/Docker packages
and reads local Docker images. It does not install/upgrade Docker, mutate the
supplier bundle, or contact a drone. A private supplier bundle is never made a
public release by this command.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

IMAGE_REFERENCES = ["eclipse-mosquitto:2.0", "aqua-hub/dmx-fleet-controller:latest", "aqua-hub/gcs:latest",
                    "aqua-hub/net-setup:latest", "aqua-hub/mavlink-router:latest", "aqua-hub/rtcm-mavlink:latest", "linuxserver/wireguard:latest"]
PORTAINER = "portainer/portainer-ce:2.45.2"
RUNTIME_PACKAGES = ["python3", "python3-gi", "python3-cairo", "python3-gi-cairo", "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1",
                    "ca-certificates", "openssh-client", "pkexec", "polkitd", "wireguard-tools", "kmod",
                    "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"]


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_info(reference):
    data = json.loads(run(["docker", "--context", "default", "image", "inspect", reference]))[0]
    return {"reference": reference, "image_id": data["Id"], "size": data["Size"], "repo_digests": data.get("RepoDigests", [])}


def export_images(images, target):
    fingerprint = hashlib.sha256(json.dumps(images, sort_keys=True).encode()).hexdigest()
    marker = target.parent.parent / "build-cache" / (target.name + ".source")
    if target.is_file() and marker.is_file() and marker.read_text().strip() == fingerprint:
        print(f"Réutilisation de {target.name}", flush=True)
        return
    raw = target.with_suffix(".raw")
    subprocess.run(["docker", "--context", "default", "image", "save", "--output", str(raw), *[item["reference"] for item in images]], check=True)
    partial = target.with_suffix(".partial")
    try:
        with raw.open("rb") as source, partial.open("wb") as output, gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=3, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed, 1024**2)
        for expected in images:
            if image_info(expected["reference"])["image_id"] != expected["image_id"]:
                raise RuntimeError("Une référence d’image a changé pendant l’export.")
        partial.replace(target)
        marker.write_text(fingerprint + "\n")
    finally:
        raw.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def bind_archive_identities(images, archive):
    from aquahub_agent.image_identity import archive_identities, canonical_reference
    mapping = archive_identities(archive)
    archive_hash = digest(archive)
    for image in images:
        proof = mapping.get(canonical_reference(image["reference"]))
        if proof is None or image["image_id"] not in proof["identities"]:
            raise RuntimeError("Une référence d’image ne correspond pas à l’archive exportée.")
        if proof.get("platform") != {"os": "linux", "architecture": "amd64"}:
            raise RuntimeError("Le kit contient une image pour une plateforme inattendue.")
        image["archive_sha256"] = archive_hash
        image["archive_identity"] = proof


def write_checksums(root):
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            entries.append(digest(path) + "  " + path.relative_to(root).as_posix())
    (root / "SHA256SUMS").write_text("\n".join(entries) + "\n")
    return entries


def refresh_identities(root):
    metadata = json.loads((root / "kit.json").read_text())
    bind_archive_identities(metadata["images"], root / "images.tar.gz")
    bind_archive_identities([metadata["portainer"]], root / "portainer.tar.gz")
    (root / "kit.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    entries = write_checksums(root)
    print(json.dumps({"payload": str(root), "identities_refreshed": True, "files": len(entries), "private": True}), flush=True)


def package_version(name):
    # Keep the existing engine/Compose versions; do not silently advance a major
    # dependency on the build machine or on a customer's existing installation.
    if name in {"docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"}:
        return run(["dpkg-query", "-W", "-f=${Version}", name])
    output = run(["apt-cache", "policy", name])
    for line in output.splitlines():
        if line.strip().startswith("Candidate:"):
            value = line.partition(":")[2].strip()
            if value != "(none)":
                return value
    raise RuntimeError(f"Paquet runtime indisponible : {name}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private-kit", action="store_true", required=True)
    parser.add_argument("--refresh-identities", action="store_true", help="Recalculer uniquement les identités et empreintes du kit existant, sans téléchargement")
    options = parser.parse_args(argv)
    os.umask(0o077)
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if options.refresh_identities:
        refresh_identities(root)
        return 0
    if options.bundle is None:
        parser.error("--bundle est requis sauf avec --refresh-identities")
    repository = root / "repository"
    packages = repository / "debs"
    packages.mkdir(parents=True, exist_ok=True)
    cache = root.parent / "build-cache"
    cache.mkdir(exist_ok=True, mode=0o700)
    status = cache / "empty-status"
    status.write_text("")
    targets = [name + "=" + package_version(name) for name in RUNTIME_PACKAGES]
    print("Téléchargement de toutes les dépendances transitives dans le kit…", flush=True)
    log = cache / "apt-download.log"
    with log.open("w") as output:
        subprocess.run(["sudo", "-n", "apt-get", "-y", "--download-only", "--no-install-recommends",
                        "-o", f"Dir::State::status={status}", "-o", f"Dir::Cache::archives={packages}",
                        "-o", "APT::Sandbox::User=root", "install", *targets], stdout=output, stderr=subprocess.STDOUT, check=True)
    subprocess.run(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(packages)], check=True)
    # apt's lock and partial directory are not distribution artefacts.
    for unwanted in (packages / "lock", packages / "partial"):
        if unwanted.is_dir():
            shutil.rmtree(unwanted)
        else:
            unwanted.unlink(missing_ok=True)
    (root / "package-list.txt").write_text("\n".join(targets) + "\n")
    print("Construction du dépôt local signé…", flush=True)
    with (repository / "Packages").open("w") as output:
        subprocess.run(["dpkg-scanpackages", "--multiversion", "debs", "/dev/null"], cwd=repository, check=True, stdout=output)
    with (repository / "Packages").open("rb") as source, (repository / "Packages.gz").open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        shutil.copyfileobj(source, compressed)
    (repository / "Release").unlink(missing_ok=True)
    (repository / "InRelease").unlink(missing_ok=True)
    (repository / "Release.gpg").unlink(missing_ok=True)
    release = run(["apt-ftparchive", "-o", "APT::FTPArchive::Release::Origin=AquaHub", "-o", "APT::FTPArchive::Release::Label=AquaHub offline QA", "release", "."], cwd=repository)
    (repository / "Release").write_text(release + "\n")
    keys = cache / "signing-key"
    keys.mkdir(exist_ok=True, mode=0o700)
    gpg = ["gpg", "--homedir", str(keys), "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", ""]
    if not (keys / "key.ready").exists():
        subprocess.run([*gpg, "--quick-generate-key", "AquaHub local QA (not commercial) <qa@localhost>", "ed25519", "sign", "0"], check=True)
        (keys / "key.ready").touch()
    with (root / "repo-key.gpg").open("wb") as output:
        subprocess.run([*gpg, "--export"], stdout=output, check=True)
    subprocess.run([*gpg, "--clearsign", "--output", str(repository / "InRelease"), str(repository / "Release")], check=True)
    subprocess.run([*gpg, "--detach-sign", "--output", str(repository / "Release.gpg"), str(repository / "Release")], check=True)
    print("Export des images exactes, sans arrêt des conteneurs…", flush=True)
    images = [image_info(reference) for reference in IMAGE_REFERENCES]
    portainer = image_info(PORTAINER)
    export_images(images, root / "images.tar.gz")
    export_images([portainer], root / "portainer.tar.gz")
    bind_archive_identities(images, root / "images.tar.gz")
    bind_archive_identities([portainer], root / "portainer.tar.gz")
    shutil.copyfile(options.bundle, root / "bundle.tar.gz")
    metadata = {"schema": 1, "product": "aquahub-kit", "platform": "debian13/amd64", "private": True,
                "version": "0.1.0", "created_at": datetime.now(timezone.utc).isoformat(),
                "bundle": {"sha256": digest(root / "bundle.tar.gz")}, "images": images, "portainer": portainer,
                "packages": targets, "requirements": {"free_bytes": 10 * 1024**3},
                "qualification": "Local development kit, contains supplier private configurations. Not a redistributable commercial release."}
    (root / "kit.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    entries = write_checksums(root)
    print(json.dumps({"payload": str(root), "packages": len(list(packages.glob('*.deb'))), "files": len(entries), "private": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
