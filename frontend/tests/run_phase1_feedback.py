"""WebKit UX fixture. Native Save As responses are synthetic; no live services."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tests/ui-phase1-feedback-result.json'
REPORT.unlink(missing_ok=True)
fixture = {'mode': 'fresh', 'export_mode': 'cancel', 'exports': [], 'app_checks': 0, 'bundle_checks': 0, 'mutations': 0}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(65536))
        method, params = request['method'], request.get('params', {})
        error = None
        result = None
        if method == 'fixture.set':
            fixture.update(params)
            result = {'ok': True}
        elif method == 'fixture.stats': result = fixture.copy()
        elif method == 'status.get':
            deadline = time.monotonic() + 15
            while fixture['mode'] == 'slow' and time.monotonic() < deadline: time.sleep(.05)
            if fixture['mode'] == 'offline': error = {'code': 'unavailable', 'message': 'Coupure synthétique.'}
            else:
                collected = datetime.now(timezone.utc) - timedelta(seconds=45 if fixture['mode'] == 'old' else 0)
                states = ['running', 'exited', 'dead', 'missing', 'restarting', 'paused', 'created', 'unknown']
                result = {'version': 'test-isolé', 'machine': 'Banc synthétique', 'docker_available': True, 'portainer_available': True, 'active_job': None, 'collected_at': collected.isoformat(), 'stale': fixture['mode'] == 'stale', 'services': [{'id': f'service-{index}', 'name': f'Service {index}', 'state': state, 'health': 'unhealthy' if index == 0 else None, 'actions': {'restart': True}} for index, state in enumerate(states)]}
        elif method == 'settings.get': result = {'github_url': 'https://gitlab.com/exemple/bundle'}
        elif method == 'app.version': result = {'version': 'test-isolé'}
        elif method == 'drones.list': result = {'drones': []}
        elif method == 'events.list': result = {'events': []}
        elif method == 'service.logs': result = {'text': 'Extrait synthétique récent.'}
        elif method in {'app_updates.status', 'app_updates.check'}:
            if method == 'app_updates.check': fixture['app_checks'] += 1
            result = {'current_version': 'test-isolé', 'repository': 'https://github.com/mehdi7129/aquahub', 'available': False, 'message': 'Aucune nouvelle version disponible.'}
        elif method == 'updates.check':
            fixture['bundle_checks'] += 1
            result = {'releases': [], 'message': 'Aucune livraison synthétique disponible.'}
        elif method == 'fixture.export_logs':
            fixture['exports'].append(params)
            if fixture['export_mode'] == 'cancel': result = {'saved': False, 'canceled': True}
            else: result = {'saved': True, 'partial': fixture['export_mode'] == 'partial', 'filename': 'logs-<img src=x onerror="window.pwned=true">.zip'}
        else:
            fixture['mutations'] += 1
            error = {'code': 'fixture_only', 'message': 'Aucune mutation autorisée dans ce banc.'}
        self.wfile.write((json.dumps({'id': request['id'], 'error' if error else 'result': error or result}) + '\n').encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


with tempfile.TemporaryDirectory(prefix='aquahub-phase1-ui-', dir='/tmp') as folder, Server(str(Path(folder) / 'agent.sock'), Handler) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent / 'desktop'), 'GDK_BACKEND': 'x11'}
    environment.pop('WAYLAND_DISPLAY', None)
    # Fixture-only bridge substitution: exercise UI cancel/success/partial without
    # choosing host files. Real GTK Save As is tested separately by desktop tests.
    launcher = Path(folder) / 'launcher.py'
    launcher.write_text("from aquahub_desktop import app\napp.BRIDGE_SOURCE = app.BRIDGE_SOURCE.replace('const id = String(++sequence);', \"if (method === 'native.export_logs') method = 'fixture.export_logs'; const id = String(++sequence);\")\nraise SystemExit(app.main())\n")
    command = ['xvfb-run', '-a', 'python3', str(launcher), '--frontend', str(ROOT / 'dist'), '--no-agent', '--socket', str(Path(folder) / 'agent.sock'), '--state-dir', str(Path(folder) / 'state'), '--test-script', str(ROOT / 'tests/ui-phase1-feedback.js'), '--test-output', str(REPORT), '--quit-after', '20']
    try: completed = subprocess.run(command, cwd=ROOT.parent, env=environment, timeout=30)
    finally: server.shutdown()
if completed.returncode or not REPORT.is_file(): raise SystemExit('Rapport WebKit phase 1 absent.')
report = json.loads(REPORT.read_text())
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report.get('passed') else 1)
