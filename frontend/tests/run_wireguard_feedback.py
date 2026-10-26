"""WireGuard GUI fixture: isolated Unix socket/WebKit; no Docker or network access."""
import copy
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--zoom', type=int, choices=(1, 2), default=1)
args = parser.parse_args()
suffix = '-200' if args.zoom == 2 else ''
ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tests' / f'ui-wireguard-feedback{suffix}-result.json'
SCREENSHOT = ROOT / 'tests' / f'ui-wireguard-feedback{suffix}.png'
REPORT.unlink(missing_ok=True)
SCREENSHOT.unlink(missing_ok=True)
fixture = {'tunnel': 'inactive', 'container': 'running', 'can_reactivate': True,
           'cache': 'fresh', 'before': None, 'job': None, 'requests': 0,
           'checks': 0, 'generic_restarts': 0, 'release_request': False,
           'reply': 'normal', 'invalid_status': False, 'invalid_probe': False,
           'probe_error': False, 'probe_state': None, 'captures': []}


def stamp(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def observation(probe=False):
    state = fixture['probe_state'] if probe and fixture['probe_state'] else fixture['tunnel']
    checked = stamp() if probe or fixture['cache'] == 'fresh' else fixture['before']
    if fixture['cache'] == 'stale' and not probe:
        checked = stamp(-35)
    value = {'state': state, 'checked_at': checked, 'can_reactivate':
             fixture['can_reactivate'] and state in {'inactive', 'absent'},
             'peer_count': 1, 'recent_peer_count': 0,
             'message': 'Observation synthétique du tunnel ; aucun appareil réel contacté.'}
    if state == 'unsupported':
        value['reason'] = 'Ce démarrage fournisseur n’est pas qualifié. Aucun redémarrage générique ne sera lancé.'
    if fixture['invalid_probe' if probe else 'invalid_status']:
        value['can_reactivate'] = 'true'
    return value


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(65536))
        method, params = request['method'], request.get('params', {})
        result, error = None, None
        if method == 'fixture.set':
            fixture.update(params)
            result = {'ok': True}
        elif method == 'fixture.stats':
            result = copy.deepcopy(fixture)
        elif method == 'fixture.finish':
            status = params.get('status', 'succeeded')
            fixture['job'].update(status=status, finished_at=stamp())
            if status == 'succeeded':
                fixture['tunnel'] = 'active'
                fixture['job']['result'] = {'message': 'Tunnel WireGuard réactivé ; aucun échange distant récent confirmé.'}
            else:
                fixture['job']['error'] = {'message': 'Activation synthétique refusée ; le tunnel reste à vérifier.'}
            result = {'ok': True}
        elif method == 'fixture.history':
            fixture['before'] = stamp(-.2)
            fixture['cache'] = 'old'
            fixture['job'] = {'id': 'wg-before-reload', 'kind': 'wireguard.reactivate',
                              'target': 'wireguard', 'status': 'succeeded',
                              'created_at': stamp(-2), 'finished_at': stamp(-.05)}
            fixture['tunnel'] = 'active'
            result = {'ok': True}
        elif method == 'status.get':
            job = copy.deepcopy(fixture['job'])
            collected = fixture['before'] if fixture['cache'] == 'old' else stamp()
            result = {'version': 'test-wireguard', 'machine': 'Poste de démonstration',
                      'docker_available': True, 'portainer_available': False, 'stale': False,
                      'collected_at': collected, 'active_job': job if job and job['status'] == 'running' else None,
                      'jobs': [job] if job else [], 'services': [
                          {'id': 'gcs', 'name': 'GCS', 'state': 'running', 'health': None, 'actions': {'restart': True}},
                          {'id': 'wireguard', 'name': 'WireGuard', 'state': fixture['container'], 'health': None,
                           'actions': {'restart': False}, 'wireguard': observation()}]}
        elif method == 'wireguard.status':
            fixture['checks'] += 1
            if fixture['probe_error']:
                error = {'code': 'wireguard_probe', 'message': 'Sonde synthétique indisponible.'}
            else:
                result = observation(probe=True)
        elif method == 'wireguard.reactivate':
            if params != {'confirmed': True}:
                error = {'code': 'confirmation_required', 'message': 'Confirmation absente.'}
            else:
                fixture['requests'] += 1
                fixture['before'] = stamp(-.2)
                fixture['cache'] = 'old'
                previous = copy.deepcopy(fixture['job'])
                fixture['job'] = {'id': f"wg-{fixture['requests']}", 'kind': 'wireguard.reactivate',
                                  'target': 'wireguard', 'status': 'running', 'created_at': stamp(),
                                  'started_at': stamp()}
                deadline = time.monotonic() + 10
                while not fixture['release_request'] and time.monotonic() < deadline:
                    time.sleep(.02)
                result = copy.deepcopy(fixture['job'])
                if fixture['reply'] != 'normal':
                    if fixture['reply'] == 'untracked':
                        fixture['job'] = previous
                    error = {'code': 'agent_protocol', 'message': 'Réponse synthétique perdue ; résultat non confirmé.'}
        elif method == 'service.restart':
            fixture['generic_restarts'] += 1
            error = {'code': 'network_guard', 'message': 'Redémarrage générique interdit dans ce banc.'}
        elif method == 'jobs.get':
            result = copy.deepcopy(fixture['job'])
        elif method == 'settings.get':
            result = {'github_url': ''}
        elif method == 'app.version':
            result = {'version': 'test-wireguard'}
        elif method == 'drones.list':
            result = {'drones': []}
        elif method == 'events.list':
            result = {'events': []}
        elif method == 'app_updates.status':
            result = {'current_version': 'test-wireguard', 'repository': 'https://github.com/mehdi7129/aquahub',
                      'available': False, 'message': 'Banc isolé ; aucune recherche réseau.'}
        else:
            error = {'code': 'unexpected_fixture_method', 'message': 'Action hors du scénario synthétique.'}
        self.wfile.write((json.dumps({'id': request['id'], 'error' if error else 'result': error or result}) + '\n').encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


with tempfile.TemporaryDirectory(prefix='aquahub-wg-ui-', dir='/tmp') as folder, \
        Server(str(Path(folder) / 'agent.sock'), Handler) as server:
    Path(server.server_address).chmod(0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent / 'desktop'), 'GDK_BACKEND': 'x11'}
    environment.pop('WAYLAND_DISPLAY', None)
    command = ['xvfb-run', '-a', 'python3', '-m', 'aquahub_desktop', '--frontend', str(ROOT / 'dist'),
               '--no-agent', '--socket', server.server_address, '--state-dir', str(Path(folder) / 'state'),
               '--width', '1366', '--height', '768',
               '--zoom', str(args.zoom),
               '--test-script', str(ROOT / 'tests/ui-wireguard-feedback.js'), '--test-output', str(REPORT),
               '--screenshot', str(SCREENSHOT), '--quit-after', '48']
    process = subprocess.Popen(command, cwd=ROOT.parent, env=environment)
    captures = set()
    deadline = time.monotonic() + 58
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if REPORT.exists():
                try:
                    data = json.loads(REPORT.read_text())
                    if (data.get('phase') == 'appearance' and data.get('theme') not in captures
                            and SCREENSHOT.exists() and SCREENSHOT.stat().st_mtime_ns > REPORT.stat().st_mtime_ns):
                        name = data['theme']
                        if name not in {'light', 'dark'}:
                            raise ValueError('Unexpected theme')
                        raw = SCREENSHOT.read_bytes()
                        if raw.startswith(b'\x89PNG\r\n\x1a\n') and raw.endswith(b'\x00\x00\x00\x00IEND\xaeB`\x82'):
                            (ROOT / 'tests' / f'ui-wireguard-{name}{suffix}.png').write_bytes(raw)
                            captures.add(name)
                            fixture['captures'] = sorted(captures)
                except (OSError, ValueError):
                    pass
            time.sleep(.05)
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        server.shutdown()
if process.returncode or not REPORT.is_file():
    raise SystemExit('Le banc WireGuard n’a pas produit de rapport complet.')
report = json.loads(REPORT.read_text())
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report.get('passed') and report.get('phase') == 'complete' else 1)
