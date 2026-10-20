VALIDER L’INTERFACE AQUAHUB

Depuis la racine du dépôt :
  npm --prefix frontend ci
  npm --prefix frontend run check
  npm --prefix frontend test
  npm --prefix frontend run build

Les tests natifs utilisent WebKitGTK, GTK, Xvfb et XTest. Ils supposent
les dépendances de développement décrites dans docs/REPRISE_DEVELOPPEMENT.txt.

Pannes et pagination, transport Unix factice, aucun matériel contacté :
  python3 frontend/tests/run_fixtures.py

Mise à jour de l’application (Releases GitHub séparées du bundle) :
  python3 frontend/tests/run_updates_feedback.py
Ce banc vérifie téléchargement avant installation, annulation, confirmation,
perte de l’agent pendant sa mise à jour, reprise, résultat installé ou échoué,
absence de réessai automatique et rendu inerte des notes. Il ne contacte
pas GitHub et n’installe aucun paquet réel.

Clavier natif et disposition des cinq vues :
  xvfb-run -a python3 frontend/tests/run_layout.py --zoom 1
  xvfb-run -a python3 frontend/tests/run_layout.py --zoom 2
  xvfb-run -a python3 frontend/tests/run_layout.py --zoom 1 --scale 2

Secrets dans les formulaires GTK, jamais dans WebKit :
  xvfb-run -a python3 frontend/tests/run_portainer_native.py
  xvfb-run -a python3 frontend/tests/run_source_native.py
Ces deux bancs saisissent uniquement un secret synthétique dans GTK et
utilisent un socket privé factice. Aucun compte réel n’est créé.

Le parcours ui-webkit.js s’exécute dans l’application avec --demo,
--test-script et --test-output. Les configurations, redémarrages et mises
à jour sont des simulations explicitement signalées. Il vérifie aussi
le numéro du drone, la progression observée de la configuration, l’attente
indéterminée pendant le redémarrage et les cinq contrôles manuels
accessibles au survol, au focus clavier et au clic.

ui-live-readonly.js lit une installation réelle sans lancer de maintenance.
Son getter des réglages SSH reste soumis à PolicyKit : un test sans session
d’autorisation peut ne pas terminer ce contrôle. Ne pas désactiver la
politique de production pour faire passer ce banc.

Les rapports JSON, captures et répertoires .runtime générés restent locaux
et sont exclus du dépôt public. Distinguer les résultats des fixtures des
essais réels d’installation et de configuration du matériel.
