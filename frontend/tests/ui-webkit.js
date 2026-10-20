(async () => {
  const results = [];
  const errors = [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => {
    const element = document.querySelector(selector);
    if (!element) throw new Error(`Élément absent : ${selector}`);
    return element;
  };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (check, label, timeout = 6500) => {
    const start = Date.now();
    while (!check()) { if (Date.now() - start > timeout) throw new Error(`Délai dépassé : ${label}`); await pause(80); }
  };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, operation) => {
    try { await operation(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, message: error.message }); }
  };
  try {
    await check('Le mode démonstration est explicitement signalé, avec huit composants', async () => {
      await wait(() => document.querySelectorAll('.service-row').length === 8, 'services chargés');
      assert(find('.demo-banner').textContent.includes('Données fictives'), 'Bandeau démonstration absent');
      assert(document.querySelectorAll('.health .icon').length === 2, 'Nombre de contrôles santé incohérent');
    });
    await check('Le thème sombre puis clair est appliqué et mémorisé', async () => {
      if (document.documentElement.dataset.theme !== 'dark') click('#theme-toggle');
      assert(document.documentElement.dataset.theme === 'dark', 'Mode sombre non appliqué');
      assert(localStorage.getItem('aquahub-theme') === 'dark', 'Thème non mémorisé');
      click('#theme-toggle'); assert(document.documentElement.dataset.theme === 'light', 'Mode clair non appliqué');
    });
    await check('Annuler une confirmation ne lance pas de maintenance', async () => {
      click('[data-action="restart"]'); await wait(() => find('#confirmation').open, 'confirmation');
      find('#confirmation button[value="cancel"]').click();
      await pause(300); assert(!document.querySelector('.job-banner'), 'Maintenance déclenchée après annulation');
    });
    await check('Un redémarrage confirmé est suivi et bloque les doubles actions', async () => {
      click('[data-action="restart"]'); find('#confirmation button[value="confirm"]').click();
      await wait(() => document.querySelector('.job-banner'), 'tâche visible');
      assert(find('[data-action="restart"]').disabled, 'Redémarrage concurrent encore disponible');
      await wait(() => !document.querySelector('.job-banner'), 'tâche terminée');
      assert(document.querySelector('#notifications').textContent.includes('Terminée'), 'Résultat final non affiché');
    });
    await check('Les logs sont affichés comme texte dans leur panneau dédié', async () => {
      click('[data-action="detail-logs"]');
      await wait(() => document.querySelector('.logs')?.textContent.includes('DÉMONSTRATION'), 'logs chargés');
      assert(find('#logs-tab').getAttribute('aria-selected') === 'true', 'Onglet logs non sélectionné');
    });
    await check('Un extrait historique de panne se consulte séparément des logs actuels', async () => {
      click('[data-action="detail-events"]');
      await wait(() => document.querySelector('[data-action="event-snapshot"]'), 'capture de panne simulée');
      click('[data-action="event-snapshot"]');
      await wait(() => find('#confirmation').open, 'extrait historique');
      assert(find('.snapshot-logs').textContent.includes('DÉMONSTRATION'), 'Extrait historique absent');
      find('#confirmation .dialog-actions button').click();
    });
    await check('Les adresses des Aqua_Drones sont consultables sans champ modifiable', async () => {
      click('[data-page="drones"]');
      await wait(() => document.querySelector('.readonly-value'), 'drone chargé');
      assert(find('.readonly-value').textContent.includes('192.168.205.2'), 'Adresse du numéro 5 incorrecte');
      assert(!document.querySelector('.drone-config input'), 'Champ réseau modifiable exposé');
      assert(find('[data-action="configure-drone"]').disabled, 'Configuration disponible avant précontrôle');
    });
    await check('L’enregistrement SSH exige une confirmation manuelle explicite', async () => {
      click('[data-action="enroll-drone"]');
      await wait(() => document.querySelector('#confirmation').open, 'dialogue empreinte');
      assert(find('#dialog-confirm').disabled, 'Empreinte automatiquement approuvée');
      assert(find('.identity-confirmation').textContent.includes('SHA256:'), 'Empreinte invisible');
      find('#confirmation button[value="cancel"]').click();
      await wait(() => !document.querySelector('.job-banner'), 'fin vérification');
    });
    await check('La configuration du numéro 5 reste bloquée jusqu’à la confirmation des identifiants', async () => {
      click('[data-action="preflight"]');
      await wait(() => !find('[data-action="configure-drone"]').disabled, 'précontrôles valides');
      click('[data-action="configure-drone"]');
      assert(find('#dialog-confirm').disabled, 'Identité automatiquement approuvée');
      assert(find('.identity-confirmation').textContent.includes('APPAREIL-DEMONSTRATION-5'), 'Identifiant machine invisible');
      const checkbox = find('#confirm-identity'); checkbox.checked = true; checkbox.dispatchEvent(new Event('change', { bubbles: true }));
      click('#dialog-confirm');
      await wait(() => document.querySelector('.job-banner'), 'configuration en cours');
      await wait(() => !document.querySelector('.job-banner'), 'configuration terminée');
      await wait(() => find('.drone-item').textContent.includes('Configuration vérifiée'), 'inventaire actualisé après tâche');
    });
    await check('L’archive est vérifiée avant de proposer son installation', async () => {
      click('[data-page="updates"]'); click('[data-action="import"]');
      await wait(() => document.querySelector('[data-action="apply-update"]'), 'analyse archive');
      assert(!find('[data-action="apply-update"]').disabled, 'Archive de démonstration refusée');
      click('[data-action="apply-update"]'); find('#confirmation button[value="cancel"]').click();
      assert(!document.querySelector('.job-banner'), 'Installation déclenchée après annulation');
    });
    await check('La source GitHub s’enregistre sans déclencher d’installation', async () => {
      click('[data-page="settings"]');
      const field = find('#github-url'); field.value = 'https://github.com/exemple/aqua'; field.dispatchEvent(new Event('input', { bubbles: true }));
      find('#settings-form').requestSubmit();
      await wait(() => !document.querySelector('.job-banner'), 'source enregistrée');
      click('[data-page="updates"]');
      assert(find('.source-card').textContent.includes('https://github.com/exemple/aqua'), 'Source non conservée');
      assert(!document.querySelector('.job-banner'), 'Mise à jour déclenchée par la source');
    });
    await check('Le téléchargement d’une release revient à l’inspection sans appliquer la livraison', async () => {
      click('[data-action="check-updates"]');
      await wait(() => document.querySelector('[data-action="download-release"]'), 'release simulée');
      assert(find('.release-notes').textContent.includes('fictives'), 'Notes de version absentes');
      click('[data-action="download-release"]');
      await wait(() => document.querySelector('.job-banner'), 'téléchargement suivi');
      await wait(() => !document.querySelector('.job-banner'), 'archive téléchargée et analysée');
      await wait(() => !find('[data-action="apply-update"]').disabled, 'inspection de la release présentée et active');
      assert(!find('#confirmation').open, 'Installation automatiquement proposée en confirmation');
    });
    await check('Le profil SSH sans secret est décoché par défaut et configurable explicitement', async () => {
      click('[data-page="settings"]'); click('[data-action="load-ssh"]');
      await wait(() => document.querySelector('#ssh-none-auth'), 'paramètres SSH chargés');
      assert(!find('#ssh-none-auth').checked, 'Profil sans secret activé par défaut');
      assert(!document.querySelector('input[type="password"]'), 'Un secret est demandé dans le renderer');
    });
    await check('La relance Portainer exige confirmation puis expose son résultat sans doubler la tâche', async () => {
      click('[data-action="restart-portainer"]'); find('#confirmation button[value="cancel"]').click();
      assert(!document.querySelector('.job-banner'), 'Portainer relancé après annulation');
      click('[data-action="restart-portainer"]'); click('#dialog-confirm');
      await wait(() => document.querySelector('.job-banner'), 'relance Portainer suivie');
      assert(find('[data-action="restart-portainer"]').disabled, 'Seconde relance Portainer proposée pendant la tâche');
      await wait(() => !document.querySelector('.job-banner'), 'relance Portainer terminée');
    });
    await check('La création du compte initial Portainer utilise le parcours natif et suit sa tâche', async () => {
      await wait(() => !find('[data-action="setup-portainer"]').disabled, 'initialisation disponible après relance');
      click('[data-action="setup-portainer"]');
      await wait(() => document.querySelector('.job-banner'), 'initialisation Portainer suivie');
      assert(!document.querySelector('input[type="password"]'), 'Mot de passe initial demandé dans le renderer');
      assert(find('[data-action="setup-portainer"]').disabled, 'Initialisation concurrente disponible');
      await wait(() => !document.querySelector('.job-banner'), 'initialisation Portainer terminée');
    });
    await check('Les événements de maintenance apparaissent dans l’historique', async () => {
      click('[data-page="history"]');
      await wait(() => document.querySelectorAll('#history-events .timeline li').length >= 2, 'historique chargé');
      assert(find('#history-events').textContent.includes('simulée terminée'), 'Événement de maintenance absent');
    });
    await check('Aucune erreur JavaScript non traitée durant le parcours', async () => assert(errors.length === 0, errors.join('; ')));
    click('[data-page="overview"]');
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await window.aquahub.request('test.report', { mode: 'demo-only', engine: navigator.userAgent, results, passed: results.every(result => result.passed), errors });
})();
