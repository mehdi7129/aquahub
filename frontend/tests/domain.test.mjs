import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionGate, counts, escape, healthState, identifiedDrones, inspectionAllowed, isActive, jobSucceeded, normalizeRepository, plainLog, preflightAllowed, restartAllowed, rpcResultError, validateStatus } from './.runtime/domain.mjs';

const drone = { number: 5, name: 'Aqua_Drone_5', address: '192.168.205.2', reachable: true, identity_verified: false, configured: false, supported: true };
const preflight = { preflight_id: 'p-5', number: 5, address: drone.address, allowed: true, checks: [{ label: 'Réseau', ok: true }] };
const service = { id: 'gcs', name: 'GCS', state: 'running', health: null };
const status = { docker_available: true, services: [service], active_job: null };

test('un service en cours sans contrôle de santé ne devient pas sain', () => {
  const summary = counts([service, { ...service, id: 'mqtt', health: 'healthy' }, { ...service, id: 'fleet', health: 'unhealthy' }, { ...service, id: 'old', state: 'exited', health: null }]);
  assert.deepEqual(summary, { total: 4, running: 3, healthy: 1, unhealthy: 1, undefined: 2 });
  assert.equal(healthState(null), 'Non défini');
  assert.equal(healthState('none'), 'Non défini');
  assert.equal(healthState('new-unknown-state'), 'Inconnu');
});

test('une configuration exige le numéro et l’adresse exacts vérifiés par le backend', () => {
  assert.equal(preflightAllowed(preflight, drone), true);
  for (const changed of [{ number: 4 }, { address: '192.168.204.2' }, { number: undefined }, { preflight_id: '' }, { allowed: false }, { allowed: undefined, ok: true }]) {
    assert.equal(preflightAllowed({ ...preflight, ...changed }, drone), false, JSON.stringify(changed));
  }
});

test('un appareil absent, inconnu ou non pris en charge ne peut pas être configuré', () => {
  assert.equal(preflightAllowed(preflight, { ...drone, reachable: false }), false);
  assert.equal(preflightAllowed(preflight, { ...drone, reachable: null }), false);
  assert.equal(preflightAllowed(preflight, { ...drone, supported: false }), false);
  assert.equal(preflightAllowed(null, drone), false);
  assert.equal(preflightAllowed(preflight, undefined), false);
});

test('une vérification contradictoire ou échouée bloque la configuration', () => {
  assert.equal(preflightAllowed({ ...preflight, checks: [{ label: 'IP modifiée', ok: false }] }, drone), false);
});

test('une archive reconnue compatible seule ne permet jamais son application', () => {
  assert.equal(inspectionAllowed({ inspection_id: 'legacy', compatible: true, allowed: true, can_apply: false }), false);
  assert.equal(inspectionAllowed({ inspection_id: 'legacy', compatible: true }), false);
  assert.equal(inspectionAllowed({ inspection_id: 'bad', allowed: false, can_apply: true }), false);
  assert.equal(inspectionAllowed({ allowed: true, can_apply: true }), false);
  assert.equal(inspectionAllowed({ inspection_id: 'good', allowed: true, can_apply: true }), true);
  assert.equal(inspectionAllowed({ inspection_id: 'bad', allowed: true, can_apply: true, checks: [{ ok: false }] }), false);
});

test('les actions de maintenance sont refusées si l’agent, le moteur ou le service manque', () => {
  assert.equal(restartAllowed(status, service, false), true);
  assert.equal(restartAllowed(null, service, false), false);
  assert.equal(restartAllowed({ ...status, docker_available: false }, service, false), false);
  assert.equal(restartAllowed(status, undefined, false), false);
  assert.equal(restartAllowed(status, { ...service, state: 'missing' }, false), false);
  assert.equal(restartAllowed(status, { ...service, actions: { restart: false } }, false), false);
  assert.equal(restartAllowed({ ...status, stale: true }, service, false), false);
});

test('une tâche en cours bloque les mutations même après réouverture de la fenêtre', () => {
  assert.equal(restartAllowed({ ...status, active_job: { id: 'existing', status: 'running' } }, service, false), false);
  assert.equal(restartAllowed(status, service, true), false);
  assert.equal(isActive({ id: 'existing', status: 'future-unknown-state' }), true);
  for (const status of ['succeeded', 'failed', 'interrupted', 'canceled']) assert.equal(isActive({ id: 'j', status }), false);
  assert.equal(jobSucceeded({ id: 'j', status: 'interrupted' }), false);
  assert.equal(isActive(null), false);
});

test('un double clic concurrent déclenche une seule requête mutante', async () => {
  const gate = new ActionGate();
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  let calls = 0;
  const first = gate.run(async () => { calls++; await wait; return 'created'; });
  assert.equal(gate.busy, true);
  const second = await gate.run(async () => { calls++; return 'duplicate'; });
  assert.equal(second, undefined);
  assert.equal(calls, 1);
  release();
  assert.equal(await first, 'created');
  assert.equal(gate.busy, false);
});

test('une erreur libère le verrou frontend pour permettre un nouvel essai', async () => {
  const gate = new ActionGate();
  await assert.rejects(gate.run(async () => { throw new Error('Accès refusé'); }), /Accès refusé/);
  assert.equal(gate.busy, false);
  assert.equal(await gate.run(async () => 'retry'), 'retry');
});

test('les sources GitHub et GitLab sont normalisées sans dépendre d’une branche', () => {
  assert.equal(normalizeRepository('  https://github.com/aqua/fleet.git/  '), 'https://github.com/aqua/fleet');
  assert.equal(normalizeRepository('https://gitlab.com/aqua/groupe/fleet.git/'), 'https://gitlab.com/aqua/groupe/fleet');
  assert.equal(normalizeRepository('https://gitlab.com/aqua/fleet'), 'https://gitlab.com/aqua/fleet');
  assert.equal(normalizeRepository(''), '');
  assert.equal(normalizeRepository('https://github.com/aqua/fleet/'), 'https://github.com/aqua/fleet');
});

test('une URL privée avec secret, un domaine trompeur ou une branche sont refusés', () => {
  for (const url of ['http://github.com/aqua/fleet', 'https://secret@github.com/aqua/fleet', 'https://github.com.evil.invalid/aqua/fleet', 'https://github.com:8443/aqua/fleet', 'https://github.com/aqua/fleet/tree/main', 'https://github.com/aqua/fleet?token=secret', 'https://github.com/aqua/fleet#secret', '/tmp/archive.tar', 'javascript:alert(1)', 'https://gitlab.com/group/project/-/tree/main', 'https://gitlab.com/group', 'https://gitlab.com.evil.invalid/group/project', 'https://token@gitlab.com/group/project', 'https://gitlab.com/group/project?private_token=secret']) assert.throws(() => normalizeRepository(url), undefined, url);
});

test('les noms de services, événements et logs sont du texte inerte dans le rendu', () => {
  const payload = '<img src=x onerror="alert(1)"> & \'secret\'';
  const safe = escape(payload);
  assert.equal(safe.includes('<'), false);
  assert.equal(safe.includes('"'), false);
  assert.equal(safe.includes("'"), false);
  assert.equal(safe.includes('&lt;img'), true);
  assert.equal(escape(null), '');
});

test('les journaux restent bornés et les contrôles terminaux ne sont pas interprétés', () => {
  assert.equal(plainLog('\x1b[31mÉchec\x1b[0m\x00\x07\nservice'), 'Échec\nservice');
  assert.equal(plainLog('x'.repeat(210000)).length, 200000);
});

test('une réponse de supervision avec error:null reste une réponse valide', () => {
  assert.equal(rpcResultError({ services: [], error: null }), null);
  assert.equal(rpcResultError({ services: [], stale: true, error: { message: 'Docker inaccessible' } }), null);
  assert.equal(rpcResultError({ status: 'failed', error: { message: 'Échec de tâche' } }), null);
  assert.equal(rpcResultError({ error: { code: 'denied', message: 'Accès refusé' } }), 'Accès refusé');
  assert.equal(rpcResultError({ error: 'Refus' }), 'Refus');
});

test('les profils du bundle ne sont pas comptés comme appareils réellement identifiés', () => {
  const profiles = [1, 2, 3, 4, 5, 6].map(number => ({ ...drone, number, reachable: null }));
  assert.equal(identifiedDrones(profiles).length, 0);
  profiles[4].identity_verified = true;
  assert.deepEqual(identifiedDrones(profiles).map(drone => drone.number), [5]);
  profiles[1].reachable = true;
  assert.deepEqual(identifiedDrones(profiles).map(drone => drone.number), [2, 5]);
  profiles[1].reachable = false;
  assert.deepEqual(identifiedDrones(profiles).map(drone => drone.number), [5]);
});

test('une réponse de supervision partielle est refusée avant de remplacer les données visibles', () => {
  for (const invalid of [null, {}, { services: null }, { services: [{ name: 'GCS' }] }, { services: [{ ...service, id: null }] }, { ...status, active_job: { status: 'running' } }]) assert.throws(() => validateStatus(invalid), /réponse de supervision est incomplète/);
  assert.equal(validateStatus(status), status);
});
