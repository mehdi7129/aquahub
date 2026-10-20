"""Small, bounded JSON RPC client and desktop navigation policy.

This module deliberately does not import GTK, so transport and authorization
boundaries can be exercised without a display server.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
import socket
import uuid
from urllib.parse import unquote, urlsplit

MAX_REPLY = 2 * 1024 * 1024
MAX_REQUEST = 256 * 1024


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_app_uri(uri: str | None, index: Path) -> bool:
    """Only the exact installed entry point is an authorized bridge origin."""
    if not uri:
        return False
    value = urlsplit(uri)
    if value.scheme != "file" or value.netloc not in ("", "localhost"):
        return False
    try:
        return Path(unquote(value.path)).resolve() == index.resolve()
    except (ValueError, OSError):
        return False


def fixed_url(target: str, portainer_port: int = 9443) -> str:
    if not isinstance(portainer_port, int) or not 1 <= portainer_port <= 65535:
        raise BridgeError("invalid_port", "Le port de Portainer est invalide.")
    urls = {
        "gcs": "http://localhost:4200",
        "fleet": "http://localhost:8000",
        "portainer": f"https://localhost:{portainer_port}",
    }
    if target not in urls:
        raise BridgeError("invalid_target", "Ce lien n’est pas autorisé.")
    return urls[target]


def portainer_url(value: str | None) -> str:
    try:
        url=urlsplit(value or '')
        if (url.scheme not in {'http','https'} or url.hostname not in {'localhost','127.0.0.1','::1'} or
            url.username or url.password or url.path not in {'','/'} or url.query or url.fragment or not url.port):
            raise ValueError('unexpected URL')
    except ValueError:
        raise BridgeError('portainer_unavailable','L’adresse locale de Portainer est indisponible. Vérifiez son installation dans les réglages.') from None
    return value


def agent_connection(installed: bool, runtime: Path, system: Path, requested: Path | None = None):
    """Installed applications never create a parallel session agent."""
    path=requested or (system if installed or system.exists() else runtime)
    return path, not installed and path != system


def portainer_credentials(username: str, password: str, confirmation: str) -> dict:
    username=username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}',username):
        raise BridgeError('invalid_username','Utilisez un identifiant simple, sans espace ni accent.')
    if len(password)<12:
        raise BridgeError('weak_password','Le mot de passe doit contenir au moins 12 caractères.')
    if len(password.encode('utf-8'))>72:
        raise BridgeError('invalid_password','Ce mot de passe est trop long. Utilisez une phrase plus courte.')
    if password!=confirmation:
        raise BridgeError('password_mismatch','Les deux mots de passe ne correspondent pas.')
    return {'username':username,'password':password}


class RpcClient:
    def __init__(self, path: Path, timeout: float = 185):
        self.path = Path(path)
        self.timeout = timeout

    def request(self, method: str, params: dict | None = None, *, idempotency_key: str | None = None):
        if not isinstance(method, str) or not method or len(method) > 100:
            raise BridgeError("invalid_request", "La demande est invalide.")
        if params is not None and not isinstance(params, dict):
            raise BridgeError("invalid_request", "Les paramètres sont invalides.")
        request_id = str(uuid.uuid4())
        request = {"id": request_id, "method": method, "params": params or {}}
        if idempotency_key:
            request["idempotency_key"] = idempotency_key
        data = json.dumps(request, ensure_ascii=True).encode() + b"\n"
        if len(data) > MAX_REQUEST:
            raise BridgeError("request_too_large", "La demande est trop volumineuse.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.path))
                connection.sendall(data)
                chunks = bytearray()
                while b"\n" not in chunks:
                    part = connection.recv(min(65536, MAX_REPLY + 1 - len(chunks)))
                    if not part:
                        raise BridgeError("agent_protocol", "La réponse de l’agent est incomplète.")
                    chunks.extend(part)
                    if len(chunks) > MAX_REPLY:
                        raise BridgeError("reply_too_large", "La réponse de l’agent dépasse la limite.")
                response = json.loads(bytes(chunks).split(b"\n", 1)[0])
        except BridgeError:
            raise
        except (FileNotFoundError, ConnectionRefusedError):
            raise BridgeError("agent_unavailable", "L’agent AquaHub est indisponible. Relancez l’application.") from None
        except PermissionError:
            raise BridgeError("permission_denied", "L’accès à l’agent AquaHub n’est pas autorisé.") from None
        except (TimeoutError, socket.timeout):
            raise BridgeError("agent_timeout", "L’agent n’a pas répondu à temps. L’état de la tâche doit être vérifié.") from None
        except (OSError, ValueError, UnicodeError):
            raise BridgeError("agent_protocol", "La communication avec l’agent a échoué.") from None
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise BridgeError("agent_protocol", "L’identifiant de réponse de l’agent est invalide.")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise BridgeError("agent_protocol", "L’agent a renvoyé une erreur invalide.")
            raise BridgeError(str(error.get("code", "agent_error")), str(error.get("message", "La demande a échoué.")))
        if "result" not in response:
            raise BridgeError("agent_protocol", "L’agent a renvoyé une réponse invalide.")
        return response["result"]
