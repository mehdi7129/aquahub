"""Read-only, bounded snapshots of the known supplier containers' logs.

The agent returns text and metadata only. A user-session native dialog chooses
and writes the destination; a renderer can never ask this root process to write
an arbitrary file.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
import time

from .docker import SERVICES
from .security import ApiError, redact

MAX_TAIL = 1000
MAX_TEXT_BYTES = 48 * 1024
MAX_DOCKER_BYTES = 192 * 1024
COLLECTION_SECONDS = 75


def _tail_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", "ignore")


class LogExports:
    def __init__(self, application):
        self.application = application
        self._lock = threading.Lock()

    def export(self, params, _context):
        if set(params) - {"service", "tail"}:
            raise ApiError("invalid_params", "Seuls le composant et le nombre de lignes sont acceptés.")
        service = params.get("service")
        if "service" in params and (not isinstance(service, str) or service not in SERVICES):
            raise ApiError("invalid_service", "Composant inconnu.")
        tail = params.get("tail", MAX_TAIL)
        if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= MAX_TAIL:
            raise ApiError("invalid_params", "Le nombre de lignes doit être compris entre 1 et 1000.")
        if not self._lock.acquire(blocking=False):
            raise ApiError("busy", "Un export des journaux est déjà en cours.")
        try:
            return self._collect([service] if service else list(SERVICES), tail)
        finally:
            self._lock.release()

    def _collect(self, services, tail):
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        deadline = time.monotonic() + COLLECTION_SECONDS
        # Resolve the supplier project once, rather than inspecting it eight times.
        try:
            rows = {item["service"]: item for item in self.application.docker.containers()}
            snapshot_error = None
        except ApiError as error:
            rows, snapshot_error = {}, error
        entries = []
        for service in services:
            item = {"service": service, "label": SERVICES[service][0], "state": "unavailable",
                    "text": "", "lines": 0, "truncated": False, "error": None}
            entries.append(item)
            row = rows.get(service) or {}
            identifier = row.get("id")
            if snapshot_error:
                item["error"] = {"code": snapshot_error.code, "message": redact(snapshot_error.message, 1000)}
                continue
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                item["error"] = {"code": "missing_service", "message": "Le conteneur de ce composant est absent."}
                continue
            requested = tail
            # A large single line must never produce an unbounded RPC response.
            # If necessary, retry a smaller tail and report the loss explicitly.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    item["error"] = {"code": "timeout", "message": "La durée maximale de collecte a été atteinte."}
                    break
                try:
                    output = self.application.docker.run(
                        ["logs", "--timestamps", "--tail", str(requested), identifier],
                        timeout=min(6, remaining), max_bytes=MAX_DOCKER_BYTES, combine=True)
                except ApiError as error:
                    if error.code == "output_limit" and requested > 1:
                        item["truncated"] = True
                        requested = max(1, requested // 2)
                        continue
                    item["error"] = {"code": error.code, "message": redact(error.message, 1000)}
                    if error.code == "output_limit":
                        item["truncated"] = True
                    break
                # Redact before clipping, so a PEM key crossing the clipping
                # boundary cannot survive as an unrecognizable key fragment.
                text = redact(output, MAX_DOCKER_BYTES)
                lines = len(text.splitlines())
                if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                    item["truncated"] = True
                    text = _tail_bytes(text, MAX_TEXT_BYTES)
                # Docker returns at most the requested tail. Reaching that limit
                # means older lines may exist; do not claim an exhaustive history.
                item["truncated"] = item["truncated"] or lines >= requested
                item.update(text=text, lines=len(text.splitlines()),
                            state="truncated" if item["truncated"] else "available" if text else "empty")
                break
        return {"schema": 1, "created_at": created_at, "tail": tail, "entries": entries,
                "partial": any(item["state"] in {"unavailable", "truncated"} for item in entries),
                "notice": "Instantané des journaux Docker disponibles, filtré et limité aux dernières lignes ; ce fichier ne constitue pas un historique exhaustif."}


def register(application):
    module = LogExports(application)
    application.register_handler("logs.export", module.export, False)
