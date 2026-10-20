"""A closed Docker CLI adapter. No shell, remote context, or network reconfiguration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from .security import ApiError, redact


SERVICES = {
    "mosquitto": ("MQTT", "aqua-mosquitto"),
    "dmx_fleet_controller": ("Fleet Controller", "aqua-dmx-fleet"),
    "gcs": ("GCS", "aqua-gcs"),
    "gcs_reactor": ("GCS Reactor", "aqua-gcs-reactor"),
    "net_setup": ("Réseau du bundle", "aqua-net-setup"),
    "mavlink_router": ("MAVLink Router", "aqua-mavlink"),
    "rtcm_mavlink": ("Corrections RTK", "aqua-rtcm-mavlink"),
    "wireguard": ("WireGuard", "aqua-wireguard"),
}
PROJECT = "aqua-hub-bundle"
INSPECT = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
           '"image":{{json .Config.Image}},"state":{{json .State.Status}},'
           '"running":{{json .State.Running}},"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}},'
           '"exit_code":{{json .State.ExitCode}},"started_at":{{json .State.StartedAt}},'
           '"finished_at":{{json .State.FinishedAt}},"restart_count":{{json .RestartCount}},'
           '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
           '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"working_dir":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}}')


class Docker:
    def __init__(self, context: str = "default", executable: str = "/usr/bin/docker"):
        if context != "default":
            raise ValueError("Only the native default Docker context is supported")
        self.context = context
        self.executable = executable

    def run(self, args: list[str], timeout: float = 20, max_bytes: int = 2097152, combine: bool = False) -> str:
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent",
               "DOCKER_CONFIG": "/etc/aquahub/docker-client", "LANG": "C.UTF-8"}
        try:
            proc = subprocess.Popen([self.executable, "--context", self.context, *args],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
        except OSError:
            raise ApiError("docker_unavailable", "Docker natif n’est pas disponible.") from None
        chunks = {"out": bytearray(), "err": bytearray()}
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, "out")
                selector.register(proc.stderr, selectors.EVENT_READ, "err")
                total = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ApiError("timeout", "Docker n’a pas répondu dans le délai prévu.")
                    for key, _ in selector.select(min(remaining, .5)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > max_bytes:
                            raise ApiError("output_limit", "La sortie Docker dépasse la limite ; réduire le nombre de lignes.")
                        chunks[key.data].extend(data)
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise ApiError("timeout", "Docker n’a pas répondu dans le délai prévu.") from None
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        if proc.returncode:
            detail = redact(chunks["err"].decode("utf-8", "replace"), 1000).strip()
            raise ApiError("docker_error", f"Docker a refusé l’opération : {detail or 'erreur non précisée'}")
        output = chunks["out"] + chunks["err"] if combine else chunks["out"]
        return output.decode("utf-8", "replace")

    command = run

    def _inspect(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        try:
            return [json.loads(line) for line in self.run(["inspect", "--format", INSPECT, *ids]).splitlines() if line.strip()]
        except (ValueError, TypeError):
            raise ApiError("docker_output", "La réponse de Docker n’est pas exploitable.") from None

    def containers(self) -> list[dict]:
        raw = self.run(["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"])
        ids = raw.split()
        if len(ids) > 64 or any(not all(c in "0123456789abcdef" for c in item) for item in ids):
            raise ApiError("docker_output", "Le projet Docker comporte une liste inattendue de conteneurs.")
        rows = self._inspect(ids)
        by_service = {}
        for row in rows:
            service = row.get("service")
            if row.get("project") != PROJECT or service not in SERVICES:
                continue
            if service in by_service:
                raise ApiError("ambiguous_service", f"Plusieurs conteneurs correspondent au composant {service}.")
            row["health"] = row.get("health") or "not_defined"
            row["version"] = None
            row["label"] = SERVICES[service][0]
            row["actions"] = self.allowed_actions(service)
            by_service[service] = row
        return [by_service.get(name, {"service": name, "label": label, "name": container,
                                     "state": "missing", "health": "not_defined", "running": False,
                                     "image": None, "image_id": None, "version": None,
                                     "actions": self.allowed_actions(name, missing=True)})
                for name, (label, container) in SERVICES.items()]

    @staticmethod
    def allowed_actions(service: str, missing: bool = False) -> dict:
        guarded = service in {"net_setup", "wireguard", "gcs_reactor"}
        reason = "Conteneur absent." if missing else ("Précontrôle sans modification d’IP non disponible pour ce composant réseau." if guarded else None)
        return {"start": not (guarded or missing), "stop": not (guarded or missing),
                "restart": not (guarded or missing), "disabled_reason": reason}

    def resolve(self, service: str) -> dict:
        if service not in SERVICES:
            raise ApiError("invalid_service", "Composant inconnu.")
        item = next(row for row in self.containers() if row["service"] == service)
        if not item.get("id"):
            raise ApiError("missing_service", "Le conteneur de ce composant est absent.")
        return item

    def action(self, service: str, operation: str) -> dict:
        if operation not in {"start", "stop", "restart"}:
            raise ApiError("invalid_action", "Opération non autorisée.")
        # These containers configure the network at startup. Their mutation is deliberately
        # refused until a provider-supported no-IP-change check exists.
        if service in {"net_setup", "wireguard", "gcs_reactor"}:
            raise ApiError("network_guard", "Ce composant gère des adresses réseau. Action bloquée pour garantir qu’aucune IP ne change.")
        before = self.resolve(service)
        args = [operation]
        if operation in {"stop", "restart"}:
            args += ["--time", "15"]
        self.run([*args, before["id"]], timeout=45)
        expected = "exited" if operation == "stop" else "running"
        # Fleet Controller probes every 30s. A restart may be healthy only after
        # its next probe, so wait in the durable job rather than failing early.
        deadline = time.monotonic() + 75
        while True:
            after = self.resolve(service)
            if after.get("image_id") != before.get("image_id") or after.get("id") != before.get("id"):
                raise ApiError("container_changed", "Le conteneur ou son image a changé pendant l’opération.")
            state_ok = after["state"] == expected
            health = after.get("health")
            if state_ok and (operation == "stop" or health in {"healthy", "not_defined"}):
                return {"service": service, "state": after["state"], "health": health,
                        "image_id": after["image_id"], "health_verified": health == "healthy"}
            if time.monotonic() >= deadline:
                raise ApiError("state_unconfirmed", "La commande a été envoyée mais l’état attendu n’a pas été confirmé. Consulter les journaux.")
            time.sleep(.5)

    def logs(self, service: str, tail: int = 200) -> str:
        item = self.resolve(service)
        return redact(self.run(["logs", "--timestamps", "--tail", str(tail), item["id"]], max_bytes=524288, combine=True))

    def events(self, since: str, until: str) -> list[dict]:
        output = self.run(["events", "--since", since, "--until", until,
                           "--filter", "type=container", "--filter", f"label=com.docker.compose.project={PROJECT}",
                           "--format", "{{json .}}"], timeout=15)
        try:
            return [json.loads(row) for row in output.splitlines() if row.strip()]
        except ValueError:
            raise ApiError("docker_output", "Les événements Docker ne sont pas exploitables.") from None

    def portainer(self) -> dict:
        # Exact names only; do not install a container or expose arbitrary admin links.
        for name in ("aqua-portainer", "portainer"):
            ids = self.run(["ps", "-q", "--filter", f"name=^/{name}$"]).split()
            if ids:
                try:
                    ports = json.loads(self.run(["inspect", "--format", "{{json .NetworkSettings.Ports}}", ids[0]])) or {}
                except ValueError:
                    raise ApiError("docker_output", "Les ports Portainer ne sont pas exploitables.") from None
                for port, scheme in (("9443/tcp", "https"), ("9000/tcp", "http")):
                    for binding in ports.get(port) or []:
                        host = binding.get("HostIp")
                        number = str(binding.get("HostPort", ""))
                        if host in {"0.0.0.0", "127.0.0.1", "::", "::1"} and number.isdigit() and 1 <= int(number) <= 65535:
                            return {"available": True, "url": f"{scheme}://127.0.0.1:{number}", "name": name}
                return {"available": True, "url": None, "name": name, "reason": "Aucun port publié accessible localement."}
        return {"available": False, "url": None}

    def discover_bundle(self, rows: list[dict]) -> Path | None:
        paths = {str(row["working_dir"]) for row in rows if row.get("working_dir")}
        if len(paths) != 1:
            return None
        candidate = Path(paths.pop())
        if candidate.is_absolute() and (candidate / "docker-compose.yml").is_file():
            return candidate.resolve()
        return None

    def restart_portainer(self):
        ids=self.run(["ps","-aq","--filter","name=^/aqua-portainer$"]).split()
        if len(ids)!=1:
            raise ApiError('portainer_unmanaged','Le Portainer installé par AquaHub est introuvable. Aucun autre conteneur ne sera redémarré.')
        template=('{"image":{{json .Config.Image}},"image_id":{{json .Image}},'
                  '"privileged":{{json .HostConfig.Privileged}},"mode":{{json .HostConfig.NetworkMode}},'
                  '"ports":{{json .HostConfig.PortBindings}},"running":{{json .State.Running}}}')
        def inspect():
            try:
                return json.loads(self.run(['inspect','--format',template,ids[0]]))
            except ValueError:
                raise ApiError('docker_output','Les informations Portainer sont invalides.') from None
        before=inspect()
        ports=before.get('ports') or {}
        expected={'9443/tcp':[{'HostIp':'127.0.0.1','HostPort':'9443'}]}
        if (not str(before.get('image','')).startswith('portainer/portainer-ce:') or
            before.get('privileged') is not False or before.get('mode') not in {'bridge','default'} or ports!=expected):
            raise ApiError('portainer_unmanaged','Ce Portainer ne correspond pas à l’installation locale AquaHub. Sa configuration est conservée.')
        self.run(['restart','--time','15',ids[0]],timeout=45)
        after=inspect()
        if not after.get('running') or after.get('image_id')!=before.get('image_id'):
            raise ApiError('state_unconfirmed','Le redémarrage de Portainer n’a pas été confirmé. Ses données sont conservées.')
        return {'name':'aqua-portainer','running':True,'image_id':after['image_id'],'accounts_preserved':True}
