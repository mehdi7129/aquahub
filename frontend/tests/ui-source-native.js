(async () => {
  const results = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const check = async (name, operation) => { try { await operation(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: error.message }); } };
  await check('Échap dans le formulaire GTK de jeton annule sans appel au backend', async () => {
    const pending = window.aquahub.request('native.source_credentials');
    await window.aquahub.request('test.report', { phase: 'native_cancel' });
    const result = await pending;
    assert(result.canceled === true, 'Annulation non retournée au renderer');
    const stats = await window.aquahub.request('fixture.stats');
    assert(stats.settings_calls === 0, 'updates.settings appelé après annulation');
    assert(!document.querySelector('input[type="password"]'), 'Un jeton est demandé dans WebKit');
  });
  await check('Le jeton GTK utilise updates.settings et ne retourne que saved/configured au renderer', async () => {
    const pending = window.aquahub.request('native.source_credentials');
    await window.aquahub.request('test.report', { phase: 'native_fill' });
    const result = await pending;
    assert(result.saved === true && result.configured === true, 'Enregistrement non confirmé');
    assert(Object.keys(result).sort().join(',') === 'configured,saved', 'Une donnée supplémentaire est retournée au renderer');
    assert(!document.querySelector('input[type="password"]'), 'Secret présent dans le DOM');
    assert(!document.documentElement.innerHTML.includes('synthetic-demo'), 'Secret synthétique présent dans le HTML');
    const stats = await window.aquahub.request('fixture.stats');
    assert(stats.settings_calls === 1 && stats.native_credentials_match === true, 'Le backend factice ne reçoit pas exactement updates.settings et le jeton natif');
    assert(stats.unexpected_calls === 0, 'Une méthode autre que updates.settings a été appelée');
  });
  await window.aquahub.request('test.report', { phase: 'complete', mode: 'isolated-native-source-credentials-gtk', passed: results.every(result => result.passed), results, notice: 'Socket factice privée. Aucun dépôt distant, agent réel ou matériel contacté. Jeton synthétique saisi uniquement dans GTK.' });
})();
