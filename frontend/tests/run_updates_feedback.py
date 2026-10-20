"""Native WebKit updater UX with a Unix fixture; no network or host mutations."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tests/ui-updates-feedback-result.json'
REPORT.unlink(missing_ok=True)
fixture = {'mode': 'idle', 'checks': 0, 'downloads': 0, 'installs': 0, 'version': '0.1.2', 'release': None, 'download': None, 'installation': None, 'restart_reads': 0, 'fail_next_install': False}

def update_status():
    return {'current_version': fixture['version'], 'repository': 'https://github.com/mehdi7129/aquahub', 'available': bool(fixture['release'] and fixture['release']['version'] != fixture['version']), 'release': fixture['release'], 'download': fixture['download'], 'installation': fixture['installation'], 'message': 'Fixture isolée. Aucune requête réseau.'}

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(65536))
        method, params = request['method'], request.get('params', {})
        error = None
        if method == 'fixture.set': fixture.update(params); result = {'ok': True}
        elif method == 'fixture.stats': result = fixture.copy()
        elif method == 'app.version': result = {'version': fixture['version']}
        elif method == 'status.get': result = {'version': fixture['version'], 'machine': 'Banc isolé', 'docker_available': True, 'portainer_available': False, 'active_job': None, 'services': [{'id': 'gcs', 'name': 'GCS', 'state': 'running', 'health': None}]}
        elif method == 'settings.get': result = {'github_url': 'https://gitlab.com/fournisseur/bundle'}
        elif method == 'drones.list': result = {'drones': []}
        elif method == 'events.list': result = {'events': []}
        elif method == 'app_updates.status':
            if fixture['installation'] and fixture['installation']['status'] in {'scheduled', 'installing'}:
                fixture['restart_reads'] += 1
                if fixture['restart_reads'] == 2:
                    error = {'code': 'agent_unavailable', 'message': 'Coupure synthétique pendant le redémarrage.'}
                elif fixture['restart_reads'] >= 3:
                    failed = fixture['fail_next_install']
                    fixture['installation'] = {'status': 'failed' if failed else 'installed', 'version': fixture['release']['version'], 'message': 'Échec synthétique du paquet.' if failed else 'Version vérifiée après redémarrage.'}
                    if not failed: fixture['version'] = fixture['release']['version']
            result = update_status()
        elif method == 'app_updates.check':
            fixture['checks'] += 1
            if fixture['mode'] == 'check_error':
                error = {'code': 'rate_limited', 'message': 'Limite GitHub synthétique atteinte. Réessayez plus tard.'}
            else:
                fixture['release'] = {'release_id': fixture['checks'], 'asset_id': 10 + fixture['checks'], 'version': '0.3.0' if fixture['fail_next_install'] else '0.2.0', 'tag': 'v0.2.0', 'notes': '<img src=x onerror="window.pwned=true"> Notes synthétiques.', 'asset_name': 'aquahub-application_0.2.0_amd64.deb', 'asset_size': 1024, 'sha256': 'a' * 64}
                fixture['download'] = None; fixture['installation'] = None
            result = update_status()
        elif method == 'app_updates.download':
            fixture['downloads'] += 1
            result = {'id': 'download-fixture', 'kind': 'app.download', 'target': 'AquaHub', 'status': 'running', 'progress': 20}
        elif method == 'jobs.get':
            fixture['download'] = {'version': fixture['release']['version'], 'ready': True, 'sha256': 'a' * 64}
            result = {'id': 'download-fixture', 'kind': 'app.download', 'target': 'AquaHub', 'status': 'succeeded', 'result': {'downloaded': True}}
        elif method == 'app_updates.install':
            fixture['installs'] += 1; fixture['restart_reads'] = 0
            if params != {'version': fixture['release']['version'], 'confirmed': True}:
                error = {'code': 'bad_contract', 'message': 'Contrat de confirmation invalide.'}
            fixture['installation'] = {'status': 'scheduled', 'version': fixture['release']['version'], 'message': 'Installation synthétique programmée.'}
            result = {'scheduled': True, 'version': fixture['release']['version']}
        else:
            error = {'code': 'fixture_only', 'message': f'Action interdite dans ce banc: {method}'}
            result = None
        self.wfile.write((json.dumps({'id': request['id'], 'error' if error else 'result': error or result}) + '\n').encode())

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

with tempfile.TemporaryDirectory(prefix='aquahub-updates-ui-', dir='/tmp') as folder, Server(str(Path(folder) / 'agent.sock'), Handler) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent / 'desktop'), 'GDK_BACKEND': 'x11'}
    environment.pop('WAYLAND_DISPLAY', None)
    command = ['xvfb-run', '-a', 'python3', '-m', 'aquahub_desktop', '--frontend', str(ROOT/'dist'), '--no-agent', '--socket', str(Path(folder)/'agent.sock'), '--state-dir', str(Path(folder)/'state'), '--test-script', str(ROOT/'tests/ui-updates-feedback.js'), '--test-output', str(REPORT), '--quit-after', '42']
    try: completed = subprocess.run(command, cwd=ROOT.parent, env=environment, timeout=50)
    finally: server.shutdown()
if completed.returncode or not REPORT.is_file(): raise SystemExit('Rapport natif des mises à jour absent.')
report = json.loads(REPORT.read_text()); print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report.get('passed') else 1)
