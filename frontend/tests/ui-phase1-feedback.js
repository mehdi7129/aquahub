(async () => {
  const results = [], errors = [];
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw new Error(`Élément absent : ${selector}`); return element; };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (check, label) => { const start = Date.now(); while (!check()) { if (Date.now() - start > 5000) throw new Error(`Délai dépassé : ${label}`); await pause(60); } };
  const click = selector => { const element = find(selector); assert(!element.disabled, `Bouton désactivé : ${selector}`); element.click(); };
  const check = async (name, operation) => { try { await operation(); results.push({name, passed:true}); } catch (error) { results.push({name, passed:false, message:error.message}); } };
  const mode = async value => { await window.aquahub.request('fixture.set', {mode:value}); click('[data-action="refresh"]'); await pause(250); };
  try {
    click('.sidebar [data-page="overview"]');
    await wait(() => document.querySelectorAll('.service-row').length === 8, 'services synthétiques');
    await check('Les contours distinguent les états observés et gardent les libellés accessibles', async () => {
      const tones = ['running','failed','failed','failed','transition','transition','transition','unknown'];
      tones.forEach((tone,index) => assert(find(`[data-service="service-${index}"] .service-state`).classList.contains(`state-${tone}`), `Contour incorrect pour service-${index}`));
      assert(find('[data-service="service-0"]').textContent.includes('Santé en échec'), 'Échec de santé masqué par En cours');
      assert(!find('.services-card').textContent.includes('Non défini'), 'Absences de sonde répétées');
      assert(find('.summary-health').textContent.includes('1 en échec'), 'Échec santé absent du résumé');
      assert(find('.service-profile').textContent.includes('signale un échec'), 'Santé absente du détail');
    });
    await check('Une observation ancienne ou une coupure efface tout contour courant', async () => {
      for (const value of ['old','stale','offline']) {
        await mode(value);
        assert([...document.querySelectorAll('.service-row .service-state')].every(element => element.classList.contains('state-unknown')), `Couleur courante conservée : ${value}`);
        assert(find('[data-action="restart"]').disabled, `Relance disponible : ${value}`);
      }
      await mode('fresh'); assert(find('[data-service="service-0"] .service-state').classList.contains('state-running'), 'Reconnexion non actualisée');
    });
    await check('Le délai de fraîcheur grise les données même quand la requête de supervision reste bloquée', async () => {
      const now = Date.now;
      try {
        await window.aquahub.request('fixture.set', {mode:'slow'}); click('[data-action="refresh"]'); await pause(120);
        Date.now = () => now() + 31000;
        await pause(1300);
        assert(find('[data-service="service-0"] .service-state').classList.contains('state-unknown'), 'Ancien vert conservé sans retour RPC');
        assert(find('[data-action="restart"]').disabled, 'Ancienne action disponible sans retour RPC');
      } finally {
        Date.now = now;
        await window.aquahub.request('fixture.set', {mode:'fresh'});
        await wait(() => find('[data-service="service-0"] .service-state').classList.contains('state-running'), 'nouvelle observation après requête bloquée');
      }
    });
    await check('La recherche de mises à jour est prioritaire et aucune recherche ne démarre seule', async () => {
      click('[data-action="go-updates"]');
      const app = find('.app-update-card'), online = find('.bundle-online'), local = find('.bundle-local');
      assert(!app.textContent.includes('github.com') && !app.textContent.includes('Source :') && !app.textContent.includes('Releases'), 'Source technique application visible');
      assert(app.textContent.includes('test-isolé'), 'Version application absente');
      assert(online.compareDocumentPosition(local) & Node.DOCUMENT_POSITION_FOLLOWING, 'Archive avant le parcours en ligne');
      let stats = await window.aquahub.request('fixture.stats');
      assert(stats.app_checks === 0 && stats.bundle_checks === 0 && stats.mutations === 0, 'Recherche ou installation automatique');
      click('[data-action="check-app-update"]'); await wait(() => !document.querySelector('.job-banner'), 'fin recherche app');
      click('[data-action="check-updates"]'); await wait(() => find('.bundle-online').textContent.includes('Aucune livraison synthétique'), 'recherche bundle');
      stats = await window.aquahub.request('fixture.stats'); assert(stats.app_checks === 1 && stats.bundle_checks === 1 && stats.mutations === 0, 'Canaux confondus ou installation déclenchée');
    });
    await check('Annuler l’enregistrement individuel est silencieux et conserve le service exact', async () => {
      click('.sidebar [data-page="overview"]'); click('[data-action="detail-logs"]');
      await wait(() => document.querySelector('[data-action="export-service-logs"]'), 'export individuel');
      const before = find('#notifications').textContent;
      click('[data-action="export-service-logs"]'); await wait(() => !document.querySelector('.job-banner'), 'annulation export');
      const stats = await window.aquahub.request('fixture.stats');
      assert(JSON.stringify(stats.exports[0]) === JSON.stringify({service:'service-0'}), 'Identifiant service non transmis');
      assert(find('#notifications').textContent === before, 'Annulation signalée comme erreur ou réussite');
    });
    await check('Un enregistrement réussi affiche un nom de fichier inerte', async () => {
      await window.aquahub.request('fixture.set', {export_mode:'saved'});
      click('[data-action="export-service-logs"]'); await wait(() => find('#notifications').textContent.includes('Logs enregistrés'), 'succès export');
      assert(find('#notifications').textContent.includes('<img'), 'Nom de fichier altéré');
      assert(!document.querySelector('#notifications img') && window.pwned !== true, 'HTML du nom exécuté');
    });
    await check('L’export groupé transmet une requête bornée et explique un résultat partiel', async () => {
      click('.sidebar [data-page="settings"]');
      assert(find('.diagnostics-card').textContent.includes('1 000 lignes'), 'Limite groupée absente');
      await window.aquahub.request('fixture.set', {export_mode:'partial'});
      click('[data-action="export-all-logs"]'); await wait(() => find('#notifications').textContent.includes('Export partiel'), 'résultat partiel');
      const stats = await window.aquahub.request('fixture.stats');
      assert(JSON.stringify(stats.exports.at(-1)) === '{}', 'Chemin arbitraire ou service ajouté à l’export groupé');
      assert(stats.mutations === 0, 'Mutation inattendue');
    });
    await check('Aucune erreur JavaScript non traitée', async () => assert(errors.length === 0, errors.join('; ')));
  } catch(error) { results.push({name:'Parcours interrompu',passed:false,message:String(error)}); }
  await window.aquahub.request('test.report', {mode:'native-webkit-with-synthetic-save-dialog',results,errors,passed:results.every(result=>result.passed),notice:'Transport Unix factice et réponses Enregistrer sous synthétiques uniquement. Aucun matériel, fichier client ou réseau contacté.'});
})();
