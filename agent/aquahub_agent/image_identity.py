"""Derive Docker config/OCI manifest/index identities from verified archive bytes.

The root OCI layout index is a catalogue, not an image identity. Only digest-
addressed image manifests/indexes reachable through its descriptors qualify.
No declared list of alternative IDs is trusted. This module is stdlib-only so
the offline installer and the maintenance agent use the same proof.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile


class ImageIdentityError(ValueError):
    pass


_HEX = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
_INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
_MAX_META = 2 * 1024**2
_MAX_META_TOTAL = 32 * 1024**2
_MAX_BYTES = 32 * 1024**3
_MAX_MEMBERS = 20000
_MAX_DESCRIPTORS = 8192


def canonical_reference(reference):
    if not isinstance(reference, str) or not 0 < len(reference) <= 255 or any(c.isspace() for c in reference) or "://" in reference:
        raise ImageIdentityError("Référence d’image invalide.")
    if _DIGEST.fullmatch(reference):
        raise ImageIdentityError("Un digest seul est une identité, pas une référence de dépôt.")
    name, separator, digest = reference.partition("@")
    if separator and not _DIGEST.fullmatch(digest):
        raise ImageIdentityError("Digest de référence non pris en charge.")
    slash = name.rfind("/")
    colon = name.rfind(":")
    tag = ""
    if colon > slash:
        name, tag = name[:colon], name[colon + 1:]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ImageIdentityError("Étiquette d’image invalide.")
    parts = name.split("/")
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first.lower(), parts[1:]
    else:
        registry, repository = "docker.io", parts
    if registry == "index.docker.io":
        registry = "docker.io"
    hostname, port_separator, port = registry.partition(":")
    valid_hostname = all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in hostname.split("."))
    if (not valid_hostname or not repository or
            (port_separator and (not port.isdigit() or not 1 <= int(port) <= 65535))):
        raise ImageIdentityError("Registre d’image invalide.")
    if registry == "docker.io" and len(repository) == 1:
        repository.insert(0, "library")
    if any(not re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*", part) for part in repository):
        raise ImageIdentityError("Nom de dépôt d’image invalide.")
    result = registry + "/" + "/".join(repository)
    if tag:
        result += ":" + tag
    elif not separator:
        result += ":latest"
    return result + ("@" + digest if separator else "")


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ImageIdentityError("Métadonnées JSON ambiguës (clé répétée).")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ImageIdentityError("Métadonnées d’image JSON invalides.") from exc


class _TarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ImageIdentityError("Fichier sparse interdit dans une archive d’images.")
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            total = getattr(archive, "_identity_metadata", 0) + self.size
            count = getattr(archive, "_identity_headers", 0) + 1
            if self.size < 0 or self.size > _MAX_META or total > _MAX_META * 4 or count > 128:
                raise ImageIdentityError("En-têtes d’archive excessivement volumineux.")
            archive._identity_metadata, archive._identity_headers = total, count
        return super()._proc_member(archive)

    def _proc_gnusparse_10(self, *args, **kwargs):
        raise ImageIdentityError("Fichier sparse interdit dans une archive d’images.")
    _proc_gnusparse_01 = _proc_gnusparse_10
    _proc_gnusparse_00 = _proc_gnusparse_10


def _path(name):
    value = PurePosixPath(name)
    if (not name or value.is_absolute() or ".." in value.parts or "\\" in name or
            len(name) > 4096 or len(value.parts) > 64 or any(ord(c) < 32 for c in name)):
        raise ImageIdentityError("Chemin interdit dans l’archive d’images.")
    return str(value)


def _read_archive(path):
    path = Path(path)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= _MAX_BYTES:
        raise ImageIdentityError("Archive d’images absente, liée ou trop volumineuse.")
    records, metadata, total, kept, ancestors = {}, {}, 0, 0, set()
    try:
        with tarfile.open(path, "r|*", tarinfo=_TarInfo) as archive:
            for member in archive:
                name = _path(member.name)
                if name in records or len(records) >= _MAX_MEMBERS or not (member.isfile() or member.isdir()) or member.sparse:
                    raise ImageIdentityError("Archive d’images ambiguë, liée ou excessive.")
                if any(str(parent) in records and not records[str(parent)]["directory"] for parent in PurePosixPath(name).parents):
                    raise ImageIdentityError("Conflit fichier/répertoire dans l’archive d’images.")
                if member.isfile() and name in ancestors:
                    raise ImageIdentityError("Conflit fichier/répertoire dans l’archive d’images.")
                ancestors.update(str(parent) for parent in PurePosixPath(name).parents)
                total += member.size
                if member.size < 0 or total > _MAX_BYTES or total > initial.st_size * 200:
                    raise ImageIdentityError("Taille décompressée de l’archive excessive.")
                record = {"size": member.size, "directory": member.isdir()}
                records[name] = record
                if member.isdir():
                    continue
                digest, captured, actual = hashlib.sha256(), bytearray(), 0
                candidate = member.size <= _MAX_META and (name in ("index.json", "manifest.json", "oci-layout", "repositories") or name.endswith(".json") or _BLOB.fullmatch(name))
                with archive.extractfile(member) as stream:
                    while True:
                        block = stream.read(min(1024**2, member.size - actual + 1))
                        if not block:
                            break
                        actual += len(block)
                        if actual > member.size:
                            raise ImageIdentityError("Taille réelle du fichier d’image incorrecte.")
                        digest.update(block)
                        if candidate:
                            if not captured and block.lstrip()[:1] not in (b"{", b"["):
                                candidate = False
                            else:
                                captured.extend(block)
                if actual != member.size:
                    raise ImageIdentityError("Archive d’images tronquée.")
                record["digest"] = "sha256:" + digest.hexdigest()
                blob = _BLOB.fullmatch(name)
                if blob and blob.group(1) != digest.hexdigest():
                    raise ImageIdentityError("Le SHA-256 d’un blob ne correspond pas à son nom.")
                if captured:
                    kept += len(captured)
                    if kept > _MAX_META_TOTAL:
                        raise ImageIdentityError("Trop de métadonnées d’images en mémoire.")
                    metadata[name] = bytes(captured)
    except ImageIdentityError:
        raise
    except (OSError, tarfile.TarError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ImageIdentityError("Archive d’images illisible ou invalide.") from exc
    after = path.stat()
    if (initial.st_ino, initial.st_size, initial.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ImageIdentityError("L’archive d’images a changé pendant sa vérification.")
    archive_hash = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            archive_hash.update(block)
    final = path.stat()
    if (initial.st_ino, initial.st_size, initial.st_mtime_ns) != (final.st_ino, final.st_size, final.st_mtime_ns):
        raise ImageIdentityError("L’archive d’images a changé pendant son calcul d’empreinte.")
    return records, metadata, archive_hash.hexdigest()


def archive_image_records(path):
    """Return native linux/amd64 image groups, including exports by ID with no tag."""
    files, metadata, archive_sha256 = _read_archive(path)
    groups, legacy, oci_layers = {}, {}, {}
    documents, walked = {}, {}
    descriptor_visits = 0

    def document(name):
        if name not in documents:
            documents[name] = _json(metadata[name])
        return documents[name]

    def config_group(name):
        if name not in metadata or name not in files:
            raise ImageIdentityError("Configuration d’image absente ou trop volumineuse.")
        config = document(name)
        if not isinstance(config, dict) or config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ImageIdentityError("Seules les configurations Linux amd64 explicites sont prises en charge.")
        digest = files[name]["digest"]
        match = re.fullmatch(r"([0-9a-f]{64})\.json", name)
        if match and digest != "sha256:" + match.group(1):
            raise ImageIdentityError("Le nom de configuration ne correspond pas à son SHA-256.")
        return groups.setdefault(digest, {"config_digest": digest, "manifest_digests": set(), "index_digests": set(),
            "references": set(), "platform": {"os": "linux", "architecture": "amd64"}, "archive_sha256": archive_sha256})

    if "manifest.json" in metadata:
        manifest = document("manifest.json")
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 64:
            raise ImageIdentityError("Manifeste Docker-save absent ou excessif.")
        for item in manifest:
            if not isinstance(item, dict) or not isinstance(item.get("Config"), str):
                raise ImageIdentityError("Entrée Docker-save invalide.")
            group = config_group(_path(item["Config"]))
            layers, tags = item.get("Layers"), item.get("RepoTags") or []
            if not isinstance(layers, list) or len(layers) > 1024 or not isinstance(tags, list):
                raise ImageIdentityError("Couches ou étiquettes Docker-save invalides.")
            layer_digests = []
            for name in layers:
                if not isinstance(name, str) or _path(name) not in files or files[name]["directory"]:
                    raise ImageIdentityError("Couche Docker-save absente.")
                layer_digests.append(files[name]["digest"])
            previous = legacy.setdefault(group["config_digest"], tuple(layer_digests))
            if previous != tuple(layer_digests):
                raise ImageIdentityError("Une configuration Docker-save désigne des couches contradictoires.")
            for tag in tags:
                group["references"].add(canonical_reference(tag))

    def descriptor_blob(descriptor):
        if not isinstance(descriptor, dict) or not _DIGEST.fullmatch(str(descriptor.get("digest", ""))):
            raise ImageIdentityError("Descripteur OCI invalide.")
        name = "blobs/sha256/" + descriptor["digest"].split(":", 1)[1]
        item = files.get(name)
        if (not item or item["directory"] or item.get("digest") != descriptor["digest"] or
                type(descriptor.get("size")) is not int or item["size"] != descriptor["size"]):
            raise ImageIdentityError("Blob OCI absent ou différent du digest/de la taille annoncés.")
        return name

    def descriptor_reference(descriptor):
        annotations = descriptor.get("annotations") or {}
        if not isinstance(annotations, dict):
            raise ImageIdentityError("Annotations OCI invalides.")
        reference = annotations.get("io.containerd.image.name")
        if not reference:
            reference = annotations.get("org.opencontainers.image.ref.name")
            if reference and isinstance(reference, str) and "/" not in reference and ":" not in reference:
                raise ImageIdentityError("Référence OCI réduite à une étiquette : dépôt non vérifiable.")
        if not reference:
            return None
        if not isinstance(reference, str):
            raise ImageIdentityError("Référence OCI invalide.")
        if _DIGEST.fullmatch(reference):
            if reference != descriptor["digest"]:
                raise ImageIdentityError("L’identité OCI annotée ne correspond pas au descripteur.")
            return None
        return canonical_reference(reference)

    def walk(descriptor, depth=0, parents=()):
        nonlocal descriptor_visits
        descriptor_visits += 1
        if descriptor_visits > _MAX_DESCRIPTORS:
            raise ImageIdentityError("Parcours OCI excessivement volumineux.")
        if depth > 8:
            raise ImageIdentityError("Arborescence OCI trop profonde.")
        platform = descriptor.get("platform") if isinstance(descriptor, dict) else None
        if isinstance(platform, dict) and (platform.get("os"), platform.get("architecture")) != ("linux", "amd64"):
            return set(), 0
        name = descriptor_blob(descriptor)
        digest, media = descriptor["digest"], descriptor.get("mediaType")
        if digest in parents or name not in metadata:
            raise ImageIdentityError("Cycle OCI ou métadonnées d’image indisponibles.")
        content = document(name)
        if not isinstance(content, dict) or content.get("schemaVersion") != 2:
            raise ImageIdentityError("Version de manifeste OCI invalide.")
        if not isinstance(media, str) or content.get("mediaType", media) != media:
            raise ImageIdentityError("Le type du blob OCI ne correspond pas au descripteur.")
        reference = descriptor_reference(descriptor)
        key = digest, media
        # Reused OCI indexes form a DAG. Validate every incoming descriptor,
        # including its size/type/annotations, but expand each verified blob
        # once. Retain subtree depth so memoization cannot evade depth limits.
        if key in walked:
            configs, height = walked[key]
            if depth + height > 8:
                raise ImageIdentityError("Arborescence OCI trop profonde.")
            if reference:
                for config in configs:
                    groups[config]["references"].add(reference)
            return set(configs), height
        if media in _MANIFEST_TYPES:
            config_name = descriptor_blob(content.get("config"))
            group = config_group(config_name)
            layers = content.get("layers")
            if not isinstance(layers, list) or len(layers) > 1024:
                raise ImageIdentityError("Liste des couches OCI invalide.")
            layer_digests = []
            for layer in layers:
                descriptor_blob(layer)
                layer_digests.append(layer["digest"])
            oci_layers.setdefault(group["config_digest"], []).append(tuple(layer_digests))
            group["manifest_digests"].add(digest)
            if reference:
                group["references"].add(reference)
            configs = frozenset({group["config_digest"]})
            walked[key] = configs, 0
            return set(configs), 0
        if media in _INDEX_TYPES:
            children = content.get("manifests")
            if not isinstance(children, list) or not 1 <= len(children) <= 128:
                raise ImageIdentityError("Index OCI absent ou excessif.")
            configs, height = set(), 0
            for child in children:
                found, child_height = walk(child, depth + 1, (*parents, digest))
                configs |= found
                if found:
                    height = max(height, child_height + 1)
            if len(configs) != 1:
                raise ImageIdentityError("Index OCI sans image native unique ; sélection de plateforme ambiguë.")
            for config in configs:
                groups[config]["index_digests"].add(digest)
                if reference:
                    groups[config]["references"].add(reference)
            walked[key] = frozenset(configs), height
            return configs, height
        raise ImageIdentityError("Type de manifeste OCI non pris en charge.")

    if "index.json" in metadata:
        root = document("index.json")
        if not isinstance(root, dict) or root.get("schemaVersion") != 2 or not isinstance(root.get("manifests"), list) or not 1 <= len(root["manifests"]) <= 64:
            raise ImageIdentityError("Catalogue OCI invalide.")
        for descriptor in root["manifests"]:
            configs, _ = walk(descriptor)
            if not configs:
                raise ImageIdentityError("Une entrée du catalogue ne contient aucune image Linux amd64 vérifiable.")
    elif "oci-layout" in metadata:
        raise ImageIdentityError("Format OCI annoncé sans catalogue index.json.")
    if not groups:
        raise ImageIdentityError("Aucune image native vérifiable dans l’archive.")
    if "repositories" in files:
        repositories = document("repositories") if "repositories" in metadata else _json(b"")
        if not isinstance(repositories, dict):
            raise ImageIdentityError("Catalogue historique de références invalide.")
        known_references = {reference for group in groups.values() for reference in group["references"]}
        for repository, tags in repositories.items():
            if not isinstance(tags, dict):
                raise ImageIdentityError("Étiquettes historiques invalides.")
            if any(canonical_reference(repository + ":" + tag) not in known_references for tag in tags):
                raise ImageIdentityError("Le catalogue historique chargerait des étiquettes non déclarées.")
    for config, layer_lists in oci_layers.items():
        if config in legacy and legacy[config] not in layer_lists:
            raise ImageIdentityError("Les couches Docker-save et OCI d’une même configuration sont contradictoires.")
    reference_owners = {}
    result = []
    for config, group in groups.items():
        for reference in group["references"]:
            if reference in reference_owners and reference_owners[reference] != config:
                raise ImageIdentityError("Une référence désigne plusieurs configurations d’image.")
            reference_owners[reference] = config
        result.append({**group, "references": sorted(group["references"]),
            "manifest_digests": sorted(group["manifest_digests"]), "index_digests": sorted(group["index_digests"]),
            "identities": sorted({config, *group["manifest_digests"], *group["index_digests"]})})
    return sorted(result, key=lambda item: item["config_digest"])


def archive_identities(path):
    """Map canonical tagged references to identities authenticated by archive bytes."""
    result = {}
    for group in archive_image_records(path):
        proof = {key: value for key, value in group.items() if key != "references"}
        for reference in group["references"]:
            result[reference] = proof
    return result
