"""Reactivate the existing, narrowly qualified WireGuard tunnel.

This adapter never restarts the legacy container entrypoint. A private copy of
the already installed profile is passed to the image's unmodified wg-quick.
Neither a recent handshake nor a connected drone is required for local readiness.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import threading
import time
import uuid

from .network import stable_network
from .security import ApiError

IMAGE = "sha256:827484bad77ba53ee94ce3e9f2136ca28123924daad01c387bc185c880a6f519"
# These exact config/manifest identities are cryptographically linked in the
# qualified linux/amd64 image archive, for classic and containerd stores.
IMAGE_IDENTITIES = frozenset({IMAGE, "sha256:8542b6e39ddd0fb12792759feb9b61b3b92c02a3120b87c84a6619587a53054f"})
PROFILE = "config/wireguard/hub/wg0.conf"
MOUNT = "/config/wg_confs/wg0.conf"
PENDING_KEY = "wireguard_pending"
MAX_PROFILE = 16384
MAX_STATE = 262144
ENV = ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C"]
PINNED_FILES = {
    "usr/bin/wg-quick": "5a799722e7d1796cbb185e79ccd712380613d8482e02853cd7fa08452795b925",
    "usr/sbin/resolvconf": "b2d4d5c9d04f4f3bf5c79e7094ad5a617cadf52f6ce578330b16d01cbd9052a0",
    "usr/lib/resolvconf/libc": "9cb875f9a224d0ad1a42033d7b91b04fa4d845051c51d16f1728e507bd50ab6f",
    "etc/resolvconf.conf": "7d32732be299ba10151d137e00b304b8912b4d2e6f03c08385c40d7d02f2b2ad",
}
INSPECT = ('{"id":{{json .Id}},"image_id":{{json .Image}},"running":{{json .State.Running}},'
           '"state":{{json .State.Status}},"pid":{{json .State.Pid}},"started_at":{{json .State.StartedAt}},'
           '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
           '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
           '"network":{{json .HostConfig.NetworkMode}},"privileged":{{json .HostConfig.Privileged}},'
           '"pid_mode":{{json .HostConfig.PidMode}},"mounts":{{json .Mounts}},'
           '"resolv_conf":{{json .ResolvConfPath}}}')
READ_FIREWALL = r'''set -e -o pipefail
if command -v nft >/dev/null 2>&1; then nft list tables; fi
if command -v iptables-save >/dev/null 2>&1; then iptables-save; fi
if command -v ip6tables-save >/dev/null 2>&1; then ip6tables-save; fi
'''

# These scripts are closed constants. Profile data and generated proof fields
# enter on stdin, never through shell interpolation or command arguments.
READ_STATE = r'''set -e -o pipefail
printf '__AQ_PUBLIC__\n'; /usr/bin/wg show wg0 public-key
printf '__AQ_ALLOWED__\n'; /usr/bin/wg show wg0 allowed-ips
printf '__AQ_HANDSHAKES__\n'; /usr/bin/wg show wg0 latest-handshakes
printf '__AQ_FWMARK__\n'; /usr/bin/wg show wg0 fwmark
printf '__AQ_ADDR__\n'; ip -j -d address show dev wg0
printf '__AQ_ROUTES4__\n'; ip -j route show dev wg0 table all
printf '__AQ_ROUTES6__\n'; ip -j -6 route show dev wg0 table all
printf '__AQ_FIREWALL__\n'
if command -v nft >/dev/null 2>&1; then nft list tables; fi
if command -v iptables-save >/dev/null 2>&1; then iptables-save; fi
if command -v ip6tables-save >/dev/null 2>&1; then ip6tables-save; fi
'''

ACTIVATE = r'''set -e -o pipefail
umask 077
IFS= read -r expires
IFS= read -r mode
IFS= read -r expected_profile
IFS= read -r expected_public
IFS= read -r expected_allowed
IFS= read -r expected_addr
IFS= read -r expected_routes4
IFS= read -r expected_routes6
[[ "$expires" =~ ^[0-9]{1,12}$ ]] || exit 71
[[ "$mode" == absent || "$mode" == inactive ]] || exit 71
[[ "$expected_public" =~ ^[A-Za-z0-9+/]{43}=$ ]] || exit 71
for value in "$expected_profile" "$expected_allowed" "$expected_addr" "$expected_routes4" "$expected_routes6"; do
    [[ "$value" =~ ^[a-f0-9]{64}$ ]] || exit 71
done
[[ "$(date +%s)" -le "$expires" ]] || exit 74
workdir="$(mktemp -d /run/aquahub-wireguard.XXXXXXXX)"
trap 'rm -f "$workdir/wg0.conf"; rmdir "$workdir"' EXIT
cat > "$workdir/wg0.conf"
actual="$(sha256sum "$workdir/wg0.conf")"
[[ "${actual%% *}" == "$expected_profile" ]] || exit 73
firewall="$(
  set -e -o pipefail
  {
    set -e
    if command -v nft >/dev/null 2>&1; then nft list tables; fi
    if command -v iptables-save >/dev/null 2>&1; then iptables-save; fi
    if command -v ip6tables-save >/dev/null 2>&1; then ip6tables-save; fi
  } | head -c 262145
)"
[[ "${#firewall}" -le 262144 ]] || exit 73
if printf '%s\n' "$firewall" | grep -E 'wg-quick-wg0|wg-quick\(8\).*wg0' >/dev/null; then exit 72; fi
if [[ "$mode" == absent ]]; then
    if ip link show dev wg0 >/dev/null 2>&1; then exit 72; fi
else
    [[ "$(/usr/bin/wg show wg0 public-key)" == "$expected_public" ]] || exit 72
    [[ "$(/usr/bin/wg show wg0 fwmark)" == off ]] || exit 72
    actual="$(/usr/bin/wg show wg0 allowed-ips | awk '{$1=$1;print}' | LC_ALL=C sort | sha256sum)"
    [[ "${actual%% *}" == "$expected_allowed" ]] || exit 72
    actual="$(ip -j -d address show dev wg0 | sha256sum)"
    [[ "${actual%% *}" == "$expected_addr" ]] || exit 72
    actual="$(ip -j route show dev wg0 table all | sha256sum)"
    [[ "${actual%% *}" == "$expected_routes4" ]] || exit 72
    actual="$(ip -j -6 route show dev wg0 table all | sha256sum)"
    [[ "${actual%% *}" == "$expected_routes6" ]] || exit 72
    /usr/bin/wg-quick down "$workdir/wg0.conf"
fi
/usr/bin/wg-quick up "$workdir/wg0.conf"
printf 'AQUAHUB_WIREGUARD_REACTIVATED\n'
'''
OUTER = ("umask 077\nexec /usr/bin/timeout -s TERM -k 5 20 /usr/bin/flock -x -n "
         "/run/aquahub-wireguard.lock /bin/bash --noprofile --norc -c " + shlex.quote(ACTIVATE))
LOCK_PROBE = ("umask 077\nexec /usr/bin/timeout -s TERM -k 2 5 /usr/bin/flock -x -n "
              "/run/aquahub-wireguard.lock /bin/echo AQUAHUB_LOCK_FREE")


def _error(code="wireguard_unavailable"):
    messages = {
        "wireguard_profile": "Le profil WireGuard existant ne correspond pas au parcours qualifié. Aucun paramètre réseau n’est modifié.",
        "wireguard_unmanaged": "Cette instance WireGuard n’est pas reconnue ou son conteneur est arrêté. Son démarrage reste protégé.",
        "wireguard_unknown": "L’état du tunnel WireGuard n’est pas confirmé. Consultez les journaux avant une autre intervention.",
        "wireguard_pending": "Une intervention WireGuard reste à vérifier. AquaHub attend la fin confirmée de son exécution.",
        "wireguard_changed": "Le profil, le conteneur ou le réseau a changé pendant l’intervention. Aucun réglage réseau n’est réécrit automatiquement.",
        "wireguard_unavailable": "La réactivation WireGuard n’a pas pu être confirmée. Les paramètres existants sont conservés ; ne relancez pas aveuglément l’opération.",
    }
    return ApiError(code, messages.get(code, messages["wireguard_unavailable"]))


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs)


def _key(value):
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded).decode() != value or not any(decoded):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise _error("wireguard_profile") from None


@dataclass(frozen=True)
class Profile:
    raw: bytes = field(repr=False)
    private: str = field(repr=False)
    address: str
    peers: dict = field(repr=False)
    allowed: frozenset


def parse_profile(raw: bytes) -> Profile:
    """A deliberately small IPv4 client contract; no scripts or full tunnel."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PROFILE:
        raise _error("wireguard_profile")
    try:
        text = raw.decode("utf-8")
        normalized = text.replace("\r\n", "\n")
        if "\x00" in text or "\r" in normalized:
            raise ValueError()
        lines = normalized.split("\n")
        if len(lines) > 512:
            raise ValueError()
        interface, peers, section = {}, [], None
        for original in lines:
            body = original.split("#", 1)[0]
            # Match Bash's LF records and C-locale whitespace. Python's Unicode
            # splitlines/strip must not validate a different profile than wg-quick.
            if any(ord(char) > 126 or (ord(char) < 32 and char != "\t") for char in body):
                raise ValueError()
            line = body.strip()
            if not line:
                continue
            if line == "[Interface]":
                if section is not None:
                    raise ValueError()
                section = interface
            elif line == "[Peer]":
                if section is None or len(peers) >= 16:
                    raise ValueError()
                peers.append({})
                section = peers[-1]
            else:
                if section is None or "=" not in line:
                    raise ValueError()
                name, value = [part.strip() for part in line.split("=", 1)]
                if name in section or any(name.casefold() == key.casefold() for key in section):
                    raise ValueError()
                section[name] = value
        if (not peers or set(interface) - {"PrivateKey", "Address", "DNS", "MTU", "ListenPort", "Table", "SaveConfig"}
                or not {"PrivateKey", "Address"}.issubset(interface)):
            raise ValueError()
        private = _key(interface["PrivateKey"])
        address = ipaddress.ip_interface(interface["Address"])
        if address.version != 4 or address.network.prefixlen != 32 or address.ip.is_unspecified or address.ip.is_multicast:
            raise ValueError()
        if interface.get("Table", "auto") != "auto" or interface.get("SaveConfig", "false") != "false":
            raise ValueError()
        for name, low, high in (("MTU", 576, 9000), ("ListenPort", 1, 65535)):
            if name in interface and (not interface[name].isdigit() or not low <= int(interface[name]) <= high):
                raise ValueError()
        if "DNS" in interface:
            dns = interface["DNS"].split(",")
            if not 1 <= len(dns) <= 4:
                raise ValueError()
            for item in dns:
                value = ipaddress.ip_address(item.strip())
                if value.version != 4 or value.is_unspecified or value.is_multicast:
                    raise ValueError()
        result, networks = {}, []
        for peer in peers:
            if (set(peer) - {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"}
                    or not {"PublicKey", "AllowedIPs", "Endpoint"}.issubset(peer)):
                raise ValueError()
            public = _key(peer["PublicKey"])
            if public in result:
                raise ValueError()
            if "PresharedKey" in peer:
                _key(peer["PresharedKey"])
            allowed = []
            for item in peer["AllowedIPs"].split(","):
                network = ipaddress.ip_network(item.strip(), strict=True)
                if network.version != 4 or network.prefixlen == 0 or network.is_multicast or network.is_unspecified:
                    raise ValueError()
                if network in networks or any(network.overlaps(old) for old in networks):
                    raise ValueError()
                allowed.append(str(network)); networks.append(network)
            if not 1 <= len(allowed) <= 16:
                raise ValueError()
            endpoint, separator, port = peer["Endpoint"].rpartition(":")
            if (not separator or not port.isdigit() or not 1 <= int(port) <= 65535 or len(endpoint) > 253
                    or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", endpoint)
                    or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in endpoint.split("."))):
                raise ValueError()
            try:
                endpoint_ip = ipaddress.ip_address(endpoint)
            except ValueError:
                endpoint_ip = None
            if endpoint_ip and (endpoint_ip.version != 4 or any(endpoint_ip in n for n in networks)):
                raise ValueError()
            if "PersistentKeepalive" in peer and (not peer["PersistentKeepalive"].isdigit() or not 0 <= int(peer["PersistentKeepalive"]) <= 65535):
                raise ValueError()
            result[public] = frozenset(allowed)
        return Profile(raw, private, str(address), result, frozenset(str(n) for n in networks))
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise _error("wireguard_profile") from None


def _read_under(root: Path, relative: str, limit=MAX_STATE):
    """Read relative to anchored directories; reject final links and specials."""
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", "..", "/"} for part in parts):
        raise _error("wireguard_profile")
    # /proc/<validated container PID>/root is an intentional kernel magic link.
    # Every descendant is still opened relative to its fd without following links.
    proc_root = root.parts[:2] == ("/", "proc") and len(root.parts) == 4 and root.parts[2].isdigit() and root.parts[3] == "root"
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | (0 if proc_root else os.O_NOFOLLOW))
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
                raise _error("wireguard_profile")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise _error("wireguard_profile")
            return data, (info.st_dev, info.st_ino, info.st_mode & 0o7777, info.st_uid, info.st_gid)
    finally:
        os.close(directory)


class WireGuardManager:
    def __init__(self, app):
        self.app, self.docker = app, app.docker
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache = None
        self._cached_at = 0
        self._thread = None
        self._closed = False

    def _exec(self, container, args, **kwargs):
        command = ["exec", "--user", "0"]
        if kwargs.get("input_data") is not None:
            command.append("--interactive")
        return self.docker.run([*command, container["id"], *ENV, *args], **kwargs)

    def _inspect(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise _error("wireguard_unmanaged")
        value = _json(self.docker.run(["inspect", "--format", INSPECT, identifier], timeout=8, max_bytes=32768))
        if (not isinstance(value, dict) or value.get("id") != identifier or type(value.get("running")) is not bool
                or type(value.get("pid")) is not int or value["pid"] < 0 or not isinstance(value.get("started_at"), str)
                or not value["started_at"] or len(value["started_at"]) > 100
                or value.get("state") not in {"running", "exited", "created", "dead", "paused", "restarting", "removing"}):
            raise _error("wireguard_unmanaged")
        return value

    def _container(self):
        ids = self.docker.run(["ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=aqua-hub-bundle",
                               "--filter", "label=com.docker.compose.service=wireguard"], timeout=8, max_bytes=1024).split()
        if len(ids) != 1:
            raise _error("wireguard_unmanaged")
        value = self._inspect(ids[0])
        bundle = self.app.config.bundle_path
        if (not bundle or not Path(bundle).is_absolute() or value.get("working_dir") != str(bundle)
                or value.get("project") != "aqua-hub-bundle" or value.get("service") != "wireguard"
                or value.get("image_id") not in IMAGE_IDENTITIES or value.get("network") != "host" or value.get("privileged") is not False
                or value.get("pid_mode") not in {"", "private"} or value.get("running") is not True
                or value.get("state") != "running" or type(value.get("pid")) is not int or value["pid"] <= 1):
            raise _error("wireguard_unmanaged")
        # Docker's "host" mode may refer to another initial namespace. Network
        # snapshots and the commands must concern the same kernel interfaces.
        if os.readlink(f"/proc/{value['pid']}/ns/net") != os.readlink("/proc/self/ns/net"):
            raise _error("wireguard_unmanaged")
        mounts = value.get("mounts")
        if not isinstance(mounts, list) or len(mounts) != 2:
            raise _error("wireguard_unmanaged")
        by_target = {item.get("Destination"): item for item in mounts if isinstance(item, dict)}
        if set(by_target) != {"/lib/modules", MOUNT}:
            raise _error("wireguard_unmanaged")
        if (by_target[MOUNT].get("Type") != "bind" or by_target[MOUNT].get("Source") != str(Path(bundle) / PROFILE)
                or by_target["/lib/modules"].get("Source") != "/lib/modules" or by_target["/lib/modules"].get("RW") is not False):
            raise _error("wireguard_unmanaged")
        return value

    @staticmethod
    def _same(left, right):
        return all(left.get(key) == right.get(key) for key in ("id", "image_id", "pid", "started_at"))

    def _attest(self, container):
        bundle = Path(self.app.config.bundle_path)
        root = Path("/proc") / str(container["pid"]) / "root"
        source, source_stat = _read_under(bundle, PROFILE, MAX_PROFILE)
        mounted, mounted_stat = _read_under(root, MOUNT.lstrip("/"), MAX_PROFILE)
        if source != mounted or source_stat[:2] != mounted_stat[:2] or source_stat[2] & 0o022:
            raise _error("wireguard_profile")
        for path, digest in PINNED_FILES.items():
            if _hash(_read_under(root, path)[0]) != digest:
                raise _error("wireguard_unmanaged")
        active, _ = _read_under(root, "run/activeconfs", 1024)
        if active.strip() != b'declare -a WG_CONFS=([0]="/config/wg_confs/wg0.conf")':
            raise _error("wireguard_unmanaged")
        host_resolver = Path("/etc/resolv.conf").stat()
        inside = (root / "etc/resolv.conf").stat()
        if ((host_resolver.st_dev, host_resolver.st_ino) == (inside.st_dev, inside.st_ino)
                or Path(container.get("resolv_conf", "")).resolve() == Path("/etc/resolv.conf").resolve()
                or (root / "run/dbus/system_bus_socket").exists()):
            raise _error("wireguard_unmanaged")
        profile = parse_profile(source)
        public = self._exec(container, ["/usr/bin/timeout", "-k", "2", "5", "/usr/bin/wg", "pubkey"],
                            input_data=(profile.private + "\n").encode(), timeout=8, max_bytes=1024).strip()
        _key(public)
        if not self._same(container, self._container()):
            raise _error("wireguard_changed")
        return profile, public

    def _host_snapshot(self):
        observations = []
        for args in (["-j", "-d", "address", "show"], ["-j", "route", "show", "table", "all"],
                     ["-j", "-6", "route", "show", "table", "all"], ["-j", "rule", "show"], ["-j", "-6", "rule", "show"]):
            result = subprocess.run(["/usr/sbin/ip", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
            if result.returncode or len(result.stdout) > MAX_STATE:
                raise _error("wireguard_unknown")
            value = _json(result.stdout)
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise _error("wireguard_unknown")
            observations.append(value)
        addresses = observations[0]
        if any(row.get(key) == "wg0" for rows in observations[3:] for row in rows
               for key in ("iif", "oif", "iifname", "oifname")):
            raise _error("wireguard_unknown")
        routes = list({json.dumps(item, sort_keys=True): item for item in observations[1] + observations[2]}.values())
        dns = Path("/etc/resolv.conf")
        dns_target = dns.resolve(strict=True)
        raw, info = _read_under(dns_target.parent, dns_target.name, 65536)
        outside = stable_network([item for item in addresses if item.get("ifname") != "wg0"],
                                 [item for item in routes if item.get("dev") != "wg0"])
        outside["rules"] = [sorted(json.dumps(item, sort_keys=True) for item in rows) for rows in observations[3:]]
        outside["dns"] = (str(dns_target), _hash(raw), info[2:])
        return {"interfaces": addresses, "routes": routes, "outside": outside}

    def _fingerprints(self):
        bundle = Path(self.app.config.bundle_path)
        paths = {"docker-compose.yml", ".env", "deploy.py", "install.sh", "mavlink-config-generator/main.py"}
        for index, path in enumerate((bundle / "config").rglob("*")):
            if index >= 1024 or path.is_symlink():
                raise _error("wireguard_profile")
            if not path.is_dir():
                paths.add(str(path.relative_to(bundle)))
        result = {}
        for path in sorted(paths):
            data, info = _read_under(bundle, path, 4 * 1024**2)
            result[path] = (_hash(data), info[2:])
        return result

    def _wg_state(self, container):
        raw = self._exec(container, ["/usr/bin/timeout", "-k", "2", "8", "/bin/bash", "--noprofile", "--norc", "-c", READ_STATE],
                         timeout=12, max_bytes=MAX_STATE)
        parts = re.split(r"(?m)^__AQ_([A-Z0-9]+)__\n", raw)
        data = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
        if set(data) != {"PUBLIC", "ALLOWED", "HANDSHAKES", "FWMARK", "ADDR", "ROUTES4", "ROUTES6", "FIREWALL"}:
            raise _error("wireguard_unknown")
        if re.search(r"wg-quick-wg0|wg-quick\(8\).*wg0", data["FIREWALL"]):
            raise _error("wireguard_unknown")
        public = _key(data["PUBLIC"].strip())
        peers = {}
        lines = []
        for line in data["ALLOWED"].splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] in peers:
                raise _error("wireguard_unknown")
            key = _key(fields[0])
            allowed = frozenset(str(ipaddress.ip_network(value, strict=True)) for value in fields[1:])
            peers[key] = allowed
            lines.append(" ".join(fields))
        stamps = {}
        for line in data["HANDSHAKES"].splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] in stamps or not fields[1].isdigit():
                raise _error("wireguard_unknown")
            stamps[_key(fields[0])] = int(fields[1])
        if set(stamps) != set(peers):
            raise _error("wireguard_unknown")
        return {"public": public, "peers": peers, "fwmark": data["FWMARK"].strip(),
                "recent": sum(0 <= time.time() - value <= 180 and value > 0 for value in stamps.values()),
                "hashes": [_hash(("\n".join(sorted(lines)) + "\n").encode()),
                           *[_hash(data[key].encode()) for key in ("ADDR", "ROUTES4", "ROUTES6")]]}

    @staticmethod
    def _tunnel_shape(snapshot, profile):
        interfaces = [row for row in snapshot["interfaces"] if row.get("ifname") == "wg0"]
        if not interfaces:
            if any(row.get("dev") == "wg0" for row in snapshot["routes"]):
                raise _error("wireguard_unknown")
            return "absent"
        if len(interfaces) != 1 or interfaces[0].get("linkinfo", {}).get("info_kind") != "wireguard":
            raise _error("wireguard_unknown")
        addresses = set()
        for item in interfaces[0].get("addr_info", []):
            try:
                address = str(ipaddress.ip_interface(f"{item['local']}/{item['prefixlen']}"))
            except (ValueError, KeyError, TypeError):
                raise _error("wireguard_unknown") from None
            if item.get("family") != "inet" or address != profile.address:
                raise _error("wireguard_unknown")
            addresses.add(address)
        main, local = set(), set()
        own = str(ipaddress.ip_interface(profile.address).ip)
        allowed = set(profile.allowed) | {profile.address}
        for row in snapshot["routes"]:
            if row.get("dev") != "wg0":
                continue
            if (row.get("type") == "multicast" and row.get("dst") == "ff00::/8"
                    and row.get("table") in {"local", 255} and row.get("protocol") in {"kernel", 2}
                    and row.get("metric") == 256 and not set(row) - {"type", "dst", "dev", "table", "protocol", "metric", "flags", "pref"}
                    and row.get("flags", []) == []):
                continue
            if set(row) - {"type", "dst", "dev", "table", "protocol", "scope", "prefsrc", "flags"} or row.get("flags", []) != []:
                raise _error("wireguard_unknown")
            try:
                destination = str(ipaddress.ip_network(row.get("dst", ""), strict=True))
            except (ValueError, TypeError):
                raise _error("wireguard_unknown") from None
            table, kind = row.get("table", "main"), row.get("type", "unicast")
            if destination not in allowed or row.get("prefsrc", own) != own:
                raise _error("wireguard_unknown")
            if table in {"main", 254} and kind == "unicast":
                main.add(destination)
            elif table in {"local", 255} and kind == "local" and destination == profile.address:
                local.add(destination)
            else:
                raise _error("wireguard_unknown")
        if addresses == {profile.address} and profile.allowed.issubset(main) and profile.address in local and "UP" in interfaces[0].get("flags", []):
            return "active"
        return "inactive"

    def _observe(self):
        container = self._container()
        profile, public = self._attest(container)
        snapshot = self._host_snapshot()
        shape = self._tunnel_shape(snapshot, profile)
        state = None
        if shape != "absent":
            state = self._wg_state(container)
            if state["public"] != public or state["peers"] != profile.peers or state["fwmark"] != "off":
                raise _error("wireguard_unknown")
        else:
            firewall = self._exec(container, ["/usr/bin/timeout", "-k", "2", "8", "/bin/bash", "--noprofile", "--norc", "-c", READ_FIREWALL],
                                  timeout=12, max_bytes=MAX_STATE)
            if re.search(r"wg-quick-wg0|wg-quick\(8\).*wg0", firewall):
                raise _error("wireguard_unknown")
        return container, profile, public, snapshot, shape, state

    @staticmethod
    def _dto(state="unknown", *, reason=None, peers=0, recent=0):
        messages = {"active": "Tunnel local actif. Un échange récent n’est pas requis pour cet état ; la liaison distante reste distincte.",
                    "absent": "Le tunnel local est absent. Le profil existant peut être réactivé sans relancer le conteneur.",
                    "inactive": "Le tunnel reconnu a perdu une adresse ou une route attendue. Son profil existant peut être réactivé.",
                    "unsupported": "Le profil ou le démarrage WireGuard n’est pas pris en charge par ce parcours.",
                    "unknown": "L’état du tunnel n’est pas confirmé. Aucun drone n’est déclaré connecté par ce contrôle."}
        result = {"state": state, "checked_at": datetime.now(timezone.utc).isoformat(), "peer_count": peers,
                  "recent_peer_count": recent, "can_reactivate": state in {"absent", "inactive"}, "message": messages[state]}
        if reason:
            result["reason"] = ({"wireguard_busy": "Une intervention est en cours ; le tunnel sera vérifié ensuite.",
                                 "wireguard_checking": "La vérification du tunnel local est en cours."}.get(reason)
                                or _error(reason).message)
        return result

    def _set_cache(self, dto):
        with self._cache_lock:
            self._cache = dto.copy(); self._cached_at = time.monotonic()
        return dto.copy()

    def _probe(self):
        try:
            if self.is_pending():
                return self._dto(reason="wireguard_pending")
            _, profile, _, _, shape, state = self._observe()
            return self._dto(shape, peers=len(profile.peers), recent=state["recent"] if state else 0)
        except ApiError as exc:
            return self._dto("unsupported" if exc.code in {"wireguard_profile", "wireguard_unmanaged"} else "unknown", reason=exc.code)
        except Exception:
            return self._dto(reason="wireguard_unknown")

    def status(self, params=None, context=None):
        if not isinstance(params if params is not None else {}, dict) or params:
            raise ApiError("invalid_params", "Paramètres WireGuard non reconnus.")
        if not self._lock.acquire(blocking=False):
            return self._dto(reason="wireguard_busy")
        try:
            return self._set_cache(self._probe())
        finally:
            self._lock.release()

    def snapshot(self):
        def collect():
            if not self._lock.acquire(blocking=False):
                return
            try:
                if not self._closed:
                    self._set_cache(self._probe())
            finally:
                self._lock.release()
        with self._cache_lock:
            if not self._closed and time.monotonic() - self._cached_at >= 5 and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=collect, name="aquahub-wireguard-observer", daemon=True)
                self._thread.start()
            return self._cache.copy() if self._cache else self._dto(reason="wireguard_checking")

    def is_pending(self):
        """Only a free in-container lock or a stopped generation clears intent."""
        try:
            pending = self.app.store.setting_get(PENDING_KEY)
            if pending is None:
                return False
            if (not isinstance(pending, dict) or pending.get("schema") != 1 or type(pending.get("until")) not in {int, float}
                    or not math.isfinite(pending["until"]) or pending["until"] <= 0
                    or type(pending.get("pid")) is not int or pending["pid"] <= 1
                    or not isinstance(pending.get("started_at"), str) or not pending["started_at"]
                    or not re.fullmatch(r"[0-9a-f]{32}", str(pending.get("intent_id", "")))
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(pending.get("image_id", "")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(pending.get("id", "")))):
                return True
            if time.time() <= pending["until"]:
                return True
            ids = self.docker.run(["ps", "-aq", "--no-trunc", "--filter", "id=" + pending["id"]], timeout=8, max_bytes=1024).split()
            free = not ids
            if ids:
                if ids != [pending["id"]]:
                    return True
                instance = self._inspect(pending["id"])
                free = (instance.get("running") is False and instance.get("pid") == 0
                        and instance.get("state") in {"exited", "created", "dead"})
                if instance.get("running") is True and instance.get("state") == "running":
                    free = (instance.get("pid") != pending.get("pid") or instance.get("started_at") != pending.get("started_at"))
                if not free:
                    if instance.get("image_id") != pending.get("image_id"):
                        return True
                    result = self._exec(instance, ["/bin/bash", "--noprofile", "--norc", "-c", LOCK_PROBE], timeout=9, max_bytes=1024)
                    free = result.strip() == "AQUAHUB_LOCK_FREE"
            if free and not self._closed:
                # Another dispatcher may have proved M1 complete and started M2
                # while this check was waiting on Docker. Never erase M2.
                return not self.app.store.setting_compare_and_set(PENDING_KEY, pending, None)
        except Exception:
            pass
        return True

    def reactivate(self, params, context=None):
        if not isinstance(params, dict) or set(params) != {"confirmed"} or params.get("confirmed") is not True:
            raise ApiError("confirmation_required", "Confirmez la réactivation du profil WireGuard existant.")
        def work():
            with self._lock:
                self._set_cache(self._dto(reason="wireguard_busy"))
                try:
                    return self._reactivate()
                except ApiError as exc:
                    if exc.code.startswith("wireguard_"):
                        raise
                    raise _error() from None
                except Exception:
                    raise _error() from None
        return self.app.submit_job("wireguard.reactivate", "wireguard", {}, work)

    def _reactivate(self):
        if self.is_pending():
            raise _error("wireguard_pending")
        container, profile, public, before, shape, state = self._observe()
        fingerprints = self._fingerprints()
        if fingerprints.get(PROFILE, (None,))[0] != _hash(profile.raw):
            raise _error("wireguard_changed")
        if shape == "active":
            self._set_cache(self._dto("active", peers=len(profile.peers), recent=state["recent"]))
            return {"service": "wireguard", "tunnel_active": True, "already_active": True, "profile_preserved": True}
        # Reject a more-specific tunnel route which would capture a current LAN.
        for row in before["routes"]:
            if row.get("dev") == "wg0" or row.get("type", "unicast") != "unicast" or row.get("dst", "default") == "default":
                continue
            try:
                network = ipaddress.ip_network(row["dst"], strict=False)
            except (ValueError, KeyError):
                raise _error("wireguard_unknown") from None
            if network.version == 4 and network.prefixlen > 0 and any(network.overlaps(ipaddress.ip_network(value)) for value in profile.allowed):
                raise _error("wireguard_changed")
        if not self._same(container, self._container()) or self._fingerprints() != fingerprints:
            raise _error("wireguard_changed")
        deadline = int(time.time()) + 20
        hashes = state["hashes"] if state else ["0" * 64] * 4
        payload = (str(deadline) + "\n" + shape + "\n" + _hash(profile.raw) + "\n" + public + "\n" + "\n".join(hashes) + "\n").encode() + profile.raw
        pending = {"schema": 1, "intent_id": uuid.uuid4().hex,
                   **{key: container[key] for key in ("id", "image_id", "pid", "started_at")}, "until": time.time() + 45}
        self.app.store.setting_set(PENDING_KEY, pending)
        command_error = False
        try:
            result = self._exec(container, ["/bin/bash", "--noprofile", "--norc", "-c", OUTER],
                                input_data=payload, timeout=45, max_bytes=32768)
            command_error = "AQUAHUB_WIREGUARD_REACTIVATED" not in result.splitlines()
        except Exception:
            command_error = True
        # An early CLI failure is not proof that its remote process is gone.
        # Probe under its lock; the stdin expiry also fences delayed exec starts.
        try:
            instance = self._container()
            if not self._same(container, instance):
                raise _error("wireguard_changed")
            # A successful exec has returned from the in-container flock holder.
            # On failure retain the full grace period, even if a probe is free.
            if not command_error:
                free = self._exec(container, ["/bin/bash", "--noprofile", "--norc", "-c", LOCK_PROBE], timeout=9, max_bytes=1024)
                if free.strip() != "AQUAHUB_LOCK_FREE":
                    raise _error("wireguard_pending")
                if not self.app.store.setting_compare_and_set(PENDING_KEY, pending, None):
                    raise _error("wireguard_pending")
            after = self._host_snapshot()
            if after["outside"] != before["outside"] or self._fingerprints() != fingerprints:
                raise _error("wireguard_changed")
            self._attest(instance)
            final_shape = self._tunnel_shape(after, profile)
            final = self._wg_state(instance) if final_shape != "absent" else None
            if final and (final["public"] != public or final["peers"] != profile.peers or final["fwmark"] != "off"):
                raise _error("wireguard_unknown")
            self._set_cache(self._dto(final_shape, peers=len(profile.peers), recent=final["recent"] if final else 0))
            if command_error or final_shape != "active":
                raise _error()
            return {"service": "wireguard", "tunnel_active": True, "profile_preserved": True,
                    "external_network_preserved": True, "remote_connectivity_verified": False}
        except ApiError:
            self._set_cache(self._dto(reason="wireguard_unknown"))
            raise
        except Exception:
            self._set_cache(self._dto(reason="wireguard_unknown"))
            raise _error() from None

    def close(self):
        self._closed = True
        with self._cache_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        return thread is None or not thread.is_alive()


def register(app):
    manager = WireGuardManager(app)
    app.wireguard = manager
    app.register_handler("wireguard.status", manager.status, False)
    app.register_handler("wireguard.reactivate", manager.reactivate, True)
    return manager
