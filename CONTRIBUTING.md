# Contribuer à AquaHub

Lire [le guide de reprise](docs/REPRISE_DEVELOPPEMENT.txt), puis choisir un
chantier dans [la feuille de route](docs/ETAT_ET_FEUILLE_DE_ROUTE.txt). Le projet
vise Debian 13 amd64 et doit rester utilisable graphiquement chez le client.

## Préparer une modification

Créer une branche depuis `main`, décrire le problème et le comportement attendu.
Garder la modification centrée sur un besoin. Un changement de documentation
se vérifie par relecture ; un changement fonctionnel doit être accompagné du
test qui démontre son effet ou prévient sa régression.

Les commandes de validation sont dans [TESTS.txt](docs/TESTS.txt). Pour une
modification frontend, exécuter au minimum le contrôle TypeScript, les tests
frontend et la compilation. Pour une modification de l’agent, de l’IPC ou du
paquet, exécuter la suite Python adaptée. Les fixtures ne remplacent pas la
recette réelle d’un installateur ou d’un appareil.

## Invariants à préserver

- Réutiliser les fonctions connues du bundle fournisseur ; vérifier la
  compatibilité avant une mutation.
- Préserver les adresses et routes configurées. Ne pas contourner les gardes
  réseau pour faire réussir un test.
- Lier toute configuration de drone à une cible et une identité vérifiées.
- Garder les opérations privilégiées derrière l’agent et PolicyKit.
- Afficher les logs comme du texte et conserver les secrets hors de WebKit.
- Conserver un résultat incomplet ou échoué quand une vérification manque.
- Distinguer la mise à jour d’AquaHub, celle du bundle et le provisioning.

## Ouvrir une pull request

Décrire le problème, le comportement après modification et les vérifications
effectuées. Indiquer clairement les tests non exécutés qui concernent le
changement. Ne pas joindre d’export réel contenant une configuration client,
une identité matérielle, un jeton ou des journaux non expurgés.

Relire aussi la [politique du dépôt public](docs/CONFIDENTIALITE_DEPOT_PUBLIC.txt)
avant de joindre des exemples ou des captures. Employer des rôles génériques,
des données fictives et des chemins de documentation ; vérifier les métadonnées
des images et des commits. Un fichier ignoré par Git ou un log déjà filtré
n’est pas automatiquement prêt à être rendu public.

Le code et la documentation proposés à ce dépôt sont destinés à être distribués
sous la [licence MIT du projet](LICENSE). Conserver les notices des composants
tiers lorsqu’ils sont utilisés.
