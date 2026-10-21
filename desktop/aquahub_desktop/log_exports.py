"""Build and save user-owned log files, without importing GTK or agent code."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from .bridge import BridgeError

SERVICES = frozenset({"mosquitto", "dmx_fleet_controller", "gcs", "gcs_reactor", "net_setup",
                      "mavlink_router", "rtcm_mavlink", "wireguard"})
MAX_TEXT_BYTES = 48 * 1024
MAX_EXPORT_BYTES = 1024 * 1024
STATES = {"available", "empty", "truncated", "unavailable"}


def export_request(params: dict) -> dict:
    if not isinstance(params, dict) or set(params) - {"service"}:
        raise BridgeError("invalid_params", "La demande d’export est invalide.")
    if "service" in params and (not isinstance(params["service"], str) or params["service"] not in SERVICES):
        raise BridgeError("invalid_service", "Composant inconnu.")
    return dict(params)


def export_filename(params: dict, now=None) -> str:
    request = export_request(params)
    instant = now or datetime.now(timezone.utc)
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return (f"aquahub-journal-{request['service']}-{stamp}.log" if request else
            f"aquahub-journaux-{stamp}.zip")


def _checked(snapshot, request):
    if not isinstance(snapshot, dict) or snapshot.get("schema") != 1:
        raise BridgeError("agent_protocol", "Le résultat de l’export est invalide.")
    entries = snapshot.get("entries")
    expected = {request["service"]} if request else SERVICES
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise BridgeError("agent_protocol", "La liste des journaux est incomplète.")
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or item.get("service") not in expected or item["service"] in seen:
            raise BridgeError("agent_protocol", "La liste des journaux est invalide.")
        seen.add(item["service"])
        if (item.get("state") not in STATES or not isinstance(item.get("text"), str) or
                len(item["text"].encode("utf-8")) > MAX_TEXT_BYTES):
            raise BridgeError("agent_protocol", "Le contenu d’un journal est invalide ou trop volumineux.")
        for field in ("label",):
            if not isinstance(item.get(field), str) or len(item[field]) > 100:
                raise BridgeError("agent_protocol", "Les informations d’un journal sont invalides.")
        error = item.get("error")
        if error is not None and (not isinstance(error, dict) or not isinstance(error.get("message"), str) or
                                  len(error["message"]) > 1000):
            raise BridgeError("agent_protocol", "Les informations d’un journal sont invalides.")
    if (not isinstance(snapshot.get("created_at"), str) or len(snapshot["created_at"]) > 40 or
            isinstance(snapshot.get("tail"), bool) or not isinstance(snapshot.get("tail"), int) or
            not 1 <= snapshot["tail"] <= 1000):
        raise BridgeError("agent_protocol", "Les limites de l’export sont invalides.")
    return entries


def build_log_export(snapshot, params: dict) -> tuple[bytes, bool]:
    """All ZIP paths derive from closed service IDs, never Docker names or text."""
    request = export_request(params)
    entries = _checked(snapshot, request)
    partial = any(item["state"] in {"truncated", "unavailable"} for item in entries)

    def text_file(item):
        status = {"available": "Disponible", "empty": "Aucune ligne disponible",
                  "truncated": "Export limité", "unavailable": "Indisponible"}[item["state"]]
        header = (f"AquaHub — journal Docker : {item['service']} ({item['label']})\n"
                  f"Collecte UTC : {snapshot['created_at']}\nÉtat : {status}\n"
                  f"Limite demandée : {snapshot['tail']} dernières lignes\n"
                  "Les secrets reconnus ont été masqués. Cet instantané ne constitue pas un historique exhaustif.\n")
        if item.get("error"):
            header += f"Erreur : {item['error']['message']}\n"
        return (header + "\n---\n" + item["text"] + ("\n" if item["text"] and not item["text"].endswith("\n") else "")).encode("utf-8")

    if request:
        content = text_file(entries[0])
    else:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            summary = {"schema": 1, "created_at": snapshot["created_at"], "tail": snapshot["tail"],
                       "partial": partial, "notice": "Journaux Docker disponibles, filtrés et limités ; aucun historique exhaustif garanti.",
                       "entries": [{key: item.get(key) for key in ("service", "label", "state", "lines", "truncated", "error")}
                                   for item in entries]}
            archive.writestr("resume.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            for item in entries:
                info = zipfile.ZipInfo(item["service"] + ".log")
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, text_file(item))
        content = stream.getvalue()
    if len(content) > MAX_EXPORT_BYTES:
        raise BridgeError("export_too_large", "Le fichier de journaux dépasse la taille autorisée.")
    return content, partial


def destination_identity(path: Path):
    """Remember exactly which existing file the native chooser approved."""
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise BridgeError("unsafe_path", "Choisissez un fichier local valide.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise BridgeError("save_failed", "Le fichier choisi n’est pas accessible.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise BridgeError("unsafe_path", "Le fichier choisi doit être un fichier normal appartenant à votre compte.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def save_log_export(path: Path, content: bytes, expected_identity=None) -> None:
    """Atomic 0600 save from the desktop UID, including when replacing a file."""
    if os.geteuid() == 0:
        raise BridgeError("unsafe_path", "Les journaux doivent être enregistrés depuis votre session utilisateur.")
    if not isinstance(content, bytes) or len(content) > MAX_EXPORT_BYTES:
        raise BridgeError("export_too_large", "Le fichier de journaux dépasse la taille autorisée.")
    if destination_identity(path) != expected_identity:
        raise BridgeError("destination_changed", "Le fichier de destination a changé. Relancez l’export pour confirmer son remplacement.")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".aquahub-journaux-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if destination_identity(path) != expected_identity:
            raise BridgeError("destination_changed", "Le fichier de destination a changé. Relancez l’export pour confirmer son remplacement.")
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise BridgeError("save_failed", "Impossible d’enregistrer les journaux à cet emplacement. Choisissez un autre dossier.") from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def choose_log_destination(Gtk, parent, params, accepted, canceled):
    """Asynchronous native SAVE dialog; tests can supply a small GTK fixture."""
    request = export_request(params)
    chooser = Gtk.FileChooserNative.new("Enregistrer les journaux AquaHub", parent,
                                        Gtk.FileChooserAction.SAVE, "Enregistrer", "Annuler")
    chooser.set_local_only(True)
    chooser.set_do_overwrite_confirmation(True)
    chooser.set_current_name(export_filename(request))
    file_filter = Gtk.FileFilter()
    file_filter.set_name("Journal Docker (*.log)" if request else "Journaux Docker (*.zip)")
    file_filter.add_pattern("*.log" if request else "*.zip")
    chooser.add_filter(file_filter)

    def chosen(dialog, response):
        filename = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        dialog.destroy()
        if filename:
            accepted(Path(filename))
        else:
            canceled()

    chooser.connect("response", chosen)
    chooser.show()
    return chooser
