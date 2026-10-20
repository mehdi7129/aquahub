"""Authenticated JSON-lines transport on a private Unix socket only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import struct
import threading

from .security import ApiError, RequestContext, process_start_time, safe_value

MAX_REQUEST_BYTES = 1048576
MAX_RESPONSE_BYTES = 2097152


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        pid, uid, _gid = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            context = RequestContext(uid, pid, process_start_time(pid))
            self.server.application.policy.authorize(context, False)
        except ApiError as error:
            self._reply({"id": None, "error": {"code": error.code, "message": error.message}})
            return
        while True:
            request_id = None
            try:
                line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                    self._reply({"id": None, "error": {"code": "request_too_large", "message": "La requête est trop volumineuse ou incomplète."}})
                    return
                try:
                    request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError("invalid_json", "La requête JSON n’est pas valide.") from None
                if not isinstance(request, dict):
                    raise ApiError("invalid_request", "La requête doit être un objet JSON.")
                if set(request) - {"id", "method", "params", "idempotency_key"}:
                    raise ApiError("invalid_request", "La requête contient des champs inconnus.")
                request_id = request.get("id")
                if isinstance(request_id, bool) or not isinstance(request_id, (str, int, type(None))) or isinstance(request_id, str) and len(request_id) > 128:
                    request_id = None
                    raise ApiError("invalid_request", "Identifiant de requête invalide.")
                context = RequestContext(uid, pid, context.start_time, request.get("idempotency_key"))
                result = self.server.application.dispatch(request.get("method"), request.get("params"), context)
                self._reply({"id": request_id, "result": result})
            except ApiError as error:
                self._reply({"id": request_id, "error": {"code": error.code, "message": error.message}})
            except (socket.timeout, ConnectionError, OSError):
                return
            except Exception:
                self._reply({"id": request_id, "error": {"code": "internal_error", "message": "Erreur interne AquaHub. Aucune information sensible n’est exposée."}})

    def _reply(self, response):
        data = json.dumps(safe_value(response), ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_RESPONSE_BYTES:
            data = json.dumps({"id": response.get("id"), "error": {"code": "response_too_large", "message": "Le résultat dépasse la taille autorisée."}}).encode() + b"\n"
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (OSError, ConnectionError):
            pass


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, application):
        self.application = application
        self._slots = threading.BoundedSemaphore(16)
        path = application.config.socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755 if os.geteuid() == 0 else 0o700)
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid not in {os.getuid(), application.policy.allowed_uid}:
                raise ApiError("socket_conflict", "Le chemin de communication est déjà utilisé par un fichier inattendu.")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.settimeout(.2)
                    probe.connect(str(path))
                except (ConnectionRefusedError, FileNotFoundError):
                    path.unlink(missing_ok=True)
                else:
                    raise ApiError("already_running", "Un agent utilise déjà cette socket.")
        super().__init__(str(path), _RequestHandler)
        os.chmod(path, 0o600)
        if os.geteuid() == 0:
            os.chown(path, application.policy.allowed_uid, -1)
        self._socket_inode = path.stat().st_ino

    def process_request(self, request, client_address):
        if not self._slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default traceback may include request-sensitive data.
        self.application.store.event("rpc_error", "Erreur interne de communication locale.", severity="error")

    def server_close(self):
        super().server_close()
        path = self.application.config.socket_path
        try:
            if path.lstat().st_ino == self._socket_inode:
                path.unlink()
        except FileNotFoundError:
            pass
