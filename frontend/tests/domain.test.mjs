import test from 'node:test';
import assert from 'node:assert/strict';
import { ActionGate, counts, escape, healthState, identifiedDrones, inspectionAllowed, isActive, jobLabel, jobProgress, jobSucceeded, normalizeRepository, observationFresh, plainLog, preflightAllowed, progressCaption, restartAllowed, rpcResultError, servicePresentation, serviceTone, validateStatus } from './.runtime/domain.mjs';

const drone = { number: 5, name: 'Aqua_Drone_5', address: '192.168.205.2', reachable: true, identity_verified: false, configured: false, supported: true };
const preflight = { preflight_id: 'p-5', number: 5, address: drone.address, allowed: true, checks: [{ label: 'Réseau', ok: true }] };
const service = { id: 'gcs', name: 'GCS', state: 'running', health: null };
const status = { docker_available: true, services: [service], active_job: null };

test('la couleur dépend d’une observation récente et non du dernier état mémorisé', () => {
  const now = Date.parse('2026-10-21T12:00:00Z');
  const fresh = { ...status, collected_at: new Date(now - 1000).toISOString() };
  assert.equal(observationFresh(fresh, true, now), true);
  for (const changed of [{ stale: true }, { docker_available: false }, { collected_at: new Date(now - 30001).toISOString() }, { collected_at: new Date(now + 5001).toISOString() }, { collected_at: 'invalide' }, { collected_at: undefined }]) assert.equal(observationFresh({ ...fresh, ...changed }, true, now), false);
  assert.equal(observationFresh(fresh, false, now), false);
  assert.equal(observationFresh(null, true, now), false);
});

test('les contours séparent exécution, arrêt, transition et état inconnu sans inventer une santé', () => {
  assert.equal(serviceTone(service, true), 'running');
  assert.equal(serviceTone({ ...service, health: 'unhealthy' }, true), 'running');
  for (const state of ['exited', 'stopped', 'dead', 'missing']) assert.equal(serviceTone({ ...service, state }, true), 'failed');
  for (const state of ['restarting', 'paused', 'created']) assert.equal(serviceTone({ ...service, state }, true), 'transition');
  assert.equal(serviceTone({ ...service, state: 'future-state' }, true), 'unknown');
  for (const state of ['running', 'exited', 'restarting']) assert.equal(serviceTone({ ...service, state }, false), 'unknown');
});

const restartNow = Date.parse('2026-10-21T12:00:00Z');
const iso = offset => new Date(restartNow + offset).toISOString();
const restartJob = { id: 'restart-1', kind: 'service.restart', target: 'gcs', status: 'running', created_at: iso(-5000), started_at: iso(-4000) };
const observed = { ...status, collected_at: iso(-1000) };
const present = (changedStatus = observed, options = {}, changedService = service) => servicePresentation(changedService, changedStatus, true, { now: restartNow, ...options });

test('la demande et la tâche de relance colorent uniquement leur service cible', () => {
  assert.equal(present(observed, { requesting: 'gcs' }).operation, 'requesting');
  assert.equal(present(observed, { requesting: 'gcs' }).tone, 'transition');
  assert.equal(present(observed, { requesting: 'mqtt' }).tone, 'running');
  assert.equal(present(observed, { job: restartJob }).label, 'Redémarrage en cours');
  assert.equal(present(observed, { job: { ...restartJob, status: 'queued' } }).label, 'Relance en attente');
  assert.equal(present(observed, { job: { ...restartJob, target: 'gcs-old' } }).tone, 'running');
  assert.equal(present(observed, { job: { ...restartJob, kind: 'configure_drone' } }).tone, 'running');
});

test('une tâche connue ne rétablit pas une couleur courante pendant une coupure ou une erreur de suivi', () => {
  for (const changed of [{ stale: true }, { docker_available: false }, { collected_at: iso(-31000) }]) assert.equal(present({ ...observed, ...changed }, { job: restartJob }).operation, 'uncertain');
  assert.equal(servicePresentation(service, observed, false, { job: restartJob, now: restartNow }).tone, 'unknown');
  assert.equal(present(observed, { job: restartJob, jobError: 'Suivi indisponible' }).tone, 'unknown');
});

test('la fin du job attend une collecte commencée après sa fin avant de rétablir le badge Docker', () => {
  const job = { ...restartJob, status: 'succeeded', finished_at: iso(-500) };
  const cached = present(observed, { job });
  assert.equal(cached.tone, 'unknown'); assert.equal(cached.label, 'Actualisation');
  assert.equal(present({ ...observed, collected_at: job.finished_at }, { job }).operation, 'refreshing');
  const refreshed = present({ ...observed, collected_at: iso(-100) }, { job });
  assert.equal(refreshed.tone, 'running'); assert.equal(refreshed.operation, undefined);
});

test('un échec de commande ne prétend pas que le conteneur encore observé en cours est arrêté', () => {
  const job = { ...restartJob, status: 'failed', finished_at: iso(-2000) };
  assert.equal(present(observed, { job }).tone, 'running');
  assert.equal(present(observed, { job }, { ...service, state: 'exited' }).tone, 'failed');
});

test('des dates absentes, invalides ou futures imposent un état à vérifier', () => {
  for (const finished_at of [undefined, '', 'date-invalide', iso(6000)]) {
    const result = present(observed, { job: { ...restartJob, status: 'succeeded', finished_at } });
    assert.equal(result.tone, 'unknown'); assert.equal(result.label, 'État à vérifier');
  }
  assert.equal(present({ ...observed, collected_at: 'date-invalide' }, { job: restartJob }).operation, 'uncertain');
  assert.equal(present({ ...observed, collected_at: iso(-32000) }, { job: { ...restartJob, status: 'succeeded', finished_at: iso(-31000) } }).label, 'État à vérifier');
});

test('la réouverture retrouve une relance récente même terminée sans stockage supplémentaire', () => {
  const ended = { ...restartJob, status: 'succeeded', finished_at: iso(-500) };
  assert.equal(present({ ...observed, jobs: [ended] }).operation, 'refreshing');
  assert.equal(present({ ...observed, jobs: [ended], collected_at: iso(-100) }).tone, 'running');
  assert.equal(present({ ...observed, active_job: restartJob }).operation, 'active');
  const newer = { ...ended, id: 'restart-2', created_at: iso(-1000), finished_at: iso(-500) };
  const older = { ...ended, created_at: iso(-9000), finished_at: iso(-8000) };
  assert.equal(present({ ...observed, jobs: [newer] }, { job: older }).operation, 'refreshing');
  assert.equal(present({ ...observed, active_job: restartJob, jobs: [restartJob] }, { job: ended }).operation, 'refreshing');
});

test('une réponse perdue conserve l’incertitude jusqu’à un job retrouvé ou une observation postérieure', () => {
  const unconfirmed = { requestedAt: restartNow - 2000, failedAt: restartNow - 500 };
  assert.equal(present(observed, { unconfirmed }).operation, 'uncertain');
  assert.equal(present({ ...observed, collected_at: iso(-100) }, { unconfirmed }).tone, 'running');
  const recovered = { ...restartJob, created_at: iso(-1500) };
  assert.equal(present(observed, { unconfirmed, job: recovered }).operation, 'active');
  assert.equal(present(observed, { unconfirmed, job: { ...restartJob, status: 'succeeded', finished_at: iso(-3000) } }).operation, 'uncertain');
});

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


test('la configuration nomme toujours le numéro de l’appareil de la tâche', () => {
  assert.equal(jobLabel({id:'j', status:'running', kind:'configure_drone', target:'Aqua_Drone_5', label:'Configuration de Aqua_Drone'}), 'Configuration de Aqua_Drone_5');
  assert.equal(jobLabel({id:'j', status:'running', kind:'configure_drone', target:'17'}), 'Configuration de Aqua_Drone_17');
  assert.equal(jobLabel({id:'j', status:'running', kind:'service.restart', target:'Aqua_Drone_5', label:'GCS'}), 'GCS');
});
test('la progression reste indéterminée pendant une étape sans acquittement', () => {
  assert.deepEqual(jobProgress({id:'j',status:'running'}), {indeterminate:true});
  const stage=jobProgress({id:'j',status:'running',progress:{completed:5,total:12,percent:41,indeterminate:true,label:'Attente du redémarrage'}});
  assert.equal(stage.indeterminate,true); assert.equal(stage.percent,41); assert.equal(stage.completed,5);
  assert.equal(jobProgress({id:'j',status:'running',progress:Infinity}).indeterminate,true);
  assert.equal(jobProgress({id:'j',status:'running',progress:120}).percent,100);
  assert.equal(jobProgress({id:'j',status:'running',progress:{percent:NaN}}).indeterminate,true);
});

test('le téléchargement distingue les octets des étapes de configuration', () => {
  assert.equal(progressCaption({phase:'download', completed:1024*1024,total:2*1024*1024}), '1 / 2 Mo reçus');
  assert.equal(progressCaption({phase:'reboot',completed:7,total:12}), '7/12 étapes validées');
  assert.equal(progressCaption({completed:0,total:0}), '');
});
