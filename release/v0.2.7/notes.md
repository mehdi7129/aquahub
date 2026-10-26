AquaHub 0.2.7 — première préversion publique, Debian 13 amd64 / GNOME.

Le fichier `aquahub-application_0.2.7_amd64.deb` met à jour une installation
AquaHub existante. Il conserve le bundle installé, Docker, Portainer, les
configurations et le kit de réparation local. Ce fichier est le paquet exact
qualifié et installé lors de la recette 0.2.7 ; il n’a pas été reconstruit pour
sa publication. Taille : 162512 octets.

Cette version ajoute la vérification et la réactivation ciblée du tunnel
WireGuard. Le compte opérateur autorisé peut réactiver le profil existant sans
mot de passe PC, avec confirmation et contrôles d’appartenance/maintenance.
La configuration des Aqua_Drones conserve aussi sa délégation sans mot de passe
PC et le hub actif. Aucun script fournisseur ni paramètre IP n’est réécrit.

Pour installer la mise à jour : télécharger le .deb, l’ouvrir avec le
gestionnaire de paquets graphique Debian, valider l’installation, puis fermer
et rouvrir AquaHub. Voir `INSTALLATION.txt`. L’autorisation système de
l’installation est distincte de la réactivation courante de WireGuard.

Le kit complet pour un nouveau poste est fourni séparément : le .deb de mise
à jour ne contient ni Docker, ni images, ni bundle fournisseur, ni accès client.
Les archives automatiques « Source code » ne sont pas des installateurs.

Validation : 335 tests Python locaux avec préconditions explicites, 38 tests
JavaScript, TypeScript/build, inspection du paquet, mise à niveau réelle et
appel depuis l’opérateur sur tunnel déjà actif. Les recettes réseau détaillées
et leurs limites sont dans `docs/TESTS.txt`. Cette livraison ne démontre pas
une liaison distante ni le fonctionnement d’une flotte physique.

Les fichiers `SHA256SUMS` et `provenance.json` identifient les octets et leur
source. Le SHA-256 du .deb est
`defba0618c8bf94318b957d78d51bd3bae63f139245642d72b226254a6539748`.
Aucune signature indépendante d’éditeur n’est annoncée pour cette préversion.

Cette Release est marquée « Pre-release ». Le bouton de mise à jour actuel
d’AquaHub propose uniquement les Releases normales ; il ignore volontairement
les préversions. Le téléchargement manuel reste disponible sur cette page.
