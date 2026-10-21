"""Native WebKit visual feedback fixtures; no Docker, network or drone access."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'tests/ui-visual-feedback-result.json'
REPORT.unlink(missing_ok=True)
fixture = {'offline': False, 'cache': 'fresh', 'release_request': False,
           'finish_on_response': False, 'requests': 0, 'job': None,
           'before': None, 'job_error': False, 'restart_reply': 'normal'}


def stamp(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def finish(status='succeeded', finished_at=None):
    fixture['job'].update(status=status, finished_at=finished_at or stamp(),
                          result={'message': 'Résultat synthétique vérifié.'})
    if status == 'failed':
        fixture['job']['error'] = {'message': 'Commande synthétique refusée ; vérifier le service.'}


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
            finish(params.get('status', 'succeeded'))
            result = {'ok': True}
        elif method == 'fixture.history':
            fixture['before'] = stamp(-.1)
            fixture['job'] = {'id': f"history-{params.get('id', 1)}", 'kind': 'service.restart',
                              'target': 'gcs', 'status': 'succeeded', 'created_at': stamp(-.05),
                              'started_at': stamp(-.04), 'finished_at': params.get('finished_at', stamp())}
            fixture['cache'] = params.get('cache', 'old')
            result = {'ok': True}
        elif method == 'fixture.progress':
            if not fixture['job'] or fixture['job']['kind'] != 'configure_drone':
                fixture['job'] = {'id': 'configuration-5', 'kind': 'configure_drone', 'target': 'Aqua_Drone_5',
                                  'status': 'running', 'created_at': stamp()}
            fixture['job']['progress'] = params['progress']
            fixture['cache'] = 'fresh'
            result = {'ok': True}
        elif method == 'status.get':
            if fixture['offline']:
                error = {'code': 'fixture_offline', 'message': 'Coupure synthétique du banc.'}
            else:
                job = copy.deepcopy(fixture['job'])
                result = {'version': 'test-visuel', 'machine': 'Banc visuel isolé', 'docker_available': True,
                          'portainer_available': False, 'stale': False,
                          'collected_at': fixture['before'] if fixture['cache'] == 'old' else stamp(),
                          'active_job': job if job and job['status'] == 'running' else None,
                          'jobs': [job] if job else [],
                          'services': [{'id': identity, 'name': name, 'state': 'running', 'health': None,
                                        'actions': {'restart': True}}
                                       for identity, name in [('gcs', 'GCS'), ('mqtt', 'MQTT')]]}
        elif method == 'service.restart':
            fixture['requests'] += 1
            fixture['before'] = stamp(-.1)
            fixture['cache'] = 'old'
            previous_job = copy.deepcopy(fixture['job'])
            fixture['job'] = {'id': f"restart-{fixture['requests']}", 'kind': 'service.restart',
                              'target': params['service'], 'status': 'running',
                              'created_at': stamp(), 'started_at': stamp()}
            deadline = time.monotonic() + 10
            while not fixture['release_request'] and time.monotonic() < deadline:
                time.sleep(.03)
            if fixture['finish_on_response']:
                finish()
            result = copy.deepcopy(fixture['job'])
            if fixture['restart_reply'] != 'normal':
                if fixture['restart_reply'] == 'untracked':
                    fixture['job'] = previous_job
                error = {'code': 'agent_protocol', 'message': 'Réponse synthétique perdue ; résultat de la demande non confirmé.'}
        elif method == 'jobs.get':
            if fixture['job_error']:
                error = {'code': 'fixture_job_unavailable', 'message': 'Suivi synthétique indisponible.'}
            else:
                result = copy.deepcopy(fixture['job'])
        elif method == 'settings.get':
            result = {'github_url': ''}
        elif method == 'app.version':
            result = {'version': 'test-visuel'}
        elif method == 'drones.list':
            result = {'drones': []}
        elif method == 'events.list':
            result = {'events': []}
        elif method == 'app_updates.status':
            result = {'current_version': 'test-visuel', 'repository': 'https://github.com/mehdi7129/aquahub',
                      'available': False, 'message': 'Banc isolé ; aucune recherche réseau.'}
        else:
            error = {'code': 'unexpected_fixture_method', 'message': 'Action hors du scénario synthétique.'}
        self.wfile.write((json.dumps({'id': request['id'], 'error' if error else 'result': error or result}) + '\n').encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


with tempfile.TemporaryDirectory(prefix='aquahub-visual-', dir='/tmp') as folder, \
        Server(str(Path(folder) / 'agent.sock'), Handler) as server:
    Path(server.server_address).chmod(0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent / 'desktop'), 'GDK_BACKEND': 'x11'}
    environment.pop('WAYLAND_DISPLAY', None)
    command = ['xvfb-run', '-a', 'python3', '-m', 'aquahub_desktop', '--frontend', str(ROOT / 'dist'),
               '--no-agent', '--socket', server.server_address, '--state-dir', str(Path(folder) / 'state'),
               '--test-script', str(ROOT / 'tests/ui-visual-feedback.js'), '--test-output', str(REPORT),
               '--quit-after', '42']
    try:
        completed = subprocess.run(command, cwd=ROOT.parent, env=environment, timeout=50)
    finally:
        server.shutdown()
if completed.returncode or not REPORT.is_file():
    raise SystemExit('Le banc visuel n’a pas produit de rapport.')
report = json.loads(REPORT.read_text())
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report.get('passed') else 1)
