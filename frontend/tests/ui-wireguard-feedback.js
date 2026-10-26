(async () => {
  const saved = sessionStorage.getItem('aquahub-wg-before-reload');
  const results = saved ? JSON.parse(saved) : [], errors = saved ? JSON.parse(sessionStorage.getItem('aquahub-wg-errors-before-reload') || '[]') : [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw Error(`Élément absent : ${selector}`); return element; };
  const assert = (value, message) => { if (!value) throw Error(message); };
  const wait = async (condition, label, timeout = 6500) => { const start = Date.now(); while (!condition()) { if (Date.now() - start > timeout) throw Error(`Délai : ${label}`); await pause(40); } };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, action) => { try { await action(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: String(error) }); } };
  const rpc = (method, params = {}) => window.aquahub.request(method, params);
  const tunnel = () => document.querySelector('[data-service="wireguard"] [data-wireguard-state]');
  const operation = value => tunnel()?.dataset.operation === value;
  const refresh = async () => { click('[data-action="refresh"]'); await pause(180); };
  const activate = () => { click('[data-action="wireguard-reactivate"]'); click('#dialog-confirm'); };
  const reset = async (params = {}) => {
    await rpc('fixture.set', { tunnel: 'inactive', cache: 'fresh', job: null, reply: 'normal', release_request: true, invalid_status: false, invalid_probe: false, probe_error: false, probe_state: null, container: 'running', can_reactivate: true, ...params });
    await refresh();
    if (!document.querySelector('[data-wireguard-detail]')) click('[data-service="wireguard"]');
  };
  try {
    await wait(() => document.querySelectorAll('.service-row').length === 2, 'services synthétiques');
    click('[data-service="wireguard"]');
    if (saved) {
      sessionStorage.removeItem('aquahub-wg-before-reload');
      sessionStorage.removeItem('aquahub-wg-errors-before-reload');
      await check('La réouverture garde la réactivation terminée à vérifier jusqu’à une nouvelle observation', async () => {
        assert(operation('refreshing') && tunnel().dataset.wireguardState === 'unknown', 'Ancien tunnel présenté comme actuel après réouverture');
        await rpc('fixture.set', { cache: 'fresh' }); await refresh();
        await wait(() => tunnel()?.dataset.wireguardState === 'active', 'observation après réouverture');
      });
    } else {
      await check('Un conteneur en cours avec interface incomplète propose la bonne action sans drone', async () => {
        assert(find('[data-service="wireguard"] .service-state').textContent.includes('En cours'), 'État Docker remplacé par le tunnel');
        assert(tunnel().textContent.includes('Tunnel incomplet'), 'Interface incomplète masquée');
        assert(!find('[data-action="wireguard-reactivate"]').disabled, 'Réactivation inaccessible');
        assert(!document.querySelector('[data-action="restart"]'), 'Redémarrage générique WireGuard affiché');
      });
      await check('Annuler la réactivation ne crée aucune commande', async () => {
        click('[data-action="wireguard-reactivate"]');
        find('#confirmation button[value="cancel"]').click();
        assert((await rpc('fixture.stats')).requests === 0, 'Commande envoyée malgré annulation');
      });
      await check('Le double clic et la demande lente restent attachés à WireGuard pendant la navigation', async () => {
        await rpc('fixture.set', { release_request: false });
        click('[data-action="wireguard-reactivate"]');
        find('#dialog-confirm').click(); find('#dialog-confirm').click();
        await wait(() => operation('requesting'), 'demande de réactivation');
        assert(find('[data-service="gcs"] .service-state').classList.contains('state-running'), 'GCS faussement coloré');
        click('[data-service="gcs"]');
        await rpc('fixture.set', { release_request: true });
        await wait(() => operation('active'), 'tâche WireGuard reconnue');
        assert(!find('.service-profile').textContent.includes('Réactivation'), 'Indicateur déplacé sur GCS');
        assert((await rpc('fixture.stats')).requests === 1, 'Double commande envoyée');
        click('[data-service="wireguard"]');
        assert(find('#job-banner').textContent.includes('Réactivation du tunnel WireGuard'), 'Libellé de tâche générique');
      });
      await check('La fin attend aussi une observation propre au tunnel postérieure à la tâche', async () => {
        await rpc('fixture.finish');
        await rpc('fixture.set', { cache: 'container-only' });
        await wait(() => operation('refreshing'), 'tunnel ancien après fin');
        assert(tunnel().dataset.wireguardState === 'unknown', 'Succès annoncé depuis un ancien tunnel');
        await rpc('fixture.set', { cache: 'fresh' }); await refresh();
        await wait(() => tunnel()?.dataset.wireguardState === 'active', 'tunnel observé après fin');
      });
      await check('Un tunnel actif sans échange récent propose une vérification consultative', async () => {
        const before = await rpc('fixture.stats');
        assert(find('[aria-label="Tunnel WireGuard"]').textContent.includes('Aucun échange récent observé'), 'Absence d’échange présentée comme panne');
        assert(!document.querySelector('[data-action="wireguard-reactivate"]'), 'Mutation proposée malgré tunnel actif');
        click('[data-action="wireguard-check"]');
        await wait(() => !document.querySelector('.job-banner'), 'fin de la consultation');
        const after = await rpc('fixture.stats');
        assert(after.checks === before.checks + 1 && after.requests === before.requests, 'Vérification transformée en mutation');
        assert(find('#notifications').textContent.includes('La présence d’un Aqua_Drone n’est pas confirmée'), 'Présence drone inventée');
      });
      await check('Un ancien cache ne remplace pas la sonde consultative plus récente', async () => {
        await reset({ job: null, before: new Date(Date.now() - 1000).toISOString(), cache: 'container-only', probe_state: 'active' });
        click('[data-action="wireguard-check"]');
        await wait(() => tunnel()?.dataset.wireguardState === 'active', 'résultat de la sonde');
        await refresh();
        assert(tunnel().dataset.wireguardState === 'active', 'Ancien cache réintroduit un tunnel incomplet');
      });
      await check('Un échec de réactivation est explicite et ne prétend pas arrêter Docker', async () => {
        await reset(); activate(); await wait(() => operation('active'), 'tâche à faire échouer');
        await rpc('fixture.finish', { status: 'failed' });
        await rpc('fixture.set', { cache: 'fresh' });
        await wait(() => find('#notifications').textContent.includes('Activation synthétique refusée'), 'message de refus');
        await wait(() => tunnel()?.dataset.wireguardState === 'inactive', 'état après échec');
        assert(find('[data-service="wireguard"] .service-state').textContent.includes('En cours'), 'Échec confondu avec arrêt Docker');
      });
      await check('Une réponse perdue retrouve la tâche sans rejouer l’activation', async () => {
        await reset({ reply: 'lost' });
        const before = (await rpc('fixture.stats')).requests;
        activate();
        await wait(() => operation('active'), 'tâche retrouvée après réponse perdue');
        assert((await rpc('fixture.stats')).requests === before + 1, 'Activation rejouée');
        await rpc('fixture.finish'); await rpc('fixture.set', { cache: 'fresh' });
        await wait(() => tunnel()?.dataset.wireguardState === 'active', 'fin de la tâche retrouvée');
      });
      await check('Sans tâche retrouvée la réponse perdue garde une incertitude sans réessai', async () => {
        await reset({ reply: 'untracked' });
        const before = (await rpc('fixture.stats')).requests;
        activate();
        await wait(() => operation('uncertain'), 'résultat inconnu sans tâche');
        assert(!document.querySelector('[data-action="wireguard-reactivate"]'), 'Nouvel essai proposé depuis l’ancien état');
        assert((await rpc('fixture.stats')).requests === before + 1, 'Demande renvoyée automatiquement');
        await rpc('fixture.set', { cache: 'fresh', reply: 'normal' }); await refresh();
        await wait(() => tunnel()?.dataset.wireguardState === 'inactive', 'nouvelle observation après perte');
      });
      await check('DTO invalide, tunnel ancien et démarrage non pris en charge restent protégés', async () => {
        for (const params of [{ invalid_status: true }, { cache: 'stale' }, { tunnel: 'unsupported', container: 'exited' }]) {
          await reset(params);
          assert(!document.querySelector('[data-action="wireguard-reactivate"]'), 'Activation autorisée sans état fiable');
          assert(!find('[data-action="wireguard-check"]').disabled, 'Consultation rendue impossible');
          assert(!document.querySelector('[data-action="restart"]'), 'Ancien démarrage dangereux exposé');
        }
        await reset({ invalid_probe: true, before: new Date(Date.now() - 1000).toISOString(), cache: 'container-only' });
        click('[data-action="wireguard-check"]');
        await wait(() => tunnel()?.dataset.wireguardState === 'unknown', 'sonde invalide');
        assert(!document.querySelector('[data-action="wireguard-reactivate"]'), 'Ancien état rétabli après sonde invalide');
      });
      await reset({ tunnel: 'inactive' });
      document.querySelectorAll('#notifications button').forEach(button => button.click());
      for (const theme of ['light', 'dark']) await check(`Le détail WireGuard reste lisible à 1366×768 en mode ${theme}`, async () => {
        if (document.documentElement.dataset.theme !== theme) click('[data-action="theme"]');
        await pause(120);
        const panel = find('[aria-label="Tunnel WireGuard"]'); panel.scrollIntoView({ block: 'start' }); await pause(100);
        const rect = panel.getBoundingClientRect();
        assert(rect.width > 200 && rect.right <= innerWidth + 1, 'Détail trop large ou tronqué');
        assert(document.documentElement.scrollWidth <= innerWidth + 1, 'Débordement horizontal');
        const button = find('[data-action="wireguard-check"]'), badge = find('[data-wireguard-detail]');
        assert(button.getBoundingClientRect().top >= badge.getBoundingClientRect().bottom, 'Bouton superposé au statut');
        const activation = find('[data-action="wireguard-reactivate"]').getBoundingClientRect(), verification = button.getBoundingClientRect();
        assert(activation.right <= verification.left || activation.bottom <= verification.top || verification.right <= activation.left || verification.bottom <= activation.top, 'Boutons WireGuard superposés');
        assert(activation.top >= 0 && verification.top >= 0 && activation.bottom <= innerHeight + 1 && verification.bottom <= innerHeight + 1, 'Contrôles WireGuard inaccessibles dans la fenêtre');
        for (const control of [find('[data-action="wireguard-reactivate"]'), button]) { const bounds = control.getBoundingClientRect(); assert(document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2)?.closest('button') === control, 'Contrôle WireGuard masqué par un autre élément'); }
        await rpc('test.report', { phase: 'appearance', theme, results });
        const start = Date.now(); while (!(await rpc('fixture.stats')).captures.includes(theme)) { if (Date.now() - start > 5000) throw Error('Capture non enregistrée'); await pause(70); }
      });
      await check('Aucun redémarrage générique ni JavaScript non traité', async () => {
        assert((await rpc('fixture.stats')).generic_restarts === 0, 'Redémarrage générique appelé');
        assert(!errors.length, errors.join('; '));
      });
      await rpc('fixture.history');
      sessionStorage.setItem('aquahub-wg-before-reload', JSON.stringify(results));
      sessionStorage.setItem('aquahub-wg-errors-before-reload', JSON.stringify(errors));
      location.reload(); return;
    }
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await check('Aucune erreur JavaScript non traitée après réouverture', async () => assert(!errors.length, errors.join('; ')));
  await rpc('test.report', { phase: 'complete', mode: 'isolated-native-wireguard-feedback', passed: results.every(result => result.passed), results, errors, notice: 'Socket Unix synthétique et WebKit natif ; aucun Docker, réseau ou drone réel.' });
})();
