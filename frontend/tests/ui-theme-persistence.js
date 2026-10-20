(async () => {
  const theme = document.documentElement.dataset.theme;
  const stored = localStorage.getItem('aquahub-theme');
  await window.aquahub.request('test.report', { mode: 'native-theme-new-process', theme, stored, passed: theme === 'dark' && stored === 'dark', notice: 'Un processus natif distinct avait choisi le thème sombre puis fermé sa fenêtre.' });
})();
