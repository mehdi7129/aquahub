(async () => {
  if (location.hash !== '#overview') document.querySelector('[data-page="overview"]').click();
  if (document.documentElement.dataset.theme !== 'dark') document.querySelector('#theme-toggle').click();
  document.querySelector('[data-action="detail-events"]')?.click();
  await new Promise(resolve => setTimeout(resolve, 600));
  await window.aquahub.request('test.report', { mode: 'live-read-only-screenshot', theme: document.documentElement.dataset.theme, services: document.querySelectorAll('.service-row').length, passed: document.querySelectorAll('.service-row').length === 8 && document.documentElement.dataset.theme === 'dark' });
})();
