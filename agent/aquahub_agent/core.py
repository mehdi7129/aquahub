"""Application orchestration; only this layer schedules serialized mutations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
import uuid

from . import __version__
from .docker import Docker, SERVICES
from .health_history import FAILURES, HealthHistory, event_timestamp
from .security import ApiError, AuthPolicy, RequestContext, redact, safe_value
from .store import Store, timestamp, HISTORY_DAYS, MAX_EVENTS


@dataclass
class Config:
    socket_path: Path = Path("/run/aquahub/agent.sock")
    state_dir: Path = Path("/var/lib/aquahub")
    bundle_path: Path | None = None
    docker_context: str = "default"
    poll_interval: float = 5
    allow_uid: int | None = None
    no_poll: bool = False

    def __post_init__(self):
        self.socket_path = Path(self.socket_path)
        self.state_dir = Path(self.state_dir)
        if self.bundle_path:
            self.bundle_path = Path(self.bundle_path).resolve()


class Application:
    def __init__(self, config: Config, docker=None, policy=None):
        self.config = config
        self.docker = docker or Docker(config.docker_context)
        self.policy = policy or AuthPolicy(config.allow_uid)
        self.store = Store(config.state_dir)
        self._process_lock = open(config.state_dir / "agent.lock", "a+")
        try:
            fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._process_lock.close()
            self.store.close()
            raise ApiError("already_running", "Un agent AquaHub utilise déjà ce dossier d’état.") from None
        self.store.interrupt_jobs()
        self.health_history = HealthHistory(self.store, self.docker)
        self._mutation_lock = threading.Lock()
        self._maintenance_path = config.state_dir / "maintenance.lock"
        descriptor = os.open(self._maintenance_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self._thread_context = threading.local()
        self._stop = threading.Event()
        self._threads = set()
        self._threads_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._collection_lock = threading.Lock()
        self._snapshot = None
        self._snapshot_at = None
        self._snapshot_monotonic = None
        self._last_error = None
        self._previous = {}
        self._events_since = timestamp()
        self._poll_thread = None
        self._handlers = {}
        self._register_builtin()
        self.store.event("history_gap", "Démarrage de la collecte AquaHub. Les événements survenus avant cette collecte ne sont pas garantis.", severity="warning")
        if not config.no_poll:
            self._poll_thread = threading.Thread(target=self._poll_loop, name="aquahub-observer", daemon=True)
            self._poll_thread.start()

    def register_handler(self, method: str, handler, mutation: bool = False):
        if method in self._handlers:
            raise ValueError(f"Handler already registered: {method}")
        self._handlers[method] = (handler, mutation)

    def _register_builtin(self):
        for method, handler, mutation in (
            ("status.get", self._status, False), ("app.version", lambda p,c: {"version": __version__, "stage": "preproduction", "protocol": 1}, False),
            ("service.logs", self._logs, False), ("events.list", self._events, False),
            ("events.logs", self._event_logs, False),
            ("jobs.get", self._job, False), ("settings.get", self._settings, False),
            ("settings.save", self._save_settings, True), ("diagnostics.export", self._diagnostics, False),
            ("portainer.restart", self._restart_portainer, True),
        ):
            self.register_handler(method, handler, mutation)
        for operation in ("start", "stop", "restart"):
            self.register_handler(f"service.{operation}", lambda p,c,op=operation: self._service_action(op,p,c), True)

    def dispatch(self, method: str, params: dict | None = None, context: RequestContext | None = None):
        if context is None:
            raise ApiError("unauthorized", "L’identité du demandeur est obligatoire.")
        if not isinstance(method, str) or method not in self._handlers:
            raise ApiError("unknown_method", "Cette fonction n’existe pas.")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ApiError("invalid_params", "Les paramètres doivent être un objet JSON.")
        handler, mutation = self._handlers[method]
        self.policy.authorize(context, mutation)
        key = context.idempotency_key
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise ApiError("invalid_params", "La clé d’opération doit contenir de 1 à 128 caractères.")
        self._thread_context.idempotency_key = key
        try:
            return safe_value(handler(params, context))
        finally:
            self._thread_context.idempotency_key = None

    @staticmethod
    def _check_keys(params, allowed):
        unknown = set(params) - set(allowed)
        if unknown:
            raise ApiError("invalid_params", "Paramètre non reconnu : " + ", ".join(sorted(unknown)))

    @staticmethod
    def _integer(value, low, high, name):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ApiError("invalid_params", f"{name} doit être un entier entre {low} et {high}.")
        return value

    def _service(self, params):
        value = params.get("service")
        if not isinstance(value, str) or value not in SERVICES:
            raise ApiError("invalid_service", "Composant inconnu.")
        return value

    def _collect(self):
        with self._collection_lock:
            return self._do_collect()

    def _do_collect(self):
        now = timestamp()
        try:
            rows = self.docker.containers()
            if not self.config.bundle_path:
                self.config.bundle_path = self.docker.discover_bundle(rows)
            portainer = self.docker.portainer()
            observed_events, events_error = [], None
            try:
                observed_events = self.docker.events(self._events_since, now)
            except ApiError as error:
                events_error = error
            failure_events = {}
            for event in observed_events:
                service = event.get("Actor", {}).get("Attributes", {}).get("com.docker.compose.service")
                action = str(event.get("Action", event.get("status", "unknown")))[:200]
                if service in SERVICES and action in FAILURES:
                    failure_events.setdefault(service, event)

            def failure_excerpt(event, service, action):
                identifier = event.get("Actor", {}).get("ID")
                key = f"{event.get('timeNano',event.get('time'))}:{identifier}:{action}"
                return self.health_history.capture(key, service, identifier, action, event_timestamp(event))

            for row in rows:
                name = row["service"]
                state = (row.get("state"), row.get("health"), row.get("restart_count", 0))
                previous = self._previous.get(name)
                failed = state[0] in {"exited", "dead", "restarting", "missing"} or state[1] == "unhealthy"
                if (previous is not None and previous != state) or (previous is None and failed):
                    severity = "error" if failed else "info"
                    details = {"before": list(previous) if previous is not None else None, "after": list(state)}
                    if failed:
                        matching_event = failure_events.get(name)
                        if matching_event:
                            action = str(matching_event.get("Action", matching_event.get("status", "unknown")))[:200]
                            excerpt = failure_excerpt(matching_event, name, action)
                        else:
                            excerpt = self.health_history.capture(f"observed:{name}:{now}:{row.get('id')}:{state}",
                                name, row.get("id"), f"observed: {state[0]}, {state[1]}", now)
                        details["log_snapshot_id"] = excerpt["id"]
                    self.store.event("state_change" if previous is not None else "state_observed",
                                     f"{row.get('label',name)} : {state[0]}, santé {state[1]}.", service=name,
                                     severity=severity, details=details)
                self._previous[name] = state
            try:
                if events_error:
                    raise events_error
                for event in observed_events:
                    attrs = event.get("Actor", {}).get("Attributes", {})
                    service = attrs.get("com.docker.compose.service")
                    if service not in SERVICES:
                        continue
                    action = str(event.get("Action", event.get("status", "unknown")))[:200]
                    if action not in {"create", "start", "stop", "die", "restart", "kill", "oom", "destroy", "pause", "unpause", "update",
                                      "health_status: healthy", "health_status: unhealthy", "health_status: starting"}:
                        # exec_* actions may embed the executed command and its secrets.
                        continue
                    key = f"{event.get('timeNano',event.get('time'))}:{event.get('Actor',{}).get('ID')}:{action}"
                    details = {"action": action, "exit_code": attrs.get("exitCode")}
                    if action in FAILURES:
                        details["log_snapshot_id"] = failure_excerpt(event, service, action)["id"]
                    failed = action in {'oom','health_status: unhealthy'} or (action=='die' and attrs.get('exitCode') not in ('0',0))
                    self.store.event("docker_event", f"Docker : {action}", service=service,
                                     severity="error" if failed else "info",
                                     details=details, dedupe=key)
                self._events_since = now
            except ApiError as error:
                self.store.event("history_gap", "Collecte des événements Docker interrompue : " + error.message, severity="warning")
                self._events_since = now
            with self._snapshot_lock:
                recovering = self._last_error is not None
                self._snapshot = {"services": rows, "portainer": portainer}
                self._snapshot_at = now
                self._snapshot_monotonic = time.monotonic()
                self._last_error = None
            if recovering:
                self.store.event("collection_resumed", "La supervision Docker est rétablie ; une interruption de collecte a eu lieu.", severity="warning")
            return rows
        except ApiError as error:
            with self._snapshot_lock:
                first = self._last_error is None
                self._last_error = {"code": error.code, "message": error.message}
            if first:
                self.store.event("history_gap", "Supervision indisponible : " + error.message, severity="error")
            return None

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self._collect()
            except Exception:
                # A failed collector must never silently disappear.
                with self._snapshot_lock:
                    self._last_error = {"code": "collector_error", "message": "Erreur interne de collecte ; les valeurs affichées peuvent être anciennes."}
                self.store.event("history_gap", "Erreur interne de collecte. Consulter le diagnostic AquaHub.", severity="error")
            self._stop.wait(max(1, self.config.poll_interval))

    def _status(self, params, context):
        self._check_keys(params, ())
        if self._snapshot is None or self.config.no_poll:
            self._collect()
        with self._snapshot_lock:
            expired = self._snapshot_monotonic is not None and time.monotonic() - self._snapshot_monotonic > max(15, self.config.poll_interval * 3)
            snapshot = dict(self._snapshot or {"services": [], "portainer": {"available": False, "url": None}})
            error = self._last_error or ({"code": "stale_snapshot", "message": "La collecte est en retard ; vérifier la supervision."} if expired else None)
            snapshot.update({"collected_at": self._snapshot_at, "stale": error is not None,
                             "error": error, "bundle_path": str(self.config.bundle_path) if self.config.bundle_path else None,
                             "version": __version__, "jobs": self.store.jobs(20)})
        snapshot["summary"] = {"total": len(SERVICES), "running": sum(bool(row.get("running")) for row in snapshot["services"]),
                               "unhealthy": sum(row.get("health") == "unhealthy" for row in snapshot["services"]),
                               "without_healthcheck": sum(row.get("health") == "not_defined" for row in snapshot["services"])}
        snapshot["active_job"] = self.store.active_job()
        # Stable UI DTO: the transport id is the Compose service, not the
        # ephemeral Docker container id used internally by the adapter.
        snapshot["services"] = [{
            **row,
            "container_id": row.get("id"),
            "id": row["service"],
            "name": row.get("label", row["service"]),
            "restarts": row.get("restart_count", 0),
        } for row in snapshot["services"]]
        snapshot["machine"] = os.uname().nodename
        snapshot["docker_available"] = self._snapshot is not None and error is None
        snapshot["portainer_available"] = bool(snapshot.get("portainer", {}).get("available"))
        return snapshot

    def _logs(self, params, context):
        self._check_keys(params, ("service", "tail"))
        service = self._service(params)
        tail = self._integer(params.get("tail", 200), 1, 1000, "Le nombre de lignes")
        return {"service": service, "text": self.docker.logs(service, tail), "tail": tail, "redacted": True}

    def _events(self, params, context):
        self._check_keys(params, ("service", "limit", "before_id"))
        self.health_history.prune()
        service = self._service(params) if params.get("service") is not None else None
        limit = self._integer(params.get("limit", 100), 1, 500, "La limite")
        before=params.get('before_id')
        if before is not None: before=self._integer(before,1,2**63-1,'Le curseur')
        events=self.store.events(service,limit+1,before)
        has_more=len(events)>limit
        events=events[:limit]
        return {"events": self.health_history.enrich(events),
                'has_more':has_more,'next_before_id':events[-1]['id'] if has_more else None,
                'retention':{'days':HISTORY_DAYS,'max_events':MAX_EVENTS},
                "log_retention": {"days": self.health_history.retention_days,
                    "max_bytes": self.health_history.max_total_bytes,
                    "excerpt_max_bytes": self.health_history.max_entry_bytes,
                    "notice": "Extraits de pannes bornés et expurgés ; leur expiration n’efface pas les événements."}}

    def _event_logs(self, params, context):
        self._check_keys(params, ("snapshot_id",))
        return self.health_history.get(params.get("snapshot_id"))

    def _job(self, params, context):
        self._check_keys(params, ("id",))
        if not isinstance(params.get("id"), str):
            raise ApiError("invalid_params", "L’identifiant de tâche est obligatoire.")
        return self.store.job(params["id"])

    def _service_action(self, operation, params, context):
        self._check_keys(params, ("service",))
        service = self._service(params)
        if service in {"net_setup", "wireguard", "gcs_reactor"}:
            raise ApiError("network_guard", "Ce composant peut configurer des adresses réseau. Action bloquée pour garantir qu’aucune IP ne change.")
        return self.submit_job(f"service.{operation}", service, {"service": service}, lambda: self.docker.action(service, operation))

    def _restart_portainer(self, params, context):
        self._check_keys(params, ('confirmed',))
        if params.get('confirmed') is not True:
            raise ApiError('confirmation_required','Confirmez le redémarrage de Portainer. Ses comptes et volumes seront conservés.')
        return self.submit_job('portainer.restart','Portainer',{},self.docker.restart_portainer)

    def submit_job(self, kind: str, target: str, payload: dict, work, idempotency_key: str | None = None):
        if self._stop.is_set():
            raise ApiError("agent_stopping", "L’agent est en cours d’arrêt.")
        key = idempotency_key or getattr(self._thread_context, "idempotency_key", None)
        # A repeated click with the same key returns its original job even while locked.
        if key:
            job, created = self.store.create_job(kind, target, payload, key)
            if not created:
                return job
        else:
            job = None
        if not self._mutation_lock.acquire(blocking=False):
            if job:
                self.store.set_job(job["id"], "failed", error={"code": "busy", "message": "Une autre opération est en cours."})
            raise ApiError("busy", "Une opération est déjà en cours. Attendre sa fin avant de continuer.")
        maintenance_fd = None
        try:
            maintenance_fd = os.open(self._maintenance_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(maintenance_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ApiError("maintenance", "L’application est en cours de mise à jour. Attendre la fin de l’installation.") from None
            if job is None:
                job, _ = self.store.create_job(kind, target, payload)
            self.store.event("job_queued", f"Opération demandée : {kind}.", service=target if target in SERVICES else None,
                             details={"job_id": job["id"]})
            thread = threading.Thread(target=self._run_job, args=(job, work, maintenance_fd), name=f"aquahub-job-{job['id'][:8]}", daemon=True)
            with self._threads_lock:
                self._threads.add(thread)
            thread.start()
            return job
        except BaseException as error:
            if maintenance_fd is not None:
                os.close(maintenance_fd)
            if job is not None:
                self.store.set_job(job["id"], "failed", error={
                    "code": error.code if isinstance(error, ApiError) else "job_start_failed",
                    "message": error.message if isinstance(error, ApiError) else "L’opération n’a pas démarré."})
            self._mutation_lock.release()
            raise

    def _run_job(self, job, work, maintenance_fd=None):
        job_id = job["id"]
        service = job["target"] if job["target"] in SERVICES else None
        try:
            self.store.set_job(job_id, "running")
            result = work()
            self.store.set_job(job_id, "succeeded", result=result)
            self.store.event("job_succeeded", f"Opération terminée : {job['kind']}.", service=service, details={"job_id": job_id})
        except ApiError as error:
            self.store.set_job(job_id, "failed", error={"code": error.code, "message": error.message})
            self.store.event("job_failed", f"Échec de {job['kind']} : {error.message}", service=service, severity="error", details={"job_id": job_id})
        except Exception:
            self.store.set_job(job_id, "failed", error={"code": "internal_error", "message": "L’opération a échoué. Vérifier l’état réel avant de relancer."})
            self.store.event("job_failed", f"Erreur interne pendant {job['kind']}.", service=service, severity="error", details={"job_id": job_id})
        finally:
            if maintenance_fd is not None:
                os.close(maintenance_fd)
            self._mutation_lock.release()
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def _settings(self, params, context):
        self._check_keys(params, ())
        return {"github_url": self.store.setting_get("github_url", ""), "theme": self.store.setting_get("theme", "system")}

    def _save_settings(self, params, context):
        self._check_keys(params, ("github_url", "theme"))
        if "theme" in params and (not isinstance(params["theme"], str) or params["theme"] not in {"light", "dark", "system"}):
            raise ApiError("invalid_params", "Thème inconnu.")
        if "github_url" in params:
            value = params["github_url"]
            if not isinstance(value, str) or len(value) > 450:
                raise ApiError("invalid_params", "URL du dépôt Git invalide.")
            if value:
                from .updates import normalise_repository
                try:
                    params = {**params, "github_url": normalise_repository(value)}
                except ApiError as exc:
                    raise ApiError("invalid_params", exc.message) from None
        for key, value in params.items():
            self.store.setting_set(key, value)
        return self._settings({}, context)

    def _diagnostics(self, params, context):
        self._check_keys(params, ())
        self.health_history.prune()
        exports = self.config.state_dir / "exports"
        exports.mkdir(mode=0o700, exist_ok=True)
        path = exports / f"aquahub-diagnostic-{uuid.uuid4().hex[:12]}.json"
        status = self._status({}, context)
        # Job results can contain long update reports. Diagnostics keep their audit metadata.
        status["jobs"] = [{key: job.get(key) for key in ("id", "kind", "target", "status", "created_at", "started_at", "finished_at", "error")} for job in status["jobs"]]
        if status.get("active_job"):
            status["active_job"] = {key: status["active_job"].get(key) for key in ("id", "kind", "target", "status")}
        events = self.health_history.enrich(self.store.events(limit=100))
        payload = {"version": __version__, "generated_at": timestamp(), "status": status,
                   "events": events, "failure_log_excerpts": self.health_history.export(events),
                   "notice": "Logs filtrés ; au plus dix extraits de pannes (256 Kio au total). Aucun fichier de configuration ni clé privée inclus. Un extrait ne constitue pas l’historique intégral des logs."}
        raw = json.dumps(safe_value(payload), ensure_ascii=False, indent=2).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        # The authenticated operator must be able to save this private artifact from GTK.
        if os.geteuid() == 0 and context.uid != 0:
            os.chown(path, context.uid, -1)
        return {"path": str(path), "filename": path.name, "bytes": len(raw), "content": raw.decode()}

    def close(self):
        self._stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=45)
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=60)
        # Long-running extension jobs may still hold references. Do not close beneath them.
        if any(thread.is_alive() for thread in threads) or (self._poll_thread and self._poll_thread.is_alive()):
            return
        self.store.close()
        self._process_lock.close()
