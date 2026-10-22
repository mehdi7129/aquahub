import type { AppUpdateStatus, HubEvent, HubStatus, Job, Settings } from './types';
import { rpcResultError } from './domain';

export const demo = new URLSearchParams(location.search).get('demo') === '1';

export async function request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  let result: unknown;
  if (demo) result = await demoRequest(method, params);
  else {
    if (!window.aquahub?.request) throw new Error('Le service AquaHub n’est pas connecté. Ouvrez l’application depuis le menu de votre ordinateur, ou réessayez lorsque son agent est disponible.');
    result = await window.aquahub.request(method, params);
  }
  const error = rpcResultError(result);
  if (error) throw new Error(error);
  return result as T;
}

const serviceNames = [ ['mqtt', 'MQTT'], ['fleet-controller', 'Fleet Controller'], ['gcs', 'GCS'], ['reactor', 'Reactor'], ['net-setup', 'Réseau'], ['mavlink-router', 'Routeur MAVLink'], ['rtcm', 'Corrections RTCM'], ['wireguard', 'WireGuard'] ];
const mockStatus: HubStatus = {
  version: '0.2.3-démo', machine: 'Poste de démonstration', docker_available: true, portainer_available: true,
  bundle_path: '/opt/exemple/aqua-hub-bundle', active_job: null,
  services: serviceNames.map(([id, name], index) => ({ id, name, state: 'running', health: index < 2 ? 'healthy' : null, image_id: `sha256:demo-${id}`, restarts: 0, started_at: new Date(Date.now() - 3600000).toISOString() })),
};
let demoConfigured = false;
let demoSettings: Settings = {};
try { demoSettings = JSON.parse(localStorage.getItem('aquahub-demo-settings') ?? '{}'); } catch { /* invalid preferences are ignored */ }
let job: Job | null = null;
let jobStarted = 0;
let demoJobTarget = '';
let demoSsh = { known_hosts_file: '', identity_file: '', allow_none_auth: false };
const demoEvents: HubEvent[] = [{ id: 'demo-start', timestamp: new Date().toISOString(), kind: 'observation_started', service: null, message: 'Démonstration ouverte. Ces données sont fictives.' }, { id: 'demo-failure', timestamp: new Date(Date.now() - 60000).toISOString(), kind: 'unexpected_stop', service: 'gcs', message: 'Arrêt inattendu simulé, puis reprise du service.', log_snapshot: { id: 'demo-log-snapshot', service: 'gcs', status: 'available', captured_at: new Date(Date.now() - 60000).toISOString(), truncated: true } }];
let sequence = 0;
const demoManualChecks = [
  {id:'router',label:'Routeur RUTC50',details:'Vérifier la configuration LAN existante, sans modifier les adresses IP.'},
  {id:'route',label:'Route vers le GCS',details:'Vérifier le prochain saut et la route existante vers le GCS, sans modifier le réseau.'},
  {id:'qgc',label:'Paramètres et moteurs QGroundControl',details:'Contrôler MAV_SYS_ID = 5, l’écartement des antennes et les paramètres EKF2_GPS_POS_Y / X / Z ; les paramètres BT_POS_XY P/I/D et BT_POS_IMAX ; la numérotation des moteurs.'},
  {id:'firmware',label:'Versions Heron',details:'Relever les versions du firmware Heron et du chargeur de démarrage (BL).'},
  {id:'console',label:'Diagnostic matériel MAVLink',details:'Dans la console MAVLink de QGroundControl : uavcan status, gps status et listener esc_status.'},
];
let demoAppUpdate: AppUpdateStatus = {current_version: '0.2.3-démo', repository:'https://github.com/mehdi7129/aquahub', available:false, message:'Aucune recherche lancée. Données de démonstration.'};
let demoInstallationStarted = 0;


function droneData() {
  return { drones: [{ number: 5, name: 'Aqua_Drone_5', address: '192.168.205.2', reachable: true, identity_verified: demoConfigured, configured: demoConfigured, supported: true, manual_checklist: demoManualChecks }], warnings: ['Démonstration : aucun appareil réel n’est contacté.'] };
}
function syncJob() {
  if (job?.status === 'running') {
    const elapsed = Date.now() - jobStarted;
    job.progress = demoJobTarget === 'drone' ? {label:elapsed<1300?'Envoi des configurations existantes':'Attente du redémarrage de Aqua_Drone_5',completed:elapsed<1300?3:7,total:12,percent:elapsed<1300?25:58,indeterminate:elapsed>=1300,phase:elapsed<1300?'supplier':'reboot'} : Math.min(95, Math.round(elapsed / 28));
    if (elapsed > 2800) {
      job.status = 'succeeded'; job.finished_at = new Date().toISOString(); job.progress = 100; job.message = 'Opération simulée terminée.';
      if (demoJobTarget === 'drone') { demoConfigured = true; job.result={manual_skipped:5,manual_checklist:demoManualChecks,message:'Configuration logicielle de Aqua_Drone_5 vérifiée. Cinq contrôles manuels restent à effectuer manuellement.'}; }
      if (demoJobTarget === 'app-download' && demoAppUpdate.release) demoAppUpdate.download = {version:demoAppUpdate.release.version,ready:true,sha256:'f'.repeat(64)};
      if (demoJobTarget === 'download') job.result = { downloaded: true, inspection: demoInspection() };
      const service = mockStatus.services.find(item => item.id === demoJobTarget);
      if (service) { service.restarts = (service.restarts ?? 0) + 1; service.started_at = new Date().toISOString(); }
      demoEvents.unshift({ id: `event-${sequence}`, timestamp: new Date().toISOString(), service: service?.id ?? null, kind: 'maintenance_succeeded', message: `${job.label} : opération simulée terminée.` });
      mockStatus.active_job = null;
    }
  }
}
function startJob(label: string, target: string) {
  syncJob();
  if (mockStatus.active_job) throw new Error('Une tâche de démonstration est déjà en cours.');
  sequence += 1; demoJobTarget = target; jobStarted = Date.now();
  job = { id: `demo-${sequence}`, label, created_at: new Date().toISOString(), started_at: new Date().toISOString(), ...(target === 'drone' ? {kind:'configure_drone',target:'Aqua_Drone_5'} : target==='app-download'?{kind:'app.download',target:'AquaHub 0.3.0-démo'}:mockStatus.services.some(service=>service.id===target)?{kind:'service.restart',target}:{}), status: 'running', message: 'Simulation en cours…', progress: 0 };
  mockStatus.active_job = job;
  mockStatus.jobs = [job, ...(mockStatus.jobs ?? [])].slice(0, 20);
  return structuredClone(job);
}
function demoInspection(path?: string) { return { inspection_id: 'demo-inspection', version: 'Démonstration', allowed: true, compatible: true, can_apply: true, path, checks: [{ label: 'Intégrité de l’archive', ok: true, message: 'Vérification simulée.' }, { label: 'Compatibilité du bundle', ok: true }, { label: 'Adresses IP inchangées', ok: true }], message: 'Archive fictive. Aucune installation réelle ne sera exécutée.' }; }
async function demoRequest(method: string, params: Record<string, unknown>): Promise<unknown> {
  await new Promise(resolve => setTimeout(resolve, 130));
  syncJob();
  switch (method) {
    case 'status.get': return structuredClone({ ...mockStatus, collected_at: new Date().toISOString() });
    case 'app.version': return { version: mockStatus.version };
    case 'app_updates.status': if(demoAppUpdate.installation?.status==='scheduled' && Date.now()-demoInstallationStarted>2500) {demoAppUpdate.installation={status:'installed',version:'0.3.0-démo',message:'Mise à jour simulée terminée.'};demoAppUpdate.current_version='0.3.0-démo';demoAppUpdate.available=false;} return structuredClone(demoAppUpdate);
    case 'app_updates.check': demoAppUpdate={...demoAppUpdate,available:true,release:{release_id:2,asset_id:3,version:'0.3.0-démo',tag:'v0.3.0-démo',notes:'Release fictive, aucun fichier réel téléchargé.',published_at:new Date().toISOString(),asset_name:'aquahub-application_0.3.0-demo_amd64.deb',asset_size:1024,sha256:'f'.repeat(64)},message:'Une version de démonstration est disponible.'};return structuredClone(demoAppUpdate);
    case 'app_updates.download': return startJob('Téléchargement d’AquaHub', 'app-download');
    case 'app_updates.install': demoInstallationStarted=Date.now();demoAppUpdate.installation={status:'scheduled',version:'0.3.0-démo',message:'Installation de démonstration en cours.'};return {scheduled:true,version:'0.3.0-démo'};

    case 'events.list': return { events: structuredClone(demoEvents.filter(event => !params.service || event.service === params.service).slice(0, Number(params.limit ?? 200))) };
    case 'events.logs': return { id: 'demo-log-snapshot', text: '[DÉMONSTRATION]\nErreur simulée avant redémarrage du service.\nAucun journal de panne réel n’est chargé.', redacted: true, truncated: true };
    case 'service.logs': return { text: `[DÉMONSTRATION] ${String(params.service)}\n${new Date().toISOString()} Service simulé en cours.\nAucun journal réel n’est chargé.` };
    case 'service.restart': return startJob(`Redémarrage de ${mockStatus.services.find(service => service.id === params.service)?.name ?? 'service'}`, String(params.service));
    case 'portainer.restart': return startJob('Relance de Portainer', 'portainer');
    case 'native.export_logs': return {saved:false,canceled:true};
    case 'native.source_credentials': return { canceled: true };
    case 'native.portainer_setup': return startJob('Initialisation Portainer', 'portainer-setup');
    case 'jobs.get': if (job?.id === params.id) return structuredClone(job); throw new Error('Tâche de démonstration introuvable.');
    case 'drones.list': case 'drones.scan': return droneData();
    case 'drones.identity': return { number: 5, address: '192.168.205.2', host_key_fingerprint: 'SHA256:EMPREINTE-DEMONSTRATION', trusted: false, message: 'Démonstration : cette empreinte est fictive.' };
    case 'drones.enroll': return { trusted: true };
    case 'drones.ssh_settings': demoSsh = { ...demoSsh, ...params }; return { ...demoSsh };
    case 'drones.preflight': return { preflight_id: 'demo-preflight', number: 5, address: '192.168.205.2', allowed: Number(params.number) === 5, needs_identity_confirmation: true, manual_checklist: demoManualChecks, identity: { machine_id: 'APPAREIL-DEMONSTRATION-5', host_key_fingerprint: 'SHA256:EMPREINTE-DEMONSTRATION' }, checks: [{ label: 'Appareil joignable', ok: true, message: 'Vérification simulée.' }, { label: 'Correspondance du numéro 5', ok: true, message: 'Configuration existante du bundle.' }, { label: 'Adresses IP conservées', ok: true, message: 'Aucune modification du réseau.' }] };
    case 'drones.configure': return startJob('Configuration de Aqua_Drone_5', 'drone');
    case 'settings.get': return { ...demoSettings };
    case 'settings.save': demoSettings = { ...demoSettings, ...params }; localStorage.setItem('aquahub-demo-settings', JSON.stringify(demoSettings)); return { ...demoSettings };
    case 'native.choose_file': return { path: '/exemple/livraison-aquahub-demo.tar.gz', canceled: false };
    case 'updates.inspect': return demoInspection(String(params.path));
    case 'updates.apply': return startJob('Installation de la mise à jour', 'update');
    case 'updates.check': if (!demoSettings.github_url) throw new Error('Renseignez d’abord le dépôt fournisseur dans les réglages.'); return { message: 'Démonstration : aucune source fournisseur réelle n’est interrogée.', releases: [{ release_id: 1, version: 'démo', name: 'Livraison de démonstration', notes: 'Données fictives. Ce téléchargement est simulé.', compatible: true, assets: [{ asset_id: 1, name: 'aqua-hub-demo-amd64.tar.gz', size: 1024, compatible: true, downloadable: true, sha256: 'demo', message: 'Le fichier sera analysé après son téléchargement simulé.' }] }] };
    case 'updates.download': return startJob('Téléchargement de la livraison', 'download');
    case 'diagnostics.export': return { path: '/exemple/diagnostic-aquahub-demo.zip' };
    case 'native.open': case 'native.show_folder': return { message: 'En démonstration, aucun programme externe n’est ouvert.' };
    default: throw new Error(`La méthode ${method} n’est pas disponible en démonstration.`);
  }
}
