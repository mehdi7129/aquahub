(async () => {
  const results = [];
  const errors = [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw new Error(`Élément absent : ${selector}`); return element; };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (check, label, timeout = 12000) => { const start = Date.now(); while (!check()) { if (Date.now() - start > timeout) throw new Error(`Délai dépassé : ${label}`); await pause(100); } };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, operation) => { try { await operation(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: error.message }); } };
  try {
    await wait(() => document.querySelectorAll('.service-row').length === 8, 'services réels');
    if (!sessionStorage.getItem('aquahub-live-reload')) {
      if (document.documentElement.dataset.theme !== 'dark') click('#theme-toggle');
      sessionStorage.setItem('aquahub-live-reload', '1');
      location.reload(); return;
    }
    await check('Le thème sombre persiste après rechargement de la fenêtre', async () => assert(document.documentElement.dataset.theme === 'dark', 'Thème sombre perdu au rechargement'));
    sessionStorage.removeItem('aquahub-live-reload');
    await check('Huit services réels sont affichés et le mode démo est absent', async () => {
      const status = await window.aquahub.request('status.get');
      assert(status.services.length === 8, 'API : huit services attendus');
      assert(status.services.filter(service => service.state === 'running').length === 8, 'Huit services en cours attendus');
      assert(status.services.filter(service => service.health === 'healthy').length === 2, 'Deux healthchecks validés attendus');
      assert(status.services.filter(service => service.health === 'not_defined' || service.health == null).length === 6, 'Six services sans healthcheck attendus');
      assert(!document.querySelector('.demo-banner'), 'Bandeau démo présent en mode réel');
      assert(document.querySelectorAll('.service-row').length === 8, 'Rendu : huit services attendus');
      assert(!document.querySelector('#connection-banner .error'), 'Erreur de connexion visible');
    });
    await check('Les relances de composants réseau sont désactivées avec une explication', async () => {
      const status = await window.aquahub.request('status.get');
      const guarded = status.services.find(service => service.actions?.restart === false && service.state !== 'missing');
      assert(Boolean(guarded), 'Aucun composant réseau protégé trouvé');
      find(`[data-service="${guarded.id}"]`).click();
      assert(find('[data-action="restart"]').disabled, 'Composant réseau redémarrable');
      assert(Boolean(document.querySelector('.action-explanation')?.textContent.trim()), 'Motif de protection absent');
    });
    await check('Les logs réels MQTT se chargent sans exécution de contenu', async () => {
      const mqtt = [...document.querySelectorAll('.service-row')].find(row => row.textContent.includes('MQTT'));
      assert(Boolean(mqtt), 'Service MQTT absent'); mqtt.click(); click('[data-action="detail-logs"]');
      await wait(() => document.querySelector('.logs') && !document.querySelector('.logs').textContent.includes('Chargement'), 'logs MQTT');
      assert(!find('.logs').textContent.includes('DÉMONSTRATION'), 'Logs fictifs en mode réel');
      assert(find('.logs').textContent.length > 0, 'Journaux vides sans explication');
      assert(!document.querySelector('.logs script,.logs img,.logs iframe'), 'Contenu technique interprété comme HTML');
    });
    await check('Le parcours mises à jour démarre sans action ni installation', async () => {
      click('[data-page="updates"]'); assert(find('#page-title').textContent === 'Mises à jour', 'Vue mises à jour absente');
      assert(!document.querySelector('[data-action="apply-update"]'), 'Application proposée sans archive analysée');
      assert(!document.querySelector('.job-banner'), 'Tâche inattendue');
    });
    await check('L’inventaire réel conserve ses IP en lecture seule', async () => {
      click('[data-page="drones"]'); await wait(() => document.querySelector('.drone-item'), 'inventaire Aqua_Drones');
      assert(!document.querySelector('.drone-config input'), 'Champ réseau modifiable exposé');
      assert(find('[data-action="configure-drone"]').disabled, 'Configuration disponible sans précontrôle');
      assert(find('.drone-list-card').textContent.includes('Aqua_Drone_5'), 'Numéro 5 absent des profils disponibles ou des appareils identifiés');
    });
    await check('L’historique persistant est lisible et filtrable par composant', async () => {
      click('[data-page="history"]'); await wait(() => document.querySelectorAll('#history-events .timeline li').length > 0, 'événements réels');
      const select = find('#history-filter'); select.selectedIndex = 1; select.dispatchEvent(new Event('change', { bubbles: true }));
      await wait(() => !document.querySelector('#history-events .loading-inline'), 'filtre historique');
      assert(!document.querySelector('#page-content .error'), 'Erreur de chargement historique');
    });
    await check('L’URL GitHub invalide est refusée avant toute requête de sauvegarde', async () => {
      click('[data-page="settings"]'); await wait(() => !find('#github-url').disabled, 'réglages');
      const input = find('#github-url'); const initial = input.value;
      input.value = 'https://github.com.evil.invalid/aqua/fleet'; input.dispatchEvent(new Event('input', { bubbles: true }));
      find('#settings-form').requestSubmit();
      await wait(() => document.querySelector('#notifications').textContent.includes('URL HTTPS'), 'erreur URL');
      const saved = await window.aquahub.request('settings.get');
      assert((saved.github_url ?? '') === initial, 'La source persistante a été modifiée');
      input.value = initial; input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await check('Les paramètres SSH existants sont consultables sans sauvegarde', async () => {
      click('[data-action="load-ssh"]'); await wait(() => document.querySelector('#ssh-form'), 'réglages SSH');
      assert(Boolean(document.querySelector('#ssh-none-auth')), 'Profil firmware absent');
      assert(!document.querySelector('input[type="password"]'), 'Secret affiché dans le renderer');
    });
    await check('Les cinq vues possèdent un titre et un lien actif accessible', async () => {
      for (const page of ['overview', 'updates', 'drones', 'history', 'settings']) {
        click(`[data-page="${page}"]`);
        assert(find(`[data-page="${page}"]`).getAttribute('aria-current') === 'page', 'Navigation active absente');
        assert(find('#page-title').textContent.trim().length > 0, 'Titre absent');
      }
    });
    await check('Aucune erreur JavaScript durant la lecture des données réelles', async () => assert(errors.length === 0, errors.join('; ')));
    click('[data-page="overview"]'); click('[data-action="detail-events"]');
    const gcs = [...document.querySelectorAll('.service-row')].find(row => row.querySelector('.service-title').textContent.trim() === 'GCS'); if (gcs) gcs.click();
    if (document.documentElement.dataset.theme !== 'light') click('#theme-toggle');
    for (const close of document.querySelectorAll('.toast button')) close.click();
    await pause(500);
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await window.aquahub.request('test.report', { mode: 'live-read-only', engine: navigator.userAgent, results, passed: results.every(result => result.passed), errors, writes: 'Aucune action sur les services, drones ou mises à jour. Thème local uniquement.' });
})();
