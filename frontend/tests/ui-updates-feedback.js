(async()=>{
 const results=[],errors=[];window.addEventListener('error',e=>errors.push(e.message));window.addEventListener('unhandledrejection',e=>errors.push(String(e.reason)));
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 const find=s=>{const el=document.querySelector(s);if(!el)throw Error(`Élément absent: ${s}`);return el;};
 const assert=(v,m)=>{if(!v)throw Error(m);};const click=s=>{const e=find(s);assert(!e.disabled,`Bouton désactivé: ${s}`);e.click();};
 const wait=async(f,m,t=12000)=>{const start=Date.now();while(!f()){if(Date.now()-start>t)throw Error(`Délai: ${m}`);await pause(70);}};
 const check=async(name,f)=>{try{await f();results.push({name,passed:true});}catch(e){results.push({name,passed:false,message:String(e)});}};
 await check('La source officielle de l’application reste distincte du dépôt fournisseur',async()=>{
   await wait(()=>document.querySelector('.service-row'),'supervision');click('[data-page="updates"]');
   await wait(()=>document.querySelector('.app-update-card'),'application');
   assert(!find('.app-update-card').textContent.includes('github.com'),'source technique de l’application visible');
   assert((await window.aquahub.request('settings.get')).github_url === 'https://gitlab.com/fournisseur/bundle','source fournisseur incorrecte');
   assert(!document.querySelector('.workspace-label'),'ancien sous-titre visible');
 });
 await check('Une erreur GitHub reste lisible et ne déclenche aucun téléchargement',async()=>{
   await window.aquahub.request('fixture.set',{mode:'check_error'});click('[data-action="check-app-update"]');
   await wait(()=>document.querySelector('.app-update-card .error'),'erreur GitHub');
   assert(find('.app-update-card .error').textContent.includes('Limite GitHub'),'message perdu');
   assert((await window.aquahub.request('fixture.stats')).downloads===0,'téléchargement inattendu');
 });
 await check('Le paquet doit être téléchargé et vérifié avant de proposer l’installation',async()=>{
   await window.aquahub.request('fixture.set',{mode:'release'});click('[data-action="check-app-update"]');
   await wait(()=>document.querySelector('[data-action="download-app-update"]'),'release');
   assert(find('[data-action="install-app-update"]').disabled,'installation avant téléchargement');
   assert(!window.pwned&&!document.querySelector('.release-notes img'),'notes HTML exécutées');
   click('[data-action="download-app-update"]');
   await wait(()=>!find('[data-action="install-app-update"]').disabled,'téléchargement vérifié');
 });
 await check('Annuler la confirmation ne programme aucune installation',async()=>{
   click('[data-action="install-app-update"]');await wait(()=>find('#confirmation').open,'confirmation');
   find('#confirmation button[value="cancel"]').click();
   assert((await window.aquahub.request('fixture.stats')).installs===0,'installation malgré annulation');
 });
 await check('La coupure de l’agent pendant la mise à jour bloque les doubles actions puis confirme le résultat',async()=>{
   click('[data-action="install-app-update"]');click('#dialog-confirm');
   await wait(()=>document.querySelector('.job-banner')?.textContent.includes('Installation d’AquaHub'),'installation programmée');
   assert(find('[data-action="check-app-update"]').disabled,'actions concurrentes disponibles');
   await wait(()=>document.querySelector('.app-update-card .error')?.textContent.includes('redémarre'),'coupure agent expliquée');
   await wait(()=>find('.app-update-card').textContent.includes('Fermez puis rouvrez AquaHub'),'installation confirmée');
   assert((await window.aquahub.request('fixture.stats')).installs===1,'installation relancée automatiquement');
   assert(!document.querySelector('.job-banner'),'tâche laissée en cours');
 });
 await check('Un échec d’installation libère l’interface en affichant sa cause',async()=>{
   await window.aquahub.request('fixture.set',{fail_next_install:true});click('[data-action="check-app-update"]');
   await wait(()=>document.querySelector('[data-action="download-app-update"]'),'seconde release');
   click('[data-action="download-app-update"]');await wait(()=>!find('[data-action="install-app-update"]').disabled,'seconde archive');
   click('[data-action="install-app-update"]');click('#dialog-confirm');
   await wait(()=>find('.app-update-card').textContent.includes('Échec synthétique du paquet.'),'échec confirmé');
   assert(!document.querySelector('.job-banner'),'interface restée bloquée');
   assert(!find('[data-action="check-app-update"]').disabled,'nouvelle recherche bloquée');
   assert((await window.aquahub.request('fixture.stats')).installs===2,'installation en échec réessayée automatiquement');
 });
 await check('Aucune erreur JavaScript non traitée',async()=>assert(!errors.length,errors.join(';')));
 await window.aquahub.request('test.report',{mode:'isolated-native-app-updates',passed:results.every(r=>r.passed),results,errors,notice:'Aucune requête réseau, aucun paquet réel installé.'});
})();
