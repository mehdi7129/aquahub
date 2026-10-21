"""Existing-bundle Aqua_Drones adapter; never assigns an address.

Network discovery is a bounded TCP check of six known bundle destinations.
Configuration needs pinned SSH, a stable identity, unchanged network fields,
private backups, and the recognised supplier script, run without modification.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .security import ApiError, redact
from .network import stable_network as _stable_network
from .updates import ArchiveReader, KNOWN_FILES, _atomic_json, _params, _private_dir, sha256_file

PREFLIGHT_TTL = 600
DEPLOY_TIMEOUT = 600
REBOOT_TIMEOUT = 300
CONFIGURATION_STEPS = 12
MAX_READ = 2 * 1024**2
REMOTE_FILES = ("/etc/mavlink-router/main.conf", "/etc/wireguard/wg0.conf", "/etc/dmx/main.config")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SECTIONS = ("MACHINE", "BOOT", "MAVLINK", "WG_HASH", "MQTT", "ADDR", "ROUTE", "SERVICES")
REMOTE_READ = (
    "set -eu; printf '__AQ_MACHINE__\\n'; cat /etc/machine-id; "
    "printf '\\n__AQ_BOOT__\\n'; cat /proc/sys/kernel/random/boot_id; "
    "printf '\\n__AQ_MAVLINK__\\n'; cat /etc/mavlink-router/main.conf; "
    "printf '\\n__AQ_WG_HASH__\\n'; sha256sum /etc/wireguard/wg0.conf; "
    "printf '\\n__AQ_MQTT__\\n'; grep '^MQTT_SERVER=' /etc/dmx/main.config; "
    "printf '\\n__AQ_ADDR__\\n'; ip -j address show; "
    "printf '\\n__AQ_ROUTE__\\n'; ip -j route show table all; "
    "printf '\\n__AQ_SERVICES__\\n'; systemctl list-units --type=service "
    "--state=active --plain --no-legend --no-pager; printf '\\n__AQ_END__\\n'"
)


def drone_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 6:
        raise ApiError("unsupported_drone", "Le bundle actuel fournit les configurations Aqua_Drones 1 à 6 uniquement.")
    return value


def drone_address(number: int) -> str:
    number = drone_number(number)
    # This literal supplier rule is valid only for the six delivered configs.
    return str(ipaddress.IPv4Address(f"192.168.20{number}.2"))


def manual_checklist(number: int) -> list[dict]:
    """Explain the five supplier reminders without claiming they were checked.

    The first two entries describe inspections of the existing installation.
    AquaHub never executes the router/IP instructions from these reminders.
    """
    number = drone_number(number)
    return [
        {"id": "router_lan", "label": "RUTC50 : configuration LAN",
         "details": "Vérifier sur le routeur la configuration LAN et le mode statique déjà installés, sans modifier les adresses IP."},
        {"id": "router_route", "label": "RUTC50 : route vers la GCS",
         "details": "Vérifier la route et le prochain saut déjà installés vers le réseau GCS, sans modifier les IP ni les routes."},
        {"id": "autopilot_parameters", "label": "Paramètres du pilote automatique",
         "details": f"Dans QGroundControl → Analyze Tools → MAVLink Console : vérifier MAV_SYS_ID = {number}, "
                    "EKF2_GPS_POS_Y = distance en mètres entre les centres des antennes, EKF2_GPS_POS_X/Z = 0, "
                    "BT_POS_XY_P/I/D et BT_POS_IMAX selon la configuration, puis la numérotation des moteurs selon les spécifications."},
        {"id": "heron_versions", "label": "Versions Heron",
         "details": "Relever les versions Firmware et BL (bootloader) Heron."},
        {"id": "hardware_status", "label": "État CAN, GPS et moteurs",
         "details": "Dans la console MAVLink de QGroundControl / nsh : contrôler les résultats de uavcan status, gps status et listener esc_status."},
    ]


def network_fields(text: str) -> tuple:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ApiError("invalid_config", "Configuration MAVLink illisible ; aucune écriture autorisée.") from exc
    fields = []
    for section in parser.sections():
        for key, value in parser.items(section):
            if key.lower() in ("address", "clientaddress", "group", "sendgroup"):
                clean = value.strip()
                if key.lower() in ("address", "clientaddress"):
                    try:
                        clean = str(ipaddress.ip_address(clean.strip("[]")))
                    except ValueError as exc:
                        raise ApiError("invalid_config", "Une adresse du fichier MAVLink est invalide.") from exc
                fields.append((section, key.lower(), clean))
    if not fields:
        raise ApiError("invalid_config", "Aucun champ réseau MAVLink n’a été trouvé.")
    return tuple(sorted(fields))


def fleet_numbers(text: str) -> set[int]:
    return {int(value) for value in re.findall(r"\[UdpEndpoint aqua_(\d+)_", text)}


def stable_network(addresses: list, routes: list) -> dict:
    return _stable_network(addresses, routes)


def parse_remote(text: str) -> dict:
    parts = re.split(r"(?:^|\n)__AQ_([A-Z_]+)__\r?\n", text)
    sections = {parts[index]: parts[index + 1].strip() for index in range(1, len(parts) - 1, 2)}
    if set(sections) != set(SECTIONS) | {"END"}:
        raise ApiError("identity_unavailable", "Les contrôles SSH de l’appareil sont incomplets.")
    machine = sections["MACHINE"].lower()
    boot = sections["BOOT"].lower()
    if not re.fullmatch(r"[0-9a-f]{32}", machine) or machine == "0" * 32:
        raise ApiError("identity_unavailable", "Identifiant stable /etc/machine-id absent ou invalide.")
    if not re.fullmatch(r"[0-9a-f-]{36}", boot):
        raise ApiError("identity_unavailable", "Identifiant de démarrage indisponible.")
    wg_hash = sections["WG_HASH"].split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", wg_hash):
        raise ApiError("invalid_config", "Empreinte WireGuard distante indisponible.")
    mqtt_line = sections["MQTT"]
    if not mqtt_line.startswith("MQTT_SERVER=") or "\n" in mqtt_line:
        raise ApiError("invalid_config", "La valeur MQTT existante est absente ou ambiguë.")
    mqtt = mqtt_line.partition("=")[2].strip()
    try:
        values = [str(ipaddress.ip_address(part.strip())) for part in mqtt.split(",")]
    except ValueError as exc:
        raise ApiError("invalid_config", "L’adresse MQTT existante ne peut pas être conservée avec ce contrat.") from exc
    mqtt = ",".join(values)
    try:
        addresses, routes = json.loads(sections["ADDR"]), json.loads(sections["ROUTE"])
        if not isinstance(addresses, list) or not isinstance(routes, list) or not addresses:
            raise ValueError()
        network = stable_network(addresses, routes)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError("network_unknown", "Impossible de comparer les IP et routes actuelles du drone.") from exc
    services = sorted(re.findall(r"(?m)^([A-Za-z0-9_.@-]+\.service)\s", sections["SERVICES"]))
    if not services:
        raise ApiError("services_unknown", "L’état des services embarqués ne peut pas être vérifié.")
    return {"machine_id": machine, "boot_id": boot, "mavlink": sections["MAVLINK"],
            "wg_hash": wg_hash, "mqtt": mqtt, "network": network, "services": services}


def _bounded_run(args, *, timeout=20, env=None, max_bytes=MAX_READ, binary=False, cwd=None):
    """Bound pipes while the process is running, including a non-terminating child."""
    import selectors
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True)
    except OSError as exc:
        raise ApiError("tool_unavailable", "Un outil système nécessaire est indisponible.") from exc
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        with __import__("selectors").DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, output)
            selector.register(proc.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ApiError("ssh_timeout", "Le contrôle n’a pas répondu dans le délai prévu.")
                for key, _ in selector.select(min(.5, deadline - time.monotonic())):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(block)
                    if len(output) + len(errors) > max_bytes:
                        raise ApiError("output_limit", "La réponse du contrôle dépasse la limite autorisée.")
        proc.wait(timeout=max(.01, deadline - time.monotonic()))
        if proc.returncode:
            raise ApiError("ssh_failed", "Le contrôle SSH a échoué : " + redact(errors.decode("utf-8", "replace"), 500))
        return bytes(output) if binary else output.decode("utf-8", "replace")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


class DroneManager:
    def __init__(self, app, runner=None, connector=None):
        self.app = app
        self.root = _private_dir(Path(app.config.state_dir) / "drones")
        self.runner = runner or _bounded_run
        self.connector = connector or socket.create_connection
        self.preflights = {}
        self.observed = {}
        self.lock = threading.RLock()

    def _bundle(self):
        path = self.app.config.bundle_path
        if not path:
            raise ApiError("bundle_missing", "Associer le bundle Aqua Hub existant avant de configurer un appareil.")
        return Path(path)

    def _wg_path(self, number):
        return self._bundle() / "config/wireguard/drones" / f"WireGuard-Server-1_crystal-aqua{number}.conf"

    def _saved(self):
        path = self.root / "inventory.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def list(self, params=None, context=None):
        _params(params or {}, set())
        saved = self._saved()
        drones = []
        for number in range(1, 7):
            entry = saved.get(str(number), {})
            observed = self.observed.get(number, {})
            supported = bool(self.app.config.bundle_path and self._wg_path(number).is_file())
            reachable = observed.get("reachable")
            # A scan result expires; absence of a scan is never a connected state.
            if time.time() - observed.get("checked", 0) > 60:
                reachable = None
            drones.append({"number": number, "name": f"Aqua_Drone_{number}",
                "address": drone_address(number), "status": "reachable" if reachable else "unreachable" if reachable is False else "unknown",
                "reachable": reachable, "identity_verified": bool(entry.get("identity")),
                "identity": entry.get("identity"), "configured": bool(entry.get("configured")),
                "supported": supported, "last_seen": entry.get("last_seen"),
                "manual_checklist": manual_checklist(number),
                "message": "Adresse du bundle en lecture seule ; la présence SSH ne prouve pas l’identité."})
        return {"drones": drones, "address_read_only": True,
                "message": "Recherche limitée aux six adresses déjà prévues par le bundle."}

    def _reachable(self, number):
        try:
            with self.connector((drone_address(number), 22), timeout=1.2):
                return True
        except OSError:
            return False

    def scan(self, params=None, context=None):
        _params(params or {}, set())
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="aqua-probe") as pool:
            results = list(pool.map(self._reachable, range(1, 7)))
        with self.lock:
            for number, reachable in enumerate(results, 1):
                self.observed[number] = {"reachable": reachable, "checked": time.time()}
        return self.list()

    def _recognised(self):
        bundle = self._bundle()
        for relative, expected in KNOWN_FILES.items():
            path = bundle / relative
            if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
                raise ApiError("unsupported_bundle", "Le script fournisseur ou son contrat a changé ; cette version doit être validée avant exécution.")

    def ssh_settings(self, params, context=None):
        path = self.root / "ssh.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        if set(params) - {"known_hosts_file", "identity_file", "allow_none_auth"}:
            raise ApiError("invalid_params", "Réglage SSH inconnu.")
        for key in ("known_hosts_file", "identity_file"):
            if key in params:
                value = params[key]
                if not isinstance(value, str) or (value and (not Path(value).is_absolute() or not Path(value).is_file() or Path(value).is_symlink())):
                    raise ApiError("invalid_path", "Le fichier SSH doit être un fichier local régulier absolu.")
                current[key] = value
        if "allow_none_auth" in params:
            if not isinstance(params["allow_none_auth"], bool):
                raise ApiError("invalid_params", "L’authentification sans secret doit être acceptée explicitement.")
            current["allow_none_auth"] = params["allow_none_auth"]
        if params:
            _atomic_json(path, current)
        return {"known_hosts_file": current.get("known_hosts_file", ""),
                "identity_file": current.get("identity_file", ""),
                "allow_none_auth": bool(current.get("allow_none_auth"))}

    @staticmethod
    def _public_key(raw):
        candidates = []
        for line in raw.splitlines():
            values = line.split()
            if len(values) >= 3 and not line.startswith("#") and not values[0].startswith("@"):
                keytype, key = values[1:3]
                if keytype in ("ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"):
                    try:
                        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key, validate=True)).digest()).decode().rstrip("=")
                    except ValueError:
                        continue
                    candidates.append((keytype, key, fingerprint))
        if not candidates:
            raise ApiError("ssh_identity_untrusted", "Aucune clé SSH exploitable n’est disponible pour cette adresse.")
        candidates.sort(key=lambda item: {"ssh-ed25519": 0, "ecdsa-sha2-nistp256": 1, "ssh-rsa": 2}[item[0]])
        return candidates[0]

    def identity(self, params, context=None):
        _params(params, {"number"})
        number = drone_number(params.get("number"))
        raw = self.runner(["/usr/bin/ssh-keyscan", "-T", "3", "-t", "ed25519", drone_address(number)], timeout=5)
        keytype, key, fingerprint = self._public_key(raw)
        return {"number": number, "address": drone_address(number), "host_key_fingerprint": fingerprint,
                "trusted": False, "message": "Empreinte observée sur le réseau. La confirmer depuis une source physique fiable avant association."}

    def enroll(self, params, context=None):
        _params(params, {"number", "host_key_fingerprint", "confirmed"})
        number = drone_number(params.get("number"))
        if params.get("confirmed") is not True or not isinstance(params.get("host_key_fingerprint"), str):
            raise ApiError("identity_confirmation_required", "Confirmer physiquement l’empreinte SSH présentée avant association.")
        raw = self.runner(["/usr/bin/ssh-keyscan", "-T", "3", "-t", "ed25519", drone_address(number)], timeout=5)
        keytype, key, fingerprint = self._public_key(raw)
        if fingerprint != params["host_key_fingerprint"]:
            raise ApiError("identity_mismatch", "La clé présentée a changé depuis sa vérification.")
        hosts_dir = _private_dir(self.root / "hosts")
        target = hosts_dir / f"drone-{number}.known_hosts"
        with self.lock:
            if target.exists() and self._public_key(target.read_text())[2] != fingerprint:
                raise ApiError("identity_mismatch", "La clé déjà associée a changé ; aucun remplacement automatique n’est autorisé.")
            if not target.exists():
                with target.open("x") as stream:
                    target.chmod(0o600)
                    stream.write(f"{drone_address(number)} {keytype} {key}\n")
        return {"enrolled": True, "number": number, "host_key_fingerprint": fingerprint,
                "message": "Clé SSH associée. L’identité stable de l’appareil sera encore vérifiée avant configuration."}

    def _ssh_credentials(self, number, uid, stage):
        home = Path(pwd.getpwuid(uid).pw_dir)
        settings = self.ssh_settings({})
        enrolled = self.root / "hosts" / f"drone-{number}.known_hosts"
        hosts = enrolled if enrolled.is_file() else Path(settings["known_hosts_file"]) if settings["known_hosts_file"] else home / ".ssh/known_hosts"
        address = drone_address(number)
        if not hosts.is_file():
            raise ApiError("ssh_identity_untrusted", "La clé SSH de cet appareil doit déjà être approuvée dans le compte opérateur.")
        raw = self.runner(["/usr/bin/ssh-keygen", "-F", address, "-f", str(hosts)], timeout=5)
        keytype, key, fingerprint = self._public_key(raw)
        pinned = stage / "known_hosts"
        pinned.write_text(f"{address} {keytype} {key}\n")
        pinned.chmod(0o600)
        options = ["-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={pinned}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=2",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "IdentitiesOnly=yes", "-o", "HostKeyAlgorithms=" + ("rsa-sha2-512,rsa-sha2-256" if keytype == "ssh-rsa" else keytype)]
        found = False
        candidates = [Path(settings["identity_file"])] if settings["identity_file"] else [home / ".ssh" / name for name in ("id_ed25519", "id_ecdsa", "id_rsa")]
        for identity in candidates:
            if identity.is_file() and not identity.is_symlink():
                mode = identity.stat()
                if mode.st_uid not in (uid, 0) or mode.st_mode & 0o077:
                    continue
                options += ["-i", str(identity)]
                found = True
        if not found and not settings["allow_none_auth"]:
            raise ApiError("ssh_credentials_missing", "Aucune clé privée SSH locale utilisable en mode non interactif n’a été trouvée. Aucun mot de passe ne sera enregistré.")
        return {"options": options, "fingerprint": fingerprint, "host": "root@" + address,
                "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}}

    def _remote(self, credentials):
        raw = self.runner(["/usr/bin/ssh", *credentials["options"], credentials["host"], REMOTE_READ],
                          timeout=25, env=credentials["env"])
        data = parse_remote(raw)
        data["host_key_fingerprint"] = credentials["fingerprint"]
        return data

    def _hub_network(self):
        addresses = json.loads(self.runner(["/usr/sbin/ip", "-j", "-d", "address", "show"], timeout=5))
        routes = json.loads(self.runner(["/usr/sbin/ip", "-j", "route", "show", "table", "all"], timeout=5))
        return stable_network(addresses, routes)

    def _hashes(self, number):
        paths = [*KNOWN_FILES, "config/mavlink/config.conf", ".env",
                 str(self._wg_path(number).relative_to(self._bundle()))]
        result = {}
        for relative in paths:
            path = self._bundle() / relative
            if path.is_symlink():
                raise ApiError("unsafe_path", "Lien symbolique interdit dans les fichiers de configuration.")
            if path.is_file():
                result[relative] = sha256_file(path)
            elif relative != ".env":
                raise ApiError("configuration_missing", "Un fichier requis du bundle est absent.")
        return result

    def _copy_verified_code(self, relative, target):
        """Execute the same bytes that were checked, from a private directory."""
        descriptor = os.open(self._bundle() / relative, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ApiError("unsafe_path", "Le code fournisseur doit être un fichier régulier.")
            content = stream.read(MAX_READ + 1)
        if len(content) > MAX_READ or hashlib.sha256(content).hexdigest() != KNOWN_FILES[relative]:
            raise ApiError("unsupported_bundle", "Le code fournisseur a changé pendant sa préparation.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            target.chmod(0o600)
            stream.write(content)

    def preflight(self, params, context=None):
        # Read-only SSH and private local staging need no administrator prompt,
        # but must not overlap a configuration, bundle update or app upgrade.
        with self.app.operation_guard():
            return self._preflight(params, context)

    def _preflight(self, params, context=None):
        _params(params, {"number"})
        number = drone_number(params.get("number"))
        token = uuid.uuid4().hex
        stage = _private_dir(self.root / token)
        checks = []
        identity = None
        try:
            self._recognised()
            checks.append({"label": "Bundle fournisseur reconnu", "ok": True})
            if not self._wg_path(number).is_file():
                raise ApiError("configuration_missing", "Configuration WireGuard absente pour ce numéro.")
            uid = getattr(context, "uid", os.getuid())
            credentials = self._ssh_credentials(number, uid, stage)
            remote = self._remote(credentials)
            identity = {"machine_id": remote["machine_id"], "host_key_fingerprint": remote["host_key_fingerprint"]}
            saved = self._saved().get(str(number), {})
            if saved.get("identity") and saved["identity"] != identity:
                raise ApiError("identity_mismatch", "L’identité de cet appareil a changé. Association existante conservée ; intervention requise.")
            checks.append({"label": "Clé SSH approuvée et identité stable lues", "ok": True})
            bundle = self._bundle()
            generator = stage / "generator/main.py"
            self._copy_verified_code("mavlink-config-generator/main.py", generator)
            generated_hub, generated_drone = stage / "hub.conf", stage / "drone.conf"
            self.runner([sys.executable, "-I", "-B", str(generator), "f::1", str(number), str(generated_hub)], timeout=10, env=credentials["env"])
            self.runner([sys.executable, "-I", "-B", str(generator), "drone", str(number), str(generated_drone)], timeout=10, env=credentials["env"])
            current_hub = (bundle / "config/mavlink/config.conf").read_text()
            future_hub, future_drone = generated_hub.read_text(), generated_drone.read_text()
            generated_hub.chmod(0o600)
            generated_drone.chmod(0o600)
            if not fleet_numbers(current_hub).issubset(fleet_numbers(future_hub)):
                raise ApiError("fleet_reduction", "Le générateur réduirait la flotte du hub. Choisir ce numéro seul est refusé.")
            checks.append({"label": "Flotte du hub conservée", "ok": True})
            if network_fields(current_hub) != network_fields(future_hub):
                raise ApiError("ip_change_forbidden", "La configuration générée changerait un champ réseau du hub. Aucune IP ne sera modifiée.")
            if network_fields(remote["mavlink"]) != network_fields(future_drone):
                raise ApiError("ip_change_forbidden", "La configuration générée changerait un champ réseau du drone. Action bloquée.")
            if sha256_file(self._wg_path(number)) != remote["wg_hash"]:
                raise ApiError("wireguard_mismatch", "La configuration WireGuard livrée diffère du fichier installé ; son application n’est pas autorisée.")
            checks.append({"label": "IP, groupes MAVLink et WireGuard inchangés", "ok": True})
            checks.append({"label": "Valeur MQTT existante conservée", "ok": True, "message": remote["mqtt"]})
            required = {name for name in remote["services"] if "mavlink" in name.lower() or "dmx" in name.lower() or
                        name in ("wg0.service", "mqtt-bridge.service", "artnet-node.service", "enttec.service")}
            if not {"mavlink-router.service", "wg0.service", "mqtt-bridge.service", "dmx-scheduler.service"}.issubset(required):
                raise ApiError("services_unknown", "Les services MAVLink, WireGuard, MQTT et DMX attendus doivent être actifs avant configuration.")
            hashes, hub_network = self._hashes(number), self._hub_network()
            plan = {"number": number, "stage": stage, "credentials": credentials, "remote": remote,
                "identity": identity, "hashes": hashes, "hub_network": hub_network,
                "generated_hub_sha": sha256_file(generated_hub), "generated_drone_sha": sha256_file(generated_drone),
                "created": time.time(), "uid": uid, "required_services": sorted(required), "used": False}
            with self.lock:
                self.preflights[token] = plan
                self.observed[number] = {"reachable": True, "checked": time.time()}
            return {"id": token, "preflight_id": token, "number": number, "address": drone_address(number),
                "allowed": True, "ok": True, "checks": checks, "identity": identity,
                "needs_identity_confirmation": not bool(saved.get("identity")),
                "manual_checklist": manual_checklist(number),
                "manual_checks": "Les cinq contrôles manuels resteront ignorés dans la checklist fournisseur. Aucun MAV_SYS_ID matériel n’est réglé automatiquement.",
                "message": "Précontrôles réussis. Vérifier la correspondance physique de l’appareil avant de lancer la configuration."}
        except (ApiError, OSError, ValueError) as exc:
            message = exc.message if isinstance(exc, ApiError) else "Un fichier ou contrôle système nécessaire est indisponible. Aucune configuration envoyée."
            checks.append({"label": "Configuration autorisée", "ok": False, "message": message})
            return {"id": token, "preflight_id": token, "number": number, "address": drone_address(number),
                "allowed": False, "ok": False, "checks": checks, "identity": identity,
                "manual_checklist": manual_checklist(number),
                "code": exc.code if isinstance(exc, ApiError) else "preflight_unavailable", "message": message}

    def _progress(self, plan, completed, phase, label, *, indeterminate=True):
        """Persist observed milestones only; elapsed time never advances progress."""
        progress = {"completed": completed, "total": CONFIGURATION_STEPS,
                    "percent": completed * 100 // CONFIGURATION_STEPS,
                    "phase": phase, "label": label, "indeterminate": indeterminate}
        if progress != plan.get("_last_progress"):
            self.app.report_progress(progress)
            plan["_last_progress"] = progress

    def _supplier_progress(self, plan, trace):
        number = plan["number"]
        # Only exact completion lines emitted by the recognised supplier script
        # count. A displayed prompt or the beginning of an upload does not count.
        labels = (
            ("Hub — IPv6 GCS fixée (f::1)", "Préparation MAVLink du hub"),
            ("[hub] config.conf mavlink-router (IPv6 GCS + N drones)", "Envoi MAVLink au drone"),
            (f"[aqua {number}] mavlink.conf → drone", "Rechargement des services du hub"),
            ("[hub] recharger net_setup + mavlink_router + gcs_reactor", "Envoi de la configuration WireGuard existante"),
            (f"[aqua {number}] wdg.conf → drone", "Envoi de la configuration MQTT existante"),
            (f"[aqua {number}] IP(s) broker MQTT → drone", f"Redémarrage de Aqua_Drone_{number}"),
            (f"[aqua {number}] reboot", f"Attente du redémarrage de Aqua_Drone_{number}"),
        )
        matched = plan.get("_supplier_completed", 0)
        while matched < len(labels):
            label, following = labels[matched]
            if not re.search(r"(?m)^\s*✔ " + re.escape(label) + r" \(fait\)\s*$", trace):
                break
            matched += 1
            plan["_supplier_completed"] = matched
            self._progress(plan, 2 + matched, "reboot" if matched == 7 else "supplier", following)

    def _guard(self, plan):
        """Keep supplier deploy.py intact while enforcing pinned transports."""
        stage, number = plan["stage"], plan["number"]
        tools = _private_dir(stage / "bin")
        bundle = self._bundle()
        runtime = plan.get("runtime", bundle)
        guard_config = {"credentials": plan["credentials"], "bundle": str(bundle),
            "scp": {str(runtime / "build" / f"mavlink{number}.conf"): REMOTE_FILES[0],
                    str(runtime / "config/wireguard/drones" / self._wg_path(number).name): REMOTE_FILES[1],
                    str(runtime / "build/main.config"): REMOTE_FILES[2]}}
        _atomic_json(stage / "guard.json", guard_config)
        source = '''#!/usr/bin/python3
import json, os, pathlib, sys
c = json.loads((pathlib.Path(__file__).parent.parent / "guard.json").read_text())
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
def refuse():
    print("Transport AquaHub: commande imprévue refusée", file=sys.stderr)
    sys.exit(97)
if name in ("ssh", "scp"):
    clean = []
    while args:
        value = args.pop(0)
        if value == "-o":
            if not args: refuse()
            option = args.pop(0)
            if option not in ("ConnectTimeout=5", "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null"): refuse()
        elif value.startswith("-"): refuse()
        else: clean.append(value)
    if len(clean) != 2: refuse()
    if name == "ssh":
        if clean[0] != c["credentials"]["host"] or clean[1] not in ("mkdir -p /etc/dmx", "(sleep 1 ; reboot) >/dev/null 2>&1 &"): refuse()
    else:
        if clean[0] not in c["scp"] or clean[1] != c["credentials"]["host"] + ":" + c["scp"][clean[0]]: refuse()
    os.execve("/usr/bin/" + name, [name, *c["credentials"]["options"], *clean], c["credentials"]["env"])
elif name == "docker":
    if args != ["compose", "restart", "net_setup", "mavlink_router", "gcs_reactor"]: refuse()
    os.execve("/usr/bin/docker", ["docker", "--context", "default", "--config", "/etc/aquahub/docker-client", "compose", "--project-directory", c["bundle"], "-f", c["bundle"] + "/docker-compose.yml", "-p", "aqua-hub-bundle", "restart", "net_setup", "mavlink_router", "gcs_reactor"], {"PATH":"/usr/sbin:/usr/bin:/sbin:/bin", "LANG":"C.UTF-8", "HOME":"/nonexistent"})
else: refuse()
'''
        for name in ("ssh", "scp", "docker"):
            target = tools / name
            target.write_text(source)
            target.chmod(0o700)
        return tools

    def _deploy(self, plan):
        # The supplier code remains unchanged. Its private execution mirror has
        # one intentional live-file mapping: the already-verified hub config.
        # Holding that regular inode prevents a pathname swap to another file.
        hub = self._bundle() / "config/mavlink/config.conf"
        descriptor = os.open(hub, os.O_RDWR | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ApiError("unsafe_path", "La configuration du hub doit être un fichier régulier.")
            current = os.read(descriptor, MAX_READ + 1)
            if len(current) > MAX_READ or hashlib.sha256(current).hexdigest() != plan["hashes"]["config/mavlink/config.conf"]:
                raise ApiError("configuration_changed", "La configuration du hub a changé avant son ouverture.")
            runtime = _private_dir(plan["stage"] / "runtime")
            for relative in ("deploy.py", "mavlink-config-generator/main.py", "docker-compose.yml"):
                self._copy_verified_code(relative, runtime / relative)
            hub_mapping = runtime / "config/mavlink/config.conf"
            hub_mapping.parent.mkdir(parents=True, mode=0o700)
            hub_mapping.symlink_to(f"/proc/self/fd/{descriptor}")
            wg_source = self._wg_path(plan["number"])
            wg_target = runtime / "config/wireguard/drones" / wg_source.name
            wg_target.parent.mkdir(parents=True, mode=0o700)
            raw_wg = wg_source.read_bytes()
            if len(raw_wg) > 128 * 1024 or hashlib.sha256(raw_wg).hexdigest() != plan["remote"]["wg_hash"]:
                raise ApiError("configuration_changed", "La configuration WireGuard a changé avant son envoi.")
            wg_target.write_bytes(raw_wg)
            wg_target.chmod(0o600)
            plan["runtime"], plan["hub_fd"] = runtime, descriptor
            return self._deploy_pty(plan)
        finally:
            os.close(descriptor)
            plan.pop("hub_fd", None)

    def _deploy_pty(self, plan):
        number = plan["number"]
        prompts = [
            (re.escape("Aquadrones à configurer (ex: 1-3 ou 1,3,5) : "), str(number)),
            (re.escape(f"  [ ] [aqua {number}] RUTC50 : LAN interface IP = 192.168.20{number}.1/24, protocol static  (o/n) : "), "n"),
            (re.escape(f"  [ ] [aqua {number}] Next hop 192.168.20{number}.1 → IP aquadrone sur réseau GCS  (o/n) : "), "n"),
            (re.escape(f"  [ ] [aqua {number}] Vérifier les paramètres — fait  (o/n) : "), "n"),
            (re.escape(f"  [ ] [aqua {number}] Relever les versions Heron — fait  (o/n) : "), "n"),
            (re.escape("  IP(s) broker MQTT — CSV pour la redondance (ex: 10.0.0.1,10.0.0.2)") + r"(?: \[[^\r\n\]]{1,200}\])? : ", plan["remote"]["mqtt"]),
            (re.escape(f"  [ ] [aqua {number}] Vérifier le status (QGC → MAVLink Console / nsh) — fait  (o/n) : "), "n"),
        ]
        tools = self._guard(plan)
        env = {**plan["credentials"]["env"], "PATH": str(tools) + ":/usr/sbin:/usr/bin:/sbin:/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        master, slave = os.openpty()
        runtime = plan.get("runtime", self._bundle())
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(runtime / "deploy.py")],
            stdin=slave, stdout=slave, stderr=slave, cwd=runtime, env=env, start_new_session=True,
            pass_fds=(plan["hub_fd"],) if "hub_fd" in plan else ())
        os.close(slave)
        chunks, buffer, index = bytearray(), "", 0
        deadline = time.monotonic() + DEPLOY_TIMEOUT
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise ApiError("deployment_timeout", "Délai de configuration dépassé. Sauvegardes conservées ; résultat à vérifier.")
                ready, _, _ = select.select([master], [], [], .2)
                if ready:
                    try:
                        block = os.read(master, 65536)
                    except OSError:
                        block = b""
                    if not block:
                        break
                    chunks.extend(block)
                    if len(chunks) > MAX_READ:
                        raise ApiError("output_limit", "La trace fournisseur dépasse la limite autorisée.")
                    # Decode the accumulated bytes: UTF-8 may split across reads.
                    text = ANSI.sub("", chunks.decode("utf-8", "replace")).replace("\r\n", "\n")
                    if "(r)éessayer / (s)kip : " in text or "✘ échec :" in text or "non implémenté :" in text:
                        raise ApiError("deployment_partial", "Une étape fournisseur a échoué. Configuration interrompue ; les sauvegardes privées sont conservées.")
                    self._supplier_progress(plan, text)
                    while index < len(prompts):
                        match = re.search(prompts[index][0], text)
                        if not match:
                            break
                        # Every occurrence is consumed exactly once, in order.
                        if match.start() < len(buffer):
                            break
                        buffer = text[:match.end()]
                        os.write(master, (prompts[index][1] + "\n").encode())
                        index += 1
                if proc.poll() is not None and not ready:
                    break
            proc.wait(timeout=5)
            trace = ANSI.sub("", chunks.decode("utf-8", "replace")).replace("\r\n", "\n")
            if proc.returncode != 0 or index != len(prompts):
                raise ApiError("deployment_incomplete", "La procédure n’a pas terminé la séquence attendue.")
            summary = re.search(r"(\d+) fait\s*·\s*(\d+) ignoré\s*·\s*(\d+) échec", trace)
            if not summary or tuple(map(int, summary.groups())) != (7, 5, 0):
                # Hub IPv6 + six automatic steps + five explicitly skipped manual checks.
                raise ApiError("deployment_incomplete", "Le récapitulatif fournisseur diffère des étapes attendues.")
            return {"automatic_completed": 7, "manual_skipped": 5, "failed": 0}
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            os.close(master)
            # Keep a bounded, redacted trace on failures as well as success.
            # A logging failure must not replace the actual deployment error.
            try:
                trace_path = plan["stage"] / "deployment.log"
                with trace_path.open("w") as output:
                    trace_path.chmod(0o600)
                    output.write(redact(chunks.decode("utf-8", "replace"), MAX_READ))
            except OSError:
                pass

    def configure(self, params, context=None):
        _params(params, {"number", "preflight_id", "confirmed_identity"})
        token = params.get("preflight_id")
        if not isinstance(token, str):
            raise ApiError("invalid_params", "L’identifiant de précontrôle est obligatoire.")
        with self.lock:
            plan = self.preflights.get(token)
        if not plan or plan["used"] or time.time() - plan["created"] > PREFLIGHT_TTL:
            raise ApiError("preflight_expired", "Relancer les précontrôles avant cette configuration.")
        if "number" in params and params["number"] != plan["number"]:
            raise ApiError("target_mismatch", "Le numéro demandé ne correspond pas à la cible précontrôlée.")
        if getattr(context, "uid", os.getuid()) != plan["uid"]:
            raise ApiError("target_mismatch", "Les précontrôles appartiennent à un autre opérateur.")
        confirmed = params.get("confirmed_identity")
        if confirmed != plan["identity"]:
            raise ApiError("identity_confirmation_required", "Confirmer les identifiants et la correspondance physique de cet appareil.")

        def work():
            with self.lock:
                if plan["used"] or time.time() - plan["created"] > PREFLIGHT_TTL:
                    raise ApiError("preflight_expired", "Ce précontrôle est expiré ou déjà utilisé.")
                plan["used"] = True
            number, stage = plan["number"], plan["stage"]
            self._progress(plan, 0, "preparing", f"Vérification de Aqua_Drone_{number}")
            self._recognised()
            if self._hashes(number) != plan["hashes"] or self._hub_network() != plan["hub_network"]:
                raise ApiError("configuration_changed", "La configuration ou le réseau a changé depuis les précontrôles.")
            before = self._remote(plan["credentials"])
            keys = ("machine_id", "host_key_fingerprint", "boot_id", "wg_hash", "mqtt", "network", "mavlink")
            if any(before[key] != plan["remote"][key] for key in keys):
                raise ApiError("target_changed", "L’état ou l’identité du drone a changé. Aucun fichier envoyé.")
            self._progress(plan, 1, "backup", f"Sauvegarde de Aqua_Drone_{number}")
            backup = self.runner(["/usr/bin/ssh", *plan["credentials"]["options"], plan["credentials"]["host"],
                "tar -czf - /etc/mavlink-router/main.conf /etc/wireguard/wg0.conf /etc/dmx/main.config"],
                env=plan["credentials"]["env"], timeout=30, max_bytes=16 * 1024**2, binary=True)
            if not isinstance(backup, bytes) or not backup:
                raise ApiError("backup_failed", "La sauvegarde des fichiers du drone a échoué.")
            backup_path = stage / "drone-before.tar.gz"
            backup_path.write_bytes(backup)
            backup_path.chmod(0o600)
            backup_files = ArchiveReader(max_archive=16 * 1024**2, max_expanded=32 * 1024**2).inspect(backup_path)["files"]
            expected_files = {path.lstrip("/") for path in REMOTE_FILES}
            if {name for name, value in backup_files.items() if not value["directory"]} != expected_files:
                raise ApiError("backup_failed", "La sauvegarde ne contient pas exactement les fichiers attendus.")
            if backup_files[REMOTE_FILES[1].lstrip("/")]["sha256"] != before["wg_hash"]:
                raise ApiError("backup_failed", "La sauvegarde WireGuard ne correspond pas à l’état précontrôlé.")
            _atomic_json(stage / "state-before.json", {"identity": plan["identity"], "network": before["network"],
                "hub_network": plan["hub_network"], "hashes": plan["hashes"], "backup_sha256": sha256_file(backup_path)})
            hub_before = stage / "hub-before.conf"
            hub_before.write_bytes((self._bundle() / "config/mavlink/config.conf").read_bytes())
            hub_before.chmod(0o600)
            self._progress(plan, 2, "supplier", f"Configuration de Aqua_Drone_{number} : procédure fournisseur")
            result = self._deploy(plan)
            self._progress(plan, 9, "reboot", f"Attente du redémarrage de Aqua_Drone_{number}")
            # Observe the restart, rather than treating an accepted SSH reboot as completion.
            down, after, stable_observations = False, None, 0
            pending_checks = ["arrêt puis retour du drone"]
            expected_mavlink = (stage / "drone.conf").read_text().strip()
            deadline = time.monotonic() + REBOOT_TIMEOUT
            while time.monotonic() < deadline:
                if not self._reachable(number):
                    down = True
                    stable_observations = 0
                    self._progress(plan, 10, "reboot", f"Aqua_Drone_{number} redémarre ; attente de son retour")
                elif down:
                    self._progress(plan, 10, "verification", f"Retour de Aqua_Drone_{number} : vérification des services et des configurations")
                    try:
                        candidate = self._remote(plan["credentials"])
                        if candidate["boot_id"] != before["boot_id"]:
                            # SSH often starts before WireGuard and the firmware
                            # applications. A new boot_id is not readiness.
                            if any(candidate[key] != before[key] for key in ("machine_id", "host_key_fingerprint")):
                                raise ApiError("identity_mismatch", "L’identité du drone a changé après son redémarrage.")
                            postchecks = {
                                "configuration WireGuard": candidate["wg_hash"] == before["wg_hash"],
                                "valeur MQTT": candidate["mqtt"] == before["mqtt"],
                                "IP et routes du drone": candidate["network"] == before["network"],
                                "configuration MAVLink": candidate["mavlink"] == expected_mavlink,
                                "services embarqués": set(plan["required_services"]).issubset(candidate["services"]),
                                "configuration du hub": sha256_file(self._bundle() / "config/mavlink/config.conf") == plan["generated_hub_sha"],
                                "IP et routes du hub": self._hub_network() == plan["hub_network"],
                            }
                            pending_checks = [label for label, ok in postchecks.items() if not ok]
                            if not pending_checks:
                                stable_observations += 1
                                if stable_observations >= 2:
                                    after = candidate
                                    self._progress(plan, 11, "verification", f"Aqua_Drone_{number} disponible ; contrôles finaux")
                                    break
                            else:
                                stable_observations = 0
                        else:
                            pending_checks = ["nouveau démarrage"]
                    except ApiError as exc:
                        if exc.code == "identity_mismatch":
                            raise
                        stable_observations = 0
                        pending_checks = ["lecture complète de l’état embarqué"]
                time.sleep(2)
            if after is None:
                raise ApiError("reboot_unverified", "Le retour stable après redémarrage n’a pas été confirmé dans le délai prévu. Contrôles restants : " + ", ".join(pending_checks) + ". Sauvegardes conservées ; ne pas relancer aveuglément la procédure.")
            if any(after[key] != before[key] for key in ("machine_id", "host_key_fingerprint", "wg_hash", "mqtt", "network")):
                raise ApiError("postcheck_failed", "Une identité ou un paramètre réseau diffère après le redémarrage. Sauvegardes conservées.")
            if hashlib.sha256(after["mavlink"].encode()).hexdigest() != hashlib.sha256((stage / "drone.conf").read_text().strip().encode()).hexdigest():
                raise ApiError("postcheck_failed", "La configuration MAVLink attendue n’a pas été vérifiée sur le drone.")
            if not set(plan["required_services"]).issubset(after["services"]):
                raise ApiError("postcheck_failed", "Un service embarqué attendu n’est pas actif après le retour.")
            if sha256_file(self._bundle() / "config/mavlink/config.conf") != plan["generated_hub_sha"] or self._hub_network() != plan["hub_network"]:
                raise ApiError("postcheck_failed", "La configuration ou le réseau du hub diffère du résultat attendu.")
            saved = self._saved()
            saved[str(number)] = {"identity": plan["identity"], "configured": True, "last_seen": time.time()}
            _atomic_json(self.root / "inventory.json", saved)
            self.observed[number] = {"reachable": True, "checked": time.time()}
            self._progress(plan, CONFIGURATION_STEPS, "complete", f"Configuration de Aqua_Drone_{number} vérifiée", indeterminate=False)
            return {**result, "configured": True, "verified": True, "number": number,
                "address": drone_address(number), "reboot_verified": True, "ip_unchanged": True,
                "manual_checklist": manual_checklist(number),
                "message": f"Configuration logicielle de Aqua_Drone_{number} vérifiée. Les cinq contrôles manuels de la checklist fournisseur restent à effectuer."}

        return self.app.submit_job("configure_drone", f"Aqua_Drone_{plan['number']}",
            {"number": plan["number"], "preflight_id": token}, work)


def register(app):
    manager = DroneManager(app)
    for name in ("list", "scan", "preflight", "configure", "identity", "enroll", "ssh_settings"):
        app.register_handler("drones." + name, getattr(manager, name),
            mutation=name in ("preflight", "configure", "enroll", "ssh_settings"),
            requires_admin=False if name == "preflight" else None)
    app.drones = manager
    return manager
