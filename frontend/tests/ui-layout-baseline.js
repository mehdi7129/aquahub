(async () => {
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  await pause(400);
  const sidebar = document.querySelector('.sidebar');
  const settings = document.querySelector('.sidebar [data-page="settings"]');
  const theme = document.querySelector('#theme-toggle');
  const rect = element => { const value = element.getBoundingClientRect(); return { x: value.x, y: value.y, width: value.width, height: value.height, bottom: value.bottom, right: value.right }; };
  await window.aquahub.request('test.report', { mode: 'native-layout-baseline', viewport: { width: innerWidth, height: innerHeight, devicePixelRatio }, document: { scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }, sidebar: { ...rect(sidebar), scrollHeight: sidebar.scrollHeight, clientHeight: sidebar.clientHeight, overflowY: getComputedStyle(sidebar).overflowY }, settings: rect(settings), theme: rect(theme), defect: settings.getBoundingClientRect().bottom > innerHeight || theme.getBoundingClientRect().bottom > innerHeight });
})();
