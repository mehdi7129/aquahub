# AquaHub

AquaHub est une application de bureau pour superviser les services d’un poste
Aqua Hub, consulter leurs journaux et lancer les opérations de maintenance
prises en charge par le bundle installé.

Interface monochrome, navigation latérale, thèmes clair et sombre. La cible
actuelle est **Debian 13 amd64 avec GNOME**. Le code AquaHub est distribué sous
[licence MIT](LICENSE).

**État : préproduction 0.2.2.** Les sources et les tests sont disponibles ;
aucun installateur public qualifié n’est encore proposé. Le bundle fournisseur,
ses images et ses configurations privées doivent être obtenus séparément.

![Interface AquaHub en mode démonstration, avec données fictives](docs/assets/aquahub-demo.png)

## Fonctionnalités

- Supervision des huit composants reconnus du bundle, avec état du conteneur
  distinct de son contrôle de santé.
- Détail d’un service, redémarrages autorisés et tâches de maintenance suivies.
- Journaux techniques, événements persistants et extraits de panne bornés,
  avec enregistrement des logs d’un service ou export groupé.
- Accès aux interfaces GCS, Fleet Controller et Portainer.
- Inventaire Aqua_Drones, identité SSH, précontrôles et configuration d’une cible
  à partir des fonctions du fournisseur.
- Suivi de configuration par appareil, progression fondée sur les étapes
  observées et rappel détaillé des cinq contrôles manuels.
- Précontrôles Aqua_Drone sans demande de mot de passe administrateur,
  avec identité SSH et exclusion des interventions concurrentes conservées.
- Recherche, téléchargement et installation confirmée des mises à jour de
  l’application depuis les Releases GitHub officielles, séparées du bundle.
- Inspection d’archives de mise à jour et application des seules livraisons
  répondant au contrat `image-refresh-v1`.

Les adresses IP existantes ne sont pas administrées par l’interface. Les actions
réseau sans précontrôle suffisant restent bloquées. Les contrôles manuels des
drones ne sont pas validés automatiquement.

Les états utilisent des contours discrets vert, rouge, orange ou gris ; une
observation périmée n’est pas affichée comme un service actuellement en cours.
Les relances de Réseau du bundle et GCS Reactor comparent les adresses et les
routes avant et après. WireGuard reste protégé en attendant la validation de
son comportement d’échec. La santé fonctionnelle reste distincte de l’état.

## Reprendre le développement

Commencer par [le guide de reprise](docs/REPRISE_DEVELOPPEMENT.txt). Il décrit
le poste de développement, les commandes, les points d’entrée et les pièges à
éviter. Les commandes ci-dessous s’exécutent sur un poste de développement,
depuis la racine du dépôt ; elles ne sont pas le parcours d’installation client.

```sh
cd frontend
npm ci
npm run check
npm test
npm run build
cd ..
PYTHONPATH=agent:desktop /usr/bin/python3 -m unittest discover -s tests -q
```

Pour découvrir l’interface avec des données fictives, sans agent ni service réel :

```sh
PYTHONPATH=agent:desktop /usr/bin/python3 -m aquahub_desktop \
  --demo --no-agent \
  --socket /tmp/aquahub-demo-unused.sock \
  --state-dir ./local-private/demo
```

Les dépendances système, dont GTK, WebKitGTK et les outils de compilation,
sont listées dans le guide. La fenêtre se lance avec un compte utilisateur,
sans `sudo`. Le mode de démonstration n’est pas une validation matérielle.

## Architecture

```mermaid
flowchart TD
  UI[Interface TypeScript locale] --> GTK[Fenêtre GTK et WebKitGTK]
  GTK -->|JSON sur socket Unix| AGENT[Agent AquaHub et PolicyKit]
  AGENT --> DB[Historique et tâches SQLite]
  AGENT --> DOCKER[Moteur Docker natif]
  AGENT --> BUNDLE[Fonctions du bundle fournisseur]
  BUNDLE -->|SSH avec identité vérifiée| DRONES[Aqua_Drones]
  DOCKER --> PORTAINER[Console Portainer locale]
```

Le renderer ne reçoit pas le socket Docker, les clés SSH ou les jetons. Le
produit installé utilise un agent système ; le mode de développement possède
des chemins d’état distincts. Les invariants sont détaillés dans
[l’architecture](docs/ARCHITECTURE.txt) et [la sécurité](docs/SECURITE.txt).

## Documentation

| Besoin | Document |
| --- | --- |
| Installer le poste de dev et lancer la démo | [Reprise du développement](docs/REPRISE_DEVELOPPEMENT.txt) |
| Comprendre les modules et les flux | [Architecture](docs/ARCHITECTURE.txt) |
| Respecter les droits, les identités et le réseau | [Sécurité](docs/SECURITE.txt) |
| Exécuter les tests et comprendre leur portée | [Tests](docs/TESTS.txt) |
| Comprendre le protocole local et les tâches | [API et données](docs/API_ET_DONNEES.txt) |
| Raccorder une livraison fournisseur | [Contrat du bundle](docs/CONTRAT_BUNDLE.txt) |
| Préparer l’intégration fournisseur v1.0.1 | [Plan complémentaire](docs/PLAN_INTEGRATION_BUNDLE_V101_2026-10-21.txt) |
| Comprendre les corrections d’affichage 0.2.2 | [Plan des corrections visuelles](docs/PLAN_CORRECTIONS_VISUELLES_2026-10-21.txt) |
| Construire un paquet et préparer une publication | [Construction et publication](docs/PUBLICATION.txt) |
| Résoudre un problème sur le poste de dev | [Dépannage](docs/DEPANNAGE.txt) |
| Choisir le prochain chantier | [État et feuille de route](docs/ETAT_ET_FEUILLE_DE_ROUTE.txt) |
| Utiliser les fonctions de la GUI | [Guide utilisateur](docs/GUIDE_UTILISATEUR.txt) |
| Proposer une modification | [Contribution](CONTRIBUTING.md) |

## Mises à jour

L’onglet **Mises à jour** distingue deux canaux :

- **Application AquaHub** consulte les Releases publiques de
  `mehdi7129/aquahub`. Un commit, une branche ou une archive de sources ne
  constitue pas une mise à jour installable. Le paquet attendu est
  `aquahub-application_X.Y.Z_amd64.deb` ; il remplace uniquement AquaHub sur
  un poste déjà initialisé. Le téléchargement est vérifié, puis
  l’installation nécessite une confirmation et l’autorisation système.
- **Bundle Aqua Hub** concerne les services et configurations du fournisseur.
  Son URL et ses accès restent distincts. Un jeton de déploiement Git et
  registre Docker ne donne pas accès à l’API des Releases GitLab. La procédure
  fournisseur Git + registre reste à qualifier.

Le canal applicatif vérifie le SHA-256 fourni dans les métadonnées GitHub
reçues par HTTPS et les champs du paquet Debian. Il ne possède pas encore
une signature d’éditeur indépendante de GitHub. Aucune Release publique ni
installation complète publique qualifiée n’est annoncée par ces sources.
Voir les [notes de version](CHANGELOG.md) et la
[procédure de publication](docs/PUBLICATION.txt).

Le constructeur actuel produit un kit d’installation complet à partir d’un
payload externe. Un kit contenant des clés ou des configurations client reste
privé et ne doit jamais être joint à une publication publique. Une licence MIT
sur AquaHub ne change pas les licences des composants tiers.

## Licence

[MIT](LICENSE), pour le code AquaHub présent dans ce dépôt. Les dépendances et
les logiciels du fournisseur conservent leurs propres licences ; voir
[les composants tiers](THIRD_PARTY.txt).
