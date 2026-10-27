# Travailler hors ligne à l'agence

L'agence a son propre serveur : un PC de l'agence fait tourner l'application et une copie de la base de données.
Tous les PC de l'agence l'ouvrent par le réseau local, **avec ou sans internet**.
Quand internet est là, la synchronisation envoie les changements de l'agence vers le serveur en ligne (Render)
et récupère ceux faits en ligne, toutes les 15 secondes.

```
PC de l'agence ──réseau local──► PC serveur : application + base locale
                                      │ synchronisation (quand internet est là)
                                      ▼
                                 Render : application en ligne + base en ligne
```

## Ce qui marche sans internet

- Toute la saisie et la consultation : clients, polices, quittances, sinistres, chèques, bordereaux…
- La lecture des documents scannés **dans le navigateur** (OCR français / arabe).

## Ce qui demande internet

- L'import par l'IA (Claude / Gemini / OCR.space). Hors ligne, un message l'indique et la saisie manuelle reste possible.
- L'envoi des changements vers Render. Ils attendent sur le PC serveur et partent tout seuls au retour de la connexion.

## Installation (une seule fois, sur le PC serveur, avec internet)

Il faut sur ce PC :
- PostgreSQL (déjà installé ici, version 18) ;
- Python 3.12 ou plus ;
- Node.js LTS ;
- les deux projets `assurancebackend` et `assurancefrontend` côte à côte.

1. Sur Render, ouvrez la base PostgreSQL et copiez l'**External Database URL**.
2. Ouvrez PowerShell **en tant qu'administrateur** dans le dossier `assurancebackend`, puis lancez :

   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\agency\setup.ps1 -RenderDatabaseUrl "postgresql://…"
   ```

   Le mot de passe PostgreSQL local est demandé une fois. Le script :
   - sauvegarde **toutes** les bases locales actuelles dans `deploy\agency\backups\` ;
   - installe la synchronisation sur la base Render. Les tables ne sont pas modifiées : seul un schéma `sync` est ajouté.
   - copie la base Render dans une **nouvelle** base locale `assurance_agence`. Aucune base existante n'est écrasée.
   - prépare Python, construit l'application de l'agence, ouvre le port 8000 **pour le réseau local seulement** ;
   - crée la tâche planifiée « Assurance agence », qui démarre tout à l'allumage du PC.
3. Ouvrez l'application :
   - sur le PC serveur : `http://localhost:8000` ;
   - sur les autres PC : `http://NOM-DU-PC-SERVEUR:8000`. Le nom est affiché à la fin du script. Créez un raccourci.

### L'ancien logiciel

Il doit désormais utiliser la base locale `assurance_agence` du PC serveur, au lieu de la base où il écrit aujourd'hui :
ses saisies sont alors captées et envoyées à Render comme les autres.

S'il continuait d'écrire directement dans la base Render, ses changements seraient quand même récupérés par la
synchronisation, mais seulement quand internet est là.

## Au quotidien

En haut de l'écran, une pastille indique l'état :

| Pastille | Signification |
|---|---|
| **Synchronisé il y a 1 min** (vert) | Tout est à jour des deux côtés. |
| **Envoi vers le serveur en ligne · 12** (bleu) | Les changements partent. |
| **Hors ligne · 12 modification(s) en attente** (orange) | Pas d'internet : on continue normalement. |
| **Synchronisation arrêtée** (rouge) | Le programme de synchronisation ne tourne pas : redémarrez le PC serveur ou lancez `deploy\agency\start.bat`. |
| **Serveur de l'agence injoignable** (rouge) | Le PC serveur est éteint ou le réseau local est coupé. |
| **N conflits** | Le même enregistrement a été modifié à l'agence **et** en ligne avant la synchronisation. Voir ci-dessous. |

### Conflits

C'est rare, puisque seule l'agence saisit. Cliquez la pastille pour voir la liste : la table, l'opération et le côté
d'où vient la modification. Vérifiez l'enregistrement des deux côtés et corrigez-le à la main si besoin. Un
administrateur clique ensuite « Traité ». Le reste de la synchronisation n'est jamais bloqué par un conflit.

## Maintenance

- **Journaux** : `deploy\agency\logs\server.log` et `sync.log`.
- **Mise à jour de l'application** : récupérez les nouvelles versions des deux projets, puis :
  1. relancez `npm run build:agency -- ..\assurancebackend\deploy\agency\frontend` dans `assurancefrontend` ;
  2. redémarrez la tâche « Assurance agence ».
- **Nouvelle table ou nouvelle migration** (`migrations\*.sql`) :
  1. exécutez-la sur **les deux** bases ;
  2. lancez `.venv\Scripts\python.exe -m sync.worker --install both` pour que la nouvelle table soit synchronisée.
- **Tables non synchronisées** : les tables de travail `TTemp…` et le journal `TMouchard` restent locales.
  Cette liste se trouve dans la table `sync.excluded`.

## Comment ça marche (pour l'informaticien)

- Chaque base a un schéma `sync`. Des déclencheurs (triggers) sur chaque table `dbo` enregistrent toute insertion,
  modification, suppression ou vidage dans `sync.changes`, avec la ligne avant et après en jsonb. Cela vaut aussi pour
  les changements faits par l'ancien logiciel.
- Les tables n'ont pas de clé primaire. Une modification est donc rejouée en cherchant la ligne **identique** à
  « la ligne avant » (`to_jsonb(t) = old_row`). Si elle n'existe plus de l'autre côté, la ligne y a aussi été modifiée :
  c'est un conflit.
- `sync/worker.py` rejoue les changements dans l'ordre. Chaque côté mémorise, dans la même transaction que le
  changement rejoué, le dernier changement reçu de l'autre. Une coupure en plein envoi reprend donc exactement là où
  elle s'est arrêtée, sans doublon.
- Les compteurs (séquences) sont remis à niveau après chaque envoi, pour que les numéros ne se répètent pas.
- Les changements déjà rejoués sont supprimés de `sync.changes` après 30 jours.
