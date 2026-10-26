import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { jobLabel, restartAllowed, servicePresentation, validateWireGuardStatus, wireGuardFresh, wireGuardPresentation, wireGuardReactivateAllowed } from './.runtime/domain.mjs';

const now = Date.parse('2026-10-26T10:00:00Z');
const iso = offset => new Date(now + offset).toISOString();
const dto = { state: 'absent', checked_at: iso(-800), peer_count: 1, recent_peer_count: 0, can_reactivate: true };
const service = { id: 'wireguard', name: 'WireGuard', state: 'running', health: null, actions: { restart: false }, wireguard: dto };
const status = { docker_available: true, services: [service], active_job: null, collected_at: iso(-1000) };
const options = { now };
const present = (changedService = service, changedStatus = status, changedOptions = {}) => wireGuardPresentation(changedService, changedStatus, true, { ...options, ...changedOptions });
const allowed = (changedService = service, changedStatus = status, changedOptions = {}) => wireGuardReactivateAllowed(changedStatus, changedService, true, false, { ...options, ...changedOptions });
const job = { id: 'wg-1', kind: 'wireguard.reactivate', target: 'wireguard', status: 'running', created_at: iso(-5000), started_at: iso(-4000) };

test('les vrais DTO sérialisés du backend respectent le contrat strict du renderer', () => {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const raw = execFileSync('python3', ['-c', `import json
from aquahub_agent.wireguard import WireGuardManager
print(json.dumps([WireGuardManager._dto(state, reason=reason) for state in ('active','absent','inactive','unknown','unsupported') for reason in (None,'wireguard_busy')]))`], { cwd: root, env: { ...process.env, PYTHONPATH: 'agent', PYTHONDONTWRITEBYTECODE: '1' }, timeout: 5000, maxBuffer: 256 * 1024, encoding: 'utf8' });
  for (const value of JSON.parse(raw)) {
    assert.notEqual(validateWireGuardStatus(value), null, `DTO invalide pour ${value.state}`);
    assert.equal(wireGuardFresh(value), true);
    assert.equal(value.reason === undefined || !value.reason.startsWith('wireguard_'), true, 'Code interne exposé à la place d’un motif lisible');
  }
});

test('le DTO WireGuard est strict, borné et ne transmet aucun champ supplémentaire', () => {
  assert.deepEqual(validateWireGuardStatus({ ...dto, private_data: 'not-for-renderer' }), dto);
  for (const value of [null, [], {}, { ...dto, state: { toString: () => 'active' } }, { ...dto, state: 'new-state' }, { ...dto, can_reactivate: 1 }, { ...dto, checked_at: '1' }, { ...dto, checked_at: '2026-02-31T10:00:00Z' }, { ...dto, checked_at: '2026-10-26T24:00:00Z' }, { ...dto, peer_count: -1 }, { ...dto, peer_count: 4097 }, { ...dto, peer_count: true }, { ...dto, recent_peer_count: 2 }, { ...dto, peer_count: undefined, recent_peer_count: 0 }, { ...dto, reason: null }, { ...dto, message: 'x'.repeat(2001) }]) assert.equal(validateWireGuardStatus(value), null);
});

test('l’observation WireGuard a sa propre limite de fraîcheur et refuse les dates futures', () => {
  assert.equal(wireGuardFresh(dto, now), true);
  for (const offset of [-30001, 5001]) assert.equal(wireGuardFresh({ ...dto, checked_at: iso(offset) }, now), false);
  for (const offset of [-30000, 5000]) assert.equal(wireGuardFresh({ ...dto, checked_at: iso(offset) }, now), true);
  assert.equal(present({ ...service, wireguard: { ...dto, checked_at: iso(-30001) } }).state, 'unknown');
  assert.equal(allowed({ ...service, wireguard: { ...dto, checked_at: iso(-30001) } }), false);
});

test('un conteneur en cours peut avoir un tunnel absent ou incomplet réactivable', () => {
  for (const state of ['absent', 'inactive']) {
    const value = { ...service, wireguard: { ...dto, state } };
    assert.equal(servicePresentation(value, status, true, options).tone, 'running');
    assert.equal(present(value).tone, 'transition');
    assert.equal(present(value).state, state);
    assert.equal(allowed(value), true);
  }
  assert.equal(present({ ...service, wireguard: { ...dto, state: 'inactive' } }).label, 'Tunnel incomplet');
});

test('un tunnel actif sans échange récent ne devient pas une panne ou un drone connecté', () => {
  const value = { ...service, wireguard: { ...dto, state: 'active', can_reactivate: false } };
  const result = present(value);
  assert.equal(result.label, 'Tunnel actif');
  assert.equal(result.tone, 'running');
  assert.equal(result.peers, 'Aucun échange récent observé.');
  assert.equal(result.message.includes('ne confirme pas la présence'), true);
  assert.equal(allowed(value), false);
});

test('la réactivation exige Docker, conteneur, DTO et autorisation actuels sans tâche concurrente', () => {
  assert.equal(allowed(), true);
  assert.equal(restartAllowed(status, { ...service, actions: { restart: true } }, false), false);
  for (const value of [{ ...service, state: 'exited' }, { ...service, wireguard: undefined }, { ...service, wireguard: { ...dto, state: 'unknown' } }, { ...service, wireguard: { ...dto, state: 'unsupported' } }, { ...service, wireguard: { ...dto, can_reactivate: false } }]) assert.equal(allowed(value), false);
  for (const value of [{ ...status, docker_available: false }, { ...status, stale: true }, { ...status, collected_at: iso(-30001) }, { ...status, active_job: job }]) assert.equal(allowed(service, value), false);
  assert.equal(wireGuardReactivateAllowed(status, service, false, false, options), false);
  assert.equal(wireGuardReactivateAllowed(status, service, true, true, options), false);
  assert.equal(wireGuardReactivateAllowed(status, undefined, true, false, options), false);
});

test('la tâche de réactivation et la demande restent attachées à WireGuard', () => {
  assert.equal(present(service, status, { job }).label, 'Réactivation en cours');
  assert.equal(present(service, status, { requesting: 'wireguard' }).label, 'Réactivation demandée');
  assert.equal(allowed(service, status, { job }), false);
  const other = { id: 'gcs', name: 'GCS', state: 'running', health: null };
  assert.equal(servicePresentation(other, status, true, { job, now }).operation, undefined);
  assert.equal(jobLabel(job), 'Réactivation du tunnel WireGuard');
});

test('un ancien état du tunnel attend une observation postérieure à la tâche terminée', () => {
  const ended = { ...job, status: 'succeeded', finished_at: iso(-500) };
  const containerAfter = { ...status, collected_at: iso(-100), jobs: [ended] };
  assert.equal(present(service, containerAfter).operation, 'refreshing');
  assert.equal(allowed(service, containerAfter), false);
  const tunnelAfter = { ...service, wireguard: { ...dto, checked_at: iso(-100), state: 'active', can_reactivate: false } };
  assert.equal(present(tunnelAfter, containerAfter).state, 'active');
  assert.equal(present(tunnelAfter, containerAfter).operation, undefined);
});

test('une réponse perdue n’autorise pas de nouvel essai avant une observation du tunnel postérieure', () => {
  const unconfirmed = { requestedAt: now - 2000, failedAt: now - 500 };
  const containerAfter = { ...status, collected_at: iso(-100) };
  assert.equal(present(service, containerAfter, { unconfirmed }).operation, 'uncertain');
  assert.equal(allowed(service, containerAfter, { unconfirmed }), false);
  const tunnelAfter = { ...service, wireguard: { ...dto, checked_at: iso(-100) } };
  assert.equal(allowed(tunnelAfter, containerAfter, { unconfirmed }), true);
  assert.equal(present(service, status, { unconfirmed, job: { ...job, created_at: iso(-1500) } }).operation, 'active');
});

test('un échec suivi d’un état frais absent reste un échec de tâche et permet un nouvel essai explicite', () => {
  const failed = { ...job, status: 'failed', finished_at: iso(-2000) };
  assert.equal(present(service, { ...status, jobs: [failed] }).state, 'absent');
  assert.equal(allowed(service, { ...status, jobs: [failed] }), true);
  assert.equal(failed.status, 'failed');
});
