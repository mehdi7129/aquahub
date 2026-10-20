"""Bounded, non-executable supplier imports and transactional image refreshes.

No archive script is executed.  The legacy supplier format is recognised for
inspection; an applicable delivery needs the explicit ``image-refresh-v1``
manifest.  Network-writing containers and all configuration files stay fixed.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from .security import ApiError
from .docker import PROJECT
from .network import stable_network
from .image_identity import ImageIdentityError, archive_image_records, canonical_reference

KNOWN_FILES = {
    "docker-compose.yml": "c4cb8f71f7772a29e12fa24abf618cc99e6c6e99bd536d883df0aa7de642166d",
    "deploy.py": "ec46a8062b0c5b4e40bef1396fd75b2fed800a573aefbdb85424d73053487c0f",
    "install.sh": "27329cb219b87aae7d28ef078e7759fbfb82aaf24aaa5ffde7940660dcb6fbbe",
    "mavlink-config-generator/main.py": "4f0e58fe40c30a86d4545c54a70aedef2e90edc0d42a8af0e4c2b9ae909c6bbb",
}
SERVICES = frozenset(("mosquitto", "dmx_fleet_controller", "gcs", "gcs_reactor",
                      "net_setup", "mavlink_router", "rtcm_mavlink", "wireguard"))
NETWORK_WRITERS = frozenset(("net_setup", "wireguard", "gcs_reactor"))
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_ARCHIVE = 16 * 1024**3
MAX_EXPANDED = 32 * 1024**3
MAX_MEMBERS = 20000
MAX_METADATA = 2 * 1024**2
MAX_RATIO = 200
TTL = 3600


def _params(params, allowed):
    if not isinstance(params, dict) or set(params) - set(allowed):
        raise ApiError("invalid_params", "Paramètres non reconnus pour cette opération.")


def _strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ApiError("unsafe_path", "Le répertoire privé est un lien symbolique.")
    path.chmod(0o700)
    return path


def _atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ApiError("unsafe_archive", "Nom de fichier interdit dans l’archive.")
    path = PurePosixPath(name)
    if len(name.encode("utf-8", "surrogatepass")) > 4096 or len(path.parts) > 64:
        raise ApiError("archive_limit", "Nom de fichier ou arborescence excessivement longs.")
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        raise ApiError("unsafe_archive", "L’archive contient un chemin sortant du dossier.")
    if any(":" in part or any(ord(c) < 32 for c in part) for part in path.parts):
        raise ApiError("unsafe_archive", "L’archive contient un nom de fichier interdit.")
    normal = str(path)
    if normal in ("", "."):
        raise ApiError("unsafe_archive", "Chemin vide dans l’archive.")
    return normal


class BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized extension headers before tarfile allocates their bodies.

    PAX and GNU long-name headers are consumed internally by tarfile, before
    the caller sees a member; checking regular members alone misses them.
    """
    def _proc_member(self, archive):
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ApiError("unsafe_archive", "Fichier creux GNU interdit.")
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            total = getattr(archive, "_aquahub_extension_bytes", 0) + self.size
            count = getattr(archive, "_aquahub_extension_count", 0) + 1
            if self.size < 0 or self.size > MAX_METADATA or total > MAX_METADATA * 4 or count > 128:
                raise ApiError("archive_limit", "En-têtes étendus de l’archive trop volumineux.")
            archive._aquahub_extension_bytes = total
            archive._aquahub_extension_count = count
        return super()._proc_member(archive)

    def _proc_gnusparse_10(self, *args, **kwargs):
        raise ApiError("unsafe_archive", "Format GNU sparse interdit avant décodage des extents.")

    _proc_gnusparse_01 = _proc_gnusparse_10
    _proc_gnusparse_00 = _proc_gnusparse_10


class ArchiveReader:
    """Validate all members, hash streamed data, extract selected files only.

    No ``extractall`` and no reliance on a tar implementation's default filter.
    Limits cover actual bytes read as well as advertised sizes.
    """

    def __init__(self, max_archive=MAX_ARCHIVE, max_expanded=MAX_EXPANDED,
                 max_members=MAX_MEMBERS, max_ratio=MAX_RATIO):
        self.max_archive = max_archive
        self.max_expanded = max_expanded
        self.max_members = max_members
        self.max_ratio = max_ratio

    def inspect(self, path: Path, destination: Path | None = None,
                selected: set[str] | None = None) -> dict:
        source_stat = path.lstat()
        if not stat.S_ISREG(source_stat.st_mode) or path.is_symlink():
            raise ApiError("unsafe_path", "Choisir une archive régulière, sans lien symbolique.")
        if source_stat.st_size <= 0 or source_stat.st_size > self.max_archive:
            raise ApiError("archive_limit", "La taille de l’archive dépasse la limite autorisée.")
        seen: dict[str, dict] = {}
        total = 0
        metadata: dict[str, bytes] = {}
        ancestor_names = set()

        def receive(name, size, stream, directory=False):
            nonlocal total
            normal = safe_member(name)
            if normal in seen:
                raise ApiError("unsafe_archive", "L’archive contient plusieurs fois le même chemin.")
            if len(seen) >= self.max_members or size < 0:
                raise ApiError("archive_limit", "L’archive contient trop de fichiers.")
            # A regular member must never be the parent of another member.
            for parent in PurePosixPath(normal).parents:
                if str(parent) in seen and not seen[str(parent)]["directory"]:
                    raise ApiError("unsafe_archive", "Conflit fichier/répertoire dans l’archive.")
            if not directory and normal in ancestor_names:
                raise ApiError("unsafe_archive", "Conflit fichier/répertoire dans l’archive.")
            ancestor_names.update(str(parent) for parent in PurePosixPath(normal).parents)
            total += size
            if total > self.max_expanded or total > source_stat.st_size * self.max_ratio:
                raise ApiError("archive_limit", "Taille décompressée ou taux de compression excessif.")
            record = {"size": size, "directory": directory}
            seen[normal] = record
            if directory:
                return
            digest = hashlib.sha256()
            keep_meta = (normal.endswith("aquahub-manifest.json") or
                         any(normal.endswith("/" + x) or normal == x for x in KNOWN_FILES))
            capture = bytearray()
            output = None
            try:
                if destination is not None and (selected is None or normal in selected):
                    target = destination.joinpath(*PurePosixPath(normal).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    output = target.open("xb")
                    target.chmod(0o600)
                actual = 0
                while True:
                    block = stream.read(min(1024**2, size - actual + 1))
                    if not block:
                        break
                    actual += len(block)
                    if actual > size:
                        raise ApiError("unsafe_archive", "Taille réelle différente de la taille annoncée.")
                    digest.update(block)
                    if keep_meta:
                        if len(capture) + len(block) > MAX_METADATA:
                            raise ApiError("archive_limit", "Métadonnées trop volumineuses.")
                        capture.extend(block)
                    if output:
                        output.write(block)
                if actual != size:
                    raise ApiError("unsafe_archive", "Archive tronquée.")
            finally:
                if output:
                    output.close()
            record["sha256"] = digest.hexdigest()
            if keep_meta:
                metadata[normal] = bytes(capture)

        try:
            with path.open("rb") as source:
                if zipfile.is_zipfile(source):
                    source.seek(0)
                    end_record = zipfile._EndRecData(source)
                    if (end_record is None or end_record[zipfile._ECD_ENTRIES_TOTAL] > self.max_members or
                            end_record[zipfile._ECD_SIZE] > 16 * 1024**2):
                        raise ApiError("archive_limit", "Répertoire ZIP trop volumineux.")
                    source.seek(0)
                    with zipfile.ZipFile(source) as archive:
                        for member in archive.infolist():
                            mode = member.external_attr >> 16
                            if member.flag_bits & 1 or stat.S_ISLNK(mode):
                                raise ApiError("unsafe_archive", "Archive chiffrée ou lien symbolique interdit.")
                            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                                raise ApiError("unsafe_archive", "Type de fichier spécial interdit.")
                            if member.file_size > max(1, member.compress_size) * self.max_ratio:
                                raise ApiError("archive_limit", "Taux de compression excessif.")
                            with archive.open(member) as stream:
                                receive(member.filename, member.file_size, stream, member.is_dir())
                else:
                    source.seek(0)
                    with tarfile.open(fileobj=source, mode="r|*", tarinfo=BoundedTarInfo) as archive:
                        for member in archive:
                            if not (member.isfile() or member.isdir()) or member.sparse:
                                raise ApiError("unsafe_archive", "Liens, fichiers spéciaux et fichiers creux interdits.")
                            if member.isdir():
                                receive(member.name, 0, io.BytesIO(), True)
                            else:
                                with archive.extractfile(member) as stream:
                                    receive(member.name, member.size, stream)
        except ApiError:
            raise
        except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, RuntimeError,
                RecursionError, ValueError, TypeError, OverflowError) as exc:
            raise ApiError("invalid_archive", "Archive illisible, corrompue ou incomplète.") from exc
        after = path.stat()
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (
                source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns):
            raise ApiError("archive_changed", "L’archive a changé pendant sa lecture.")
        return {"files": seen, "metadata": metadata, "expanded_size": total,
                "archive_size": source_stat.st_size}


def _root_and_relative(files: dict) -> tuple[str, dict]:
    names = [name for name, data in files.items() if not data["directory"]]
    roots = {name.split("/", 1)[0] for name in names}
    prefix = ""
    if len(roots) == 1 and all("/" in name for name in names):
        prefix = next(iter(roots)) + "/"
    return prefix, {name[len(prefix):]: value for name, value in files.items()
                    if name.startswith(prefix) and name[len(prefix):]}


def validate_manifest(manifest: dict, files: dict) -> list[str]:
    reasons = []
    if (type(manifest.get("schema")) is not int or manifest.get("schema") != 1 or manifest.get("product") != "aqua-hub" or
            manifest.get("architecture") != "amd64" or
            manifest.get("mode") != "image-refresh-v1"):
        reasons.append("Contrat produit, plateforme ou format de mise à jour non pris en charge.")
    if not isinstance(manifest.get("version"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", manifest.get("version", "")):
        reasons.append("Version de livraison absente ou invalide.")
    if manifest.get("data_compatibility") != "unchanged" or manifest.get("rollback_compatible") is not True:
        reasons.append("La livraison ne garantit pas la conservation des formats de données.")
    if manifest.get("compose_sha256") != KNOWN_FILES["docker-compose.yml"]:
        reasons.append("Version de Compose non reconnue par cet adaptateur.")
    declared = manifest.get("files")
    if not isinstance(declared, dict) or not declared:
        reasons.append("Inventaire d’empreintes manquant.")
        declared = {}
    for name, digest in declared.items():
        try:
            safe_member(name)
        except ApiError:
            reasons.append("Chemin interdit dans le manifeste.")
            continue
        item = files.get(name)
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or not item or item.get("sha256") != digest:
            reasons.append("Un fichier déclaré est absent ou son empreinte est incorrecte.")
    if set(files) - set(declared) - {"aquahub-manifest.json"} - {
            name for name, data in files.items() if data.get("directory")}:
        reasons.append("La livraison contient des fichiers non déclarés.")
    before, after = manifest.get("from_images"), manifest.get("to_images")
    for mapping in (before, after):
        if (not isinstance(mapping, dict) or set(mapping) != SERVICES or
                any(not isinstance(v, str) or not IMAGE_ID.fullmatch(v) for v in mapping.values())):
            reasons.append("Les huit images avant et après doivent être identifiées exactement.")
            break
    if isinstance(before, dict) and isinstance(after, dict):
        if any(before.get(service) != after.get(service) for service in NETWORK_WRITERS):
            reasons.append("Cette livraison modifierait un composant qui administre les IP.")
    archives = manifest.get("image_archives")
    if (not isinstance(archives, list) or not archives or len(archives) > 8 or
            any(not isinstance(name, str) or name not in declared or not name.endswith(".tar") for name in archives)):
        reasons.append("Les archives Docker-save et leurs empreintes sont requises.")
    # This format refreshes images only. It never accepts replacement configs.
    if isinstance(archives, list) and all(isinstance(name, str) for name in archives) and any(name not in set(archives) for name in declared):
        reasons.append("Le format image-refresh-v1 accepte uniquement des archives d’images.")
    return list(dict.fromkeys(reasons))


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    """HTTPS only, short redirect chains, no credentials outside api.github.com."""
    max_redirections = 4
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        try:
            port = target.port
        except ValueError as exc:
            raise ApiError("unsafe_redirect", "Port de redirection invalide.") from exc
        if target.scheme != "https" or target.username or target.password or port not in (None, 443):
            raise ApiError("unsafe_redirect", "Redirection de téléchargement non sûre.")
        # Release assets may redirect to GitHub's storage; never arbitrary hosts.
        if target.hostname not in {"api.github.com", "github.com", "objects.githubusercontent.com",
                                   "release-assets.githubusercontent.com"}:
            raise ApiError("unsafe_redirect", "Domaine de téléchargement GitHub non autorisé.")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and target.hostname != "api.github.com":
            for key in tuple(redirected.headers):
                if key.lower() == "authorization":
                    del redirected.headers[key]
            for key in tuple(redirected.unredirected_hdrs):
                if key.lower() == "authorization":
                    del redirected.unredirected_hdrs[key]
        return redirected


class GitLabNoRedirect(urllib.request.HTTPRedirectHandler):
    """A private GitLab token never follows a server-supplied redirect."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("unsafe_redirect", "GitLab redirige ce fichier vers une autre source. Importer l’archive fournie localement.")


def normalise_repository(value: str) -> str:
    if not isinstance(value, str):
        raise ApiError("invalid_repository", "Indiquer l’URL HTTPS du dépôt GitHub ou GitLab.")
    try:
        parts = urllib.parse.urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise ApiError("invalid_repository", "L’URL du dépôt Git est invalide.") from exc
    if (parts.scheme != "https" or parts.hostname not in {"github.com", "gitlab.com"} or
            parts.username or parts.password or port not in (None, 443) or parts.query or parts.fragment):
        raise ApiError("invalid_repository", "Utiliser une URL HTTPS github.com ou gitlab.com sans identifiants.")
    path = parts.path.strip("/").removesuffix(".git")
    if parts.hostname == "github.com":
        valid = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", path)
    else:
        valid = (2 <= len(path.split("/")) <= 12 and len(path) <= 400 and
                 all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", p) for p in path.split("/")))
    if not valid or any(p in (".", "..", "-") for p in path.split("/")):
        raise ApiError("invalid_repository", "Nom de dépôt Git invalide.")
    return "https://" + parts.hostname + "/" + path


class UpdateManager:
    def __init__(self, app, reader=None, opener=None):
        self.app = app
        self.root = _private_dir(Path(app.config.state_dir) / "updates")
        self.reader = reader or ArchiveReader()
        self.opener = opener or urllib.request.build_opener(NoCredentialRedirect())
        self.gitlab_opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), GitLabNoRedirect())
        self.inspections = {}
        self.releases = {}
        self.lock = threading.RLock()

    def _purge_expired_preparations(self):
        """Only disposable archive copies; never original files or snapshots."""
        with self.lock:
            for directory in self.root.iterdir():
                if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                item = self.inspections.get(directory.name)
                if (directory / "snapshot").exists() or (item and item.get("used")):
                    continue
                if time.time() - (item["created"] if item else directory.stat().st_mtime) > TTL:
                    shutil.rmtree(directory)
                    self.inspections.pop(directory.name, None)

    def _config_hashes(self) -> dict:
        bundle = self.app.config.bundle_path
        if not bundle:
            raise ApiError("bundle_missing", "Aucun bundle existant n’est associé à AquaHub.")
        bundle = Path(bundle)
        hashes = {}
        for rel in ("docker-compose.yml", ".env", "config"):
            path = bundle / rel
            if path.is_symlink():
                raise ApiError("unsafe_path", "Un fichier du bundle est un lien symbolique.")
            paths = path.rglob("*") if path.is_dir() else [path]
            for item in paths:
                if item.is_symlink():
                    raise ApiError("unsafe_path", "Un fichier du bundle est un lien symbolique.")
                if item.is_file():
                    hashes[str(item.relative_to(bundle))] = sha256_file(item)
        if hashes.get("docker-compose.yml") != KNOWN_FILES["docker-compose.yml"]:
            raise ApiError("unsupported_bundle", "Le fichier Compose installé n’est pas reconnu.")
        return hashes

    def inspect(self, params, context=None):
        _params(params, {"path"})
        self._purge_expired_preparations()
        value = params.get("path")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ApiError("invalid_path", "Sélectionner le chemin absolu d’une archive locale.")
        path = Path(value)
        # The archive is copied privately before inspection to avoid mutable inputs.
        token = uuid.uuid4().hex
        stage = _private_dir(self.root / token)
        staged = stage / "delivery.archive"
        try:
            try:
                source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as exc:
                raise ApiError("invalid_path", "L’archive sélectionnée est absente, inaccessible ou liée symboliquement.") from exc
            with os.fdopen(source_fd, "rb") as source:
                source_stat = os.fstat(source.fileno())
                if not stat.S_ISREG(source_stat.st_mode) or not 0 < source_stat.st_size <= self.reader.max_archive:
                    raise ApiError("archive_limit", "L’archive est vide, spéciale ou trop volumineuse.")
                if context is not None and getattr(context, "uid", 0) not in (0, source_stat.st_uid) and not source_stat.st_mode & 0o004:
                    raise ApiError("access_denied", "Cette archive n’est pas lisible par votre compte.")
                if shutil.disk_usage(stage).free < source_stat.st_size * 2 + 64 * 1024**2:
                    raise ApiError("disk_full", "Espace insuffisant pour préparer et vérifier l’archive.")
                with staged.open("xb") as output:
                    staged.chmod(0o600)
                    count = 0
                    for block in iter(lambda: source.read(1024**2), b""):
                        count += len(block)
                        if count > source_stat.st_size:
                            raise ApiError("archive_changed", "L’archive a changé pendant sa préparation.")
                        output.write(block)
                    if count != source_stat.st_size:
                        raise ApiError("archive_changed", "L’archive a changé pendant sa préparation.")
            inspected = self.reader.inspect(staged)
            prefix, files = _root_and_relative(inspected["files"])
            raw_manifest = inspected["metadata"].get(prefix + "aquahub-manifest.json")
            checks = [{"label": "Archive et chemins vérifiés", "ok": True}]
            warnings = ["Une empreinte prouve l’intégrité du contenu, pas l’identité de son fournisseur."]
            manifest = None
            compatible = False
            reasons = []
            if raw_manifest is not None:
                try:
                    manifest = _strict_json(raw_manifest)
                    if not isinstance(manifest, dict):
                        raise ValueError()
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ApiError("invalid_manifest", "Le manifeste de livraison est invalide.") from exc
                reasons = validate_manifest(manifest, files)
                compatible = not reasons
                if compatible:
                    try:
                        config_hashes = self._config_hashes()
                    except ApiError as exc:
                        reasons.append(str(exc))
                        config_hashes = {}
                else:
                    config_hashes = {}
            else:
                compatible = all(files.get(name, {}).get("sha256") == digest for name, digest in KNOWN_FILES.items())
                if compatible:
                    compatible = "aqua-hub-images-amd64.tar" in files
                reasons.append("Bundle historique reconnu : aucun contrat vérifiable de mise à jour et de retour arrière n’est fourni. Aucune installation automatique n’est lancée." if compatible else
                               "Cette archive ne correspond à aucun format fournisseur reconnu.")
                warnings.append("Le bundle historique peut contenir des configurations privées ; il reste dans un espace protégé.")
                config_hashes = {}
            can_apply = compatible and not reasons and manifest is not None
            if manifest is not None:
                checks.append({"label": "Contrat de mise à jour et empreintes", "ok": compatible})
                checks.append({"label": "Bundle installé compatible", "ok": can_apply})
            result = {"inspection_id": token, "id": token, "allowed": can_apply,
                      "compatible": compatible, "can_apply": can_apply,
                      "version": manifest.get("version") if manifest else "Bundle historique non versionné",
                      "files": len(files), "archive_size": inspected["archive_size"],
                      "expanded_size": inspected["expanded_size"], "checks": checks,
                      "warnings": warnings, "reasons": reasons,
                      "message": "Livraison préparée ; l’application demandera une confirmation." if can_apply else " ".join(reasons)}
            with self.lock:
                self.inspections[token] = {"path": staged, "digest": sha256_file(staged),
                    "prefix": prefix, "manifest": manifest, "config_hashes": config_hashes,
                    "created": time.time(), "result": result, "used": False}
            if not can_apply:
                # A refused import has no executable preparation to retain.
                shutil.rmtree(stage)
            return result
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _docker(self, args, timeout=60):
        result = self.app.docker.run(args, timeout=timeout, max_bytes=MAX_METADATA)
        # Adapter returns stdout text; test doubles may return a process object.
        return result.stdout if hasattr(result, "stdout") else result

    def _compose(self, args, timeout=60):
        if not self.app.config.bundle_path:
            raise ApiError("bundle_missing", "Aucun bundle existant n’est associé à AquaHub.")
        bundle = Path(self.app.config.bundle_path)
        # The installation directory can be named simply "bundle". Compose
        # must always address the supplier project already discovered by Docker,
        # including verification and rollback, rather than derive another name.
        return self._docker(["compose", "-p", PROJECT, "--project-directory", str(bundle),
                             "-f", str(bundle / "docker-compose.yml"), *args], timeout=timeout)

    def _model(self):
        raw = self._compose(["config", "--format", "json"])
        model = json.loads(raw)
        services = model.get("services", {})
        if set(services) != SERVICES or not all(isinstance(v.get("image"), str) for v in services.values()):
            raise ApiError("incompatible_runtime", "Le projet actif ne contient pas les huit services attendus.")
        return model

    def _network_state(self):
        # This observes addresses and routes. It never repairs or assigns them.
        collected = {}
        for key, arguments in (("addresses", ["-d", "address", "show"]), ("routes", ["route", "show", "table", "all"])):
            try:
                process = subprocess.run(["/usr/sbin/ip", "-j", *arguments], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=True,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
                if len(process.stdout) > MAX_METADATA:
                    raise ValueError()
                rows = json.loads(process.stdout)
                if not isinstance(rows, list):
                    raise ValueError()
                collected[key] = rows
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError) as exc:
                raise ApiError("network_unknown", "Les IP et routes du hub ne peuvent pas être comparées ; mise à jour refusée.") from exc
        try:
            normalised = stable_network(collected["addresses"], collected["routes"])
            # Keep the persisted JSON-friendly shape of previous image snapshots.
            return {"addresses": sorted(json.dumps(record, sort_keys=True) for record in normalised["addresses"]),
                    "routes": sorted(json.dumps({key: json.loads(value) for key, value in record}, sort_keys=True)
                                     for record in normalised["routes"])}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ApiError("network_unknown", "Les métadonnées réseau du hub sont incomplètes ; mise à jour refusée.") from exc

    def _image_ids(self, references):
        ids = {}
        for service, reference in references.items():
            if reference.startswith("-") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,240}", reference):
                raise ApiError("invalid_image", "Référence d’image invalide.")
            raw = self._docker(["image", "inspect", "--format", "{{.Id}}", reference])
            image = str(raw).strip()
            if not IMAGE_ID.fullmatch(image):
                raise ApiError("image_missing", "Une image locale nécessaire est absente.")
            ids[service] = image
        return ids

    def _compose_up(self):
        self._compose(["up", "-d", "--pull", "never", "--wait", "--wait-timeout", "120"], timeout=160)

    def _running_ids(self, references):
        hash_raw = self._compose(["config", "--hash", "*"])
        expected_hashes = {}
        for line in str(hash_raw).splitlines():
            pair = line.split()
            if len(pair) != 2 or pair[0] not in SERVICES or not SHA256.fullmatch(pair[1]):
                raise ApiError("configuration_unknown", "Les empreintes Compose ne peuvent pas être vérifiées.")
            expected_hashes[pair[0]] = pair[1]
        if set(expected_hashes) != SERVICES:
            raise ApiError("configuration_unknown", "Les empreintes Compose sont incomplètes.")
        raw = self._compose(["ps", "--all", "--format", "json"])
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                rows = [rows]
        except ValueError:
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        by_service = {row.get("Service"): row for row in rows}
        if set(by_service) != SERVICES or len(rows) != len(SERVICES):
            raise ApiError("verification_failed", "Tous les conteneurs attendus ne sont pas présents.")
        for service, row in by_service.items():
            if row.get("State") != "running" or row.get("Health") not in ("", None, "healthy"):
                raise ApiError("verification_failed", "Un conteneur n’est pas opérationnel après l’application.")
            actual = str(self._docker(["container", "inspect", "--format", "{{.Image}}", row["ID"]])).strip()
            if actual != references[service]:
                raise ApiError("verification_failed", "L’image active diffère de la livraison attendue.")
            config_hash = str(self._docker(["container", "inspect", "--format",
                '{{index .Config.Labels "com.docker.compose.config-hash"}}', row["ID"]])).strip()
            if config_hash != expected_hashes[service]:
                raise ApiError("configuration_mismatch", "Un conteneur ne correspond pas à la configuration du bundle. Aucune recréation implicite n’est autorisée.")

    def _validate_image_tar(self, path, allowed_ids, allowed_refs):
        try:
            actual_ids = set()
            references = {canonical_reference(reference) for reference in allowed_refs}
            records = archive_image_records(path)
            if len(records) > 8:
                raise ApiError("unsafe_image_archive", "Trop d’images dans cette livraison.")
            for record in records:
                identities = set(record["identities"])
                if not identities.intersection(allowed_ids) or not set(record["references"]).issubset(references):
                    raise ApiError("unexpected_image", "L’archive chargerait une image ou une étiquette non autorisée.")
                actual_ids |= identities.intersection(allowed_ids)
            return actual_ids
        except ApiError:
            raise
        except (ImageIdentityError, TypeError, AttributeError) as exc:
            raise ApiError("invalid_image_archive", "Archive d’images sans preuve d’identité Docker/OCI valide : " + str(exc)) from exc

    def _snapshot(self, stage, config_hashes, references, image_ids, network):
        snapshot = _private_dir(stage / "snapshot")
        bundle = Path(self.app.config.bundle_path)
        for relative in config_hashes:
            target = snapshot / "config" / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(bundle / relative, target)
            target.chmod(0o600)
        image_file = snapshot / "previous-images.tar"
        self._docker(["image", "save", "-o", str(image_file), *sorted(set(image_ids.values()))], timeout=600)
        if not image_file.is_file() or image_file.stat().st_size == 0:
            raise ApiError("snapshot_failed", "La sauvegarde des images précédentes a échoué.")
        image_file.chmod(0o600)
        record = {"config_hashes": config_hashes, "references": references,
                  "image_ids": image_ids, "image_sha256": sha256_file(image_file),
                  "created": time.time(), "bundle": str(bundle), "network": network, "completed": False}
        _atomic_json(snapshot / "snapshot.json", record)
        return snapshot, record

    def _restore(self, snapshot, record):
        if str(self.app.config.bundle_path) != record["bundle"]:
            raise ApiError("rollback_mismatch", "La sauvegarde concerne un autre bundle.")
        if self._config_hashes() != record["config_hashes"]:
            raise ApiError("rollback_mismatch", "Les configurations ont changé ; restauration automatique refusée.")
        if self._network_state() != record["network"]:
            raise ApiError("rollback_mismatch", "Le réseau a changé. Aucune IP ne sera restaurée automatiquement.")
        images = snapshot / "previous-images.tar"
        if not images.is_file() or sha256_file(images) != record["image_sha256"]:
            raise ApiError("rollback_corrupt", "La sauvegarde d’images est absente ou corrompue.")
        self._docker(["image", "load", "-i", str(images)], timeout=600)
        for service, reference in record["references"].items():
            self._docker(["image", "tag", record["image_ids"][service], reference])
        self._compose_up()
        self._running_ids(record["image_ids"])
        if self._network_state() != record["network"]:
            raise ApiError("rollback_mismatch", "Le réseau diffère après restauration ; contrôle manuel requis.")
        return {"restored": True, "verified": True}

    def apply(self, params, context=None):
        _params(params, {"inspection_id"})
        token = params.get("inspection_id")
        if not isinstance(token, str):
            raise ApiError("invalid_params", "L’identifiant d’inspection est obligatoire.")
        with self.lock:
            item = self.inspections.get(token)
        if not item or time.time() - item["created"] > TTL:
            raise ApiError("inspection_expired", "Inspecter à nouveau cette livraison avant application.")
        if not item["result"]["can_apply"]:
            raise ApiError("update_not_available", item["result"]["message"])

        def work():
            with self.lock:
                if item["used"] or time.time() - item["created"] > TTL:
                    raise ApiError("inspection_expired", "Cette inspection est expirée ou déjà utilisée.")
                item["used"] = True
            if sha256_file(item["path"]) != item["digest"]:
                raise ApiError("archive_changed", "L’archive préparée a changé.")
            config_hashes = self._config_hashes()
            if config_hashes != item["config_hashes"]:
                raise ApiError("configuration_changed", "Les configurations ont changé depuis l’inspection.")
            manifest = item["manifest"]
            model = self._model()
            references = {name: value["image"] for name, value in model["services"].items()}
            if any("@" in reference or IMAGE_ID.fullmatch(reference) for reference in references.values()):
                raise ApiError("immutable_reference", "Les références immuables demandent un autre contrat fournisseur ; aucune étiquette ne sera remplacée.")
            desired = manifest["to_images"]
            for reference in set(references.values()):
                if len({desired[service] for service, value in references.items() if value == reference}) != 1:
                    raise ApiError("image_alias_conflict", "Deux services partageant une référence demandent des images différentes.")
            before = self._image_ids(references)
            if before != manifest["from_images"]:
                raise ApiError("version_mismatch", "Les images installées diffèrent du point de départ prévu.")
            self._running_ids(before)
            network = self._network_state()
            stage = item["path"].parent
            extract = _private_dir(stage / "content")
            archives = manifest["image_archives"]
            selected = {item["prefix"] + name for name in archives}
            self.reader.inspect(item["path"], extract, selected)
            available = set()
            for name in archives:
                available |= self._validate_image_tar(extract / (item["prefix"] + name),
                                                      set(desired.values()), set(references.values()))
            if not set(desired.values()).issubset(available | set(before.values())):
                raise ApiError("image_missing", "Toutes les nouvelles images ne sont pas dans la livraison.")
            if shutil.disk_usage(stage).free < sum(p.stat().st_size for p in extract.rglob("*") if p.is_file()) * 2 + 256 * 1024**2:
                raise ApiError("disk_full", "Espace insuffisant pour les images et leur sauvegarde.")
            snapshot, record = self._snapshot(stage, config_hashes, references, before, network)
            try:
                for name in archives:
                    self._docker(["image", "load", "-i", str(extract / (item["prefix"] + name))], timeout=600)
                for service, reference in references.items():
                    self._docker(["image", "tag", desired[service], reference])
                if self._config_hashes() != config_hashes:
                    raise ApiError("configuration_changed", "Les configurations ont changé pendant la préparation.")
                self._compose_up()
                self._running_ids(desired)
                if self._config_hashes() != config_hashes:
                    raise ApiError("configuration_changed", "Une configuration a changé pendant l’application.")
                if self._network_state() != network:
                    raise ApiError("network_changed", "Le réseau diffère après application. Contrôle manuel requis ; aucune IP ne sera réécrite.")
            except Exception as original:
                try:
                    self._restore(snapshot, record)
                except Exception as restore_error:
                    raise ApiError("rollback_failed", "Mise à jour échouée et restauration non vérifiée. Intervention requise ; sauvegarde conservée.") from restore_error
                raise ApiError("update_failed_restored", "La mise à jour a échoué. Les images précédentes ont été restaurées et vérifiées.") from original
            record.update(completed=True, after_images=desired, version=manifest["version"])
            _atomic_json(snapshot / "snapshot.json", record)
            _atomic_json(self.root / "last-rollback.json", {"inspection_id": token})
            with self.lock:
                item["result"]["can_apply"] = False
            return {"applied": True, "verified": True, "version": manifest["version"],
                    "rollback_available": True, "configurations_preserved": True}

        return self.app.submit_job("update", "aqua-hub", {"inspection_id": token}, work)

    def rollback(self, params=None, context=None):
        _params(params or {}, set())
        pointer = self.root / "last-rollback.json"
        if not pointer.is_file():
            raise ApiError("rollback_unavailable", "Aucune mise à jour restaurable n’a été appliquée par AquaHub.")
        try:
            token = _strict_json(pointer.read_bytes()).get("inspection_id", "")
        except (OSError, ValueError, AttributeError, UnicodeError) as exc:
            raise ApiError("rollback_unavailable", "Les métadonnées de restauration sont illisibles ; les sauvegardes sont conservées.") from exc
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ApiError("rollback_unavailable", "Référence de sauvegarde invalide.")
        snapshot = self.root / token / "snapshot"
        try:
            metadata = snapshot / "snapshot.json"
            if metadata.stat().st_size > MAX_METADATA:
                raise ValueError()
            record = _strict_json(metadata.read_bytes())
            if (not isinstance(record, dict) or not {"config_hashes", "references", "image_ids", "image_sha256", "network", "bundle"}.issubset(record)):
                raise ValueError()
        except (OSError, ValueError, UnicodeError) as exc:
            raise ApiError("rollback_unavailable", "Le catalogue de sauvegarde est absent ou corrompu ; aucun chargement d’image n’est lancé.") from exc
        if not record.get("completed"):
            raise ApiError("rollback_unavailable", "La sauvegarde ne correspond pas à une mise à jour terminée.")

        def work():
            model = self._model()
            refs = {name: value["image"] for name, value in model["services"].items()}
            if refs != record["references"] or self._image_ids(refs) != record["after_images"]:
                raise ApiError("rollback_mismatch", "Les images ont changé depuis la mise à jour.")
            result = self._restore(snapshot, record)
            record["completed"] = False
            _atomic_json(snapshot / "snapshot.json", record)
            pointer.unlink(missing_ok=True)
            return result
        return self.app.submit_job("rollback", "aqua-hub", {}, work)

    def settings(self, params, context=None):
        _params(params, {"repository_url", "token"})
        path = self.root / "github.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.setdefault("token_provider", "gitlab" if current.get("repository_url", "").startswith("https://gitlab.com/") else "github")
        store = getattr(self.app, "store", None)
        if store is not None:
            current["repository_url"] = store.setting_get("github_url", current.get("repository_url", ""))
        if current.get("repository_url"):
            current["repository_url"] = normalise_repository(current["repository_url"])
        if "repository_url" in params:
            url = params["repository_url"]
            current["repository_url"] = normalise_repository(url) if url else ""
            if store is not None:
                store.setting_set("github_url", current["repository_url"])
            self.releases.clear()
        if "token" in params:
            token = params["token"]
            if not isinstance(token, str) or len(token) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                if token != "":
                    raise ApiError("invalid_token", "Le jeton d’accès a un format invalide.")
            current["token"] = token
            current["token_provider"] = "gitlab" if current.get("repository_url", "").startswith("https://gitlab.com/") else "github"
        if params:
            _atomic_json(path, current)
        provider = "gitlab" if current.get("repository_url", "").startswith("https://gitlab.com/") else "github"
        return {"repository_url": current.get("repository_url", ""),
                "token_configured": bool(current.get("token")) and current["token_provider"] == provider, "provider": provider}

    def _gitlab(self, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "gitlab.com" or
                not parsed.path.startswith("/api/v4/projects/") or parsed.fragment):
            raise ApiError("invalid_source", "Source API GitLab non autorisée.")
        path = self.root / "github.json"
        config = json.loads(path.read_text()) if path.is_file() else {}
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AquaHub/1"})
        if config.get("token") and config.get("token_provider", "github") == "gitlab":
            request.add_unredirected_header("PRIVATE-TOKEN", config["token"])
        try:
            with self.gitlab_opener.open(request, timeout=30) as response:
                data = response.read(MAX_METADATA + 1)
                if len(data) > MAX_METADATA:
                    raise ApiError("gitlab_limit", "Réponse GitLab trop volumineuse.")
                return _strict_json(data)
        except urllib.error.HTTPError as exc:
            messages = {401: "GitLab refuse l’accès API : utiliser un jeton avec permission read_api. Un jeton de déploiement Git ne suffit pas.",
                        403: "Permission GitLab insuffisante ou limite atteinte.",
                        404: "Projet ou livraison GitLab absent, privé ou non accessible.",
                        429: "Limite GitLab atteinte ; réessayer ultérieurement."}
            raise ApiError("gitlab_http_error", messages.get(exc.code, "GitLab n’a pas fourni les livraisons.")) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ApiError("gitlab_unavailable", "Connexion GitLab indisponible ou réponse invalide.") from exc

    def _check_gitlab(self, repository):
        slug = repository.removeprefix("https://gitlab.com/")
        project = urllib.parse.quote(slug, safe="")
        releases = []
        self.releases.clear()
        for page in range(1, 4):
            data = self._gitlab(f"https://gitlab.com/api/v4/projects/{project}/releases?per_page=20&page={page}")
            if not isinstance(data, list):
                raise ApiError("gitlab_invalid", "La liste des livraisons GitLab est invalide.")
            for release in data[:20]:
                if not isinstance(release, dict) or release.get("upcoming_release"):
                    continue
                tag = release.get("tag_name")
                if not isinstance(tag, str) or not tag or len(tag) > 255:
                    continue
                # GitLab identifies releases by tag. This stable 48-bit key is
                # safe for the existing desktop numeric identifier protocol.
                release_id = int(hashlib.sha256(tag.encode()).hexdigest()[:12], 16) or 1
                content = release.get("assets")
                links = content.get("links", []) if isinstance(content, dict) else []
                if not isinstance(links, list):
                    raise ApiError("gitlab_invalid", "Les fichiers de la livraison GitLab sont invalides.")
                assets = []
                for link in links[:100]:
                    if not isinstance(link, dict) or type(link.get("id")) is not int or link["id"] <= 0:
                        continue
                    name = link.get("name")
                    if not isinstance(name, str):
                        continue
                    archive = name.endswith((".tar.gz", ".tar", ".zip")) and "amd64" in name.lower() and "source" not in name.lower()
                    # Release links permit arbitrary external targets and do not
                    # supply authenticated size/SHA256. Never follow these URLs
                    # or attach the private token. Only local verified imports
                    # are offered until the supplier delivery contract is known.
                    assets.append({"id": link["id"], "asset_id": link["id"], "name": name[:255],
                        "size": None, "sha256": None, "compatible": archive, "downloadable": False,
                        "message": "Importer l’archive localement : ce lien GitLab ne fournit pas de taille et SHA-256 vérifiables."})
                releases.append({"id": release_id, "release_id": release_id, "version": tag[:100],
                    "name": str(release.get("name") or "")[:200], "notes": str(release.get("description") or "")[:8000],
                    "assets": assets, "compatible": False})
            if len(data) < 20:
                break
        return {"configured": True, "provider": "gitlab", "repository_url": repository, "releases": releases,
                "message": "Livraisons GitLab consultées. Import local requis tant que le contrat fournisseur et les empreintes des fichiers ne sont pas vérifiés."}

    def _github(self, url, binary=False):
        config_path = self.root / "github.json"
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            raise ApiError("invalid_source", "Source API GitHub non autorisée.")
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "AquaHub/1", "X-GitHub-Api-Version": "2026-03-10"}
        request = urllib.request.Request(url, headers=headers)
        if config.get("token") and config.get("token_provider", "github") == "github":
            request.add_unredirected_header("Authorization", "Bearer " + config["token"])
        try:
            response = self.opener.open(request, timeout=30)
            if binary:
                return response
            with response:
                data = response.read(MAX_METADATA + 1)
                if len(data) > MAX_METADATA:
                    raise ApiError("github_limit", "Réponse GitHub trop volumineuse.")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            message = {401: "Accès GitHub refusé : vérifier le jeton.", 403: "Accès GitHub limité ou permission insuffisante.",
                       404: "Dépôt ou livraison absent, privé ou non accessible.", 429: "Limite GitHub atteinte ; réessayer ultérieurement."}.get(exc.code, "Le serveur GitHub n’a pas fourni la livraison.")
            raise ApiError("github_http_error", message) from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ApiError("github_unavailable", "Connexion GitHub indisponible ou réponse invalide.") from exc

    def check(self, params=None, context=None):
        _params(params or {}, set())
        settings = self.settings({})
        repository = settings["repository_url"]
        if not repository:
            return {"configured": False, "releases": [], "message": "Renseigner le dépôt fournisseur dans les réglages."}
        if repository.startswith("https://gitlab.com/"):
            return self._check_gitlab(repository)
        slug = repository.removeprefix("https://github.com/")
        releases = []
        self.releases.clear()
        for page in range(1, 4):
            data = self._github(f"https://api.github.com/repos/{slug}/releases?per_page=20&page={page}")
            if not isinstance(data, list):
                raise ApiError("github_invalid", "La liste des livraisons GitHub est invalide.")
            for release in data[:20]:
                if not isinstance(release, dict) or release.get("draft") or release.get("prerelease") or not isinstance(release.get("id"), int):
                    continue
                assets = []
                for asset in release.get("assets", [])[:100]:
                    if not isinstance(asset, dict) or not isinstance(asset.get("id"), int):
                        continue
                    name, size = asset.get("name", ""), asset.get("size", 0)
                    if not isinstance(name, str) or not isinstance(size, int):
                        continue
                    compatible = (name.endswith((".tar.gz", ".tar", ".zip")) and
                                  "amd64" in name.lower() and "source" not in name.lower() and 0 < size <= MAX_ARCHIVE)
                    digest = asset.get("digest") or ""
                    integrity = isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None
                    candidate = {"id": asset["id"], "asset_id": asset["id"], "name": name[:255],
                                 "size": size, "compatible": compatible, "downloadable": compatible and integrity,
                                 "sha256": digest[7:] if integrity else None,
                                 "message": "Inspection obligatoire après téléchargement." if compatible and integrity else
                                            "Archive amd64 complète et empreinte SHA-256 requises ; les ZIP de code source sont exclus."}
                    assets.append(candidate)
                    if candidate["downloadable"]:
                        self.releases[(release["id"], asset["id"])] = {**candidate, "repository": slug, "created": time.time()}
                releases.append({"id": release["id"], "release_id": release["id"],
                                 "version": str(release.get("tag_name", ""))[:100],
                                 "name": str(release.get("name") or "")[:200],
                                 "notes": str(release.get("body") or "")[:8000], "assets": assets,
                                 "compatible": any(a["downloadable"] for a in assets)})
            if len(data) < 20:
                break
        return {"configured": True, "repository_url": repository, "releases": releases,
                "message": "Les archives doivent être inspectées avant toute application." if releases else "Aucune release publiée compatible n’est disponible."}

    def download(self, params, context=None):
        _params(params, {"release_id", "asset_id"})
        if any(isinstance(params.get(name), bool) or not isinstance(params.get(name), int) or params[name] <= 0 for name in ("release_id", "asset_id")):
            raise ApiError("invalid_params", "Les identifiants de release et de fichier doivent être des nombres positifs.")
        key = (params.get("release_id"), params.get("asset_id"))
        asset = self.releases.get(key)
        if (not asset or time.time() - asset["created"] > TTL or
                self.settings({})["repository_url"] != "https://github.com/" + asset["repository"]):
            raise ApiError("release_expired", "Rechercher les livraisons avant de télécharger ce fichier.")
        stage = _private_dir(self.root / ("download-" + uuid.uuid4().hex))
        target = stage / "delivery.archive"

        def work():
            try:
                if shutil.disk_usage(stage).free < asset["size"] * 2 + 64 * 1024**2:
                    raise ApiError("disk_full", "Espace insuffisant pour télécharger et inspecter cette livraison.")
                digest, count = hashlib.sha256(), 0
                url = f"https://api.github.com/repos/{asset['repository']}/releases/assets/{asset['id']}"
                with self._github(url, binary=True) as response, target.open("xb") as output:
                    target.chmod(0o600)
                    for block in iter(lambda: response.read(1024**2), b""):
                        count += len(block)
                        if count > asset["size"] or count > MAX_ARCHIVE:
                            raise ApiError("download_limit", "Le fichier dépasse la taille annoncée.")
                        digest.update(block)
                        output.write(block)
                if count != asset["size"] or digest.hexdigest() != asset["sha256"]:
                    raise ApiError("download_integrity", "La taille ou l’empreinte du fichier téléchargé est incorrecte.")
                inspection = self.inspect({"path": str(target)})
                return {"downloaded": True, "inspection": inspection}
            finally:
                shutil.rmtree(stage, ignore_errors=True)
        return self.app.submit_job("download", "github", {"release_id": key[0], "asset_id": key[1]}, work)


def register(app):
    manager = UpdateManager(app)
    for name in ("inspect", "apply", "rollback", "settings", "check", "download"):
        app.register_handler("updates." + name, getattr(manager, name), mutation=name != "check")
    app.updates = manager
    return manager
