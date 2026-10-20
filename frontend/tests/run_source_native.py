"""Real GTK Git-token dialog with a private Unix fixture; no live services."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
from x11_keyboard import Keyboard

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'tests/.runtime/source-native'
RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
REPORT = ROOT/'tests/ui-source-native-result.json'
REPORT.unlink(missing_ok=True)
stats = {'settings_calls': 0, 'native_credentials_match': False, 'unexpected_calls': 0}
# Synthetic fixture data only. These values never enter the renderer or a report.
FIXTURE_PASSWORD = 'synthetic-demo-2026'


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(65536))
        method, params = request['method'], request.get('params', {})
        if method == 'fixture.stats': result = stats.copy()
        elif method == 'updates.settings':
            stats['settings_calls'] += 1
            stats['native_credentials_match'] = params == {'token': FIXTURE_PASSWORD}
            result = {'configured': True}
        elif method == 'app.version': result = {'version': 'test-isole'}
        elif method == 'settings.get': result = {'github_url': ''}
        elif method == 'drones.list': result = {'drones': [], 'warnings': ['Banc de test isolé.']}
        elif method == 'events.list': result = {'events': []}
        elif method == 'status.get': result = {'version': 'test-isole', 'machine': 'Banc synthétique', 'docker_available': True, 'portainer_available': True, 'active_job': None, 'services': [{'id': 'gcs', 'name': 'GCS de test', 'state': 'running', 'health': None}]}
        else:
            stats['unexpected_calls'] += 1
            result = {'error': {'code': 'fixture_only', 'message': 'Action non autorisée par ce banc.'}}
        self.wfile.write((json.dumps({'id': request['id'], 'result': result})+'\n').encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


keyboard = Keyboard()
with tempfile.TemporaryDirectory(prefix='aquahub-source-', dir='/tmp') as socket_dir, \
        Server(str(Path(socket_dir)/'agent.sock'), Handler) as server:
    # AF_UNIX paths are limited on Linux; keep fixtures independent of checkout depth.
    SOCKET = Path(socket_dir)/'agent.sock'
    SOCKET.chmod(0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent/'desktop'), 'GDK_BACKEND': 'x11'}
    environment.pop('WAYLAND_DISPLAY', None)
    process = subprocess.Popen(['python3', '-m', 'aquahub_desktop', '--frontend', str(ROOT/'dist'), '--no-agent', '--socket', str(SOCKET), '--state-dir', str(RUNTIME/'state'), '--test-script', str(ROOT/'tests/ui-source-native.js'), '--test-output', str(REPORT), '--quit-after', '30'], env=environment, cwd=ROOT)
    completed = None
    handled = set()
    deadline = time.monotonic()+25
    try:
        while time.monotonic() < deadline and process.poll() is None:
            if REPORT.is_file():
                try: data = json.loads(REPORT.read_text())
                except (OSError, ValueError): time.sleep(.05); continue
                phase = data.get('phase')
                if phase == 'complete': completed = data; break
                if phase in {'native_cancel', 'native_fill'} and phase not in handled:
                    window = keyboard.find('Accès au dépôt privé')
                    if not window: time.sleep(.05); continue
                    handled.add(phase)
                    if phase == 'native_cancel': keyboard.press(window, 'Escape')
                    else:
                        for character in FIXTURE_PASSWORD: keyboard.press(window, character); time.sleep(.02)
                        keyboard.press(window, 'Enter')
            time.sleep(.04)
    finally:
        process.terminate()
        try: process.wait(timeout=4)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        server.shutdown()
if completed is None:
    raise SystemExit('Le test du dialogue de jeton Git n’a pas terminé.')
print(json.dumps(completed, ensure_ascii=False, indent=2))
raise SystemExit(0 if completed['passed'] else 1)
