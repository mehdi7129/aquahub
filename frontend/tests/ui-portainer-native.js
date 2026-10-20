(async () => {
  const results = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const check = async (name, operation) => { try { await operation(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: error.message }); } };
  await check('Échap dans le formulaire GTK annule sans envoyer de création de compte', async () => {
    const pending = window.aquahub.request('native.portainer_setup');
    await window.aquahub.request('test.report', { phase: 'native_cancel' });
    const result = await pending;
    assert(result.canceled === true, 'Annulation non retournée au renderer');
    const stats = await window.aquahub.request('fixture.stats');
    assert(stats.initialize_calls === 0, 'Création de compte appelée après Échap');
    assert(!document.querySelector('input[type="password"]'), 'Mot de passe demandé dans WebKit');
  });
  await check('Une saisie native valide retourne un job sans transmettre les identifiants au renderer', async () => {
    const pending = window.aquahub.request('native.portainer_setup');
    await window.aquahub.request('test.report', { phase: 'native_fill' });
    const result = await pending;
    assert(result.id === 'portainer-native-fixture' && result.status === 'queued', 'Tâche de création non retournée');
    assert(!JSON.stringify(result).includes('password'), 'Un champ secret apparaît dans la réponse renderer');
    assert(!document.querySelector('input[type="password"]'), 'Mot de passe présent dans le DOM');
    const stats = await window.aquahub.request('fixture.stats');
    assert(stats.initialize_calls === 1 && stats.native_credentials_match === true, 'Identifiants natifs non reçus correctement par l’agent factice');
    assert(stats.unexpected_calls === 0, 'Une autre action a été exécutée');
  });
  await window.aquahub.request('test.report', { phase: 'complete', mode: 'isolated-native-portainer-gtk', passed: results.every(result => result.passed), results, notice: 'Socket factice privée. Aucune connexion HTTP, Docker, VM ou Portainer réel. Données de mot de passe synthétiques saisies uniquement dans GTK.' });
})();
