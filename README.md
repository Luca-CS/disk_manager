# Disk Manager

Gestionnaire de stockage **100 % local** pour Windows : visualiser ce qui occupe votre disque, comprendre où
part l'espace et le libérer en toute sécurité.

## Lancer l'application

- Double-cliquez sur **`Disk Manager.cmd`** : une fenêtre d'application s'ouvre (Edge en mode application,
  avec un profil isolé et sans trafic réseau en arrière-plan).
- Pour créer un raccourci sur le Bureau et dans le menu Démarrer :
  ```
  python run.py --install-shortcut
  ```
- Mode simulation (aucun fichier n'est jamais supprimé, tout le reste fonctionne) :
  ```
  "Disk Manager.cmd" --dry-run
  ```

Prérequis : Python 3.10+ (installé depuis python.org). Aucune dépendance à installer.

## Fonctionnalités

| Écran | Ce qu'il fait |
|---|---|
| **Vue d'ensemble** | Espace libre, santé du SSD, plan des partitions, répartition du disque, espace récupérable, conseils |
| **Explorateur** | Carte proportionnelle (treemap) ou diagramme en soleil, navigation par dossier, liste triable, mise à la corbeille |
| **Types de fichiers** | Répartition par catégorie et par extension, plus gros fichiers d'un type, ancienneté des fichiers |
| **Gros fichiers** | Les plus gros fichiers du disque, filtrables par type, ancienneté et nom |
| **Doublons** | Fichiers identiques (comparaison par empreinte du contenu), sélection automatique en gardant un exemplaire |
| **Nettoyage intelligent** | Caches et fichiers temporaires (système, navigateurs, applications, outils de développement), anciennes versions d'applications, vieux installeurs : classés par niveau de risque |
| **Applications** | Programmes installés triés par taille, accès direct à l'outil de désinstallation de Windows |
| **Historique** | Évolution de l'espace utilisé, ce qui a grossi entre deux analyses, journal de toutes les suppressions |

Une analyse complète de C: prend environ 30 à 60 secondes ; elle est enregistrée et rouverte instantanément au
lancement suivant.

## Sécurité et confidentialité

- **Aucun appel réseau** : le serveur écoute uniquement sur `127.0.0.1`, l'interface ne charge aucune ressource
  externe (politique CSP stricte), aucune dépendance tierce (bibliothèque standard Python uniquement).
- **Accès protégé** : jeton aléatoire par lancement, vérification de l'hôte et de l'origine (protège contre les
  sites web malveillants qui tenteraient de piloter l'application).
- **Suppressions encadrées** (`dm/safety.py`, seul module autorisé à supprimer) :
  - vos fichiers (gros fichiers, doublons, explorateur) vont **à la corbeille** et sont restaurables ;
  - la suppression définitive est réservée aux caches et fichiers temporaires régénérables, dans une liste
    blanche d'emplacements, jamais dans vos dossiers personnels ;
  - emplacements système refusés (Windows, Program Files, ProgramData, racines de profil, fichiers système,
    clés `.ssh`…), ainsi que tout dossier qui en contient ;
  - jonctions et liens symboliques jamais suivis, noms courts 8.3 et chemins ambigus refusés ;
  - chaque suppression passe par un *handle* ouvert sans suivre les liens et dont l'emplacement réel est
    re-vérifié juste avant d'agir : remplacer un dossier par une jonction pendant un nettoyage ne permet pas de
    rediriger la suppression ;
  - fichiers en cours d'utilisation ignorés, fichiers OneDrive « en ligne » jamais lus ni supprimés ;
  - confirmation systématique et journal de chaque opération (`data/journal.jsonl`).
- Les fichiers système volumineux (hibernation, fichier d'échange, disques WSL, WinSxS, points de restauration)
  ne sont **jamais** supprimés : l'application explique comment les réduire avec les outils de Windows.

### Mode administrateur

Le bouton « Relancer en administrateur » sert uniquement à nettoyer les caches système (Windows Update,
fichiers temporaires de Windows…). Dans ce mode, l'application exécute le code de ce dossier avec des droits
élevés : ne l'utilisez que si ce dossier n'est modifiable que par vous (ou installez l'application dans un
emplacement protégé comme `C:\Program Files`). Le jeton de session n'est jamais écrit sur disque en mode
administrateur.

## Données locales

Tout est rangé dans `data/` (ignoré par git) : analyses enregistrées, historique, journal, profil isolé de la
fenêtre, journal technique `app.log`. Supprimer ce dossier remet l'application à zéro.

## Développement

```
python run.py --no-window --dry-run     # serveur seul, affiche l'URL (avec son jeton)
python run.py --debug                   # journalise chaque requête dans data/app.log
python -m unittest discover -s tests    # tests
```

- `dm/` : backend (scanner parallèle, règles de nettoyage, sécurité, API) ; `web/` : interface (modules ES,
  sans build) ; `docs/API.md` : référence de l'API ; `docs/CLEANERS_SPEC.md` : règles de nettoyage.
- `web/dev/charts.html` : démo des composants graphiques.
