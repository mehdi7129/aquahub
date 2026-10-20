(async () => {
  document.querySelector('.sidebar [data-page="overview"]').click();
  await new Promise(resolve => setTimeout(resolve, 250));
  document.querySelector('[data-action="restart"]').click();
  await new Promise(resolve => setTimeout(resolve, 300));
  const rect = element => { const r = element.getBoundingClientRect(); return { x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right }; };
  const modal = document.querySelector('#confirmation');
  const cancel = modal.querySelector('[value="cancel"]');
  const confirm = modal.querySelector('#dialog-confirm');
  await window.aquahub.request('test.report', { mode:'native-modal-before-fix',viewport:{width:innerWidth,height:innerHeight,devicePixelRatio},modal:{...rect(modal),scrollHeight:modal.scrollHeight,scrollTop:modal.scrollTop},cancel:rect(cancel),confirm:rect(confirm),focused:document.activeElement.textContent,defect:cancel.getBoundingClientRect().bottom>innerHeight });
})();
