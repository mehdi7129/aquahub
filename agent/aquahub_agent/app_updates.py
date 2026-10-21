"""Application-only updates from the fixed public AquaHub Releases channel.

This channel never consumes supplier credentials, archives or images. Public
HTTPS release metadata supplies the SHA-256; the downloaded Debian package
must also identify itself as an application-only AquaHub update. Installation
is handed to a fixed, independent system service so replacing the agent does
not kill its installer.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import __version__
from .security import ApiError, redact
from .updates import NoCredentialRedirect, _atomic_json

REPOSITORY = "https://github.com/mehdi7129/aquahub"
API = "https://api.github.com/repos/mehdi7129/aquahub"
PACKAGE = "aquahub-setup"
STAGE = Path("/var/lib/aquahub/agent/app-updates")
MAX_PACKAGE = 256 * 1024**2
MAX_METADATA = 2 * 1024**2
TTL = 600
VERSION = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$")
DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
PENDING = frozenset({"scheduled", "installing"})


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ApiError("invalid_version", "La version AquaHub doit avoir la forme majeure.mineure.correctif.")
    return tuple(map(int, value.split(".")))


def _params(params, allowed):
    if not isinstance(params, dict) or set(params) - set(allowed):
        raise ApiError("invalid_params", "Paramètres de mise à jour AquaHub non reconnus.")


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


class AppUpdates:
    def __init__(self, app, *, opener=None, runner=None, launcher=None):
        self.app = app
        self.root = Path(app.config.state_dir) / "app-updates"
        if self.root.is_symlink():
            raise ApiError("unsafe_path", "Le dossier des mises à jour AquaHub ne doit pas être un lien.")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.geteuid():
            raise ApiError("unsafe_path", "Le dossier des mises à jour AquaHub possède un propriétaire inattendu.")
        self.root.chmod(0o700)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())
        self.runner = runner or subprocess.run
        self.launcher = launcher or self._launch
        self.lock = threading.RLock()
        self.release = None
        self.checked_at = 0
        self.message = "Recherchez les mises à jour d’AquaHub."
        # Application owns the exclusive agent lock and has interrupted jobs
        # from the previous process before registering this manager. A killed
        # download cannot otherwise execute its finally block.
        if not app.store.active_job():
            for path in self.root.glob("download-*.part"):
                if (re.fullmatch(r"download-[0-9a-f]{32}\.part", path.name) and
                        not path.is_symlink() and path.is_file() and path.stat().st_uid == os.geteuid()):
                    path.unlink()

    def _read(self, name):
        path = self.root / name
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
            raise ApiError("unsafe_path", "Un fichier de mise à jour AquaHub possède des permissions inattendues.")
        if path.stat().st_size > MAX_METADATA:
            raise ApiError("invalid_state", "L’état de la mise à jour AquaHub est trop volumineux.")
        try:
            data = _strict_json(path.read_bytes())
            if not isinstance(data, dict):
                raise ValueError("object expected")
            return data
        except (ValueError, UnicodeError):
            raise ApiError("invalid_state", "L’état de la mise à jour AquaHub est illisible.") from None

    def _recover_installation(self, state):
        """Reconcile an abandoned supervisor, without replaying installation.

        The fixed unit remains active across the agent restart. Its absence,
        a grace period and the maintenance lock must agree before recovery.
        A failed probe leaves maintenance blocked rather than guessing.
        """
        if not state or state.get("status") not in PENDING or self.root != STAGE:
            return state
        stamp = state.get("updated_at")
        try:
            updated = (float(stamp) if isinstance(stamp, (int, float)) else
                       datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, AttributeError, OverflowError):
            updated = (self.root / "status.json").stat().st_mtime
        if time.time() - updated < 45:
            return state
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                   "stderr": subprocess.PIPE, "text": True, "timeout": 10,
                   "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}, "check": False}
        descriptor = None
        try:
            unit = self.runner(["/usr/bin/systemctl", "is-active", "--quiet",
                                "aquahub-application-upgrade.service"], **options)
            if unit.returncode not in (3, 4):
                return state
            import fcntl
            descriptor = os.open(self.app._maintenance_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return state
            package = self.runner(["/usr/bin/dpkg-query", "--show", "--showformat=${Status}\t${Version}", PACKAGE], **options)
            if package.returncode not in (0, 1):
                return state
            installed = package.returncode == 0 and package.stdout.strip() == "install ok installed\t" + str(state.get("version"))
            success = installed and state.get("version") == __version__
            state = {**state, "status": "installed" if success else "failed",
                     "message": ("AquaHub a été mis à jour. Fermez puis rouvrez la fenêtre pour charger cette version."
                                 if success else "L’installation d’AquaHub a été interrompue. Consultez l’historique et relancez une installation vérifiée ; aucun redémarrage automatique n’a été effectué."),
                     "updated_at": datetime.now(timezone.utc).isoformat()}
            _atomic_json(self.root / "status.json", state)
            self.app.store.event("app_update_recovered", "État d’une installation AquaHub interrompue vérifié.",
                                 details={"version": state.get("version"), "status": state["status"]})
        except (OSError, subprocess.SubprocessError):
            return state
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return state

    def installation_pending(self):
        with self.lock:
            state = self._recover_installation(self._read("status.json"))
        return bool(state and state.get("status") in PENDING)

    def _status(self):
        state = self._recover_installation(self._read("status.json"))
        ready = self._read("ready.json")
        download = None
        if ready and isinstance(ready.get("version"), str):
            try:
                newer = version_tuple(ready["version"]) > version_tuple(__version__)
            except ApiError:
                newer = False
            path = self.root / "package.deb"
            usable = newer and path.is_file() and not path.is_symlink() and path.stat().st_size == ready.get("bytes")
            download = {"version": ready["version"], "ready": bool(usable), "sha256": ready.get("sha256")}
        result = {"current_version": __version__, "repository": REPOSITORY,
                  "available": self.release is not None, "message": self.message,
                  "download": download, "installation": None}
        if self.release:
            result["release"] = {key: value for key, value in self.release.items() if not key.startswith("_")}
        if state:
            result["installation"] = {key: state.get(key) for key in ("status", "version", "message", "updated_at")}
        return result

    def status(self, params, context=None):
        _params(params, set())
        with self.lock:
            return self._status()

    def _request(self, url, *, binary=False):
        # No supplier or user token is ever attached to this public channel.
        request = urllib.request.Request(url, headers={
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
            "User-Agent": "AquaHub/" + __version__, "X-GitHub-Api-Version": "2022-11-28"})
        try:
            return self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                message = "Le service de mise à jour limite temporairement les recherches. Réessayez ultérieurement."
            elif error.code == 404:
                message = "La version AquaHub demandée n’est plus publiée ou n’est pas accessible."
            else:
                message = "Le service de mise à jour n’a pas fourni la version AquaHub demandée."
            raise ApiError("app_update_source", message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError("app_update_network", "Impossible de joindre le service de mise à jour. Vérifiez la connexion Internet.") from None

    def _json(self, url):
        try:
            with self._request(url) as response:
                data = response.read(MAX_METADATA + 1)
            if len(data) > MAX_METADATA:
                raise ValueError("metadata limit")
            return _strict_json(data)
        except (ValueError, UnicodeError):
            raise ApiError("app_update_metadata", "Les informations de version AquaHub sont invalides.") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ApiError("app_update_network", "La lecture des informations de mise à jour a été interrompue. Réessayez la recherche.") from None

    @staticmethod
    def _candidate(release):
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            return None
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            return None
        version = tag.removeprefix("v")
        try:
            version_tuple(version)
        except ApiError:
            return None
        if not _positive(release.get("id")):
            return None
        name = f"aquahub-application_{version}_amd64.deb"
        assets = release.get("assets")
        if not isinstance(assets, list) or len(assets) > 100:
            return None
        matching = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
        if len(matching) != 1:
            return None
        asset = matching[0]
        digest = DIGEST.fullmatch(asset.get("digest", "")) if isinstance(asset.get("digest"), str) else None
        if (not digest or not _positive(asset.get("id")) or not _positive(asset.get("size")) or
                asset["size"] > MAX_PACKAGE or asset.get("state") != "uploaded"):
            return None
        url = API + "/releases/assets/" + str(asset["id"])
        browser = REPOSITORY + "/releases/download/" + urllib.parse.quote(tag, safe="") + "/" + name
        if asset.get("url") != url or asset.get("browser_download_url") != browser:
            return None
        return {"version": version, "tag": tag, "notes": str(release.get("body") or "")[:64000],
                "published_at": release.get("published_at"), "release_id": release["id"],
                "asset_id": asset["id"], "asset_name": name, "asset_size": asset["size"],
                "sha256": digest[1], "_url": url}

    def check(self, params, context=None):
        _params(params, set())
        releases = self._json(API + "/releases?per_page=20")
        if not isinstance(releases, list) or len(releases) > 20:
            raise ApiError("app_update_metadata", "La liste des versions AquaHub est invalide.")
        candidates = [candidate for release in releases if (candidate := self._candidate(release)) and
                      version_tuple(candidate["version"]) > version_tuple(__version__)]
        with self.lock:
            self.release = max(candidates, key=lambda item: version_tuple(item["version"]), default=None)
            self.checked_at = time.monotonic()
            if self.release:
                self.message = "Une nouvelle version de l’application AquaHub est disponible."
            elif not releases:
                self.message = "Aucune nouvelle version d’AquaHub n’est encore publiée."
            else:
                self.message = "Aucune nouvelle mise à jour compatible n’est disponible."
            return self._status()

    def _fresh(self, expected):
        release = self._json(API + "/releases/" + str(expected["release_id"]))
        candidate = self._candidate(release)
        keys = ("release_id", "asset_id", "version", "asset_name", "asset_size", "sha256", "_url")
        if not candidate or any(candidate[key] != expected[key] for key in keys):
            raise ApiError("app_update_changed", "La livraison AquaHub a changé. Recherchez et vérifiez de nouveau la version.")
        return candidate

    def _field(self, path, field):
        try:
            result = self.runner(["/usr/bin/dpkg-deb", "--field", str(path), field],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=20, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ApiError("app_update_package", "Le paquet AquaHub n’a pas pu être vérifié.") from None
        if result.returncode or len(result.stdout) > 4096:
            raise ApiError("app_update_package", "Le paquet AquaHub n’a pas pu être vérifié.")
        return result.stdout.strip()

    def _package(self, path, version, digest, size):
        if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or
                path.stat().st_mode & 0o022 or path.stat().st_size != size):
            raise ApiError("app_update_package", "Le fichier de mise à jour AquaHub est absent ou a changé.")
        with path.open("rb") as content:
            actual = hashlib.file_digest(content, "sha256").hexdigest()
        if actual != digest:
            raise ApiError("app_update_integrity", "L’empreinte du paquet AquaHub ne correspond pas à la version publiée.")
        expected = {"Package": PACKAGE, "Version": version, "Architecture": "amd64", "X-AquaHub-Update": "application-only"}
        if any(self._field(path, key) != value for key, value in expected.items()):
            raise ApiError("app_update_package", "Ce fichier n’est pas une mise à jour d’application AquaHub compatible. Les kits complets et bundles ne sont pas acceptés ici.")

    def download(self, params, context=None):
        _params(params, {"release_id", "asset_id"})
        with self.lock:
            expected = dict(self.release) if self.release else None
            if (not expected or time.monotonic() - self.checked_at > TTL or
                    any(not _positive(params.get(key)) or params[key] != expected[key] for key in ("release_id", "asset_id"))):
                raise ApiError("app_update_expired", "Recherchez une version AquaHub avant de la télécharger.")

        def work():
            candidate = self._fresh(expected)
            size = candidate["asset_size"]
            if shutil.disk_usage(self.root).free < size * 3 + 32 * 1024**2:
                raise ApiError("disk_space", "Espace insuffisant pour préparer la mise à jour AquaHub.")
            partial = self.root / ("download-" + uuid.uuid4().hex + ".part")
            descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            received = 0
            digest = hashlib.sha256()
            try:
                with os.fdopen(descriptor, "wb") as output, self._request(candidate["_url"], binary=True) as response:
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != size):
                        raise ApiError("app_update_integrity", "La taille du téléchargement AquaHub est inattendue.")
                    self.app.report_progress({"phase": "download", "label": "Téléchargement d’AquaHub " + candidate["version"],
                        "completed": 0, "total": size, "percent": 0})
                    last = time.monotonic()
                    while block := response.read(min(1024**2, size - received + 1)):
                        received += len(block)
                        if received > size:
                            raise ApiError("app_update_integrity", "Le téléchargement dépasse la taille annoncée.")
                        output.write(block)
                        digest.update(block)
                        if time.monotonic() - last >= .25 or received == size:
                            self.app.report_progress({"phase": "download", "label": "Téléchargement d’AquaHub " + candidate["version"],
                                "completed": received, "total": size, "percent": received * 100 // size})
                            last = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                if received != size or digest.hexdigest() != candidate["sha256"]:
                    raise ApiError("app_update_integrity", "Le téléchargement AquaHub est incomplet ou son empreinte est incorrecte.")
                self._package(partial, candidate["version"], candidate["sha256"], size)
                with self.lock:
                    partial.replace(self.root / "package.deb")
                    ready = {"schema": 1, "version": candidate["version"], "sha256": candidate["sha256"], "bytes": size,
                             "release": candidate}
                    _atomic_json(self.root / "ready.json", ready)
                return {"downloaded": True, "version": candidate["version"], "ready": True, "sha256": candidate["sha256"]}
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                raise ApiError("app_update_network", "Le téléchargement AquaHub a été interrompu. Aucun paquet incomplet ne sera installé.") from None
            finally:
                partial.unlink(missing_ok=True)

        return self.app.submit_job("app.download", "AquaHub " + expected["version"], {"version": expected["version"]}, work)

    def _launch(self):
        if os.geteuid() != 0 or self.root != STAGE:
            raise ApiError("app_update_installation", "L’installation requiert le service système AquaHub installé.")
        try:
            result = self.runner(["/usr/bin/systemd-run", "--unit=aquahub-application-upgrade", "--collect",
                "--property=Type=exec", "/usr/bin/python3", "/usr/lib/aquahub/application-upgrade.py"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ApiError("app_update_installation", "Le programme d’installation d’AquaHub n’a pas démarré.") from None
        if result.returncode:
            raise ApiError("app_update_installation", "Le programme d’installation d’AquaHub n’a pas démarré. Une précédente installation peut encore être active.")

    def install(self, params, context=None):
        _params(params, {"version", "confirmed"})
        if params.get("confirmed") is not True:
            raise ApiError("confirmation_required", "Confirmez la mise à jour de l’application AquaHub.")
        target = params.get("version")
        version_tuple(target)
        with self.lock:
            state = self._recover_installation(self._read("status.json"))
            if state and state.get("status") in PENDING:
                if state.get("version") != target:
                    raise ApiError("maintenance", "Une installation d’AquaHub est déjà en cours.")
                return {"scheduled": True, "version": target, "installation": state}
            ready = self._read("ready.json")
            if not ready or ready.get("version") != target or version_tuple(target) <= version_tuple(__version__):
                raise ApiError("app_update_not_ready", "Téléchargez et vérifiez cette version d’AquaHub avant de l’installer.")
            if not self.app._mutation_lock.acquire(blocking=False):
                raise ApiError("busy", "Attendez la fin de la maintenance avant de mettre AquaHub à jour.")
            descriptor = None
            try:
                import fcntl
                descriptor = os.open(self.app._maintenance_path, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ApiError("maintenance", "Une installation d’AquaHub est déjà en cours.") from None
                if self.app.store.active_job():
                    raise ApiError("busy", "Une tâche AquaHub n’est pas terminée.")
                candidate = self._fresh(ready["release"])
                self._package(self.root / "package.deb", target, candidate["sha256"], candidate["asset_size"])
                pending = {"schema": 1, "version": target, "sha256": candidate["sha256"], "bytes": candidate["asset_size"]}
                _atomic_json(self.root / "pending.json", pending)
                state = {"status": "scheduled", "version": target, "message": "Mise à jour préparée. AquaHub va redémarrer ; les services du hub restent indépendants.", "updated_at": datetime.now(timezone.utc).isoformat()}
                _atomic_json(self.root / "status.json", state)
                try:
                    self.launcher()
                except Exception as error:
                    state.update(status="failed", message=redact(str(error), 1000), updated_at=datetime.now(timezone.utc).isoformat())
                    _atomic_json(self.root / "status.json", state)
                    raise
                self.app.store.event("app_update_scheduled", "Installation de l’application AquaHub demandée.", details={"version": target})
                return {"scheduled": True, "version": target, "installation": state}
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                self.app._mutation_lock.release()


def register(app):
    manager = AppUpdates(app)
    for name in ("status", "check", "download", "install"):
        app.register_handler("app_updates." + name, getattr(manager, name), mutation=name in {"download", "install"})
    app.app_updates = manager
    return manager
