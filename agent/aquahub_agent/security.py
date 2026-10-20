"""Small, deliberately closed authorization and output filtering boundary."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RequestContext:
    uid: int
    pid: int
    start_time: str = ""
    idempotency_key: str | None = None


def process_start_time(pid: int) -> str:
    try:
        # comm may contain spaces and parentheses; fields after its last ')' start at 3.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        raise ApiError("unauthorized", "Le processus demandeur n’est plus disponible.") from None


class AuthPolicy:
    """Peer credentials authenticate reads; PolicyKit separately gates root mutations."""
    action_id = "com.aquadrone.aquahub.maintain"

    def __init__(self, allowed_uid: int | None = None, privileged: bool | None = None):
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.privileged = os.geteuid() == 0 if privileged is None else privileged

    def authorize(self, context: RequestContext, mutation: bool = False) -> None:
        if context.uid != self.allowed_uid:
            raise ApiError("unauthorized", "Ce compte n’est pas autorisé à utiliser AquaHub.")
        if mutation and self.privileged:
            current = process_start_time(context.pid)
            if not context.start_time or current != context.start_time:
                raise ApiError("unauthorized", "L’identité du processus a changé.")
            try:
                result = subprocess.run(
                    ["/usr/bin/pkcheck", "--action-id", self.action_id, "--process",
                     f"{context.pid},{context.start_time},{context.uid}", "--allow-user-interaction"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=120, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ApiError("unauthorized", "L’autorisation administrateur n’est pas disponible.") from None
            if result.returncode != 0 or process_start_time(context.pid) != current:
                raise ApiError("unauthorized", "L’autorisation administrateur a été refusée.")


_ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(
    r"(?im)(\b(?:[a-z0-9]+[_-])*(?:private[_-]?key|preshared[_-]?key|password|passwd|secret|token|api[_-]?key|authorization|ntrip_username)(?:[_-][a-z0-9]+)*\b\s*(?:[:=]\s*|\s+))(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL = re.compile(r"([a-z][a-z0-9+.-]*://)([^\s/@]+(?::[^\s/@]*)?@)", re.I)
_PEM = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)", re.S)
_WG_KEY = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/])")
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=:-]+")
_SENSITIVE_KEY = re.compile(r"(?i)(private[_-]?key|preshared[_-]?key|password|passwd|secret|token|api[_-]?key|authorization|ntrip_username)")


def redact(value: str, limit: int = 131072) -> str:
    value = _ANSI.sub("", value)
    value = _PEM.sub("[CLÉ PRIVÉE MASQUÉE]", value)
    value = _URL.sub(r"\1[IDENTIFIANTS MASQUÉS]@", value)
    value = _BEARER.sub(r"\1 [MASQUÉ]", value)
    value = _SECRET.sub(r"\1[MASQUÉ]", value)
    value = _WG_KEY.sub("[CLÉ MASQUÉE]", value)
    value = "".join(c for c in value if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
    return value[-limit:]


def safe_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): "[MASQUÉ]" if _SENSITIVE_KEY.search(str(k)) else safe_value(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    return value
