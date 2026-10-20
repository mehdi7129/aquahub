(async () => {
  const results = [];
  const errors = [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw new Error(`Élément absent : ${selector}`); return element; };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (check, label, timeout = 8000) => { const start = Date.now(); while (!check()) { if (Date.now() - start > timeout) throw new Error(`Délai dépassé : ${label}`); await pause(80); } };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, operation) => { try { await operation(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: error.message }); } };
  const mode = async value => { await window.aquahub.request('fixture.set', { mode: value }); click('[data-action="refresh"]'); await pause(200); };
  try {
    await wait(() => document.querySelectorAll('.service-row').length === 1, 'banc isolé chargé');
    await check('Un instantané périmé garde les dernières données et bloque les mutations', async () => {
      await mode('stale'); await wait(() => document.querySelector('#connection-banner').textContent.includes('non actualisée'), 'avertissement stale');
      assert(find('.service-row').textContent.includes('GCS de test'), 'Dernières données perdues');
      assert(find('[data-action="restart"]').disabled, 'Mutation disponible sur données anciennes');
      assert(find('#freshness').textContent.includes('Dernière lecture'), 'Données anciennes présentées comme actuelles');
    });
    await check('Un moteur non supervisable laisse le diagnostic et les réglages accessibles', async () => {
      click('[data-page="settings"]');
      assert(!find('[data-action="diagnostics"]').disabled, 'Diagnostic bloqué alors que l’agent répond');
      assert(!find('#settings-form button[type="submit"]').disabled, 'Préférences bloquées avec un agent connecté');
      assert(find('.about-card .badge').textContent === 'Agent connecté', 'Agent confondu avec le moteur Docker');
      click('[data-page="overview"]'); assert(find('[data-action="restart"]').disabled, 'Garde de maintenance levée sur snapshot périmé');
    });
    await check('Une coupure conserve les données en indiquant que leur état est non confirmé', async () => {
      await mode('offline'); await wait(() => find('#connection-banner').textContent.includes('Connexion simulée interrompue'), 'coupure affichée');
      assert(document.querySelectorAll('.service-row').length === 1, 'Dernier état effacé sans nécessité');
      assert(find('[data-action="restart"]').disabled, 'Mutation disponible pendant une coupure');
    });
    await check('La reconnexion restaure les données et les actions sans redémarrer l’application', async () => {
      await mode('fresh'); await wait(() => !document.querySelector('#connection-banner .error'), 'reconnexion');
      assert(!find('[data-action="restart"]').disabled, 'Action non rétablie après reconnexion');
    });
    await check('Une actualisation conserve le focus clavier du composant sélectionné', async () => {
      find('[data-service="gcs"]').focus(); click('[data-action="refresh"]'); await pause(300);
      assert(document.activeElement.dataset.service === 'gcs', 'Le focus clavier a été perdu pendant l’actualisation');
      find('#events-tab').focus(); find('#events-tab').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
      assert(find('#logs-tab').getAttribute('aria-selected') === 'true', 'Navigation clavier des onglets absente');
      assert(document.activeElement.id === 'logs-tab', 'Focus du nouvel onglet absent');
    });
    await check('Les séquences ANSI et le HTML de logs restent des données inertes', async () => {
      click('[data-action="detail-logs"]'); await wait(() => document.querySelector('.logs')?.textContent.includes('Erreur simulée'), 'logs synthétiques');
      assert(find('.logs').textContent.includes('<img'), 'Texte technique perdu');
      assert(!document.querySelector('.logs img,.logs script'), 'HTML exécuté dans les journaux');
      assert(!find('.logs').textContent.includes('\x1b'), 'Séquence terminal conservée');
      assert(window.pwned !== true, 'Code des logs exécuté');
    });
    await check('La pause des logs suspend leur actualisation et conserve le focus du lecteur', async () => {
      const follow = find('#logs-follow'); follow.checked = false; follow.dispatchEvent(new Event('change', { bubbles: true }));
      const before = await window.aquahub.request('fixture.stats');
      find('#service-logs').focus(); click('[data-action="refresh"]'); await pause(300);
      const after = await window.aquahub.request('fixture.stats');
      assert(after.logs_reads === before.logs_reads, 'Les journaux ont été rechargés malgré la pause');
      assert(document.activeElement.id === 'service-logs', 'Focus du lecteur de logs perdu');
      assert(!find('#logs-follow').checked, 'Le mode de suivi a été réactivé implicitement');
    });
    await check('Les messages d’événements restent également du texte inerte', async () => {
      click('[data-page="history"]'); await wait(() => document.querySelector('.timeline-content'), 'événement synthétique');
      assert(find('.timeline-content').textContent.includes('<img'), 'Message technique perdu');
      assert(!document.querySelector('.timeline img'), 'HTML interprété dans un événement');
    });
    await check('Un extrait de panne conservé est consultable et inerte, avec sa limite expliquée', async () => {
      click('[data-action="event-snapshot"]'); await wait(() => find('#confirmation').open, 'extrait de panne ouvert');
      assert(find('.snapshot-logs').textContent.includes('Erreur synthétique conservée'), 'Extrait conservé non chargé');
      assert(!document.querySelector('.snapshot-logs img'), 'HTML interprété dans un extrait de panne');
      assert(find('.snapshot-limit').textContent.includes('borné'), 'Extrait tronqué non signalé');
      find('#confirmation .dialog-actions button').click();
      assert(find('#history-events').textContent.includes('durée de conservation dépassée'), 'Extrait expiré non expliqué');
      assert(find('#history-events').textContent.includes('limite de collecte atteinte'), 'Capture limitée non expliquée');
    });
    await check('Les événements précédents s’ajoutent puis le filtre réinitialise la pagination', async () => {
      click('[data-action="load-history-more"]');
      await wait(() => find('#history-events').textContent.includes('Ancien incident synthétique'), 'ancienne page ajoutée');
      assert(find('#history-events').textContent.includes('Événement synthétique'), 'Les événements déjà chargés ont été remplacés');
      assert(!document.querySelector('[data-action="load-history-more"]'), 'Pagination toujours proposée après la dernière page');
      click('[data-page="settings"]'); click('[data-page="history"]');
      assert(find('#history-events').textContent.includes('Ancien incident synthétique'), 'Une navigation a effacé la page chargée');
      const filter = find('#history-filter'); filter.value = 'gcs'; filter.dispatchEvent(new Event('change', { bubbles: true }));
      await wait(() => document.querySelector('[data-action="load-history-more"]'), 'curseur réinitialisé après filtre');
      assert(!find('#history-events').textContent.includes('Ancien incident synthétique'), 'L’ancienne page est restée après nouveau filtre');
    });
    await check('Un accès aux réglages refusé affiche une erreur exploitable', async () => {
      click('[data-page="settings"]'); await mode('settings_denied');
      await wait(() => document.querySelector('#page-content .error')?.textContent.includes('Accès aux réglages refusé'), 'refus de lecture explicite');
    });
    await check('Le lien d’accès rapide au contenu conserve la page courante', async () => {
      const hash = location.hash; find('.skip-link').click();
      assert(location.hash === hash && find('#page-title').textContent === 'Réglages', 'Le lien d’accès au contenu a changé la page');
      assert(document.activeElement.id === 'page-content', 'Le contenu n’a pas reçu le focus');
    });
    await check('La relance d’un Portainer non géré est refusée sans réessai automatique', async () => {
      click('[data-action="restart-portainer"]'); find('#confirmation button[value="cancel"]').click();
      let stats = await window.aquahub.request('fixture.stats'); assert(stats.portainer_attempts === 0, 'Annulation ignorée');
      click('[data-action="restart-portainer"]'); click('#dialog-confirm');
      await wait(() => find('#notifications').textContent.includes('n’est pas gérée par AquaHub'), 'refus Portainer affiché');
      await wait(() => !document.querySelector('.job-banner'), 'fin du refus Portainer');
      stats = await window.aquahub.request('fixture.stats'); assert(stats.portainer_attempts === 1, 'Plusieurs requêtes de relance envoyées');
    });
    await check('Une tâche récupérée de l’agent bloque les nouvelles actions', async () => {
      click('[data-page="overview"]'); await mode('job_running');
      await wait(() => document.querySelector('.job-banner'), 'tâche agent récupérée');
      assert(find('[data-action="restart"]').disabled, 'Nouvelle action disponible malgré une tâche récupérée');
    });
    await check('Une perte du suivi de tâche garde le résultat incertain et ne relance rien', async () => {
      await mode('job_error');
      await wait(() => document.querySelector('.job-banner')?.textContent.includes('résultat reste à vérifier'), 'suivi indisponible');
      assert(find('[data-action="restart"]').disabled, 'Nouvelle action permise pendant un résultat incertain');
      const stats = await window.aquahub.request('fixture.stats'); assert(stats.mutations === 0, 'Requête mutante ou réessai inattendu');
    });
    await check('Le suivi reprend et annonce le résultat confirmé', async () => {
      await mode('fresh'); await wait(() => !document.querySelector('.job-banner'), 'résultat confirmé', 10000);
      assert(!find('[data-action="restart"]').disabled, 'Maintenance non libérée après résultat');
      assert(find('.completion-notice').textContent.includes('contrôles matériels restent'), 'Les contrôles manuels restants sont masqués');
      click('[data-action="dismiss-job-result"]'); assert(!document.querySelector('.completion-notice'), 'Résultat manuel impossible à fermer');
    });
    await check('Aucune erreur JavaScript non traitée dans les scénarios de panne', async () => assert(errors.length === 0, errors.join('; ')));
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await window.aquahub.request('test.report', { mode: 'isolated-synthetic-fixtures', results, errors, passed: results.every(result => result.passed), notice: 'Aucun accès au moteur Docker ou aux appareils réels.' });
})();
