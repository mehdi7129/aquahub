"""Private, bounded log excerpts attached to observed service failures.

These are captured excerpts, never an invented complete log archive. Events
survive expiration of their excerpts, whose absence is explicit in the API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import time

from .docker import PROJECT, SERVICES
from .security import ApiError, redact
from .store import timestamp

MAX_ENTRY_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 1024
RETENTION_DAYS = 90
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")
FAILURES = frozenset(("die", "oom", "health_status: unhealthy"))
_IDENTITY_FORMAT = ('{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                    '"project":{{json (index .Config.Labels "com.docker.compose.project")}}}')


class HealthHistory:
    def __init__(self, store, docker, *, max_entry_bytes=MAX_ENTRY_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES, max_records=MAX_RECORDS,
                 retention_days=RETENTION_DAYS, minimum_interval=10):
        self.store, self.docker = store, docker
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes
        self.max_records = max_records
        self.retention_days = retention_days
        self.minimum_interval = minimum_interval
        self._last_capture = {}
        self._lock = threading.RLock()
        with store.transaction() as database:
            database.executescript("""
              CREATE TABLE IF NOT EXISTS health_logs(
                id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                service TEXT NOT NULL, container_id TEXT, reason TEXT NOT NULL,
                captured_at TEXT NOT NULL, event_at TEXT,
                status TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                bytes INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0,
                error TEXT);
              CREATE INDEX IF NOT EXISTS health_logs_time ON health_logs(captured_at);
            """)
            database.execute("UPDATE health_logs SET status='interrupted',error=? WHERE status='pending'",
                ("La collecte de cet extrait a été interrompue. L’événement reste conservé.",))
        self.prune()

    def prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.store.transaction() as database:
            database.execute("DELETE FROM health_logs WHERE captured_at<?", (cutoff,))
            database.execute("DELETE FROM health_logs WHERE id NOT IN (SELECT id FROM health_logs ORDER BY captured_at DESC,rowid DESC LIMIT ?)", (self.max_records,))
            total = database.execute("SELECT COALESCE(SUM(bytes),0) FROM health_logs").fetchone()[0]
            while total > self.max_total_bytes:
                oldest = database.execute("SELECT id,bytes FROM health_logs ORDER BY captured_at,rowid LIMIT 1").fetchone()
                if not oldest:
                    break
                database.execute("DELETE FROM health_logs WHERE id=?", (oldest["id"],))
                total -= oldest["bytes"]

    @staticmethod
    def _summary(row):
        value = dict(row)
        value.pop("event_key", None)
        value.pop("text", None)
        value["truncated"] = bool(value["truncated"])
        value["available"] = value["status"] in {"available", "empty"}
        value["redacted"] = True
        return value

    def summary(self, identifier):
        if not isinstance(identifier, str) or not SNAPSHOT_ID.fullmatch(identifier):
            raise ApiError("invalid_params", "Identifiant d’extrait de panne invalide.")
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM health_logs WHERE id=?", (identifier,)).fetchone()
        return self._summary(row) if row else {"id": identifier, "status": "expired", "available": False,
            "message": "Cet extrait a expiré ou a été supprimé par la limite de conservation. L’événement reste dans l’historique."}

    def get(self, identifier):
        self.prune()
        summary = self.summary(identifier)
        if summary["status"] == "expired":
            raise ApiError("log_expired", summary["message"])
        with self.store.lock:
            row = self.store.db.execute("SELECT text FROM health_logs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ApiError("log_expired", "Cet extrait a expiré pendant sa consultation.")
        return {**summary, "text": redact(row["text"], self.max_entry_bytes),
                "notice": "Extrait borné capturé lors du constat ; ne constitue pas l’intégralité des logs du service."}

    def _finish(self, identifier, status, text="", error=None, truncated=False):
        encoded = text.encode("utf-8")
        text = encoded[-self.max_entry_bytes:].decode("utf-8", "ignore")
        with self.store.transaction() as database:
            database.execute("UPDATE health_logs SET status=?,text=?,bytes=?,truncated=?,error=? WHERE id=?",
                (status, text, len(text.encode("utf-8")), bool(truncated or len(encoded) > self.max_entry_bytes),
                 redact(error, 500) if error else None, identifier))
        self.prune()
        return self.summary(identifier)

    def capture(self, event_key, service, container_id, reason, event_at=None):
        if service not in SERVICES:
            raise ApiError("invalid_service", "Composant inconnu pour la collecte d’un extrait.")
        if not isinstance(event_key, str) or not 1 <= len(event_key) <= 512:
            raise ApiError("invalid_params", "Clé d’événement invalide pour la collecte d’un extrait.")
        identifier = hashlib.sha256(event_key.encode()).hexdigest()
        with self._lock:
            with self.store.lock:
                existing = self.store.db.execute("SELECT * FROM health_logs WHERE id=?", (identifier,)).fetchone()
            if existing:
                return self._summary(existing)
            with self.store.transaction() as database:
                database.execute("INSERT INTO health_logs(id,event_key,service,container_id,reason,captured_at,event_at,status) VALUES(?,?,?,?,?,?,?,'pending')",
                    (identifier, event_key, service, container_id, redact(reason, 200), timestamp(), event_at))
            if not isinstance(container_id, str) or not CONTAINER_ID.fullmatch(container_id):
                return self._finish(identifier, "unavailable", error="Identité du conteneur absente ou non vérifiable ; aucun log d’un autre conteneur n’est utilisé.")
            if time.monotonic() - self._last_capture.get(service, float("-inf")) < self.minimum_interval:
                return self._finish(identifier, "rate_limited", error="Extrait non collecté : limite de fréquence atteinte. L’événement est conservé.")
            self._last_capture[service] = time.monotonic()
            try:
                raw = self.docker.run(["inspect", "--type", "container", "--format", _IDENTITY_FORMAT, container_id], timeout=3, max_bytes=4096)
                identity = json.loads(raw)
                if not isinstance(identity, dict) or identity != {"service": service, "project": PROJECT}:
                    return self._finish(identifier, "unavailable", error="Le conteneur observé ne correspond pas au service et au projet Aqua attendus.")
                arguments = ["logs", "--timestamps", "--tail", "200"]
                if event_at:
                    try:
                        instant = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
                        arguments += ["--since", (instant - timedelta(seconds=120)).isoformat(),
                                      "--until", (instant + timedelta(seconds=5)).isoformat()]
                    except (TypeError, ValueError, OverflowError):
                        pass
                raw = self.docker.run([*arguments, container_id], timeout=3, max_bytes=2 * 1024**2, combine=True)
                if not isinstance(raw, str):
                    raise ValueError("invalid log output")
                text = redact(raw, self.max_entry_bytes * 4)
                truncated = len(raw.encode("utf-8")) > self.max_entry_bytes
                return self._finish(identifier, "available" if text else "empty", text, truncated=truncated)
            except ApiError as exc:
                return self._finish(identifier, "unavailable", error="Extrait non disponible : " + exc.message)
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                return self._finish(identifier, "unavailable", error="Extrait non disponible : le conteneur est absent ou son accès n’a pas pu être vérifié.")

    def enrich(self, events):
        for event in events:
            identifier = event.get("details", {}).get("log_snapshot_id")
            if identifier:
                event["log_snapshot"] = self.summary(identifier)
        return events

    def export(self, events, *, max_records=10, max_bytes=256 * 1024):
        result, seen, total = [], set(), 0
        for event in events:
            identifier = event.get("details", {}).get("log_snapshot_id")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            summary = self.summary(identifier)
            if summary["status"] == "expired":
                continue
            item = self.get(identifier)
            size = len(item["text"].encode("utf-8"))
            if total + size > max_bytes:
                break
            result.append(item)
            total += size
            if len(result) >= max_records:
                break
        return result


def event_timestamp(event):
    try:
        value = int(event["timeNano"]) / 1_000_000_000 if event.get("timeNano") else int(event["time"])
        instant = datetime.fromtimestamp(value, timezone.utc)
        if instant.year < 2020 or instant > datetime.now(timezone.utc) + timedelta(minutes=5):
            return None
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
