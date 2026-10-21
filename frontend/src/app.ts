import { demo, request } from './bridge';
import { ActionGate, counts, errorMessage, escape as e, identifiedDrones, inspectionAllowed, isActive, jobLabel, jobProgress, jobState, jobSucceeded, normalizeRepository, observationFresh, plainLog, preflightAllowed, progressCaption, restartAllowed, servicePresentation, serviceState, validateStatus } from './domain';
import { icon, serviceIcon } from './icons';
import type { AppUpdateStatus, Check, Drone, EventsPage, HubEvent, HubStatus, Inspection, Job, ManualCheck, Page, Preflight, Release, Service, Settings, SshSettings, Theme } from './types';

const root = document.querySelector<HTMLDivElement>('#app')!;
const dialog = document.querySelector<HTMLDialogElement>('#confirmation')!;
const actionGate = new ActionGate();
const pageNames: Record<Page, string> = { overview: 'Supervision', updates: 'Mises à jour', drones: 'Aqua_Drones', history: 'Historique', settings: 'Réglages' };
const pageIcons: Record<Page, string> = { overview: 'monitor', updates: 'refresh', drones: 'drone', history: 'history', settings: 'settings' };
const state = {
  page: readPage(), theme: readTheme(), status: null as HubStatus | null, loading: true, connected: false, agentConnected: false, error: '', lastUpdate: null as Date | null,
  selectedService: '', detailTab: 'events' as 'events' | 'logs', detailEvents: [] as HubEvent[], detailError: '', logs: '', logsLoading: false, logsFollow: storageGet('aquahub-logs-follow') !== 'false',
  events: [] as HubEvent[], historyError: '', historyService: '', historySearch: '', historyLoading: false, historyLoadingMore: false, historyLoaded: false, historyBefore: null as number | string | null, historyHasMore: false, historyErrorMore: false, historyRetention: null as EventsPage['retention'] | null,
  drones: [] as Drone[], dronesLoaded: false, dronesError: '', droneWarnings: [] as string[], selectedDrone: 0, preflight: null as Preflight | null,
  appUpdate: null as AppUpdateStatus | null, appUpdateError: '', appUpdateCheckError: '', appUpdateLoaded: false, appInstallPending: false, appInstallVersion: '', inspection: null as Inspection | null, updateResult: '', releases: [] as Release[], settings: {} as Settings, settingsLoaded: false, settingsError: '', githubDraft: '',
  job: null as Job | null, jobError: '', jobResultDismissed: '', busyLabel: '', requestingService: '', unconfirmedRestarts: {} as Record<string, { requestedAt: number; failedAt: number }>, version: '', diagnosticPath: '',
  ssh: null as SshSettings | null,
};
let refreshing = false;
let detailSequence = 0;
let dronesRefreshing = false;
let historySequence = 0;
let jobTimer: ReturnType<typeof setTimeout> | undefined;
let jobPolling = false;
let stopped = false;
let filePickerOpen = false;
let snapshotLoading = false;
let appUpdateSequence = 0;
let appUpdateChecking = false;
let lastObservationFresh = false;

function storageGet(key: string): string | null { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Session preferences remain usable when storage is unavailable. */ } }
function readPage(): Page { const page = location.hash.replace('#', '') || storageGet('aquahub-page') || 'overview'; return Object.hasOwn(pageNames, page) ? page as Page : 'overview'; }
function readTheme(): Theme { const theme = storageGet('aquahub-theme'); return theme === 'dark' || theme === 'light' ? theme : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
function busy(): boolean { return state.appInstallPending || actionGate.busy || isActive(state.job) || isActive(state.status?.active_job); }
function selectedService(): Service | undefined { return state.status?.services.find(service => service.id === state.selectedService); }
function selectedDrone(): Drone | undefined { return state.drones.find(drone => drone.number === state.selectedDrone); }
function disabled(value: boolean): string { return value ? ' disabled' : ''; }
function button(label: string, action: string, options: { icon?: string; primary?: boolean; disabled?: boolean; class?: string; title?: string } = {}): string {
  return `<button type="button" class="button ${options.primary ? 'primary' : ''} ${options.class ?? ''}" data-action="${action}"${disabled(Boolean(options.disabled))}${options.title ? ` title="${e(options.title)}"` : ''}>${options.icon ? icon(options.icon) : ''}<span>${e(label)}</span></button>`;
}
function badge(label: string, tone = ''): string { return `<span class="badge ${tone}">${e(label)}</span>`; }
function servicesFresh(): boolean { return observationFresh(state.status, state.agentConnected); }
function serviceBadge(service: Service, compact = false): string {
  const presentation = servicePresentation(service, state.status, state.agentConnected, { requesting: state.requestingService, job: state.job, jobError: state.jobError, unconfirmed: state.unconfirmedRestarts[service.id] });
  const label = compact ? presentation.shortLabel ?? presentation.label : presentation.label;
  const accessible = [presentation.label, presentation.description].filter(Boolean).join(' · ');
  return `<span class="badge service-state state-${presentation.tone}${presentation.operation ? ' service-state-operation' : ''}"${presentation.description ? ` title="${e(accessible)}"` : ''}${presentation.operation ? ` data-operation="${presentation.operation}" aria-label="${e(accessible)}"` : ''}>${e(label)}${!presentation.operation && presentation.description ? '<span class="sr-only"> · État actuel non confirmé</span>' : ''}</span>`;
}
function empty(title: string, text: string, iconName = 'info'): string { return `<div class="empty-state">${icon(iconName)}<strong>${e(title)}</strong><p>${e(text)}</p></div>`; }
function errorBox(message: string, retry?: string): string { return `<div class="notice error" role="alert">${icon('warning')}<div><strong>Vérification nécessaire</strong><p>${e(message)}</p>${retry ? button('Réessayer', retry, { class: 'small' }) : ''}</div></div>`; }
function date(value?: string, full = false): string { if (!value) return 'Non disponible'; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? 'Date inconnue' : new Intl.DateTimeFormat('fr-FR', full ? { dateStyle: 'medium', timeStyle: 'short' } : { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(parsed); }
function serviceName(id?: string | null): string { return state.status?.services.find(service => service.id === id)?.name ?? id ?? 'AquaHub'; }
function jobResultMessage(job: Job): string { const result = job.result; return result && typeof result === 'object' && 'message' in result && typeof result.message === 'string' ? result.message : ''; }

function layout() {
  document.documentElement.dataset.theme = state.theme;
  root.innerHTML = `<aside class="sidebar" aria-label="Navigation principale">
    <a class="brand" href="#overview" aria-label="AquaHub, supervision"><svg viewBox="0 0 40 28" width="38" height="28" fill="none" stroke="currentColor" stroke-width="3.3" stroke-linecap="round" aria-hidden="true"><path d="M2 8c6-7 10-7 17-1s12 7 19-1M2 21c6-7 10-7 17-1s12 7 19-1"/></svg><span>Aqua<strong>Hub</strong></span></a>
    <nav>${(['overview', 'updates', 'drones', 'history'] as Page[]).map(page => `<button type="button" class="nav-item" data-page="${page}" aria-label="${pageNames[page]}">${icon(pageIcons[page])}<span>${pageNames[page]}</span>${page === 'overview' ? '<span id="nav-services" class="nav-count">—</span>' : ''}</button>`).join('')}</nav>
    <div class="sidebar-bottom">
      <button type="button" class="nav-item" data-action="open-portainer" aria-label="Ouvrir Portainer">${icon('external')}<span>Portainer</span>${icon('external', 'tiny')}</button>
      <button type="button" class="nav-item" data-page="settings" aria-label="Réglages">${icon('settings')}<span>Réglages</span></button>
      <div class="theme-control"><span id="theme-label">${icon(state.theme === 'dark' ? 'moon' : 'sun')}<span>Mode ${state.theme === 'dark' ? 'sombre' : 'clair'}</span></span><button type="button" id="theme-toggle" class="switch" role="switch" aria-checked="${state.theme === 'dark'}" aria-label="Activer le mode sombre" data-action="theme"><span></span></button></div>
      <div class="machine">${icon('monitor')}<div><span id="machine-name">Connexion au poste…</span><small id="app-version">AquaHub</small></div></div>
    </div>
  </aside>
  <main class="main"><div id="demo-banner"></div><header class="page-header"><div><div class="eyebrow">AquaHub · Exploitation</div><h1 id="page-title">Supervision</h1><p id="page-description" class="page-description"></p></div><div class="header-actions"><span id="freshness" class="freshness"></span><button type="button" class="icon-button" data-action="refresh" aria-label="Actualiser les données" title="Actualiser les données">${icon('refresh')}</button></div></header><div id="connection-banner"></div><div id="job-banner"></div><div id="page-content" tabindex="-1"></div><footer class="app-footer"><span>AquaHub <span id="footer-version"></span></span><span>Votre flotte, simplement.</span></footer></main>`;
  root.addEventListener('click', event => {
    const target = (event.target as Element).closest<HTMLElement>('button, [data-action]');
    if (!target || target.hasAttribute('disabled')) return;
    if (target.dataset.page) { setPage(target.dataset.page as Page); return; }
    if (target.dataset.service) { state.selectedService = target.dataset.service; state.detailEvents = []; state.logs = ''; state.detailError = ''; renderPage(); void loadDetail(); return; }
    if (target.dataset.drone) { state.selectedDrone = Number(target.dataset.drone); state.preflight = null; renderPage(); return; }
    if (target.dataset.action) void handleAction(target.dataset.action, target).catch(error => notify(errorMessage(error), true));
  });
  root.addEventListener('submit', event => { event.preventDefault(); if ((event.target as Element).id === 'settings-form') void saveSettings().catch(error => notify(errorMessage(error), true)); else if ((event.target as Element).id === 'ssh-form') void saveSshSettings().catch(error => notify(errorMessage(error), true)); });
  root.addEventListener('keydown', event => {
    const target = event.target as HTMLElement;
    if (target.getAttribute('role') !== 'tab' || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tab = event.key === 'Home' ? 'events' : event.key === 'End' ? 'logs' : state.detailTab === 'events' ? 'logs' : 'events';
    void handleAction(`detail-${tab}`).catch(error => notify(errorMessage(error), true));
    document.getElementById(`${tab}-tab`)?.focus();
  });
  root.addEventListener('change', event => {
    const target = event.target as HTMLInputElement;
    if (target.id === 'history-filter') { state.historyService = target.value; void loadHistory(); }
    if (target.id === 'logs-follow') { state.logsFollow = target.checked; storageSet('aquahub-logs-follow', String(target.checked)); }
    if (state.ssh && target.id === 'ssh-none-auth') state.ssh.allow_none_auth = target.checked;
  });
  root.addEventListener('input', event => {
    const target = event.target as HTMLInputElement;
    if (target.id === 'github-url') state.githubDraft = target.value;
    if (state.ssh && target.id === 'ssh-identity-file') state.ssh.identity_file = target.value;
    if (state.ssh && target.id === 'ssh-known-hosts-file') state.ssh.known_hosts_file = target.value;
    if (target.id === 'history-search') { state.historySearch = target.value; renderHistoryEvents(); }
  });
  if (demo) document.querySelector('#demo-banner')!.innerHTML = `<div class="demo-banner">${icon('info')}<span><strong>Mode démonstration</strong> · Données fictives, aucune action sur vos équipements.</span></div>`;
  renderChrome(); renderPage();
}

function renderChrome() {
  root.querySelector<HTMLElement>('.main')!.dataset.page = state.page;
  document.title = `${pageNames[state.page]} · AquaHub`;
  document.querySelector('#page-title')!.textContent = pageNames[state.page];
  const descriptions: Record<Page, string> = { overview: 'L’essentiel de votre installation, en un regard.', updates: 'Préparer, vérifier, puis installer en confiance.', drones: 'Retrouvez vos appareils et leurs configurations existantes.', history: 'Les événements de votre installation, conservés dans le temps.', settings: 'Les préférences et les informations de votre application.' };
  document.querySelector('#page-description')!.textContent = descriptions[state.page];
  root.querySelectorAll<HTMLElement>('[data-page]').forEach(element => { const active = element.dataset.page === state.page; element.classList.toggle('active', active); if (active) element.setAttribute('aria-current', 'page'); else element.removeAttribute('aria-current'); });
  document.querySelector('#machine-name')!.textContent = state.status?.machine ?? 'Poste local';
  const version = state.status?.version ?? state.version;
  document.querySelector('#app-version')!.textContent = `AquaHub${version ? ` · ${version}` : ''}`;
  document.querySelector('#footer-version')!.textContent = version;
  document.querySelector('#nav-services')!.textContent = state.status ? String(state.status.services.length) : '—';
  document.querySelector('#freshness')!.innerHTML = state.lastUpdate ? `<span class="connection-dot ${servicesFresh() ? '' : 'offline'}"></span><span>${servicesFresh() ? 'Actualisé' : 'Dernière lecture'} à ${date(state.lastUpdate.toISOString())}</span>` : 'Connexion…';
  const error = document.querySelector('#connection-banner')!;
  error.innerHTML = state.error ? `<div class="notice error" role="alert">${icon('warning')}<div><strong>${state.status?.stale ? 'Supervision non actualisée' : 'Connexion à l’agent indisponible'}</strong><p>${e(state.error)}</p>${state.status ? '<p>Les dernières données reçues restent visibles. Leur état actuel n’est pas confirmé.</p>' : ''}</div>${button('Réessayer', 'refresh', { class: 'small' })}</div>` : '';
  const current = state.job ?? state.status?.active_job;
  const jobBanner = document.querySelector('#job-banner')!;
  if (current && isActive(current)) {
    const progress = jobProgress(current);
    const stepText = progressCaption(progress);
    jobBanner.innerHTML = `<div class="job-banner" role="status"><span class="spinner"></span><div class="job-message"><strong>${e(jobLabel(current))}</strong><p>${e(progress.label ?? current.message ?? 'Vous pouvez continuer à consulter AquaHub.')}</p>${stepText ? `<p class="progress-caption">${e(stepText)}${progress.indeterminate ? ' · Vérification en cours, durée variable.' : ''}</p>` : ''}${state.jobError ? `<p class="text-warning">${e(state.jobError)} Le résultat reste à vérifier.</p>` : ''}</div><div class="job-progress"><progress max="100"${progress.percent === undefined ? '' : ` value="${progress.percent}"`} aria-label="Progression de ${e(jobLabel(current))}"></progress><span>${progress.percent === undefined ? 'En cours…' : `${progress.percent} %${progress.indeterminate ? ' validés' : ''}`}</span></div></div>`;
  }
  else if (state.appInstallPending) jobBanner.innerHTML = `<div class="job-banner" role="status"><span class="spinner"></span><div><strong>Installation d’AquaHub ${e(state.appInstallVersion)}</strong><p>${e(state.appUpdate?.installation?.message ?? 'L’agent pourra se déconnecter pendant sa mise à jour. AquaHub vérifie automatiquement le résultat.')}</p></div><progress aria-label="Installation de l’application AquaHub"></progress></div>`;
  else if (state.busyLabel) jobBanner.innerHTML = `<div class="job-banner" role="status"><span class="spinner"></span><div><strong>${e(state.busyLabel)}</strong><p>Veuillez patienter pendant la vérification.</p></div></div>`;
  else if (current && jobSucceeded(current) && current.result && typeof current.result === 'object' && 'manual_skipped' in current.result && Number(current.result.manual_skipped) > 0 && state.jobResultDismissed !== current.id) jobBanner.innerHTML = `<div class="notice completion-notice" role="status">${icon('info')}<div><strong>${e(jobLabel(current))} · Terminée</strong><p>${e(jobResultMessage(current) || `${current.result.manual_skipped} contrôles manuels restent à effectuer manuellement.`)} ${manualCheckInfo(manualChecklist(current), 'completion')}</p></div>${button('Fermer', 'dismiss-job-result', { class: 'small' })}</div>`;
  else jobBanner.innerHTML = '';
}

function renderPage() {
  const page = document.querySelector('#page-content')!;
  const renderers: Record<Page, () => string> = { overview: overviewPage, updates: updatesPage, drones: dronesPage, history: historyPage, settings: settingsPage };
  replaceKeepingFocus(page, renderers[state.page]());
  if (state.page === 'overview') lastObservationFresh = servicesFresh();
  if (state.page === 'history') renderHistoryEvents();
}

function replaceKeepingFocus(container: Element, html: string) {
  const active = document.activeElement as HTMLElement | null;
  const inside = active && container.contains(active);
  const snapshot = inside ? { id: active.id, action: active.dataset.action, service: active.dataset.service, drone: active.dataset.drone, start: active instanceof HTMLInputElement ? active.selectionStart : null, end: active instanceof HTMLInputElement ? active.selectionEnd : null } : null;
  const oldLogs = container.querySelector<HTMLElement>('#service-logs');
  const logPosition = oldLogs ? { service: oldLogs.dataset.logService, top: oldLogs.scrollTop, atBottom: oldLogs.scrollHeight - oldLogs.scrollTop - oldLogs.clientHeight < 20 } : null;
  container.innerHTML = html;
  const newLogs = container.querySelector<HTMLElement>('#service-logs');
  if (newLogs && logPosition && logPosition.service === newLogs.dataset.logService) newLogs.scrollTop = logPosition.atBottom && state.logsFollow ? newLogs.scrollHeight : logPosition.top;
  if (!snapshot) return;
  const next = snapshot.id ? document.getElementById(snapshot.id) : [...container.querySelectorAll<HTMLElement>('button')].find(element => snapshot.service ? element.dataset.service === snapshot.service : snapshot.drone ? element.dataset.drone === snapshot.drone : snapshot.action ? element.dataset.action === snapshot.action : false);
  if (!next || !container.contains(next)) return;
  next.focus({ preventScroll: true });
  if (next instanceof HTMLInputElement && snapshot.start !== null && snapshot.end !== null) { try { next.setSelectionRange(snapshot.start, snapshot.end); } catch { /* Certain input types do not expose a text selection. */ } }
}

function overviewPage(): string {
  if (!state.status) return state.loading ? `<div class="loading-state" role="status"><span class="spinner"></span><strong>Connexion à votre installation</strong><p>Lecture des services et de leur état de santé…</p></div>` : empty('Installation indisponible', 'Réessayez la connexion pour consulter les services de votre poste.', 'server');
  const summary = counts(state.status.services);
  const knownDrones = identifiedDrones(state.drones);
  return `<section class="summary-strip" aria-label="État général"><div class="summary-main">${icon('server')}<div><strong>${summary.running}<span>/${summary.total}</span></strong><p>services en cours${servicesFresh() ? '' : ' · dernière observation'}</p></div></div><div class="summary-health ${summary.unhealthy && servicesFresh() ? 'health-alert' : ''}">${icon(summary.unhealthy ? 'warning' : 'heart')}<div><strong>${servicesFresh() ? 'Contrôles de santé' : 'Derniers contrôles de santé'}</strong><p>${summary.unhealthy ? `<strong>${summary.unhealthy} en échec</strong>` : summary.healthy ? `${summary.healthy} validé${summary.healthy > 1 ? 's' : ''}` : 'Détail par service'}</p></div></div><div class="maintenance-state">${icon('clock')}<span>${busy() ? 'Maintenance en cours' : 'Aucune tâche de maintenance'}</span></div></section>
  ${!state.status.docker_available ? `<div class="notice">${icon('info')}<div><strong>Le moteur Docker n’est pas accessible</strong><p>Les états des services ne peuvent pas être vérifiés. Consultez les réglages pour exporter un diagnostic.</p></div></div>` : ''}
  <div class="supervision-grid"><section class="card services-card"><div class="card-heading"><h2>Services</h2><span class="subtle">${summary.total} composants</span></div><div class="service-table"><div class="service-table-head"><span>Composant</span><span>État</span><span class="sr-only">Détails</span></div>${state.status.services.length ? state.status.services.map(service => `<button type="button" class="service-row ${service.id === state.selectedService ? 'selected' : ''}" data-service="${e(service.id)}" aria-pressed="${service.id === state.selectedService}"><span class="service-title">${icon(serviceIcon(service.id))}<span>${e(service.name)}${service.health === 'unhealthy' ? `<small class="service-health-alert ${servicesFresh() ? 'health-alert' : ''}">${icon('warning')}${servicesFresh() ? 'Santé en échec' : 'Dernière santé : en échec'}</small>` : ''}</span></span><span>${serviceBadge(service, true)}</span>${icon('chevron', 'row-chevron')}</button>`).join('') : empty('Aucun service reconnu', 'Aucun composant du bundle n’a été identifié sur le moteur local.', 'server')}</div><div class="card-footnote">${icon('info')}<span>Le contour indique l’état du conteneur. Gris : état actuel non confirmé. Les contrôles de santé sont détaillés en sélectionnant un service.</span></div></section>
  <section class="card detail-card" aria-label="Détail du service">${serviceDetail()}</section></div>
  <div class="bottom-grid"><section class="card compact-card"><div class="card-heading"><h2>${icon('apps')}Applications</h2></div>${button('Ouvrir GCS', 'open-gcs', { icon: 'monitor', class: 'full', disabled: !state.connected })}${button('Ouvrir Fleet Controller', 'open-fleet', { icon: 'server', class: 'full', disabled: !state.connected })}<p class="card-caption">Vos outils s’ouvrent dans le navigateur.</p></section>
  <section class="card compact-card"><div class="card-heading"><h2>${icon('refresh')}Mises à jour</h2></div><p class="muted">Retrouvez les nouvelles versions.</p>${button('Rechercher une mise à jour', 'go-updates', { icon: 'refresh', primary: true, class: 'full' })}<p class="card-caption">${icon('shield')}Vérifications avant toute installation</p></section>
  <section class="card compact-card"><div class="card-heading"><h2>${icon('drone')}Aqua_Drones</h2></div><p class="muted">${state.dronesLoaded ? knownDrones.length ? `${knownDrones.length} appareil${knownDrones.length > 1 ? 's' : ''} identifié${knownDrones.length > 1 ? 's' : ''}` : 'Aucun appareil identifié' : 'Lecture de l’inventaire…'}</p>${knownDrones.slice(0, 1).map(drone => `<div class="mini-drone">${icon('drone')}<span>${e(drone.name)}</span>${badge(drone.reachable === true ? 'Joignable' : drone.reachable === false ? 'Hors ligne' : 'Non vérifié', 'outlined')}</div>`).join('')}${state.dronesLoaded && !knownDrones.length ? '<p class="card-caption inventory-empty-note">Recherchez vos appareils pour les retrouver ici.</p>' : ''}<button type="button" class="text-button card-link" data-page="drones">Gérer les appareils ${icon('arrow')}</button></section></div>`;
}

function serviceDetail(): string {
  const service = selectedService();
  if (!service) return `<div class="card-heading"><h2>Détail du service</h2></div>${empty('Choisir un composant', 'Sélectionnez un service pour consulter son activité et ses journaux.', 'monitor')}`;
  const available = restartAllowed(servicesFresh() ? state.status : null, service, busy());
  return `<div class="card-heading"><h2>Détail du service</h2>${icon(serviceIcon(service.id), 'subtle')}</div><div class="service-profile"><div class="service-profile-icon">${icon(serviceIcon(service.id))}</div><div><h3>${e(service.name)}</h3>${serviceBadge(service)}<p class="${service.health === 'unhealthy' && servicesFresh() ? 'health-alert' : ''}">${icon(service.health === 'healthy' ? 'check' : service.health === 'unhealthy' ? 'warning' : 'info')}${!servicesFresh() ? 'Dernière observation · ' : ''}${service.health === 'healthy' ? 'Contrôle de santé validé' : service.health === 'unhealthy' ? 'Le contrôle de santé signale un échec' : service.health === 'starting' ? 'Contrôle de santé en cours' : 'Aucune sonde de santé fournie par le bundle'}</p></div></div>
  ${button('Redémarrer', 'restart', { icon: 'refresh', primary: true, class: 'full', disabled: !available, title: service.actions?.disabled_reason })}${service.actions?.restart === false && service.actions.disabled_reason ? `<p class="action-explanation">${e(service.actions.disabled_reason)}</p>` : ''}
  <div class="detail-tabs" role="tablist" aria-label="Activité du service"><button type="button" role="tab" id="events-tab" aria-selected="${state.detailTab === 'events'}" aria-controls="service-activity" data-action="detail-events">Événements</button><button type="button" role="tab" id="logs-tab" aria-selected="${state.detailTab === 'logs'}" aria-controls="service-activity" data-action="detail-logs">Logs techniques</button></div>
  <div id="service-activity" role="tabpanel" aria-labelledby="${state.detailTab === 'events' ? 'events-tab' : 'logs-tab'}">${state.detailError ? errorBox(state.detailError, 'detail-refresh') : state.detailTab === 'logs' ? `<div class="logs-toolbar"><span>200 dernières lignes</span><label class="logs-follow"><input type="checkbox" id="logs-follow"${state.logsFollow ? ' checked' : ''}>Suivre</label><button class="text-button" type="button" data-action="detail-refresh"${disabled(state.logsLoading || !state.connected)}>${icon('refresh')}Actualiser</button></div><pre id="service-logs" class="logs" data-log-service="${e(service.id)}" tabindex="0" aria-label="Journaux de ${e(service.name)}">${state.logsLoading && !state.logs ? 'Chargement des journaux…' : e(state.logs || 'Aucun journal disponible pour ce service.')}</pre><div class="logs-export">${button('Enregistrer les logs', 'export-service-logs', { icon: 'download', class: 'small', disabled: busy() || !state.connected })}<p>Extrait récent : jusqu’à 1 000 lignes, environ 48 Ko après filtrage. Les logs conservés par Docker dépendent de leur rotation.</p></div>` : timeline(state.detailEvents.slice(0, 4), true)}</div>
  <button type="button" class="text-button card-link" data-action="service-history">Voir l’historique ${icon('arrow')}</button><details class="technical-details"><summary>Informations techniques</summary><dl><div><dt>Dernier démarrage</dt><dd>${e(date(service.started_at, true))}</dd></div><div><dt>Compteur Docker</dt><dd>${e(service.restarts ?? 'Non disponible')} redémarrage(s)</dd></div><div><dt>Image</dt><dd class="mono">${e(service.image_id ?? 'Non disponible')}</dd></div></dl><p>Le compteur Docker ne constitue pas un historique complet.</p></details>`;
}

function timeline(events: HubEvent[], compact = false): string {
  if (!events.length) return empty('Aucun événement enregistré', compact ? 'Les prochains changements d’état apparaîtront ici.' : 'L’historique commence avec la collecte AquaHub. Les événements antérieurs ne sont pas reconstitués.', 'history');
  return `<ol class="timeline ${compact ? 'compact' : ''}">${events.map(event => `<li><span class="timeline-dot"></span><div class="timeline-content"><div class="event-meta"><time datetime="${e(event.timestamp)}">${e(date(event.timestamp, !compact))}</time>${!compact ? `<span>${e(serviceName(event.service))}</span>` : ''}</div><div class="event-message"><p>${e(event.message)}</p>${!compact ? `<small>${e(event.kind)}</small>` : ''}${event.log_snapshot ? ['available', 'empty'].includes(event.log_snapshot.status) ? `<button type="button" class="text-button event-snapshot" data-action="event-snapshot" data-snapshot-id="${e(event.log_snapshot.id)}">${icon('history')}Extrait de panne</button>` : `<span class="snapshot-unavailable">${e(snapshotUnavailable(event.log_snapshot))}</span>` : ''}</div></div></li>`).join('')}</ol>`;
}

function snapshotUnavailable(snapshot: NonNullable<HubEvent['log_snapshot']>): string {
  const detail = snapshot.message ?? (typeof snapshot.error === 'string' ? snapshot.error : snapshot.error?.message);
  if (snapshot.status === 'expired') return 'Extrait de panne expiré : durée de conservation dépassée.';
  if (snapshot.status === 'rate_limited') return 'Extrait non disponible : limite de collecte atteinte.';
  if (snapshot.status === 'interrupted') return 'Extrait non disponible : capture interrompue.';
  return `Extrait non disponible${detail ? ` : ${detail}` : ' : les journaux n’étaient pas accessibles lors de la capture.'}`;
}

function updatesPage(): string {
  const inspection = state.inspection;
  return `${appUpdateCard()}<div class="update-section-title"><div><h2>Bundle Aqua Hub</h2><p>Les services et leur configuration, fournis par le développeur.</p></div></div>
  <section class="card source-card bundle-online" aria-label="Mise à jour du bundle en ligne"><div><h2>${icon('network')}Mise à jour en ligne</h2><p>${state.settings.github_url ? 'Recherchez une livraison du fournisseur, puis téléchargez-la pour vérifier sa compatibilité.' : 'Ajoutez le dépôt du fournisseur dans les réglages pour rechercher ses livraisons.'}</p></div><div class="button-row">${button('Rechercher une version', 'check-updates', { icon: 'refresh', primary: true, disabled: busy() || !state.connected || !state.settings.github_url })}${button('Réglages de la source', 'go-settings')}</div>${state.updateResult ? `<div class="source-result" role="status">${e(state.updateResult)}</div>` : ''}${releaseList()}<p class="online-availability">La recherche consulte les livraisons publiées du fournisseur. Sa nouvelle procédure de mise à jour reste à valider ; vous pouvez aussi importer une archive ci-dessous.</p></section>
  ${inspection ? `<section class="card inspection-card bundle-inspection"><div class="section-number">VÉRIFIER ET INSTALLER</div><h2>Vérification de la livraison</h2><div class="inspection-summary">${icon(inspectionAllowed(inspection) ? 'check' : 'info')}<div><h3>${e(inspection.version ? `Version ${inspection.version}` : 'Archive analysée')}</h3>${badge(inspectionAllowed(inspection) ? 'Prête à préparer' : 'Application indisponible', 'outlined')}</div></div>${inspection.path ? `<p class="file-path mono">${e(inspection.path)}</p>` : ''}${checks(inspection.checks ?? [])}${inspection.message ? `<div class="notice"><div><p>${e(inspection.message)}</p></div></div>` : ''}${button('Installer cette livraison', 'apply-update', { icon: 'download', primary: true, class: 'full', disabled: !inspectionAllowed(inspection) || busy() || !state.connected })}<p class="card-caption">Une confirmation est demandée avant interruption des services.</p></section>` : ''}
  <section class="card update-import bundle-local" aria-label="Mise à jour depuis une archive locale"><div><h2>Vous avez déjà téléchargé l’archive ?</h2><p class="lead">Installez depuis un fichier sur ce poste ou sur une clé USB. Les mêmes vérifications sont effectuées avant l’installation.</p></div>${button(inspection ? 'Choisir une autre archive' : 'Choisir une archive', 'import', { icon: 'file', disabled: busy() || !state.connected })}<p class="field-help">L’import seul ne modifie pas votre installation. Les paramètres existants et les adresses IP sont contrôlés avant application.</p></section>`;
}

function manualChecklist(job: Job): ManualCheck[] {
  const result = job.result;
  if (result && typeof result === 'object' && 'manual_checklist' in result && Array.isArray(result.manual_checklist)) return result.manual_checklist as ManualCheck[];
  return state.drones.find(drone => job.target === `Aqua_Drone_${drone.number}`)?.manual_checklist ?? [];
}
function manualCheckInfo(items: ManualCheck[], id: string): string {
  if (!items.length) return '';
  const summary = items.map((item, index) => `${index + 1}. ${item.label} : ${item.details}`).join('\n');
  return `<span class="info-help"><button type="button" class="info-trigger" data-action="manual-checks" data-checklist="${e(id)}" title="${e(summary)}" aria-label="Voir les ${items.length} contrôles manuels" aria-describedby="manual-help-${e(id)}">${icon('info')}</button><span role="tooltip" id="manual-help-${e(id)}" class="info-tooltip"><strong>Contrôles manuels</strong><span class="info-checks">${items.map((item,index) => `<span><strong>${index+1}. ${e(item.label)}</strong><span>${e(item.details)}</span></span>`).join('')}</span></span></span>`;
}
function appUpdateCard(): string {
  const status = state.appUpdate; const release = status?.release; const ready = status?.download?.ready === true && status.download.version === release?.version;
  return `<div class="update-section-title"><div><h2 id="application-update-title">Application AquaHub</h2><p>Votre interface de gestion et ses nouvelles fonctionnalités.</p></div>${badge(status?.current_version ?? state.status?.version ?? state.version ?? 'Version inconnue', 'outlined')}</div><section class="card app-update-card" aria-labelledby="application-update-title"><div class="app-update-main"><div><p class="field-help">Une mise à jour de l’application conserve le bundle et vos configurations.</p></div><div class="button-row">${button('Rechercher une mise à jour', 'check-app-update', { icon: 'refresh', disabled: busy() || !state.agentConnected })}</div></div>${state.appUpdateError ? errorBox(state.appUpdateError, 'load-app-update') : ''}${state.appUpdateCheckError ? errorBox(state.appUpdateCheckError, 'check-app-update') : ''}${status?.installation && ['installed', 'failed'].includes(status.installation.status) ? `<div class="notice ${status.installation.status === 'failed' ? 'error' : ''}" role="${status.installation.status === 'failed' ? 'alert' : 'status'}">${icon(status.installation.status === 'failed' ? 'warning' : 'check')}<div><strong>${status.installation.status === 'installed' ? `AquaHub ${e(status.installation.version)} installé` : 'Installation AquaHub en échec'}</strong><p>${e(status.installation.message ?? '')}${status.installation.status === 'installed' ? ' Fermez puis rouvrez AquaHub pour charger la nouvelle interface.' : ''}</p></div></div>` : ''}${status?.message ? `<p class="source-result" role="status">${e(status.message)}</p>` : ''}${release && status?.available ? `<div class="app-release"><div class="card-heading"><h3>AquaHub ${e(release.version)}</h3>${badge(ready ? 'Téléchargement vérifié' : 'Nouvelle version', 'outlined')}</div>${release.notes ? `<details class="release-notes"><summary>Notes de version</summary><p>${e(release.notes)}</p></details>` : ''}<p class="field-help">${new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(release.asset_size / (1024 * 1024))} Mo${release.published_at ? ` · ${e(date(release.published_at, true))}` : ''}</p><div class="button-row">${button(ready ? 'Télécharger à nouveau' : 'Télécharger AquaHub', 'download-app-update', { icon: 'download', disabled: busy() || !state.agentConnected })}${button('Installer la mise à jour AquaHub', 'install-app-update', { icon: 'check', primary: true, disabled: !ready || busy() || !state.agentConnected })}</div><p class="field-help">Le téléchargement n’installe rien. L’installation nécessite votre confirmation et l’autorisation du poste.</p></div>` : ''}</section>`;
}

function releaseList(): string {
  if (!state.releases.length) return '';
  return `<div class="releases">${state.releases.map(release => `<article class="release"><div class="card-heading"><h3>${e(release.name || release.version)}</h3>${badge(release.version, 'outlined')}</div>${release.notes ? `<details class="release-notes"><summary>Notes de version</summary><p>${e(release.notes)}</p></details>` : ''}${release.assets.length ? release.assets.map(asset => `<div class="release-asset">${icon('file')}<div><strong>${e(asset.name)}</strong><p>${e(asset.message ?? '')}</p><small>${new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(asset.size / (1024 * 1024))} Mo</small></div><button type="button" class="button small" data-action="download-release" data-release-id="${release.release_id}" data-asset-id="${asset.asset_id}"${disabled(!asset.downloadable || busy() || !state.connected)}>${icon('download')}<span>Télécharger pour vérifier</span></button></div>`).join('') : `<p class="muted">Aucun fichier de livraison joint à cette version.</p>`}</article>`).join('')}</div>`;
}

function checks(items: Check[]): string { return `<ul class="checks">${items.map(check => `<li>${icon(check.ok === true ? 'check' : check.ok === false ? 'warning' : 'info')}<div><strong>${e(check.label ?? check.name ?? 'Vérification')}</strong>${check.message ? `<p>${e(check.message)}</p>` : ''}</div><span class="check-outcome">${check.ok === true ? 'Validé' : check.ok === false ? 'Bloquant' : 'Information'}</span></li>`).join('')}</ul>`; }

function dronesPage(): string {
  const drone = selectedDrone();
  const reachable = state.drones.filter(item => item.reachable === true).length;
  const knownDrones = identifiedDrones(state.drones); const profiles = state.drones.filter(item => !knownDrones.includes(item));
  return `<div class="page-toolbar"><p class="muted">${state.dronesLoaded ? `${reachable} appareil${reachable > 1 ? 's' : ''} joignable${reachable > 1 ? 's' : ''} · ${knownDrones.length} identifié${knownDrones.length > 1 ? 's' : ''}` : 'Lecture de l’inventaire…'}</p>${button('Rechercher les appareils', 'scan-drones', { icon: 'search', disabled: busy() || dronesRefreshing || !state.connected })}</div>${state.dronesError ? errorBox(state.dronesError, 'load-drones') : ''}${state.droneWarnings.map(warning => `<div class="notice compact-notice">${icon('info')}<div><p>${e(warning)}</p></div></div>`).join('')}<div class="two-column drone-grid"><section class="card drone-list-card"><div class="card-heading"><h2>Appareils identifiés</h2>${badge(String(knownDrones.length), 'outlined')}</div><div class="drone-list">${knownDrones.length ? knownDrones.map(item => droneItem(item, false)).join('') : empty(state.dronesLoaded ? 'Aucun appareil identifié' : 'Chargement de l’inventaire', 'Recherchez vos Aqua_Drones ou sélectionnez leur numéro disponible dans le bundle.', 'drone')}</div>${profiles.length ? `<details class="bundle-profiles"${knownDrones.length ? '' : ' open'}><summary>Numéros disponibles du bundle <span>${profiles.length}</span></summary><p>Ces profils de configuration ne prouvent pas la présence d’un appareil. Choisissez le numéro de votre équipement pour le vérifier.</p><div class="drone-list">${profiles.map(item => droneItem(item, true)).join('')}</div></details>` : ''}<div class="sequential-note">Configuration des appareils l’un après l’autre : attendez la fin de la tâche avant de sélectionner le suivant.</div><div class="inventory-note">${icon('info')}<div><strong>Un inventaire conservé</strong><p>Un appareil reconnu reste visible hors ligne. Une réponse réseau ne suffit pas à confirmer son identité.</p></div></div></section>
  <section class="card drone-config"><div class="card-heading"><h2>Configuration du bundle</h2>${icon('shield', 'subtle')}</div>${drone ? `<dl class="config-fields"><div><dt>Appareil sélectionné</dt><dd><strong>${e(drone.name)}</strong></dd></div><div><dt>Adresse existante</dt><dd><div class="readonly-value mono">${icon('lock')}${e(drone.address)}</div><small>Adresse IP en lecture seule.</small></dd></div><div><dt>Numéro du bundle</dt><dd><div class="bundle-number">${drone.number}</div><small>Sélectionne les configurations existantes pour cet appareil.</small></dd></div></dl><div class="verification-block"><h3>Vérifier la correspondance</h3><p>Confirmez que l’appareil attendu répond avant tout envoi.</p>${button('Vérifier cet appareil', 'preflight', { icon: 'search', class: 'full', disabled: busy() || !state.connected || !drone.supported || drone.reachable === false })}<button type="button" class="text-button ssh-enroll-link" data-action="enroll-drone"${disabled(busy() || !state.connected || !drone.supported)}>${icon('lock')}Première connexion : vérifier l’identité SSH</button></div>${!drone.supported ? `<div class="notice">${icon('info')}<div><p>Le bundle ne propose pas de configuration prise en charge pour ce numéro.</p></div></div>` : ''}${state.preflight ? `<div class="preflight-result">${checks(state.preflight.checks ?? [])}${state.preflight.message ? `<p class="muted">${e(state.preflight.message)}</p>` : ''}${state.preflight.identity ? `<details class="technical-details"><summary>Identité présentée par l’appareil</summary><dl><div><dt>Identifiant machine</dt><dd class="mono">${e(state.preflight.identity.machine_id)}</dd></div><div><dt>Empreinte SSH</dt><dd class="mono">${e(state.preflight.identity.host_key_fingerprint)}</dd></div></dl></details>` : ''}</div>` : ''}<div class="configuration-files"><h3>Configuration prévue</h3><div>${icon('file')}MAVLink</div><div>${icon('file')}WireGuard</div><div>${icon('file')}MQTT</div></div>${button('Lancer la configuration', 'configure-drone', { icon: 'arrow', primary: true, class: 'full', disabled: !preflightAllowed(state.preflight, drone) || busy() || !state.connected })}<p class="card-caption">${preflightAllowed(state.preflight, drone) ? 'Vous confirmerez l’identité de l’appareil avant lancement.' : 'Vérifiez d’abord le bon appareil.'}</p>` : empty('Sélectionner un Aqua_Drone', 'Choisissez un appareil pour consulter sa correspondance avec le bundle.', 'drone')}</section></div><div class="page-note network-note">${icon('lock')}<span>AquaHub utilise les adresses et les configurations existantes. Les contrôles manuels restent à la charge de l’opérateur. ${manualCheckInfo(drone?.manual_checklist ?? [], 'drone')}</span></div>`;
}

function droneItem(item: Drone, profile: boolean): string { return `<button type="button" class="drone-item ${item.number === state.selectedDrone ? 'selected' : ''}" data-drone="${item.number}" aria-pressed="${item.number === state.selectedDrone}">${icon('drone')}<div><strong>${e(item.name)}</strong><span class="mono">${e(item.address)}</span></div><div class="drone-presence"><strong>${item.reachable === true ? 'Joignable' : item.reachable === false ? profile ? 'Non détecté' : 'Hors ligne' : profile ? 'À rechercher' : 'Non vérifié'}</strong><span>${item.configured ? 'Configuration vérifiée' : item.identity_verified ? 'Identité vérifiée' : profile ? 'Profil non associé' : 'Identité à vérifier'}</span></div>${icon('chevron')}</button>`; }

function historyPage(): string {
  return `<section class="card history-card"><div class="card-heading"><h2>Événements de l’installation</h2>${button('Actualiser', 'load-history', { icon: 'refresh', class: 'small', disabled: state.historyLoading || state.historyLoadingMore || !state.connected })}</div><div class="history-toolbar"><label for="history-filter">Composant<select id="history-filter"><option value="">Tous les composants</option>${(state.status?.services ?? []).map(service => `<option value="${e(service.id)}"${state.historyService === service.id ? ' selected' : ''}>${e(service.name)}</option>`).join('')}</select></label><label for="history-search">Rechercher<div class="search-field">${icon('search')}<input id="history-search" type="search" placeholder="Filtrer les événements chargés" value="${e(state.historySearch)}"></div></label></div>${state.historyError ? errorBox(state.historyError, state.historyErrorMore ? 'load-history-more' : 'load-history') : ''}<div id="history-events" aria-live="polite"></div>${state.historyHasMore ? `<div class="history-pagination">${button(state.historyLoadingMore ? 'Chargement des événements…' : 'Charger les événements précédents', 'load-history-more', { icon: 'history', disabled: state.historyLoading || state.historyLoadingMore || !state.agentConnected })}</div>` : ''}<div class="card-footnote">${icon('info')}<span>${state.events.length} événement(s) chargé(s).${state.historyRetention ? ` Conservation : ${state.historyRetention.days} jours, au plus ${new Intl.NumberFormat('fr-FR').format(state.historyRetention.max_events)} événements.` : ''} Les logs techniques détaillés se consultent dans Supervision.</span></div></section><div class="notice history-explanation">${icon('history')}<div><strong>L’historique commence avec AquaHub</strong><p>Les arrêts, reprises et changements observés sont conservés par l’agent. Les périodes sans observation ne prouvent pas l’absence d’incident.</p></div></div>`;
}

function renderHistoryEvents() {
  const target = document.querySelector('#history-events');
  if (!target) return;
  const query = state.historySearch.trim().toLocaleLowerCase('fr');
  const events = state.events.filter(event => !query || `${event.message} ${event.kind} ${serviceName(event.service)}`.toLocaleLowerCase('fr').includes(query));
  target.innerHTML = state.historyLoading ? `<div class="loading-inline" role="status"><span class="spinner"></span>Lecture de l’historique…</div>` : !events.length && query ? empty('Aucun résultat', 'Essayez une autre recherche dans les événements chargés.', 'search') : timeline(events);
}

function settingsPage(): string {
  return `<div class="settings-layout"><section class="card source-settings-card"><div class="card-heading"><h2>Source du bundle Aqua Hub</h2>${icon('network', 'subtle')}</div><p class="muted">Cette source concerne les services Aqua Hub. Les mises à jour de l’application AquaHub utilisent séparément les Releases GitHub officielles.</p>${state.settingsError ? errorBox(state.settingsError, 'load-settings') : ''}<form id="settings-form"><label class="field-label" for="github-url">URL du dépôt fournisseur</label><input id="github-url" type="url" name="github_url" placeholder="https://gitlab.com/organisation/projet" value="${e(state.githubDraft)}" autocomplete="off" spellcheck="false"${disabled(!state.settingsLoaded)}><p class="field-help">Laissez ce champ vide pour utiliser uniquement les archives locales. Aucun jeton d’accès ne doit être saisi dans cette URL.</p><button type="submit" class="button primary"${disabled(busy() || !state.agentConnected || !state.settingsLoaded)}>${icon('check')}<span>Enregistrer la source</span></button></form><div class="button-row source-access-actions">${button('Configurer les accès', 'source-credentials', { icon: 'lock', disabled: busy() || !state.agentConnected || !state.settingsLoaded || !state.settings.github_url })}</div><p class="field-help">Pour les Releases d’un GitLab privé, un jeton autorisé à lire l’API du projet est requis. Un jeton de déploiement Git / registre Docker ne suffit pas. Le secret se renseigne dans une fenêtre sécurisée du poste.</p><div class="inline-note">${icon('info')}<p>Enregistrer une URL ou rechercher une version ne déclenche pas de mise à jour.</p></div></section>
  <section class="card appearance-card"><div class="card-heading"><h2>Apparence</h2>${icon('sun', 'subtle')}</div><p class="muted">Un environnement sobre, adapté à votre usage.</p><div class="theme-options"><button type="button" data-action="theme-light" class="theme-option ${state.theme === 'light' ? 'selected' : ''}" aria-pressed="${state.theme === 'light'}"><span class="theme-preview light-preview"><span></span><span></span></span><span>${icon('sun')}Clair${state.theme === 'light' ? icon('check') : ''}</span></button><button type="button" data-action="theme-dark" class="theme-option ${state.theme === 'dark' ? 'selected' : ''}" aria-pressed="${state.theme === 'dark'}"><span class="theme-preview dark-preview"><span></span><span></span></span><span>${icon('moon')}Sombre${state.theme === 'dark' ? icon('check') : ''}</span></button></div></section>
  <section class="card about-card"><div class="card-heading"><h2>Votre installation</h2>${badge(state.agentConnected ? 'Agent connecté' : 'Agent indisponible', 'outlined')}</div><dl class="about-list"><div><dt>Application</dt><dd>AquaHub ${e(state.status?.version ?? (state.version || '—'))}</dd></div><div><dt>Poste</dt><dd>${e(state.status?.machine ?? 'Non disponible')}</dd></div><div><dt>Bundle existant</dt><dd class="mono">${e(state.status?.bundle_path ?? 'Non détecté')}</dd></div><div><dt>Moteur Docker</dt><dd>${state.status?.docker_available ? 'Accessible' : 'Non accessible'}</dd></div><div><dt>Portainer</dt><dd>${state.status?.portainer_available ? 'Accessible' : 'Non détecté'}</dd></div></dl><p class="field-help">Le bundle conserve son fonctionnement et ses adresses réseau.</p></section>
  <section class="card diagnostics-card"><div class="card-heading"><h2>Diagnostic et assistance</h2>${icon('history', 'subtle')}</div><p class="muted">Rassemblez les informations techniques pour analyser un problème. L’export est préparé par l’agent, sans exposer vos clés dans l’interface.</p><div class="button-row">${button('Exporter un diagnostic', 'diagnostics', { icon: 'download', disabled: busy() || !state.agentConnected })}${button('Enregistrer tous les logs', 'export-all-logs', { icon: 'download', disabled: busy() || !state.connected })}</div><p class="field-help">Les logs regroupent les extraits récents des huit services : jusqu’à 1 000 lignes et environ 48 Ko filtrés par service. Les périodes antérieures à leur rotation peuvent manquer.</p>${state.diagnosticPath ? `<div class="diagnostic-result" role="status"><strong>Diagnostic créé</strong><p class="mono">${e(state.diagnosticPath)}</p>${button('Afficher dans le dossier', 'show-diagnostic', { class: 'small', icon: 'external' })}</div>` : ''}</section><section class="card portainer-settings"><div class="card-heading"><h2>Portainer</h2>${badge(state.status?.portainer_available ? 'Détecté' : 'Non détecté', 'outlined')}</div><p class="muted">Portainer est la console avancée de Docker : conteneurs, images, volumes et journaux. Son compte administrateur protège cette console ; il n’est pas nécessaire pour les actions courantes d’AquaHub ni pour lire l’historique. Créez-le si vous souhaitez utiliser Portainer.</p><p class="field-help">Pour les logs Docker : environnement local → Containers → service → Logs. Activity logs concerne les actions d’administration, pas les sorties des services.</p><div class="button-row">${button('Créer le compte initial', 'setup-portainer', { icon: 'lock', disabled: busy() || !state.agentConnected })}${button('Ouvrir Portainer', 'open-portainer', { icon: 'external', disabled: !state.agentConnected || !state.status?.portainer_available })}${button('Relancer Portainer', 'restart-portainer', { icon: 'refresh', disabled: busy() || !state.agentConnected || state.status?.portainer?.restart_supported === false })}</div><p class="field-help">${e(state.status?.portainer?.restart_reason ?? 'La relance conserve les comptes et les données. Une instance existante non reconnue ne sera pas modifiée.')}</p></section>${sshSettingsCard()}</div>`;
}

function sshSettingsCard(): string {
  return `<section class="card ssh-settings-card"><div class="card-heading"><h2>Connexion aux Aqua_Drones</h2>${icon('lock', 'subtle')}</div><p class="muted">AquaHub utilise SSH pour les fonctions de configuration prévues par le bundle. Chaque appareil doit être identifié avant sa première connexion.</p>${state.ssh ? `<form id="ssh-form"><label class="field-label" for="ssh-identity-file">Fichier de clé privée SSH (facultatif)</label><input type="text" id="ssh-identity-file" value="${e(state.ssh.identity_file)}" placeholder="Chemin absolu d’une clé existante" spellcheck="false" autocomplete="off"><p class="field-help">Le contenu de la clé reste sur le poste et n’est pas affiché dans AquaHub.</p><label class="field-label ssh-field" for="ssh-known-hosts-file">Fichier des hôtes connus (facultatif)</label><input type="text" id="ssh-known-hosts-file" value="${e(state.ssh.known_hosts_file)}" placeholder="Utiliser le fichier privé AquaHub" spellcheck="false" autocomplete="off"><label class="confirmation-checkbox"><input type="checkbox" id="ssh-none-auth"${state.ssh.allow_none_auth ? ' checked' : ''}><span>Ce firmware autorise SSH sans secret. Utiliser ce profil uniquement pour les appareils qui le prévoient.</span></label><button type="submit" class="button primary"${disabled(busy() || !state.agentConnected)}>${icon('check')}<span>Enregistrer la connexion</span></button></form>` : button('Configurer la connexion SSH', 'load-ssh', { icon: 'settings', disabled: busy() || !state.agentConnected })}<p class="field-help">Les adresses des appareils restent inchangées. L’enregistrement de leur empreinte se fait dans Aqua_Drones.</p></section>`;
}

function setPage(page: Page, focus = true) {
  if (!Object.hasOwn(pageNames, page)) page = 'overview';
  state.page = page; storageSet('aquahub-page', page);
  if (location.hash !== `#${page}`) location.hash = page;
  renderChrome(); renderPage();
  if (focus) document.querySelector<HTMLElement>('#page-content')!.focus({ preventScroll: true });
  if (page === 'drones' && !state.dronesLoaded) void loadDrones();
  if (page === 'history' && !state.historyLoaded) void loadHistory();
  if (page === 'updates' && !state.appUpdateLoaded) void loadAppUpdate();
  if (page === 'settings' && !state.settingsLoaded) void loadSettings();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  root.querySelector('[data-action="refresh"]')?.classList.add('refreshing');
  try {
    const status = validateStatus(await request<HubStatus>('status.get'));
    const collected = status.collected_at ? new Date(status.collected_at) : new Date();
    state.status = status; state.agentConnected = true; state.connected = !status.stale; state.error = status.stale ? status.error?.message ?? 'Les données des services n’ont pas pu être actualisées.' : ''; state.lastUpdate = Number.isNaN(collected.getTime()) ? new Date() : collected;
    if (!state.selectedService || !status.services.some(service => service.id === state.selectedService)) state.selectedService = status.services.find(service => service.id.toLowerCase().includes('gcs'))?.id ?? status.services[0]?.id ?? '';
    if (status.active_job && isActive(status.active_job)) adoptJob(status.active_job);
  } catch (error) { state.error = errorMessage(error); state.connected = false; state.agentConnected = false; }
  finally {
    refreshing = false; state.loading = false;
    root.querySelector('[data-action="refresh"]')?.classList.remove('refreshing');
    renderChrome();
    if (state.page === 'overview' || state.page === 'drones') renderPage();
    if (state.page === 'overview' && state.connected && (state.detailTab === 'events' || state.logsFollow)) void loadDetail();
  }
}

async function loadDetail() {
  const service = selectedService();
  if (!service || !state.connected) return;
  const sequence = ++detailSequence; const tab = state.detailTab;
  state.detailError = '';
  if (tab === 'logs') { state.logsLoading = true; renderDetail(); }
  try {
    if (tab === 'logs') {
      const result = await request<{ text: string }>('service.logs', { service: service.id, tail: 200 });
      if (sequence !== detailSequence) return;
      state.logs = plainLog(result.text ?? '');
    } else {
      const result = await request<{ events: HubEvent[] }>('events.list', { service: service.id, limit: 6 });
      if (sequence !== detailSequence) return;
      state.detailEvents = result.events ?? [];
    }
  } catch (error) { if (sequence === detailSequence) state.detailError = errorMessage(error); }
  finally { if (sequence === detailSequence) { state.logsLoading = false; renderDetail(); } }
}
function renderDetail() { if (state.page === 'overview') { const detail = root.querySelector('.detail-card'); if (detail) replaceKeepingFocus(detail, serviceDetail()); } }

async function loadDrones(scan = false) {
  if (dronesRefreshing) return;
  dronesRefreshing = true;
  if (state.page === 'drones') renderPage();
  try {
    const result = await request<{ drones: Drone[]; warnings?: string[] }>(scan ? 'drones.scan' : 'drones.list');
    state.drones = result.drones ?? []; state.droneWarnings = result.warnings ?? []; state.dronesError = ''; state.dronesLoaded = true;
    if (!state.drones.some(drone => drone.number === state.selectedDrone)) { state.selectedDrone = identifiedDrones(state.drones)[0]?.number ?? state.drones[0]?.number ?? 0; state.preflight = null; }
    if (scan) { state.preflight = null; notify(`Recherche terminée : ${state.drones.filter(drone => drone.reachable === true).length} appareil(s) joignable(s).`); }
  } catch (error) { state.dronesError = errorMessage(error); }
  finally { dronesRefreshing = false; if (state.page === 'drones' || state.page === 'overview') renderPage(); }
}

async function loadHistory(append = false) {
  if (append && (state.historyLoading || state.historyLoadingMore || !state.historyHasMore || state.historyBefore === null)) return;
  const before = state.historyBefore;
  const sequence = ++historySequence; state.historyLoading = !append; state.historyLoadingMore = append; state.historyError = ''; state.historyErrorMore = append;
  if (!append) { state.historyBefore = null; state.historyHasMore = false; state.historyLoaded = false; }
  if (state.page === 'history') renderPage();
  try {
    const result = await request<EventsPage>('events.list', { limit: 200, ...(append ? { before_id: before } : {}), ...(state.historyService ? { service: state.historyService } : {}) });
    if (sequence === historySequence) {
      const existing = new Set(append ? state.events.map(event => String(event.id)) : []);
      state.events = [...(append ? state.events : []), ...(result.events ?? []).filter(event => !existing.has(String(event.id)))];
      state.historyBefore = result.next_before_id ?? null; state.historyHasMore = result.has_more === true && state.historyBefore !== null; state.historyRetention = result.retention ?? null; state.historyLoaded = true;
    }
  } catch (error) { if (sequence === historySequence) state.historyError = errorMessage(error); }
  finally { if (sequence === historySequence) { state.historyLoading = false; state.historyLoadingMore = false; if (state.page === 'history') renderPage(); } }
}

async function loadAppUpdate(check = false) {
  if (appUpdateChecking && !check) return;
  const sequence = ++appUpdateSequence;
  if (check) appUpdateChecking = true;
  try {
    const status = await request<AppUpdateStatus>(check ? 'app_updates.check' : 'app_updates.status');
    if (sequence !== appUpdateSequence) return;
    state.appUpdate = status; state.appUpdateLoaded = true; state.appUpdateError = ''; if (check) state.appUpdateCheckError = '';
    const installation = status.installation;
    if (installation && ['scheduled', 'installing'].includes(installation.status)) { state.appInstallPending = true; state.appInstallVersion = installation.version; }
    else if (installation && ['installed', 'failed'].includes(installation.status)) { state.appInstallPending = false; state.appInstallVersion = ''; }
  } catch (error) {
    if (sequence !== appUpdateSequence) return;
    if (check) state.appUpdateCheckError = errorMessage(error);
    else state.appUpdateError = state.appInstallPending ? 'L’agent redémarre ou n’est pas encore disponible. La vérification reprendra automatiquement ; ne relancez pas l’installation.' : errorMessage(error);
  } finally { if (check) appUpdateChecking = false; }
  renderChrome();
  if (state.page === 'updates') renderPage();
}

async function loadSettings() {
  try {
    const settings = await request<Settings>('settings.get');
    state.settings = settings; state.settingsLoaded = true; state.settingsError = ''; state.githubDraft = settings.github_url ?? '';
  } catch (error) { state.settingsError = errorMessage(error); }
  if (state.page === 'settings' || state.page === 'updates') renderPage();
}

async function runAction(label: string, operation: () => Promise<void>) {
  if (busy()) { notify('Une tâche de maintenance est déjà en cours. Attendez son résultat.'); return; }
  await actionGate.run(async () => {
    state.busyLabel = label; renderChrome(); renderPage();
    try { await operation(); }
    catch (error) { notify(errorMessage(error), true); await refresh(); }
    finally { state.busyLabel = ''; }
  });
  renderChrome(); renderPage();
}

async function saveSettings() {
  const url = normalizeRepository(state.githubDraft);
  await runAction('Enregistrement de la source', async () => {
    const result = await request<Settings>('settings.save', { github_url: url });
    state.settings = { ...state.settings, ...result, github_url: url }; state.githubDraft = url; state.releases = []; state.updateResult = ''; notify('Source des mises à jour enregistrée.');
  });
}

async function saveSshSettings() {
  if (!state.ssh) return;
  const settings = { ...state.ssh, identity_file: state.ssh.identity_file.trim(), known_hosts_file: state.ssh.known_hosts_file.trim() };
  for (const path of [settings.identity_file, settings.known_hosts_file]) if (path && !path.startsWith('/')) throw new Error('Les fichiers SSH doivent être désignés par un chemin absolu sur ce poste.');
  await runAction('Enregistrement de la connexion SSH', async () => { state.ssh = await request<SshSettings>('drones.ssh_settings', settings); state.preflight = null; notify('Paramètres de connexion SSH enregistrés.'); });
}

async function handleAction(action: string, target?: HTMLElement) {
  switch (action) {
    case 'manual-checks': {
      if (dialog.open) return;
      const items = target?.dataset.checklist === 'completion' && state.job ? manualChecklist(state.job) : selectedDrone()?.manual_checklist ?? [];
      if (!items.length) return;
      await confirmAction({ title: 'Contrôles manuels', body: `<p>Ces vérifications restent à effectuer sur le matériel. AquaHub ne les valide pas automatiquement.</p><ol class="manual-check-list">${items.map(item => `<li><strong>${e(item.label)}</strong><p>${e(item.details)}</p></li>`).join('')}</ol>`, confirm: 'Compris' }); return;
    }
    case 'load-app-update': await loadAppUpdate(); return;
    case 'check-app-update': await runAction('Recherche des mises à jour AquaHub', () => loadAppUpdate(true)); return;
    case 'download-app-update': {
      const release = state.appUpdate?.release; if (!release || !state.appUpdate?.available) return;
      await runAction('Préparation du téléchargement AquaHub', async () => receiveJob(await request<Job | { job: Job }>('app_updates.download', { release_id: release.release_id, asset_id: release.asset_id }))); return;
    }
    case 'install-app-update': {
      const release = state.appUpdate?.release; const downloaded = state.appUpdate?.download;
      if (!release || !downloaded?.ready || downloaded.version !== release.version) return;
      const yes = await confirmAction({ title: `Mettre AquaHub à jour vers ${release.version} ?`, body: `<p>L’application AquaHub et son agent seront mis à jour. L’interface pourra se déconnecter temporairement ; rouvrez AquaHub à la fin.</p><p>Cette opération conserve le bundle, les services fournisseur et les configurations réseau existantes.</p><div class="confirmation-target">${icon('apps')}<strong>AquaHub ${e(release.version)}</strong></div>`, confirm: 'Installer AquaHub' });
      if (yes) await runAction('Installation de l’application AquaHub', async () => { const result = await request<{ scheduled: boolean; version: string }>('app_updates.install', { version: release.version, confirmed: true }); if (!result.scheduled) throw new Error('La programmation de la mise à jour n’a pas été confirmée. Vérifiez son état avant de réessayer.'); state.appInstallPending = true; state.appInstallVersion = result.version; await loadAppUpdate(); }); return;
    }
    case 'theme': setTheme(state.theme === 'dark' ? 'light' : 'dark'); return;
    case 'dismiss-job-result': state.jobResultDismissed = state.job?.id ?? ''; renderChrome(); return;
    case 'theme-light': setTheme('light'); return;
    case 'theme-dark': setTheme('dark'); return;
    case 'refresh': await refresh(); if (state.page === 'drones') await loadDrones(); else if (state.page === 'history') await loadHistory(); else if (state.page === 'settings') await loadSettings(); else if (state.page === 'updates') await loadAppUpdate(); return;
    case 'go-settings': setPage('settings'); return;
    case 'go-updates': setPage('updates'); return;
    case 'load-settings': await loadSettings(); return;
    case 'source-credentials': {
      if (busy() || !state.agentConnected || !state.settings.github_url) return;
      await runAction('Accès au dépôt Git', async () => {
        const result = await request<{ saved?: boolean; configured?: boolean; canceled?: boolean }>('native.source_credentials');
        if (result.canceled) return;
        if (!result.saved) throw new Error('Les accès au dépôt n’ont pas été enregistrés.');
        notify(result.configured ? 'Accès au dépôt privé enregistrés.' : 'Accès privé effacé. Le dépôt sera consulté sans jeton.');
      }); return;
    }
    case 'load-ssh': await runAction('Lecture des paramètres de connexion', async () => { state.ssh = await request<SshSettings>('drones.ssh_settings'); }); return;
    case 'open-gcs': case 'open-fleet': case 'open-portainer': {
      const target = action.replace('open-', '');
      if (target === 'portainer' && !state.status?.portainer_available) { notify('Portainer n’est pas disponible sur ce poste. Consultez les réglages pour exporter un diagnostic.', true); return; }
      const result = await request<Job | { job?: Job; canceled?: boolean; opened?: boolean }>('native.open', { target });
      if (result && ('id' in result || ('job' in result && result.job))) receiveJob(result as Job | { job: Job });
      if (demo) notify('Démonstration : aucun programme externe n’est ouvert.'); return;
    }
    case 'restart-portainer': {
      if (busy() || !state.agentConnected || state.status?.portainer?.restart_supported === false) return;
      const yes = await confirmAction({ title: 'Relancer Portainer ?', body: '<p>L’interface d’administration Portainer sera brièvement interrompue. Ses comptes et ses données seront conservés.</p><p>Après la relance d’une instance neuve, ouvrez Portainer pour terminer la création du compte administrateur.</p>', confirm: 'Relancer Portainer' });
      if (yes) await runAction('Relance de Portainer', async () => receiveJob(await request<Job | { job: Job }>('portainer.restart', { confirmed: true })));
      return;
    }
    case 'setup-portainer': {
      if (busy() || !state.agentConnected) return;
      await runAction('Initialisation Portainer', async () => {
        const result = await request<Job | { job: Job } | { canceled: boolean }>('native.portainer_setup');
        if ('canceled' in result && result.canceled) return;
        receiveJob(result as Job | { job: Job });
      }); return;
    }
    case 'detail-events': state.detailTab = 'events'; renderDetail(); await loadDetail(); return;
    case 'detail-logs': state.detailTab = 'logs'; renderDetail(); await loadDetail(); return;
    case 'detail-refresh': await loadDetail(); return;
    case 'service-history': state.historyService = state.selectedService; state.historyLoaded = false; setPage('history'); return;
    case 'load-history': await loadHistory(); return;
    case 'load-history-more': await loadHistory(true); return;
    case 'event-snapshot': {
      if (snapshotLoading || dialog.open) return;
      const snapshot = [...state.detailEvents, ...state.events].find(event => String(event.log_snapshot?.id) === target?.dataset.snapshotId)?.log_snapshot;
      if (!snapshot || !['available', 'empty'].includes(snapshot.status)) return;
      snapshotLoading = true; target?.setAttribute('disabled', ''); target?.setAttribute('aria-busy', 'true');
      try {
        const result = await request<{ text: string; captured_at?: string; truncated?: boolean; status?: string }>('events.logs', { snapshot_id: snapshot.id });
        if (dialog.open) return;
        const previousFocus = document.activeElement as HTMLElement | null;
        dialog.innerHTML = `<form method="dialog"><div class="dialog-heading">${icon('history')}<button type="submit" class="icon-button" aria-label="Fermer">${icon('close')}</button></div><h2 id="confirmation-title">Extrait de panne conservé</h2><div class="dialog-body"><p>${e(serviceName(snapshot.service))}${result.captured_at || snapshot.captured_at ? ` · ${e(date(result.captured_at ?? snapshot.captured_at, true))}` : ''}</p><p>Capture au moment de l’événement. Les journaux actuels se consultent dans Supervision.</p><pre class="logs snapshot-logs" tabindex="0">${e(plainLog(result.text || 'Aucune ligne de journal n’était disponible au moment de la capture.'))}</pre>${result.truncated || snapshot.truncated ? '<p class="snapshot-limit">Extrait borné : seule une partie des journaux est conservée.</p>' : ''}</div><div class="dialog-actions"><button type="submit" class="button primary" autofocus>Fermer</button></div></form>`;
        dialog.addEventListener('close', () => previousFocus?.focus({ preventScroll: true }), { once: true }); prepareDialog(); dialog.showModal();
      } finally { snapshotLoading = false; target?.removeAttribute('disabled'); target?.removeAttribute('aria-busy'); }
      return;
    }
    case 'load-drones': await loadDrones(); return;
    case 'scan-drones': await runAction('Recherche des Aqua_Drones', () => loadDrones(true)); return;
    case 'restart': {
      const service = selectedService();
      if (!restartAllowed(servicesFresh() ? state.status : null, service, busy()) || !service) return;
      const yes = await confirmAction({ title: `Redémarrer ${service.name} ?`, body: `<p>Le service sera brièvement interrompu. Les autres composants restent gérés par le bundle.</p><div class="confirmation-target">${icon(serviceIcon(service.id))}<strong>${e(service.name)}</strong>${badge(serviceState(service.state))}</div><p class="muted">Les données et la version de l’image sont conservées.</p>`, confirm: 'Redémarrer le service' });
      if (yes) {
        state.requestingService = service.id;
        const requestedAt = Date.now();
        delete state.unconfirmedRestarts[service.id];
        try {
          await runAction(`Redémarrage de ${service.name}`, async () => {
            try {
              receiveJob(await request<Job | { job: Job }>('service.restart', { service: service.id }));
              state.requestingService = '';
            } catch (error) {
              const code = error && typeof error === 'object' && 'code' in error ? error.code : undefined;
              if (!code || ['agent_timeout', 'agent_protocol', 'agent_unavailable', 'desktop_error'].includes(String(code))) state.unconfirmedRestarts[service.id] = { requestedAt, failedAt: Date.now() };
              throw error;
            }
          });
        } finally { state.requestingService = ''; renderChrome(); renderPage(); }
      }
      return;
    }
    case 'preflight': {
      const drone = selectedDrone(); if (!drone) return;
      state.preflight = null;
      await runAction(`Vérification de ${drone.name}`, async () => { const result = await request<Preflight>('drones.preflight', { number: drone.number }); await loadDrones(); if (state.selectedDrone === drone.number) state.preflight = result; }); return;
    }
    case 'enroll-drone': {
      const drone = selectedDrone(); if (!drone) return;
      await runAction(`Lecture de l’identité SSH de ${drone.name}`, async () => {
        const observed = await request<{ number: number; address: string; host_key_fingerprint: string; trusted?: boolean; message?: string }>('drones.identity', { number: drone.number });
        if (observed.number !== drone.number || observed.address !== drone.address || !observed.host_key_fingerprint) throw new Error('L’identité reçue ne correspond pas à l’appareil sélectionné.');
        const yes = await confirmAction({ title: `Identifier ${drone.name}`, body: `<p>Une machine répond à <strong class="mono">${e(drone.address)}</strong>. Vérifiez l’empreinte de sa clé SSH auprès de votre équipement ou de votre fournisseur avant de l’enregistrer.</p><dl class="identity-confirmation"><div><dt>Empreinte observée</dt><dd class="mono">${e(observed.host_key_fingerprint)}</dd></div></dl>${observed.message ? `<p>${e(observed.message)}</p>` : ''}`, confirm: 'Enregistrer cette identité', checkbox: 'J’ai vérifié cette empreinte et la correspondance avec le bon appareil physique.' });
        if (!yes) return;
        await request('drones.enroll', { number: drone.number, host_key_fingerprint: observed.host_key_fingerprint, confirmed: true }); state.preflight = null;
        notify('Identité SSH enregistrée. Vous pouvez maintenant vérifier cet appareil.'); await loadDrones();
      }); return;
    }
    case 'configure-drone': {
      const drone = selectedDrone(); const preflight = state.preflight;
      if (!drone || !preflightAllowed(preflight, drone) || !preflight) return;
      if (!preflight.identity?.machine_id || !preflight.identity.host_key_fingerprint) { notify('L’identité de l’appareil est incomplète. Relancez la vérification avant de configurer.', true); return; }
      const identity = preflight.identity;
      const yes = await confirmAction({ title: `Configurer ${drone.name} ?`, body: `<p>La configuration existante du numéro <strong>${drone.number}</strong> sera envoyée à <strong class="mono">${e(drone.address)}</strong>. L’appareil pourra redémarrer.</p><p>Vérifiez la correspondance avec l’appareil physique avant de continuer.</p><dl class="identity-confirmation"><div><dt>Identifiant machine</dt><dd class="mono">${e(identity.machine_id)}</dd></div><div><dt>Empreinte de la clé SSH</dt><dd class="mono">${e(identity.host_key_fingerprint)}</dd></div></dl><p class="muted">Les contrôles manuels ne sont pas validés automatiquement.</p>`, confirm: 'Envoyer la configuration', checkbox: 'J’ai vérifié la correspondance physique de cet appareil et de ces identifiants.' });
      if (yes) await runAction(`Configuration de ${drone.name}`, async () => { receiveJob(await request<Job | { job: Job }>('drones.configure', { number: drone.number, preflight_id: preflight.preflight_id ?? preflight.id, confirmed_identity: identity })); state.preflight = null; });
      return;
    }
    case 'import': {
      if (busy() || !state.connected || filePickerOpen) return;
      filePickerOpen = true;
      let chosen: { path?: string; canceled?: boolean };
      try { chosen = await request<{ path?: string; canceled?: boolean }>('native.choose_file'); } finally { filePickerOpen = false; }
      if (chosen.canceled || !chosen.path) return;
      setPage('updates'); state.inspection = null;
      await runAction('Vérification de l’archive', async () => { const inspection = await request<Inspection>('updates.inspect', { path: chosen.path }); state.inspection = { ...inspection, path: chosen.path }; }); return;
    }
    case 'apply-update': {
      const inspection = state.inspection;
      if (!inspectionAllowed(inspection) || !inspection) return;
      const yes = await confirmAction({ title: 'Installer cette livraison ?', body: `<p>L’installation peut interrompre temporairement les services du hub.</p><div class="confirmation-target">${icon('file')}<strong>${e(inspection.version ?? 'Archive vérifiée')}</strong></div><p>Les précontrôles et la sauvegarde sont effectués par l’agent avant l’application.</p>`, confirm: 'Installer la livraison' });
      if (yes) await runAction('Préparation de la mise à jour', async () => { receiveJob(await request<Job | { job: Job }>('updates.apply', { inspection_id: inspection.inspection_id ?? inspection.id })); state.inspection = null; }); return;
    }
    case 'check-updates': await runAction('Recherche des versions publiées', async () => { state.releases = []; state.updateResult = ''; try { const result = await request<{ message?: string; releases?: Release[]; available?: boolean; version?: string }>('updates.check'); state.releases = result.releases ?? []; state.updateResult = result.message ?? (result.available ? `Version ${result.version ?? ''} disponible. Importez sa livraison complète pour l’analyser.` : 'Aucune livraison compatible proposée par cette source.'); } catch (error) { state.updateResult = errorMessage(error); throw error; } }); return;
    case 'download-release': {
      const releaseId = Number(target?.dataset.releaseId); const assetId = Number(target?.dataset.assetId);
      const asset = state.releases.find(release => release.release_id === releaseId)?.assets.find(item => item.asset_id === assetId);
      if (!asset?.downloadable || !Number.isSafeInteger(releaseId) || !Number.isSafeInteger(assetId)) return;
      await runAction('Préparation du téléchargement', async () => receiveJob(await request<Job | { job: Job }>('updates.download', { release_id: releaseId, asset_id: assetId }))); return;
    }
    case 'export-service-logs': case 'export-all-logs': {
      if (busy() || !state.connected) return;
      const service = action === 'export-service-logs' ? selectedService() : undefined;
      if (action === 'export-service-logs' && !service) return;
      await runAction('Enregistrement des logs', async () => {
        const result = await request<{ saved: boolean; canceled?: boolean; partial?: boolean; filename?: string }>('native.export_logs', service ? { service: service.id } : {});
        if (result.canceled) return;
        if (!result.saved) throw new Error('Les logs n’ont pas été enregistrés. Réessayez.');
        notify(`Logs enregistrés${result.filename ? ` dans ${result.filename}` : ''}.${result.partial ? ' Export partiel : consultez le résumé des services indisponibles dans l’archive.' : ''}`);
      }); return;
    }
    case 'diagnostics': await runAction('Préparation du diagnostic', async () => { const result = await request<{ path: string }>('diagnostics.export'); state.diagnosticPath = result.path; notify('Diagnostic exporté. Son emplacement est affiché dans les réglages.'); }); return;
    case 'show-diagnostic': if (state.diagnosticPath) await request('native.show_folder', { path: state.diagnosticPath }); return;
  }
}

function receiveJob(result: Job | { job: Job }) { const job = 'job' in result ? result.job : result; if (!job?.id) throw new Error('La tâche a été demandée mais son identifiant n’a pas été reçu. Vérifiez son état avant de réessayer.'); state.job = job; state.jobError = ''; adoptJob(job); }
function adoptJob(job: Job) {
  if (isActive(state.job) && state.job?.id !== job.id) return;
  state.job = job;
  if (isActive(job) && !jobTimer && !jobPolling) jobTimer = setTimeout(() => void pollJob(), 800);
}
async function pollJob() {
  jobTimer = undefined;
  if (!state.job || !isActive(state.job) || stopped) return;
  const id = state.job.id; jobPolling = true;
  try {
    const result = await request<Job | { job: Job }>('jobs.get', { id });
    const job = 'job' in result ? result.job : result;
    if (state.job?.id !== id) return;
    state.job = job; state.jobError = '';
    if (!isActive(job)) {
      const detail = typeof job.error === 'string' ? job.error : job.error?.message;
      const message = detail ?? job.message ?? jobResultMessage(job);
      notify(`${jobLabel(job)} · ${jobState(job)}${message ? ` : ${message}` : ''}`, !jobSucceeded(job));
      if (jobSucceeded(job) && job.result && typeof job.result === 'object' && 'inspection' in job.result) state.inspection = job.result.inspection as Inspection;
      if (state.status?.active_job?.id === id) state.status.active_job = null;
      await refresh(); await loadDrones(); if ((job.kind?.startsWith('app_updates.') || job.kind?.startsWith('app.'))) await loadAppUpdate(); if (state.page === 'history' && !state.historyLoaded) await loadHistory();
    }
  } catch (error) { state.jobError = errorMessage(error); }
  finally {
    jobPolling = false; renderChrome();
    if (state.page !== 'settings' || !isActive(state.job)) renderPage();
    if (isActive(state.job) && !stopped) jobTimer = setTimeout(() => void pollJob(), state.jobError ? 5000 : 1000);
  }
}

function setTheme(theme: Theme) {
  state.theme = theme; storageSet('aquahub-theme', theme); document.documentElement.dataset.theme = theme;
  document.querySelector('#theme-label')!.innerHTML = `${icon(theme === 'dark' ? 'moon' : 'sun')}<span>Mode ${theme === 'dark' ? 'sombre' : 'clair'}</span>`;
  document.querySelector('#theme-toggle')!.setAttribute('aria-checked', String(theme === 'dark'));
  if (state.page === 'settings') renderPage();
}

function notify(message: string, error = false) {
  const container = document.querySelector('#notifications')!;
  const toast = document.createElement('div'); toast.className = `toast ${error ? 'toast-error' : ''}`;
  toast.setAttribute('role', error ? 'alert' : 'status');
  toast.innerHTML = `${icon(error ? 'warning' : 'check')}<span>${e(message)}</span><button type="button" class="icon-button" aria-label="Fermer la notification">${icon('close')}</button>`;
  toast.querySelector('button')!.addEventListener('click', () => toast.remove()); container.append(toast);
  while (container.children.length > 4) container.firstElementChild?.remove();
  if (!error) setTimeout(() => toast.remove(), 7500);
}

function confirmAction(options: { title: string; body: string; confirm: string; checkbox?: string }): Promise<boolean> {
  if (dialog.open) return Promise.resolve(false);
  const previousFocus = document.activeElement as HTMLElement | null;
  dialog.innerHTML = `<form method="dialog"><div class="dialog-heading">${icon('shield')}<button type="button" class="icon-button" id="dialog-close" aria-label="Fermer">${icon('close')}</button></div><h2 id="confirmation-title">${e(options.title)}</h2><div class="dialog-body">${options.body}</div>${options.checkbox ? `<label class="confirmation-checkbox"><input type="checkbox" id="confirm-identity"><span>${e(options.checkbox)}</span></label>` : ''}<div class="dialog-actions"><button type="submit" value="cancel" class="button" autofocus>Annuler</button><button type="submit" value="confirm" class="button primary" id="dialog-confirm"${disabled(Boolean(options.checkbox))}>${e(options.confirm)}</button></div></form>`;
  return new Promise(resolve => {
    dialog.returnValue = 'cancel';
    const finish = () => { resolve(dialog.returnValue === 'confirm'); previousFocus?.focus({ preventScroll: true }); };
    dialog.addEventListener('close', finish, { once: true });
    dialog.querySelector('#dialog-close')!.addEventListener('click', () => dialog.close('cancel'));
    dialog.querySelector<HTMLInputElement>('#confirm-identity')?.addEventListener('change', event => { (dialog.querySelector('#dialog-confirm') as HTMLButtonElement).disabled = !(event.target as HTMLInputElement).checked; });
    prepareDialog(); dialog.showModal();
  });
}

function prepareDialog() {
  const form = dialog.querySelector('form'); const actions = form?.querySelector('.dialog-actions');
  if (!form || !actions) return;
  const scroll = document.createElement('div'); scroll.className = 'dialog-scroll';
  while (form.firstElementChild && form.firstElementChild !== actions) scroll.append(form.firstElementChild);
  form.prepend(scroll);
}

async function init() {
  layout();
  dialog.addEventListener('keydown', event => {
    if (!dialog.open || event.key !== 'Tab') return;
    const controls = [...dialog.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href],[tabindex]')].filter(element => !element.hasAttribute('disabled') && element.tabIndex >= 0 && element.getBoundingClientRect().width > 0);
    const first = controls[0]; const last = controls[controls.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.querySelector('.skip-link')!.addEventListener('click', event => { event.preventDefault(); document.querySelector<HTMLElement>('#page-content')!.focus({ preventScroll: true }); });
  const results = await Promise.allSettled([refresh(), loadSettings(), loadDrones(), request<{ version?: string }>('app.version'), loadAppUpdate()]);
  const version = results[3]; if (version.status === 'fulfilled') state.version = version.value.version ?? ''; // Each view owns its own failure message.
  renderChrome(); renderPage();
  if (state.page === 'history') void loadHistory();
  const timer = setInterval(() => { if (!document.hidden && !dialog.open) void refresh().then(() => loadDrones()); }, 8000);
  const freshnessTimer = setInterval(() => { const fresh = servicesFresh(); if (fresh !== lastObservationFresh) { lastObservationFresh = fresh; renderChrome(); if (state.page === 'overview') renderPage(); } }, 1000);
  const appTimer = setInterval(() => { if (state.appInstallPending || (!document.hidden && state.page === 'updates')) void loadAppUpdate(); }, 3000);
  window.addEventListener('hashchange', () => { const page = readPage(); if (page !== state.page) setPage(page); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh().then(() => loadDrones()); });
  window.addEventListener('beforeunload', () => { stopped = true; clearInterval(timer); clearInterval(appTimer); clearInterval(freshnessTimer); if (jobTimer) clearTimeout(jobTimer); });
}
void init();
