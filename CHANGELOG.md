# Notes de version

Les versions ci-dessous décrivent le code. Une entrée ne signifie pas qu’un
installateur public ou une Release GitHub qualifiée est déjà disponible.

## 0.2.6 — réactivation ciblée de WireGuard, préproduction

- Supervision séparée du conteneur, du tunnel local et des échanges récents.
  Aucun handshake n’est exigé pour reconnaître un tunnel local actif.
- Réactivation du profil existant lorsque le tunnel a disparu ou que son
  interface reconnue a perdu une adresse/route attendue ; confirmation et
  tâche ciblée, sans redémarrage de l’ancien conteneur.
- Scripts et configurations fournisseur conservés, copie privée figée,
  contrôle de l’appartenance de l’interface et des invariants hors tunnel.
- Exécution bornée dans le conteneur, état persistant de commande en cours
  et gardes de maintenance, y compris pour l’installation d’AquaHub.
- Refus des profils et instances hors du contrat qualifié ; le démarrage
  complet du conteneur historique reste protégé. Voir docs/WIREGUARD.txt.

## 0.2.5 — configuration ciblée et qualification V1, préproduction

- Le hub actif reste intégralement conservé pendant la configuration d’un
  drone. Les scripts fournisseur inchangés génèrent leur fichier de hub dans
  un miroir privé régulier ; seules les configurations de la cible sont envoyées.
- Toutes les sections et options requises sont comparées avant l’opération.
  Les autres appareils déjà couverts sont conservés ; une extension qui exige
  de modifier la configuration réseau reste refusée. Aucun script ni IP modifié.
- Lectures MAVLink bornées, fichiers réguliers sans suivi de lien ; refus des
  configurations ambiguës et vérification de l’empreinte initiale du hub.
- Capture privée et bornée du lien Ethernet, NetworkManager, routage, DNS et
  sondes facultatives. Outil développeur en lecture seule ; aucune réparation
  de la perte d’Internet après reconnexion n’est annoncée.
- Workflow Debian 13 des tests génériques et du paquet extrait, sans accès
  fournisseur, Docker réel ou installation. La réussite locale et le statut
  d’exécution GitHub restent des preuves distinctes.

## 0.2.4 — configuration sans mot de passe administrateur du PC, préproduction

- Configuration d’un Aqua_Drone accessible au compte opérateur autorisé sans
  demande PolicyKit. La confirmation du numéro et de l’identité, le précontrôle
  récent à usage unique, les comparaisons réseau et les verrous restent actifs.
- Les réglages et associations SSH, les services, les mises à jour et les
  autres opérations administratives conservent leurs autorisations propres.
  Aucun changement de règle système globale, d’IP ou de script fournisseur.
- Tests du dispatch privilégié, avec configuration synthétique complète et
  refus des demandes non autorisées ou incompatibles. Voir docs/TESTS.txt.

## 0.2.3 — identité bureau, préproduction

- Nom humain AquaHub, nom de programme et identifiant GTK harmonisés avec
  le lanceur existant, avant l’initialisation GTK. L’icône réellement livrée
  est utilisée par la fenêtre et ses dialogues d’erreur précoces.
- Lanceur principal et favoris conservés ; contrat d’identité X11 explicite
  et contrôle distinct du fonctionnement sous Wayland.
- Assistant de première installation identifié séparément, avec une entrée
  desktop masquée du menu pour éviter un second raccourci utilisateur.
- Recettes natives d’identité, résolution d’icône et instance unique, puis
  qualification du paquet applicatif. Aucun changement des scripts fournisseur.
- Mise à niveau depuis 0.2.2 et réinstallation en VM ; installation sur le
  poste et lancement par son entrée desktop contrôlés. Les observations
  automatiques du bureau ne couvrent pas une revue visuelle complète d’Alt-Tab.

## Documentation — 21 octobre 2026

- État fonctionnel 0.2.2 et limites détaillés dans le README, avec captures
  de démonstration récentes en clair et sombre.
- Architecture, dépannage, reprise du développement et exemples de
  construction actualisés ; décisions techniques regroupées et expliquées.
- Plan de correction du nom et de l’icône dans le bureau, avec critères
  GNOME/Wayland/X11, instance unique et recette de paquet. Correction
  proposée seulement : la version du code reste 0.2.2.
- Revue du dépôt public et des objets Git locaux, neutralisation des mentions
  personnelles dans les documents courants et exclusions renforcées. La portée
  du contrôle et les anciennes mentions conservées dans l’historique sont
  précisées dans docs/CONFIDENTIALITE_DEPOT_PUBLIC.txt.

## 0.2.2 — correctif d’interface, préproduction

- Barre de configuration conservant le pourcentage acquis pendant les
  attentes, avec indication d’activité séparée et sans estimation fictive.
- Relance visible sur le service concerné ; ancien cache neutralisé jusqu’à
  une observation postérieure à la tâche. Contours des états à 2 px.
- Titres Application AquaHub et Bundle Aqua Hub harmonisés, résultats de
  recherche et actions de Réglages espacés, icône d’actualisation plus aérée.
- Mentions de l’application harmonisées en AquaHub. Switch de thème au
  rendu explicite, sans apparence native ni animation de transformation ;
  artefact utilisateur non reproduit pendant les captures de vérification.
- Analyse de la réponse fournisseur v1.0.1 et plan complémentaire séparé :
  aucune modification de ses scripts ni intégration de ce nouveau bundle
  dans ce correctif.

## 0.2.1 — préproduction

- États des services avec contours colorés, neutralisés lorsque les données
  sont périmées. Santé secondaire et alerte lorsqu’une sonde signale un échec.
- Vérification d’un Aqua_Drone sans mot de passe administrateur du PC,
  avec contrôle de l’UID, identité SSH et exclusion de maintenance conservés.
- Relance individuelle de Réseau du bundle et GCS Reactor, avec comparaison
  des adresses et routes IPv4/IPv6 ; WireGuard reste protégé.
- Mise à jour de l’application présentée sans URL technique. Recherche en
  ligne du bundle en premier et import d’une archive locale en bas de page.
  L’adaptateur Git + registre du fournisseur attend encore sa qualification.
- Enregistrement natif des logs d’un service et archive groupée des huit
  services, avec filtres, limites et résultats partiels explicites.

## 0.2.0 — préproduction

- Canal **Application AquaHub** distinct des livraisons du bundle : recherche
  de Releases stables publiques sur `mehdi7129/aquahub`, notes, téléchargement
  vérifié et installation confirmée avec autorisation système.
- Paquet applicatif `aquahub-application_X.Y.Z_amd64.deb`, sans bundle ni
  image Docker, réservé aux postes déjà initialisés. Helper système indépendant
  pour remplacer AquaHub et redémarrer son agent sans relancer les services
  du fournisseur ; résultat persistant, reprise d’état interrompu et absence
  de réexécution automatique de l’installation.
- Suivi de configuration nommé `Aqua_Drone_N`, douze jalons observés, barre
  d’attente pendant les étapes longues et pourcentage sans estimation fictive.
- Détail des cinq contrôles manuels accessible au survol, au clavier et au
  clic. Les deux rappels réseau sont consultatifs, sans modification d’IP.
- Explications de l’état de santé, des services réseau protégés, des accès
  GitLab et du rôle facultatif de la console Portainer. Sous-titre latéral
  « Espace de pilotage » retiré.
- Documentation de reprise, API, sécurité, tests, utilisation et dépannage
  actualisée. Le travail de configuration reste séquentiel.

La vérification applicative actuelle utilise le SHA-256 des métadonnées
GitHub reçues par HTTPS ; aucune signature d’éditeur indépendante n’est
encore implémentée. Il n’existe pas de rollback automatique de l’application.
La qualification de chaque binaire et du kit de première installation reste
séparée des tests de sources. Le flux fournisseur Git + registre attend encore
sa procédure de mise à jour qualifiée.

## 0.1.2 — publication initiale des sources

Application GTK/WebKitGTK, interface monochrome avec navigation latérale,
supervision Docker, journaux, historique et extraits de panne, configuration
SSH d’une cible vérifiée, intégration Portainer et moteur de livraison
`image-refresh-v1`. Assistant de kit complet hors ligne dans les sources ;
payload fournisseur et données client exclus du dépôt public. Licence MIT
pour le code AquaHub.
