# AquaHub

AquaHub est une application de bureau pour superviser les services d’un poste
Aqua Hub, consulter leurs journaux et lancer les opérations de maintenance
prises en charge par le bundle installé.

Interface monochrome, navigation latérale, thèmes clair et sombre. La cible
actuelle est **Debian 13 amd64 avec GNOME**. Le code AquaHub est distribué sous
[licence MIT](LICENSE).

**État : préproduction 0.2.4.** Les sources et les tests sont disponibles ;
aucun installateur public qualifié n’est encore proposé. Le bundle fournisseur,
ses images et ses configurations privées doivent être obtenus séparément.

![Interface AquaHub en mode démonstration, avec données fictives](docs/assets/aquahub-demo.png)

Capture de l’interface 0.2.2 avec données fictives uniquement.
[Voir le mode sombre](docs/assets/aquahub-demo-dark.png).

## Fonctionnalités

| Fonction | État dans 0.2.4 | Limite à connaître |
| --- | --- | --- |
| Supervision | Huit services reconnus, contours colorés et détail par service | État du conteneur et santé distincts ; données anciennes neutralisées |
| Relances | Tâche ciblée visible, résultat et observation après intervention | WireGuard reste protégé ; toutes les actions réseau ne sont pas ouvertes |
| Journaux et historique | Logs techniques, événements, extraits de panne et exports | Collecte et conservation bornées ; aucun historique exhaustif garanti |
| Aqua_Drones | Inventaire, identité SSH, précontrôle et configuration ciblée sans mot de passe administrateur du PC | Compte AquaHub autorisé, identité confirmée, un appareil à la fois ; association et réglages SSH restent protégés |
| Progression | Numéro du drone, étapes validées et remplissage conservé pendant l’attente | Les cinq contrôles manuels restent séparés |
| Outils complémentaires | Accès à GCS, Fleet Controller et Portainer | Le compte Portainer est distinct des opérations courantes AquaHub |
| Mise à jour AquaHub | Recherche de Releases, téléchargement vérifié et installation confirmée | Un commit ou un ZIP de sources ne suffit pas ; aucun installateur public qualifié publié à ce stade |
| Mise à jour du bundle | Inspection et application des livraisons `image-refresh-v1` reconnues | Le futur flux Git + registre du fournisseur n’est pas encore implémenté |
| Installation | Constructeur de paquet applicatif et assistant de kit complet hors ligne | Kit fournisseur/client externe, privé s’il contient des configurations ou secrets |
| Identité dans le bureau | Nom AquaHub, identifiant natif et icône alignés sur le lanceur existant | Fermer l’ancienne fenêtre avant de rouvrir après mise à niveau ; portée des contrôles GNOME dans les tests |

Les adresses IP existantes ne sont pas administrées par l’interface. Les actions
réseau sans précontrôle suffisant restent bloquées. Les contrôles manuels des
drones ne sont pas validés automatiquement.

Les états utilisent des contours discrets vert, rouge, orange ou gris ; une
observation périmée n’est pas affichée comme un service actuellement en cours.
Les relances de Réseau du bundle et GCS Reactor comparent les adresses et les
routes avant et après. WireGuard reste protégé en attendant la validation de
son comportement d’échec. La santé fonctionnelle reste distincte de l’état.

## Évolutions récentes

| Version | Changements principaux |
| --- | --- |
| 0.2.0 | Canal applicatif séparé du bundle, paquet léger, suivi nommé des configurations et détail des contrôles manuels |
| 0.2.1 | Précontrôle sans mot de passe administrateur, exports des logs, relances réseau ciblées et supervision simplifiée |
| 0.2.2 | Barre de progression corrigée, retour visuel des relances, gestion de l’ancien cache et des réponses perdues, titres et espacements harmonisés |
| 0.2.3 | Identité native, nom et icône du bureau harmonisés ; assistant distinct sans second raccourci dans le menu |
| 0.2.4 | Configuration d’un Aqua_Drone sans demande de mot de passe administrateur du PC, avec contrôles de cible et de maintenance conservés |

Les [notes de version](CHANGELOG.md) décrivent le code livré. Les
[résultats de validation](docs/TESTS.txt) séparent tests génériques, parcours
graphiques, recettes de paquet et essais matériels. La recette 0.2.4 documente
272 tests Python réussis et huit ignorés sans options, 28 tests de domaine
JavaScript, une configuration synthétique sans demande administrateur et
une mise à niveau du paquet applicatif. Les résultats d’identité native 0.2.3
restent consignés séparément.
Ces résultats ne qualifient pas une flotte ou un kit client générique.

## Prochains chantiers

- **Consolidation V1 et reconnexion Ethernet** : diagnostiquer la perte
  d’Internet après rebranchement du RJ45, puis qualifier une reprise sans
  reboot ; préparer aussi la configuration ciblée de flotte et la récupération
  après incident. Voir le [plan consolidé](docs/PLAN_V1_ET_RECONNEXION_ETHERNET_2026-10-22.txt).
  Aucune modification des scripts fournisseur ou des IP n’est autorisée par ce plan.
- **Bundle fournisseur v1.0.1** : préparer le flux Git + registre, préserver
  les configurations client et qualifier le correctif WireGuard. Voir le
  [plan distinct](docs/PLAN_INTEGRATION_BUNDLE_V101_2026-10-21.txt).
- **Distribution commerciale** : qualifier le kit générique, les artefacts
  publics et les signatures. Voir la [feuille de route](docs/ETAT_ET_FEUILLE_DE_ROUTE.txt).

## Reprendre le développement

Commencer par [le guide de reprise](docs/REPRISE_DEVELOPPEMENT.txt). Il décrit
le poste de développement, les commandes, les points d’entrée et les pièges à
éviter. Les commandes ci-dessous s’exécutent sur un poste de développement,
depuis la racine du dépôt ; elles ne sont pas le parcours d’installation client.
Lire ensuite les [décisions techniques](docs/DECISIONS_TECHNIQUES.txt) pour
comprendre les choix à préserver, puis le document lié au chantier choisi.

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
| Comprendre les choix et leurs raisons | [Décisions techniques](docs/DECISIONS_TECHNIQUES.txt) |
| Respecter les droits, les identités et le réseau | [Sécurité](docs/SECURITE.txt) |
| Préparer un contenu publiable sans données privées | [Confidentialité du dépôt](docs/CONFIDENTIALITE_DEPOT_PUBLIC.txt) |
| Exécuter les tests et comprendre leur portée | [Tests](docs/TESTS.txt) |
| Comprendre le protocole local et les tâches | [API et données](docs/API_ET_DONNEES.txt) |
| Raccorder une livraison fournisseur | [Contrat du bundle](docs/CONTRAT_BUNDLE.txt) |
| Préparer l’intégration fournisseur v1.0.1 | [Plan complémentaire](docs/PLAN_INTEGRATION_BUNDLE_V101_2026-10-21.txt) |
| Comprendre les corrections d’affichage 0.2.2 | [Plan des corrections visuelles](docs/PLAN_CORRECTIONS_VISUELLES_2026-10-21.txt) |
| Corriger le nom et l’icône dans GNOME | [Plan d’identité bureau](docs/PLAN_IDENTITE_BUREAU_2026-10-21.txt) |
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
