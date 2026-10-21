import type { Drone, HubStatus, Job, Preflight, Service, Inspection, JobProgress } from './types';

export function escape(value: unknown): string { return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]!)); }
export function plainLog(text: string): string { return text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '').slice(-200000); }
export function serviceState(state: string): string { return ({ running: 'En cours', exited: 'Arrêté', stopped: 'Arrêté', restarting: 'Redémarrage', created: 'Créé', paused: 'En pause', dead: 'En échec', missing: 'Absent' } as Record<string, string>)[state] ?? 'État inconnu'; }
export function observationFresh(status: HubStatus | null, connected: boolean, now = Date.now()): boolean {
  if (!connected || !status?.docker_available || status.stale || !status.collected_at) return false;
  const collected = Date.parse(status.collected_at);
  return Number.isFinite(collected) && now - collected >= -5000 && now - collected <= 30000;
}
export function serviceTone(service: Service, fresh: boolean): 'running' | 'failed' | 'transition' | 'unknown' {
  if (!fresh) return 'unknown';
  if (service.state === 'running') return 'running';
  if (['exited', 'stopped', 'dead', 'missing'].includes(service.state)) return 'failed';
  if (['restarting', 'paused', 'created'].includes(service.state)) return 'transition';
  return 'unknown';
}

interface ServicePresentationOptions { requesting?: string; job?: Job | null; jobError?: string; unconfirmed?: { requestedAt: number; failedAt: number }; now?: number; }
interface ServicePresentation { tone: ReturnType<typeof serviceTone>; label: string; shortLabel?: string; description: string; operation?: 'requesting' | 'active' | 'refreshing' | 'uncertain'; }

function restartFor(service: Service, job: Job | null | undefined): job is Job {
  return Boolean(job && job.kind === 'service.restart' && job.target === service.id);
}
function timestamp(value: string | undefined): number { return typeof value === 'string' && value.trim() ? Date.parse(value) : NaN; }

function latestRestart(service: Service, status: HubStatus | null, current?: Job | null): Job | undefined {
  // The agent supplies recent jobs newest first. The polled job may be more
  // recent than the cached status, including after an almost immediate restart.
  for (const candidate of [current, status?.active_job]) {
    if (candidate?.id === current?.id && current && !isActive(current)) continue;
    if (restartFor(service, candidate) && isActive(candidate)) return candidate;
  }
  const saved = Array.isArray(status?.jobs) ? status.jobs.find(job => restartFor(service, job)) : undefined;
  if (!restartFor(service, current)) return saved;
  if (!saved || current.id === saved.id) return current;
  const currentAt = timestamp(current.created_at ?? current.finished_at);
  const savedAt = timestamp(saved.created_at ?? saved.finished_at);
  return Number.isFinite(currentAt) && Number.isFinite(savedAt) && currentAt > savedAt ? current : saved;
}

export function servicePresentation(service: Service, status: HubStatus | null, connected: boolean, options: ServicePresentationOptions = {}): ServicePresentation {
  const now = options.now ?? Date.now();
  const fresh = observationFresh(status, connected, now);
  const job = latestRestart(service, status, options.job);
  const requested = options.requesting === service.id;
  const uncertain = (): ServicePresentation => ({ tone: 'unknown', label: 'État à vérifier', description: 'Le résultat de la relance et l’état actuel de ce service restent à vérifier.', operation: 'uncertain' });
  const unconfirmed = options.unconfirmed;
  const recovered = job && unconfirmed && timestamp(job.created_at) >= unconfirmed.requestedAt;
  if (unconfirmed && !recovered && (!fresh || !Number.isFinite(unconfirmed.failedAt) || timestamp(status?.collected_at) <= unconfirmed.failedAt)) return uncertain();
  if ((job || requested) && !fresh) return uncertain();
  if (requested) return { tone: 'transition', label: 'Relance demandée', shortLabel: 'Relance', description: 'Demande de redémarrage envoyée ; attente de sa prise en compte par l’agent.', operation: 'requesting' };
  if (job) {
    if (isActive(job)) {
      if (options.jobError && options.job?.id === job.id) return uncertain();
      return { tone: 'transition', label: ['queued', 'pending'].includes(job.status) ? 'Relance en attente' : 'Redémarrage en cours', shortLabel: 'Relance', description: 'La tâche de redémarrage de ce service est suivie par AquaHub.', operation: 'active' };
    }
    const finished = timestamp(job.finished_at);
    const collected = timestamp(status?.collected_at);
    if (!Number.isFinite(finished) || finished > now + 5000 || !Number.isFinite(collected)) return uncertain();
    // collected_at is the START of collection. Only an observation begun after
    // completion can safely replace the maintenance indicator with Docker state.
    if (collected <= finished) {
      if (now - finished > 30000) return uncertain();
      return { tone: 'unknown', label: 'Actualisation', description: 'Relance terminée ; attente d’une observation du service postérieure à la tâche.', operation: 'refreshing' };
    }
  }
  return { tone: serviceTone(service, fresh), label: serviceState(service.state), description: fresh ? '' : 'Dernier état observé ; état actuel non confirmé' };
}
export function healthState(health: string | null): string { if (!health || ['none', 'not_defined', 'undefined'].includes(health)) return 'Non défini'; return ({ healthy: 'Validé', unhealthy: 'En échec', starting: 'Vérification' } as Record<string, string>)[health] ?? 'Inconnu'; }
export function counts(services: Service[]) { return { total: services.length, running: services.filter(s => s.state === 'running').length, healthy: services.filter(s => s.health === 'healthy').length, unhealthy: services.filter(s => s.health === 'unhealthy').length, undefined: services.filter(s => healthState(s.health) === 'Non défini').length }; }
export function identifiedDrones(drones: Drone[]): Drone[] { return drones.filter(drone => drone.reachable === true || drone.identity_verified === true || drone.configured === true).sort((a, b) => Number(b.reachable === true) - Number(a.reachable === true) || a.number - b.number); }
export function validateStatus(value: unknown): HubStatus {
  const invalid = () => new Error('La réponse de supervision est incomplète. Vérifiez que l’application et son agent sont de la même version.');
  if (!value || typeof value !== 'object' || !('services' in value) || !Array.isArray(value.services)) throw invalid();
  for (const service of value.services) if (!service || typeof service !== 'object' || typeof service.id !== 'string' || !service.id || typeof service.name !== 'string' || typeof service.state !== 'string' || !(service.health == null || typeof service.health === 'string')) throw invalid();
  if ('active_job' in value && value.active_job != null && (typeof value.active_job !== 'object' || !('id' in value.active_job) || typeof value.active_job.id !== 'string' || !('status' in value.active_job) || typeof value.active_job.status !== 'string')) throw invalid();
  return value as HubStatus;
}
export function isActive(job: Job | null | undefined): boolean { return Boolean(job && !['succeeded', 'completed', 'failed', 'interrupted', 'canceled', 'cancelled'].includes(job.status)); }
export function jobState(job: Job): string { return ({ queued: 'En attente', pending: 'En attente', running: 'En cours', in_progress: 'En cours', succeeded: 'Terminée', completed: 'Terminée', failed: 'Échec', interrupted: 'Interrompue', canceled: 'Annulée', cancelled: 'Annulée' } as Record<string, string>)[job.status] ?? 'Résultat inconnu'; }
export function jobSucceeded(job: Job): boolean { return ['succeeded', 'completed'].includes(job.status); }
export function preflightAllowed(preflight: Preflight | null, drone: Drone | undefined): boolean { return Boolean(drone?.supported && drone.reachable === true && preflight && (preflight.id || preflight.preflight_id) && preflight.allowed === true && preflight.number !== undefined && preflight.number === drone.number && preflight.address === drone.address && (preflight.checks ?? []).every(check => check.ok !== false)); }
export function inspectionAllowed(inspection: Inspection | null): boolean { return Boolean(inspection && (inspection.id || inspection.inspection_id) && inspection.allowed === true && inspection.can_apply === true && (inspection.checks ?? []).every(check => check.ok !== false)); }
export function restartAllowed(status: HubStatus | null, service: Service | undefined, busy: boolean): boolean { return Boolean(status?.docker_available && !status.stale && service && service.state !== 'missing' && service.actions?.restart !== false && !busy && !isActive(status.active_job)); }
export function rpcResultError(result: unknown): string | null {
  if (!result || typeof result !== 'object' || !('error' in result) || !result.error || 'status' in result || 'services' in result) return null;
  const error = result.error;
  if (typeof error === 'string') return error;
  if (typeof error === 'object' && 'message' in error && typeof error.message === 'string') return error.message;
  return 'L’agent a refusé cette opération. Consultez le diagnostic pour en connaître la cause.';
}
export function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) return String(error.message);
  if (typeof error === 'string') return error;
  return 'L’opération n’a pas abouti. Réessayez ou exportez un diagnostic depuis les réglages.';
}
export function normalizeRepository(input: string): string {
  if (!input.trim()) return '';
  let url: URL;
  try { url = new URL(input.trim()); } catch { throw new Error('Saisissez une URL de dépôt, par exemple https://github.com/organisation/depot.'); }
  if (url.protocol !== 'https:' || !['github.com', 'gitlab.com'].includes(url.hostname) || url.port || url.username || url.password || url.search || url.hash) throw new Error('Utilisez l’URL HTTPS d’un dépôt github.com ou gitlab.com, sans identifiants ni paramètres.');
  const parts = url.pathname.replace(/\.git\/?$/, '').split('/').filter(Boolean);
  if ((url.hostname === 'github.com' ? parts.length !== 2 : parts.length < 2) || !parts.every(part => /^[A-Za-z0-9_.-]+$/.test(part)) || parts.some(part => ['.', '..', '-'].includes(part))) throw new Error('L’URL doit désigner un dépôt : https://github.com/organisation/depot.');
  return `https://${url.hostname}/${parts.join('/')}`;
}
export class ActionGate {
  private pending = false;
  get busy() { return this.pending; }
  async run<T>(operation: () => Promise<T>): Promise<T | undefined> { if (this.pending) return undefined; this.pending = true; try { return await operation(); } finally { this.pending = false; } }
}

export function jobLabel(job: Job): string {
  const droneKind = ['drone.configure', 'drone_configure', 'configure_drone', 'configure'].includes(job.kind ?? '');
  const target = String(job.target ?? '');
  const droneNumber = target.match(/^(?:Aqua_Drone_)?([1-9][0-9]*)$/i)?.[1];
  if (droneKind && droneNumber) return `Configuration de Aqua_Drone_${droneNumber}`;
  return job.label ?? ({ restart: 'Redémarrage du service', service_restart: 'Redémarrage du service', 'service.restart': 'Redémarrage du service', 'portainer.restart': 'Relance de Portainer', 'portainer.initialize': 'Initialisation Portainer', 'app.download': 'Téléchargement d’AquaHub', 'app.install': 'Mise à jour de l’application AquaHub', 'app_updates.download': 'Téléchargement d’AquaHub', 'app_updates.install': 'Mise à jour de l’application AquaHub', download: 'Téléchargement de la livraison', update: 'Installation de la livraison', rollback: 'Restauration de la livraison', drone_configure: 'Configuration Aqua_Drone', configure_drone: 'Configuration Aqua_Drone', configure: 'Configuration Aqua_Drone' } as Record<string, string>)[job.kind ?? ''] ?? 'Tâche de maintenance';
}
export function jobProgress(job: Job): JobProgress {
  const progress = job.progress;
  if (typeof progress === 'number' && Number.isFinite(progress)) return { percent: Math.max(0, Math.min(100, progress)), indeterminate: false };
  if (progress && typeof progress === 'object') {
    const percent = typeof progress.percent === 'number' && Number.isFinite(progress.percent) ? Math.max(0, Math.min(100, progress.percent)) : undefined;
    return { ...progress, percent, indeterminate: progress.indeterminate === true || percent === undefined };
  }
  return { indeterminate: true };
}

export function progressCaption(progress: JobProgress): string {
  if (!Number.isSafeInteger(progress.completed) || !Number.isSafeInteger(progress.total) || Number(progress.total) <= 0) return '';
  if (progress.phase === 'download') {
    const size = (bytes: number) => new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(bytes / (1024 * 1024));
    return `${size(Number(progress.completed))} / ${size(Number(progress.total))} Mo reçus`;
  }
  return `${progress.completed}/${progress.total} étapes validées`;
}
