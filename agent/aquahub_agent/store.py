"""Durable, bounded audit records. Secrets never belong in this database."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from .security import ApiError, redact, safe_value

HISTORY_DAYS = 365
MAX_EVENTS = 100000


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / "aquahub.sqlite3"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            kind TEXT NOT NULL, service TEXT, severity TEXT NOT NULL,
            message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', dedupe TEXT UNIQUE);
          CREATE INDEX IF NOT EXISTS events_service ON events(service,id);
          CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp);
          CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
            finished_at TEXT, payload TEXT NOT NULL, result TEXT, error TEXT,
            idempotency_key TEXT UNIQUE, fingerprint TEXT NOT NULL);
        """)
        # Existing customer history survives application upgrades unchanged.
        if "progress" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN progress TEXT")
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def setting_get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def setting_set(self, key: str, value):
        with self.transaction() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))

    def event(self, kind: str, message: str, service: str | None = None,
              severity: str = "info", details=None, dedupe: str | None = None,
              at: str | None = None):
        serialized = json.dumps(safe_value(details or {}), ensure_ascii=False)
        if len(serialized.encode()) > 2048:
            serialized = json.dumps({"truncated": True, "preview": redact(serialized, 512)}, ensure_ascii=False)
        with self.transaction() as db:
            cursor=db.execute("INSERT OR IGNORE INTO events(timestamp,kind,service,severity,message,details,dedupe) VALUES(?,?,?,?,?,?,?)",
                       (at or timestamp(), kind, service, severity, redact(message, 1000), serialized, dedupe))
            if cursor.rowcount:
                cutoff=(datetime.now(timezone.utc)-timedelta(days=HISTORY_DAYS)).isoformat(timespec='milliseconds').replace('+00:00','Z')
                db.execute('DELETE FROM events WHERE id<=? OR timestamp<?',(cursor.lastrowid-MAX_EVENTS,cutoff))

    def events(self, service: str | None = None, limit: int = 100, before_id: int | None = None) -> list[dict]:
        with self.lock:
            clauses=[]; values=[]
            if service: clauses.append('service=?'); values.append(service)
            if before_id is not None: clauses.append('id<?'); values.append(before_id)
            where=' WHERE '+' AND '.join(clauses) if clauses else ''
            rows=self.db.execute('SELECT * FROM events'+where+' ORDER BY id DESC LIMIT ?',(*values,limit))
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def active_job(self):
        with self.lock:
            row=self.db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            return self._job(row) if row else None

    def create_job(self, kind: str, target: str, payload: dict, idempotency_key: str | None = None) -> tuple[dict, bool]:
        canonical = json.dumps([kind, target, payload], sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        with self.transaction() as db:
            if idempotency_key:
                row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise ApiError("idempotency_conflict", "Cette clé d’opération a déjà été utilisée pour une autre action.")
                    return self._job(row), False
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,kind,target,status,created_at,payload,idempotency_key,fingerprint) VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, kind, target, "queued", timestamp(), json.dumps(safe_value(payload)), idempotency_key, fingerprint))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    @staticmethod
    def _job(row) -> dict:
        result = dict(row)
        result.pop("fingerprint", None)
        result.pop("idempotency_key", None)
        for key in ("payload", "result", "error", "progress"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def job(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ApiError("not_found", "Cette tâche n’existe pas.")
            return self._job(row)

    def jobs(self, limit=20):
        with self.lock:
            return [self._job(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def set_job(self, job_id: str, status: str, result=None, error=None):
        with self.transaction() as db:
            start = timestamp() if status == "running" else None
            finish = timestamp() if status in {"succeeded", "failed", "interrupted"} else None
            db.execute("UPDATE jobs SET status=?,started_at=COALESCE(?,started_at),finished_at=?,result=?,error=? WHERE id=?",
                       (status, start, finish, json.dumps(safe_value(result)) if result is not None else None,
                        json.dumps(safe_value(error)) if error is not None else None, job_id))

    def set_job_progress(self, job_id: str, progress: dict):
        """Keep the last observed milestone across polling and agent restarts."""
        serialized = json.dumps(safe_value(progress), ensure_ascii=False)
        if len(serialized.encode()) > 4096:
            raise ValueError("Job progress exceeds its storage bound")
        with self.transaction() as db:
            db.execute("UPDATE jobs SET progress=? WHERE id=? AND status='running'", (serialized, job_id))

    def interrupt_jobs(self):
        with self.transaction() as db:
            count = db.execute("UPDATE jobs SET status='interrupted',finished_at=?,error=? WHERE status IN ('running','queued')",
                               (timestamp(), json.dumps({"code": "agent_restarted", "message": "Agent interrompu. Résultat non confirmé ; vérifier l’état réel avant de relancer."}))).rowcount
        if count:
            self.event("jobs_interrupted", f"{count} tâche(s) interrompue(s) ; leur résultat doit être vérifié.", severity="warning")
        return count
