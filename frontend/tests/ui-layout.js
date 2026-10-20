(async () => {
  const results = [];
  const errors = [];
  const measurements = [];
  let sequence = 0;
  window.addEventListener('error', event => errors.push(event.message));
  window.addEventListener('unhandledrejection', event => errors.push(String(event.reason)));
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const find = selector => { const element = document.querySelector(selector); if (!element) throw new Error(`Élément absent : ${selector}`); return element; };
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (check, label, timeout = 5000) => { const start = Date.now(); while (!check()) { if (Date.now() - start > timeout) throw new Error(`Délai dépassé : ${label}`); await pause(50); } };
  const check = async (name, operation) => { try { await operation(); results.push({ name, passed: true }); } catch (error) { results.push({ name, passed: false, message: error.message }); } };
  const rect = element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, bottom: r.bottom, right: r.right }; };
  const onscreen = element => { const r = element.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.x >= -1 && r.y >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1; };
  const key = async name => {
    const event = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { document.removeEventListener('keydown', listener, true); reject(new Error(`Touche native ${name} non reçue`)); }, 5000);
      function listener(event) { if (event.key !== name) return; document.removeEventListener('keydown', listener, true); clearTimeout(timer); resolve(event); }
      document.addEventListener('keydown', listener, true);
    });
    await window.aquahub.request('test.report', { phase: 'await_key', sequence: ++sequence, key: name });
    const received = await event; assert(received.isTrusted, 'La touche n’est pas issue du clavier natif'); await pause(100);
  };
  try {
    find('.sidebar [data-page="overview"]').click();
    await wait(() => document.querySelectorAll('.service-row').length === 8, 'interface chargée');
    await check('Le menu reste accessible à 100 % et 200 % sans débordement horizontal', async () => {
      const sidebar = find('.sidebar'); const settings = find('.sidebar [data-page="settings"]'); const theme = find('#theme-toggle');
      settings.focus(); await pause(50); assert(onscreen(settings), 'Réglages inaccessible après focus clavier');
      theme.focus(); await pause(50); assert(onscreen(theme), 'Thème inaccessible après focus clavier');
      assert(document.documentElement.scrollWidth <= innerWidth + 1, 'Débordement horizontal de la fenêtre');
      measurements.push({ page: 'sidebar', viewport: { width: innerWidth, height: innerHeight, devicePixelRatio }, sidebar: { ...rect(sidebar), scrollHeight: sidebar.scrollHeight, overflowY: getComputedStyle(sidebar).overflowY }, settings: rect(settings), theme: rect(theme) });
    });
    await check('Les cinq vues et toutes leurs actions visibles restent atteignables après défilement', async () => {
      for (const page of ['overview', 'updates', 'drones', 'history', 'settings']) {
        find(`.sidebar [data-page="${page}"]`).click(); await pause(180);
        const content = find('#page-content');
        const controls = [...content.querySelectorAll('button,input,select,summary,a[href]')].filter(element => !element.disabled && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0);
        let checked = 0;
        for (const control of controls) {
          if (!control.isConnected) continue;
          control.focus(); control.scrollIntoView({ block: 'center', inline: 'nearest' }); await pause(15);
          if (!control.isConnected) continue;
          assert(document.activeElement === control, `${page} : focus impossible pour ${control.textContent.trim().slice(0, 45) || control.id}`);
          assert(onscreen(control), `${page} : action coupée après défilement (${control.textContent.trim().slice(0, 45) || control.id})`); checked++;
        }
        assert(document.documentElement.scrollWidth <= innerWidth + 1, `${page} : débordement horizontal`);
        measurements.push({ page, visibleActionsChecked: checked, scrollWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth });
      }
    });
    await check('Les cinq vues sont activables au clavier natif Tab puis Entrée', async () => {
      const pages = [['overview', '.brand'], ['updates', '.sidebar [data-page="overview"]'], ['drones', '.sidebar [data-page="updates"]'], ['history', '.sidebar [data-page="drones"]'], ['settings', '.sidebar [data-action="open-portainer"]']];
      for (const [page, previous] of pages) {
        find(previous).focus(); await key('Tab');
        assert(document.activeElement.dataset.page === page, `Tab ne rejoint pas ${page}`);
        assert(onscreen(document.activeElement), `${page} hors écran après Tab natif`);
        await key('Enter');
        assert(find(`.sidebar [data-page="${page}"]`).getAttribute('aria-current') === 'page', `Entrée n’active pas ${page}`);
        assert(document.activeElement.id === 'page-content', `Le contenu ${page} ne reçoit pas le focus`);
      }
    });
    await check('La modale garde ses actions visibles, piège Tab et se ferme avec Échap sans lancer de tâche', async () => {
      find('.sidebar [data-page="overview"]').click();
      const restart = find('[data-action="restart"]'); restart.focus(); restart.scrollIntoView({ block: 'center' });
      await key('Enter'); await wait(() => find('#confirmation').open, 'confirmation ouverte');
      const cancel = find('#confirmation button[value="cancel"]');
      assert(document.activeElement === cancel, 'Annuler ne reçoit pas le focus initial');
      assert(onscreen(cancel), 'Annuler coupé à 200 %');
      await key('Tab'); assert(document.activeElement.id === 'dialog-confirm', 'Tab ne rejoint pas la confirmation');
      assert(onscreen(document.activeElement), 'Confirmation coupée à 200 %');
      await key('Tab'); assert(document.activeElement.id === 'dialog-close', 'Le focus sort de la modale');
      await key('Escape'); assert(!find('#confirmation').open, 'Échap ne ferme pas la modale');
      assert(document.activeElement.dataset.action === 'restart', 'Le focus ne revient pas au bouton de départ');
      assert(!document.querySelector('.job-banner'), 'Une opération a été lancée après Échap');
    });
    await check('Le contraste clair et sombre conserve les mêmes dimensions et actions', async () => {
      find('#theme-toggle').focus(); const before = document.documentElement.dataset.theme;
      await key(' '); assert(document.documentElement.dataset.theme !== before, 'Espace ne bascule pas le thème');
      assert(onscreen(find('#theme-toggle')), 'Bascule de thème hors écran');
      assert(document.documentElement.scrollWidth <= innerWidth + 1, 'Le thème produit un débordement');
    });
    await check('Aucune erreur JavaScript pendant les parcours de zoom et clavier', async () => assert(errors.length === 0, errors.join('; ')));
    find('.sidebar [data-page="overview"]').click(); window.scrollTo(0, 0); find('.sidebar').scrollTop = 0;
    await pause(200);
  } catch (error) { results.push({ name: 'Parcours interrompu', passed: false, message: String(error) }); }
  await window.aquahub.request('test.report', { phase: 'complete', mode: 'native-layout-keyboard-demo', viewport: { width: innerWidth, height: innerHeight, devicePixelRatio }, measurements, results, errors, passed: results.every(result => result.passed), notice: 'Touches réellement injectées par XTest dans la fenêtre WebKitGTK isolée. Données de démonstration uniquement.' });
})();
