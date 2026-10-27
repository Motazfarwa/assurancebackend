# Travailler hors ligne à l'agence

L'agence a son propre serveur : un PC de l'agence fait tourner l'application et une copie de la base de données.
Tous les PC de l'agence l'ouvrent par le réseau local, **avec ou sans internet**.
Quand internet est là, la synchronisation envoie les changements de l'agence vers le **serveur en ligne** et
récupère ceux faits en ligne, toutes les 15 secondes.

Le serveur en ligne peut être chez n'importe quel hébergeur : Render aujourd'hui, OVH demain. La synchronisation
parle à une simple base PostgreSQL, dont l'adresse est `REMOTE_DATABASE_URL` dans `.env`.

```
PC de l'agence ──réseau local──► PC serveur : application + base locale (assurance_agence)
                                      │ synchronisation (quand internet est là)
                                      ▼
                     serveur en ligne (Render / OVH) : application en ligne + base en ligne
```

## Deux versions, les mêmes données

| | Version en ligne | Version de l'agence |
|---|---|---|
| Adresse | celle de l'hébergeur (aujourd'hui `https://assurancefrontend.onrender.com`) | `http://localhost:8000` (PC serveur), `http://NOM-DU-PC-SERVEUR:8000` (autres PC) |
| Fonctionne | partout, avec internet | à l'agence, **avec ou sans internet** |

Le raccourci `deploy\agency\Assurance.bat` choisit tout seul : la version en ligne si elle répond, sinon celle de
l'agence. Copiez-le sur le bureau de chaque PC. Sur les PC autres que le serveur, remplacez `localhost` par le nom du
PC serveur (ligne `set SERVER=`).

## Ce qui marche sans internet

- Toute la saisie et la consultation : clients, polices, quittances, sinistres, chèques, bordereaux…
- La lecture des documents scannés **dans le navigateur** (OCR français / arabe).

## Ce qui demande internet

- L'import par l'IA (Claude / Gemini / OCR.space). Hors ligne, un message l'indique et la saisie manuelle reste possible.
- L'envoi des changements vers le serveur en ligne. Ils attendent sur le PC serveur et partent tout seuls au retour
  de la connexion.

## Installation (une seule fois, sur le PC serveur, avec internet)

Il faut sur ce PC :
- PostgreSQL (version égale ou supérieure à celle du serveur en ligne) ;
- Python 3.12 ou plus ;
- Node.js LTS ;
- les deux projets `assurancebackend` et `assurancefrontend` côte à côte.

1. Récupérez l'adresse PostgreSQL de la base en ligne :
   - chez Render : page de la base, section « Connections », **External Database URL** ;
   - chez OVH : l'adresse de connexion de la base (voir « Passer chez OVH »).
2. Dans le dossier `assurancebackend`, ouvrez PowerShell **en tant qu'administrateur** et lancez :

   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\agency\setup.ps1 -OnlineDatabaseUrl "postgresql://…"
   ```

   Le mot de passe PostgreSQL local est demandé une fois. Le script :
   - sauvegarde **toutes** les bases locales actuelles, ainsi que l'ancien `.env`, dans `deploy\agency\backups\` ;
   - installe la synchronisation sur la base en ligne. Les tables ne sont pas modifiées : seul un schéma `sync` est ajouté.
   - copie la base en ligne dans une **nouvelle** base locale `assurance_agence`. Aucune base existante n'est écrasée.
   - prépare Python et construit l'application de l'agence ;
   - si PowerShell est en administrateur, ouvre le port 8000 **pour le réseau local seulement** et crée la tâche
     planifiée « Assurance agence », qui démarre tout à l'allumage du PC. Sinon, lancez ensuite, en administrateur :
     `powershell -ExecutionPolicy Bypass -File deploy\agency\install-startup.ps1`.
3. Ouvrez `http://localhost:8000` sur le PC serveur, `http://NOM-DU-PC-SERVEUR:8000` sur les autres.

### L'ancien logiciel

Il doit désormais utiliser la base locale `assurance_agence` du PC serveur, au lieu de la base où il écrit aujourd'hui :
ses saisies sont alors captées et envoyées en ligne comme les autres.

## Au quotidien

En haut de l'écran, une pastille indique l'état :

| Pastille | Signification |
|---|---|
| **Synchronisé il y a 1 min** (vert) | Tout est à jour des deux côtés. |
| **Envoi vers le serveur en ligne · 12** (bleu) | Les changements partent. |
| **Hors ligne · 12 modification(s) en attente** (orange) | Pas d'internet : on continue normalement. |
| **Synchronisation arrêtée** (rouge) | Le programme de synchronisation ne tourne pas : redémarrez le PC serveur. |
| **Serveur de l'agence injoignable** (rouge) | Le PC serveur est éteint ou le réseau local est coupé. |
| **N conflits** | Le même enregistrement a été modifié à l'agence **et** en ligne avant la synchronisation. |

### Conflits

C'est rare, puisque seule l'agence saisit. Cliquez la pastille pour voir la liste : la table, l'opération et le côté
d'où vient la modification. Vérifiez l'enregistrement des deux côtés et corrigez-le à la main si besoin. Un
administrateur clique ensuite « Traité ». Un conflit ne bloque jamais le reste de la synchronisation.

## Passer chez OVH

La version de l'agence ne change pas : seule l'adresse du serveur en ligne change.

1. **Base de données chez OVH** : créez une base PostgreSQL (offre « Managed Databases » ou PostgreSQL sur un VPS),
   de version égale ou supérieure à celle de Render (18 aujourd'hui). Autorisez la connexion depuis internet pour
   l'adresse IP de l'agence.
2. **Arrêtez la synchronisation** pendant le déménagement : arrêtez la tâche « Assurance agence » sur le PC serveur.
   L'agence ne peut alors plus travailler pendant ce temps.
3. **Copiez la base en ligne vers OVH, avec son schéma `sync`**, pour garder l'historique de synchronisation :
   ```powershell
   pg_dump -d "postgresql://…render…" -Fc --no-owner --no-acl -f online.dump
   pg_restore -d "postgresql://…ovh…" --no-owner --no-acl online.dump
   ```
4. **Backend chez OVH** : déployez `assurancebackend` avec `DATABASE_URL` = la base OVH. Ajoutez l'adresse du frontend
   OVH dans `CORS_ORIGINS`, par exemple `CORS_ORIGINS=["https://app.votre-domaine.tn"]`. Ne mettez pas
   `REMOTE_DATABASE_URL` ni `FRONTEND_DIR` en ligne : ces réglages sont propres à l'agence.
5. **Frontend chez OVH** : dans `assurancefrontend\.env`, mettez `REACT_APP_API_URL=` l'adresse du backend OVH, puis
   `npm run build` et déposez le dossier `build`.
6. **À l'agence** :
   - dans `assurancebackend\.env`, remplacez `REMOTE_DATABASE_URL` par l'adresse de la base OVH
     (`postgresql+psycopg://…`) ;
   - dans `Assurance.bat`, remplacez `ONLINE_APP` et `ONLINE_API` par les adresses OVH ;
   - redémarrez la tâche « Assurance agence ». La synchronisation reprend exactement où elle s'était arrêtée.

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
  « la ligne avant » (`to_jsonb(t) = old_row`), en UTC des deux côtés. Si elle n'existe plus de l'autre côté, la ligne
  y a aussi été modifiée : c'est un conflit.
- `sync/worker.py` rejoue les changements dans l'ordre. Chaque côté mémorise, dans la même transaction que le
  changement rejoué, le dernier changement reçu de l'autre. Une coupure en plein envoi reprend donc exactement là où
  elle s'est arrêtée, sans doublon. Un seul programme de synchronisation peut tourner à la fois.
- Les compteurs (séquences) sont remis à niveau après chaque envoi, pour que les numéros ne se répètent pas.
- Les changements déjà rejoués sont supprimés de `sync.changes` après 30 jours.
