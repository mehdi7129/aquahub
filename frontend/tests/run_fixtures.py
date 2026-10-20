"""Isolated UI fault injection. Never connects to Docker or an Aqua_Drone."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tests/.runtime/fixtures"
RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
fixture = {"mode": "fresh", "mutations": 0, "logs_reads": 0, "portainer_attempts": 0}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(65536))
        method, params = request["method"], request.get("params", {})
        result = None
        error = None
        mode = fixture["mode"]
        if method == "fixture.set":
            fixture["mode"] = params["mode"]
            result = {"ok": True}
        elif method == "fixture.stats":
            result = fixture
        elif method == 'app_updates.status': result = {'current_version':'test-synthetique','repository':'https://github.com/mehdi7129/aquahub','available':False,'message':'Banc isolé.'}
        elif method == "app.version":
            result = {"version": "test-synthetique"}
        elif method == "status.get":
            if mode == "offline":
                error = {"code": "fixture_unavailable", "message": "Connexion simulée interrompue."}
            else:
                result = {"version": "test-synthetique", "machine": "Banc isolé", "docker_available": mode != "stale",
                          "portainer_available": False, "bundle_path": None, "collected_at": "2026-10-20T12:00:00Z",
                          "services": [{"id": "gcs", "name": "GCS de test", "state": "running", "health": None,
                                        "actions": {"restart": True}}], "active_job": {"id": "fixture-job", "status": "running", "label": "Tâche synthétique"} if mode.startswith("job") else None,
                          "stale": mode == "stale", "error": {"code": "stale_snapshot", "message": "Collecte synthétique en retard."} if mode == "stale" else None}
        elif method == "settings.get":
            if mode == "settings_denied":
                error = {"code": "permission_denied", "message": "Accès aux réglages refusé dans ce scénario."}
            else:
                result = {"github_url": "", "theme": "light"}
        elif method == "drones.list":
            result = {"drones": [], "warnings": ["Banc de test synthétique ; aucun matériel contacté."]}
        elif method == "service.logs":
            fixture["logs_reads"] += 1
            result = {"text": '\x1b[31mErreur simulée\x1b[0m\n<img src=x onerror="window.pwned=true">\n<script>window.pwned=true</script>'}
        elif method == "events.list":
            result = {"events": [{"id": "fixture-event", "timestamp": "2026-10-20T12:00:00Z", "service": "gcs", "kind": "fixture", "message": '<img src=x onerror="window.pwned=true"> Événement synthétique.', "log_snapshot": {"id": "0" * 64, "service": "gcs", "status": "available", "truncated": True}},
                                 {"id": "fixture-expired", "timestamp": "2026-10-19T12:00:00Z", "service": "gcs", "kind": "fixture", "message": "Panne synthétique antérieure.", "log_snapshot": {"id": "1" * 64, "service": "gcs", "status": "expired"}},
                                 {"id": "fixture-limited", "timestamp": "2026-10-19T11:00:00Z", "service": "gcs", "kind": "fixture", "message": "Capture synthétique limitée.", "log_snapshot": {"id": "2" * 64, "service": "gcs", "status": "rate_limited"}}], "next_before_id": 100, "has_more": True, "retention": {"days": 365, "max_events": 100000}}
            if params.get('before_id') == 100:
                result = {"events": [{"id": "fixture-older", "timestamp": "2026-09-01T12:00:00Z", "service": "gcs", "kind": "fixture", "message": "Ancien incident synthétique conservé."}], "next_before_id": None, "has_more": False, "retention": {"days": 365, "max_events": 100000}}
        elif method == "events.logs":
            result = {"id": "0" * 64, "service": "gcs", "text": '<img src=x onerror="window.pwned=true">\nErreur synthétique conservée.', "redacted": True, "truncated": True, "status": "available"}
        elif method == "portainer.restart":
            fixture["portainer_attempts"] += 1
            error = {"code": "portainer_unmanaged", "message": "Cette instance Portainer n’est pas gérée par AquaHub."}
        elif method == "jobs.get":
            if mode == "job_error":
                error = {"code": "fixture_job_unavailable", "message": "Suivi synthétique momentanément indisponible."}
            else:
                result = {"id": "fixture-job", "status": "running" if mode == "job_running" else "succeeded", "label": "Tâche synthétique", "result": {"manual_skipped": 5, "message": "Configuration logicielle vérifiée. Cinq contrôles matériels restent à effectuer manuellement."}}
        else:
            fixture["mutations"] += 1
            error = {"code": "unexpected_mutation", "message": "Aucune mutation n’est autorisée dans ce banc de test."}
        reply = {"id": request["id"], "error" if error else "result": error or result}
        self.wfile.write((json.dumps(reply) + "\n").encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


with tempfile.TemporaryDirectory(prefix="aquahub-ui-", dir="/tmp") as socket_dir, \
        Server(str(Path(socket_dir) / "agent.sock"), Handler) as server:
    # AF_UNIX paths are limited on Linux; keep fixtures independent of checkout depth.
    SOCKET = Path(socket_dir) / "agent.sock"
    SOCKET.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {**os.environ, "PYTHONPATH": str(ROOT.parent / "desktop"), "GDK_BACKEND": "x11"}
    environment.pop("WAYLAND_DISPLAY", None)
    command = ["xvfb-run", "-a", "python3", "-m", "aquahub_desktop", "--no-agent", "--socket", str(SOCKET),
               "--state-dir", str(RUNTIME / "state"), "--test-script", str(ROOT / "tests/ui-fixtures.js"),
               "--test-output", str(ROOT / "tests/ui-fixtures-result.json"), "--quit-after", "35"]
    try:
        completed = subprocess.run(command, env=environment, cwd=ROOT, timeout=50)
    finally:
        server.shutdown()
report = ROOT / "tests/ui-fixtures-result.json"
if completed.returncode or not report.is_file():
    raise SystemExit("Le banc de test graphique n’a pas produit de rapport.")
result = json.loads(report.read_text())
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result.get("passed") else 1)
