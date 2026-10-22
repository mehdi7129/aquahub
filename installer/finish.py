#!/usr/bin/python3
"""Complete an offline installation after the authenticated APT stage.

Only root-owned, package-installed resources are accepted. Existing Aqua Hub
files, volumes, systemd units and image tags are adopted without replacement.
No drone provisioning, IP configuration, network cleanup or Docker upgrades
are performed here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

VERSION = "0.2.5"
PROJECT = "aqua-hub-bundle"
SERVICES = {"mosquitto", "dmx_fleet_controller", "gcs", "gcs_reactor", "net_setup", "mavlink_router", "rtcm_mavlink", "wireguard"}
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "HOME": "/root", "DOCKER_CONFIG": "/etc/aquahub/docker-client"}
MAX_BUNDLE_BYTES = 8 * 1024**3


class SetupError(Exception):
    pass


def verified_archive_identities(archive: Path, images: list[dict]) -> dict:
    """Bind each exact reference to cryptographically checked archive descriptors.

    Docker's classic image store reports the config digest, while its containerd
    store reports a manifest/index digest. Both are valid only when the archive
    proves that they identify the same exact tagged image and platform.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    from aquahub_agent.image_identity import archive_identities, canonical_reference
    if not isinstance(images, list) or not images:
        raise SetupError("Le kit ne définit pas les identités d’image attendues.")
    archive_hashes = {image.get("archive_sha256") for image in images}
    if len(archive_hashes) != 1 or not isinstance(next(iter(archive_hashes)), str) or len(next(iter(archive_hashes))) != 64:
        raise SetupError("L’empreinte de l’archive d’images est absente ou ambiguë.")
    verify_resource(archive, archive_hashes.pop())
    try:
        mapping = archive_identities(archive)
        verified = {}
        for image in images:
            reference = canonical_reference(image["reference"])
            proof = mapping.get(reference)
            if reference in verified or proof is None or image["image_id"] not in proof["identities"]:
                raise ValueError("Unexpected reference identity")
            if image.get("archive_identity") != proof:
                raise ValueError("Archive identity metadata mismatch")
            if proof.get("platform") != {"os": "linux", "architecture": "amd64"}:
                raise ValueError("Unexpected image platform")
            verified[reference] = proof
        return verified
    except (KeyError, TypeError, ValueError):
        raise SetupError("Les identités d’images du kit ne correspondent pas à l’archive authentifiée.") from None


def image_matches_archive(reference: str, actual: str, proofs: dict) -> bool:
    from aquahub_agent.image_identity import canonical_reference
    proof = proofs.get(canonical_reference(reference))
    return proof is not None and actual in proof["identities"]


def ensure_image_archive(archive: Path, images: list[dict], timeout=900) -> dict:
    proofs = verified_archive_identities(archive, images)
    # Resume a partially completed installation without decompressing every image
    # again. Existing images are reused only after proving their exact identity.
    existing = {}
    for image in images:
        try:
            actual = docker(["image", "inspect", "--format", "{{.Id}}", image["reference"]]).strip()
        except SetupError:
            break
        if not image_matches_archive(image["reference"], actual, proofs):
            break
        existing[image["reference"]] = actual
    if len(existing) == len(images):
        return existing
    docker(["image", "load", "--input", str(archive)], timeout=timeout)
    result = {}
    for image in images:
        actual = docker(["image", "inspect", "--format", "{{.Id}}", image["reference"]]).strip()
        if not image_matches_archive(image["reference"], actual, proofs):
            raise SetupError("Une image chargée ne correspond pas à sa référence dans la livraison authentifiée.")
        result[image["reference"]] = actual
    return result


def progress(phase, message):
    print(json.dumps({"phase": phase, "message": message}, ensure_ascii=False), flush=True)


def run(args, timeout=120, *, capture=True):
    try:
        result = subprocess.run(args, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("Une commande système n’a pas pu se terminer. L’installation peut être reprise.") from error
    if result.returncode:
        # Do not include potentially secret-bearing Docker/config output.
        raise SetupError(f"L’étape système {Path(args[0]).name} a échoué (code {result.returncode}).")
    return result.stdout or ""


def docker(args, **kwargs):
    return run(["/usr/bin/docker", "--context", "default", *args], **kwargs)


def read_json(path):
    try:
        if path.stat().st_size > 1024**2:
            raise ValueError("too large")
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SetupError("Les métadonnées du kit sont absentes ou invalides.") from error


def verify_resource(path, expected=None):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SetupError("Une ressource du kit ne possède pas les permissions attendues.")
    if expected:
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise SetupError("L’intégrité d’une ressource du kit n’est pas valide.")


def existing_bundle():
    ids = docker(["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
    if ids:
        rows = [json.loads(line) for line in docker(["inspect", "--format", "{{json .Config.Labels}}", *ids]).splitlines()]
        paths = {row.get("com.docker.compose.project.working_dir") for row in rows}
        if len(paths) != 1 or None in paths:
            raise SetupError("Le bundle installé possède plusieurs emplacements. Aucun déplacement n’a été effectué.")
        target = Path(paths.pop())
        if not target.is_absolute() or not (target / "docker-compose.yml").is_file():
            raise SetupError("Le répertoire du bundle existant est introuvable. Réparez son emplacement avant de poursuivre.")
        return target.resolve()
    unit = Path("/etc/systemd/system/aqua-hub.service")
    if unit.is_file():
        output = run(["systemctl", "show", "aqua-hub.service", "--property=WorkingDirectory", "--value"])
        candidate = Path(output.strip())
        if candidate.is_absolute() and (candidate / "docker-compose.yml").is_file():
            return candidate.resolve()
        raise SetupError("Un service Aqua Hub existe mais son répertoire est invalide. Son unité n’a pas été remplacée.")
    return None


def extract_bundle(archive: Path, destination: Path) -> Path:
    """Extract regular members into a private staging area, then publish once."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if destination.exists():
        if (destination / "docker-compose.yml").is_file():
            return destination
        raise SetupError("Le chemin du bundle est déjà utilisé par un contenu inattendu.")
    stage = Path(tempfile.mkdtemp(prefix=".aquahub-install-", dir=destination.parent))
    os.chmod(stage, 0o700)
    try:
        with tarfile.open(archive, "r:*") as handle:
            members = handle.getmembers()
            if len(members) > 20000 or sum(m.size for m in members) > MAX_BUNDLE_BYTES:
                raise SetupError("Le bundle dépasse les limites du format pris en charge.")
            roots = []
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                    raise SetupError("Le bundle contient un chemin ou un type de fichier non autorisé.")
                if path.name == "docker-compose.yml":
                    roots.append(path.parent)
            if len(roots) != 1:
                raise SetupError("Le bundle doit contenir exactement une définition Docker Compose.")
            root = roots[0]
            for member in members:
                path = PurePosixPath(member.name)
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                target = stage.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = handle.extractfile(member)
                    if source is None:
                        raise SetupError("Un fichier de livraison ne peut pas être extrait.")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024**2)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
        required = ["docker-compose.yml", "deploy.py", "mavlink-config-generator/main.py"]
        if not all((stage / item).is_file() for item in required):
            raise SetupError("Le bundle est incomplet.")
        stage.rename(destination)
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def write_atomic(path: Path, content: str, mode=0o600):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".aquahub-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(filename, mode)
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


def port_free(port):
    with socket.socket() as stream:
        try:
            stream.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def install_portainer(resources: Path, kit: dict):
    for name in ("aqua-portainer", "portainer"):
        rows = docker(["ps", "-aq", "--filter", f"name=^/{name}$"]).split()
        if rows:
            data = json.loads(docker(["inspect", rows[0]]))[0]
            if not data["State"]["Running"]:
                docker(["start", rows[0]])
            return {"name": name, "adopted": True}
    if not port_free(9443):
        raise SetupError("Le port local 9443 est déjà utilisé. Aucun service existant n’a été arrêté.")
    metadata = kit["portainer"]
    archive = resources / "portainer.tar.gz"
    reference = metadata["reference"]
    actual = ensure_image_archive(archive, [metadata], timeout=300)[reference]
    docker(["volume", "create", "aqua-portainer-data"])
    docker(["run", "-d", "--pull", "never", "--name", "aqua-portainer", "--restart", "unless-stopped",
            "-p", "127.0.0.1:9443:9443", "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", "aqua-portainer-data:/data", reference])
    return {"name": "aqua-portainer", "adopted": False, "image_id": actual}


def install_agent(operator_uid: int, bundle: Path):
    state = Path("/var/lib/aquahub")
    state.mkdir(exist_ok=True, mode=0o755)
    state.chmod(0o755)
    Path("/etc/aquahub").mkdir(exist_ok=True, mode=0o755)
    write_atomic(Path("/etc/aquahub/operator.conf"), f"AQUAHUB_OPERATOR_UID={operator_uid}\n", 0o600)
    # The unit is AquaHub's own unit. The supplier's aqua-hub.service is untouched.
    unit = f"""[Unit]
Description=AquaHub local maintenance agent
After=docker.service

[Service]
Type=simple
Environment=PYTHONPATH=/usr/lib/aquahub/agent
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=/etc/aquahub/operator.conf
ExecStart=/usr/bin/python3 -m aquahub_agent --socket /run/aquahub/agent.sock --state-dir /var/lib/aquahub/agent --bundle {bundle} --allow-uid ${{AQUAHUB_OPERATOR_UID}}
Restart=on-failure
RestartSec=3
RuntimeDirectory=aquahub
RuntimeDirectoryMode=0755
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
"""
    # Paths with whitespace need systemd's argument quoting, and never shell quoting.
    unit = unit.replace(f"--bundle {bundle}", "--bundle " + json.dumps(str(bundle)))
    write_atomic(Path("/etc/systemd/system/aquahub-agent.service"), unit, 0o644)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "aquahub-agent.service"])
    run(["systemctl", "try-restart", "aquahub-agent.service"])


def service_readiness(required: bool, timeout=150):
    """Confirm startup without inventing probes absent from supplier images."""
    deadline=time.monotonic()+timeout
    template=('{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
              '"running":{{json .State.Running}},"state":{{json .State.Status}},'
              '"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}}}')
    while True:
        ids=docker(["ps","-aq","--filter",f"label=com.docker.compose.project={PROJECT}"]).split()
        rows=[json.loads(line) for line in docker(["inspect","--format",template,*ids]).splitlines()] if ids else []
        services={row['service']:row for row in rows}
        if set(services)!=SERVICES:
            raise SetupError("Tous les composants attendus du bundle ne sont pas présents. Consultez le détail de l’installation.")
        ready=all(row['running'] and row['health'] in (None,'healthy') for row in rows)
        if ready or not required:
            return {'ready':ready,'services':rows,'healthcheck_notice':'Les composants sans sonde restent sans validation de santé.'}
        if time.monotonic()>=deadline:
            raise SetupError("Le démarrage complet du bundle n’est pas confirmé. Les fichiers sont conservés ; consultez le détail puis reprenez l’installation.")
        time.sleep(2)


def render_new_bundle_unit(template: str, bundle: Path) -> str:
    # Compose's implicit project is the directory basename. Pin the supplier's
    # existing project name when installing into our stable /opt/.../bundle path.
    # Never apply this renderer to an adopted customer's existing unit.
    if "__AQUA_HUB_BUNDLE_DIR__" not in template or "/usr/bin/docker compose" not in template:
        raise SetupError("L’unité du bundle ne correspond pas au format pris en charge.")
    return template.replace("__AQUA_HUB_BUNDLE_DIR__", str(bundle)).replace(
        "/usr/bin/docker compose", "/usr/bin/docker --context default compose --project-name aqua-hub-bundle")


def finish(resources: Path, operator_uid: int):
    if os.geteuid() != 0:
        raise SetupError("L’installation requiert l’autorisation administrateur du système.")
    account = pwd.getpwuid(operator_uid)
    if operator_uid == 0 or account.pw_shell in {"/usr/sbin/nologin", "/bin/false"}:
        raise SetupError("Le compte opérateur doit être une session utilisateur ordinaire.")
    verify_resource(resources / "kit.json")
    kit = read_json(resources / "kit.json")
    if kit.get("schema") != 1 or kit.get("product") != "aquahub-kit" or kit.get("platform") != "debian13/amd64":
        raise SetupError("Ce kit n’est pas compatible avec le poste.")
    if os.uname().machine != "x86_64" or 'VERSION_ID="13"' not in Path("/etc/os-release").read_text():
        raise SetupError("Ce kit est destiné à Debian 13 amd64.")
    required = kit.get("requirements", {}).get("free_bytes", 8 * 1024**3)
    if not isinstance(required, int) or not 1024**3 <= required <= 128 * 1024**3:
        raise SetupError("Les exigences d’espace du kit sont invalides.")
    if shutil.disk_usage("/var/lib").free < required:
        raise SetupError("L’espace disque libre est insuffisant pour les images, le bundle et les sauvegardes.")
    progress("docker", "Vérification de Docker natif…")
    run(["systemctl", "enable", "--now", "docker.service"])
    docker(["info", "--format", "{{json .ServerVersion}}"])
    bundle = existing_bundle()
    adopted = bundle is not None
    if bundle is None:
        progress("bundle", "Installation du bundle et de ses images locales…")
        image_archive = resources / "images.tar.gz"
        ensure_image_archive(image_archive, kit["images"])
        archive = resources / "bundle.tar.gz"
        verify_resource(archive, kit.get("bundle", {}).get("sha256"))
        bundle = extract_bundle(archive, Path("/opt/aqua-hub/bundle"))
        # No newly generated IP or drone setting is introduced.
        if not (bundle / ".env").exists() and (bundle / ".env.example").is_file():
            shutil.copyfile(bundle / ".env.example", bundle / ".env")
            (bundle / ".env").chmod(0o600)
        unit = Path("/etc/systemd/system/aqua-hub.service")
        if unit.exists():
            raise SetupError("Un service Aqua Hub est apparu pendant l’installation. Son contenu a été conservé.")
        template = (bundle / "aqua-hub.service").read_text()
        content = render_new_bundle_unit(template, bundle)
        write_atomic(unit, content, 0o644)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "aqua-hub.service"], timeout=240)
    else:
        progress("bundle", "Installation Aqua Hub existante conservée sur place.")
    progress("portainer", "Vérification de Portainer…")
    portainer = install_portainer(resources, kit)
    progress("agent", "Installation du suivi AquaHub…")
    install_agent(operator_uid, bundle)
    progress("checks", "Vérification du démarrage des composants…")
    readiness=service_readiness(not adopted or bundle==Path('/opt/aqua-hub/bundle'))
    if not readiness['ready']:
        progress("warning", "Application installée sur le bundle existant. Certains services nécessitent votre attention dans AquaHub.")
    record = {"version": VERSION, "installed_at": datetime.now(timezone.utc).isoformat(), "operator_uid": operator_uid,
              "bundle_path": str(bundle), "bundle_adopted": adopted, "private_kit": kit.get("private", False), "portainer": portainer,
              "readiness":readiness}
    write_atomic(Path("/var/lib/aquahub/installer/installation.json"), json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    write_atomic(Path("/var/lib/aquahub/setup.complete"), VERSION + "\n", 0o644)
    progress("complete", "AquaHub est installé. Ouvrez l’application pour consulter l’installation et initialiser Portainer si nécessaire.")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finalisation locale du kit AquaHub")
    parser.add_argument("--operator-uid", required=True, type=int)
    current_kit = Path("/usr/share/aquahub/offline")
    preserved_kit = Path("/var/lib/aquahub/offline")
    parser.add_argument("--resources", type=Path, default=current_kit if current_kit.exists() else preserved_kit)
    args = parser.parse_args(argv)
    try:
        finish(args.resources, args.operator_uid)
        return 0
    except (SetupError, OSError, ValueError, KeyError) as error:
        # Error messages are controlled; never echo the contents of bundle files.
        message = str(error) if isinstance(error, SetupError) else "L’installation n’a pas pu être terminée. Les données existantes sont conservées."
        progress("error", message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
