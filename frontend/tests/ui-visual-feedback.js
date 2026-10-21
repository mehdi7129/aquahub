(async () => {
  const previous = sessionStorage.getItem('aquahub-visual-before-reload');
  const results = previous ? JSON.parse(previous) : [], errors = previous ? JSON.parse(sessionStorage.getItem('aquahub-visual-errors-before-reload') || '[]') : [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw Error(`Élément absent : ${selector}`); return element; };
  const assert = (condition, message) => { if (!condition) throw Error(message); };
  const wait = async (condition, label, timeout = 6500) => { const start = Date.now(); while (!condition()) { if (Date.now() - start > timeout) throw Error(`Délai : ${label}`); await pause(50); } };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, action) => { try { await action(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: String(error) }); } };
  const rpc = (method, params = {}) => window.aquahub.request(method, params);
  const badge = service => find(`[data-service="${service}"] .service-state`);
  const operation = phase => badge('gcs').dataset.operation === phase;
  const refresh = async () => { click('[data-action="refresh"]'); await pause(220); };
  const restart = () => { click('[data-action="restart"]'); click('#dialog-confirm'); };
  try {
    await wait(() => document.querySelectorAll('.service-row').length === 2, 'services synthétiques');
    if (previous) {
      sessionStorage.removeItem('aquahub-visual-before-reload');
      sessionStorage.removeItem('aquahub-visual-errors-before-reload');
      await check('La réouverture retrouve la relance terminée depuis l’historique fourni', async () => {
        assert(operation('refreshing') && badge('gcs').classList.contains('state-unknown'), 'Ancien vert rétabli après rechargement');
        await rpc('fixture.set', { cache: 'fresh' }); await refresh();
        await wait(() => badge('gcs').classList.contains('state-running'), 'observation après réouverture');
      });
    } else {
      await check('Annuler une relance ne crée ni tâche ni indicateur de maintenance', async () => {
        click('[data-action="restart"]'); find('#confirmation button[value="cancel"]').click();
        assert(!badge('gcs').dataset.operation && (await rpc('fixture.stats')).requests === 0, 'Relance créée malgré annulation');
      });
      await check('La demande lente puis la tâche active restent attachées au bon service', async () => {
        restart(); await wait(() => operation('requesting'), 'demande envoyée');
        assert(badge('gcs').classList.contains('state-transition'), 'Demande non orange');
        assert(badge('mqtt').classList.contains('state-running'), 'Mauvais service coloré');
        click('[data-service="mqtt"]');
        assert(!find('.service-profile .service-state').dataset.operation, 'Indicateur déplacé vers la sélection');
        await rpc('fixture.set', { release_request: true });
        await wait(() => operation('active'), 'tâche reconnue');
        click('[data-service="gcs"]');
        assert(find('.service-profile .service-state').textContent.includes('Redémarrage en cours'), 'Détail complet absent');
        assert(badge('gcs').getAttribute('aria-label').includes('Redémarrage en cours'), 'Nom accessible incomplet');
      });
      await check('La fin attend une observation postérieure au lieu de rétablir l’ancien vert', async () => {
        await rpc('fixture.finish'); await wait(() => operation('refreshing'), 'cache antérieur après fin');
        assert(badge('gcs').classList.contains('state-unknown'), 'Ancien snapshot présenté comme actuel');
        await rpc('fixture.set', { cache: 'fresh' }); await refresh();
        await wait(() => !badge('gcs').dataset.operation && badge('gcs').classList.contains('state-running'), 'nouvel état Docker');
      });
      await check('Une relance terminée dès la réponse conserve la vérification du cache', async () => {
        await rpc('fixture.set', { finish_on_response: true }); restart();
        await wait(() => operation('refreshing'), 'relance immédiate en attente de collecte');
        await rpc('fixture.set', { cache: 'fresh', finish_on_response: false }); await refresh();
        await wait(() => !badge('gcs').dataset.operation, 'collecte après relance immédiate');
      });
      await check('L’échec de commande est signalé sans inventer un arrêt du service', async () => {
        restart(); await wait(() => operation('active'), 'seconde tâche active');
        await rpc('fixture.finish', { status: 'failed' }); await wait(() => operation('refreshing'), 'fin échouée et ancien cache');
        assert(find('#notifications').textContent.includes('Commande synthétique refusée'), 'Cause d’échec absente');
        await rpc('fixture.set', { cache: 'fresh' }); await refresh();
        await wait(() => badge('gcs').classList.contains('state-running'), 'conteneur toujours observé en cours');
      });
      await check('Une réponse perdue retrouve la tâche et ne rejoue jamais la commande', async () => {
        const before = (await rpc('fixture.stats')).requests;
        await rpc('fixture.set', { restart_reply: 'lost' }); restart();
        await wait(() => operation('active') && !document.querySelector('.job-banner')?.textContent.includes('Veuillez patienter'), 'tâche retrouvée malgré réponse perdue');
        assert((await rpc('fixture.stats')).requests === before + 1, 'Commande rejouée après perte de réponse');
        await rpc('fixture.finish'); await wait(() => operation('refreshing'), 'fin de la tâche retrouvée');
        await rpc('fixture.set', { cache: 'fresh', restart_reply: 'normal' }); await refresh();
        await wait(() => !badge('gcs').dataset.operation, 'état de la tâche retrouvée actualisé');
      });
      await check('Sans identifiant de tâche, une réponse perdue attend une nouvelle observation', async () => {
        const before = (await rpc('fixture.stats')).requests;
        await rpc('fixture.set', { restart_reply: 'untracked' }); restart();
        await wait(() => operation('uncertain'), 'réponse perdue sans tâche retrouvée');
        assert(badge('gcs').classList.contains('state-unknown'), 'Ancien vert conservé malgré issue inconnue');
        assert((await rpc('fixture.stats')).requests === before + 1, 'Nouvel essai automatique sans identifiant');
        assert(find('#notifications').textContent.includes('résultat de la demande non confirmé'), 'Issue inconnue masquée');
        await rpc('fixture.set', { cache: 'fresh', restart_reply: 'normal' }); await refresh();
        await wait(() => !badge('gcs').dataset.operation, 'nouvelle observation après erreur de transport');
      });
      await check('Une fin mal horodatée reste explicitement à vérifier', async () => {
        await rpc('fixture.history', { id: 'invalid', finished_at: 'date-invalide', cache: 'fresh' }); await refresh();
        await wait(() => operation('uncertain'), 'fin invalide');
        assert(badge('gcs').textContent.includes('État à vérifier'), 'Date invalide transformée en réussite');
        await rpc('fixture.history', { id: 'valid', finished_at: new Date(Date.now() - 1000).toISOString(), cache: 'fresh' }); await refresh();
        await wait(() => !badge('gcs').dataset.operation, 'horodatage valide rétabli');
      });
      await check('La coupure grise le service puis une nouvelle observation rétablit son état', async () => {
        await rpc('fixture.set', { offline: true }); await refresh();
        await wait(() => badge('gcs').classList.contains('state-unknown'), 'coupure de l’agent');
        await rpc('fixture.set', { offline: false, cache: 'fresh' }); await refresh();
        await wait(() => badge('gcs').classList.contains('state-running'), 'reconnexion');
      });
      await check('La barre native remplit les étapes acquises et reste stable pendant l’attente', async () => {
        await rpc('fixture.progress', { progress: { completed: 0, total: 12, percent: 0, indeterminate: true } }); await refresh();
        await wait(() => document.querySelector('.job-progress progress'), 'progression adoptée');
        for (const [completed, percent] of [[0, 0], [3, 25], [7, 58]]) {
          await rpc('fixture.progress', { progress: { completed, total: 12, percent, indeterminate: true, label: 'Attente du retour de Aqua_Drone_5', phase: 'reboot' } });
          await wait(() => document.querySelector('.job-progress progress')?.value === percent && document.querySelector('.job-progress')?.textContent.includes(`${percent} % validés`), `${percent} % remplis`);
          const progress = find('.job-progress progress');
          assert(progress.hasAttribute('value') && Math.abs(progress.position - percent / 100) < .001, 'Position native incohérente');
        }
        const progress = find('.job-progress progress'); await pause(1200);
        assert(find('.job-progress progress').value === 58 && find('.job-progress progress').position === progress.position, 'Avancement fondé sur le temps');
        assert(find('.job-banner').textContent.includes('Aqua_Drone_5'), 'Numéro du drone absent');
        await rpc('fixture.progress', { progress: { indeterminate: true, label: 'Vérification sans estimation' } });
        await wait(() => !find('.job-progress progress').hasAttribute('value'), 'véritable attente sans pourcentage');
        assert(find('.job-progress progress').position === -1, 'Pourcentage inventé sans estimation');
        await rpc('fixture.finish'); await wait(() => !document.querySelector('.job-banner'), 'fin de progression');
      });
      // A real document reload discards state.job. Only status.jobs can now
      // recover the completed restart whose collected snapshot still predates it.
      await rpc('fixture.history', { id: 'reopen' });
      sessionStorage.setItem('aquahub-visual-before-reload', JSON.stringify(results));
      sessionStorage.setItem('aquahub-visual-errors-before-reload', JSON.stringify(errors));
      location.reload(); return;
    }
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await check('Aucune erreur JavaScript non traitée', async () => assert(!errors.length, errors.join('; ')));
  await rpc('test.report', { mode: 'isolated-native-visual-feedback', passed: results.every(result => result.passed), results, errors, notice: 'Transport Unix et WebKit natifs ; réponses factices. Aucun service réel redémarré, aucun drone contacté.' });
})();
