# Assurance FastAPI

A small FastAPI service with client management and the Saisie expert / prestataire module. It serves French forms and lists, and exposes authenticated search and CRUD endpoints. Client and prestataire table columns are reflected from PostgreSQL.

## Setup

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `DATABASE_URL` to your PostgreSQL host, port, database, username, and password. Keep `.env` private. The example assumes PostgreSQL is running on this computer; change `localhost` if it is hosted elsewhere. The web server can start without `.env`, but database endpoints return HTTP 503 until `DATABASE_URL` is configured.

   ```text
   DATABASE_URL=postgresql+psycopg://USERNAME:PASSWORD@HOST:5432/assurancejql_migration
   DB_SCHEMA=dbo
   DB_TABLE=Sinistre
   DB_CLIENT_TABLE=TClient
   DB_USER_TABLE=TUtilisateur
   AUTH_SECRET_KEY=replace_with_generated_random_value
   CORS_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
   ```

   URL-encode special characters in the username or password. If the actual PostgreSQL schema is `public`, change `DB_SCHEMA` to `public`.

   Generate `AUTH_SECRET_KEY` with `py -c "import secrets; print(secrets.token_urlsafe(48))"`; use the generated value in `.env` (at least 32 characters).

3. Before the first login, run the one-time migration in `migrations/001_user_password_hash.sql` against the PostgreSQL database. The supplied user table stores legacy passwords as plaintext in `modp varchar(15)`; the migration widens that column so successful logins can replace each legacy value with a salted PBKDF2-SHA256 hash. Existing users can sign in with their current credentials. Change any shared or weak passwords in the database after rollout.

4. Start the development server:

   ```powershell
   uvicorn app.main:app --reload
   ```

The supplied `AssurancejSQL_dataa.sql` file is a SQL Server export (`USE [AssurancejSQL]`, bracket-quoted identifiers). It is not a PostgreSQL restore script. The API expects the `TClient` table to have already been migrated/imported into the PostgreSQL database shown in pgAdmin. Set `DB_SCHEMA` and `DB_CLIENT_TABLE` to match the imported table's actual schema and name.

## Endpoints and screen

- `/` — client management screen.
- `POST /api/auth/login` — sign in with `{ "username": "...", "password": "..." }`; returns a bearer token valid for 12 hours.
- `GET /api/auth/me` — current signed-in user.
- `GET /health` — process health check.
- `GET /api/clients?q=&limit=50&offset=0` — search/list clients by code or name.
- `GET /api/clients/{code_client}` — read one client.
- `POST /api/clients` — create a client.
- `PUT /api/clients/{code_client}` — update a client.
- `DELETE /api/clients/{code_client}` — delete a client (the database may reject this if related records reference it).
- `GET /api/prestataires/lookups` — families, regions, companies, branches, and existing note labels for the expert form.
- `GET /api/prestataires?q=&limit=100&offset=0` — search/list prestataires.
- `GET /api/prestataires/{code_prestataire}` — read a prestataire and its company/branch selections.
- `POST /api/prestataires` — create the prestataire and selected company/branch links in one transaction.
- `PUT /api/prestataires/{code_prestataire}` — update the prestataire and replace its selected links atomically.
- `DELETE /api/prestataires/{code_prestataire}` — delete the prestataire and its company/branch links.
- `GET /api/sinistres?limit=100&offset=0` — paginated rows from the configured table (limit is capped at 500).
- `GET /docs` — interactive API documentation.

All business API routes require `Authorization: Bearer <token>`. Login and health check are public. The browser stores the session token for the current tab and clears it on sign out. `ModPrg` is returned as user metadata; the screenshot does not establish its authorization semantics, so it is not treated as an access-control role. The database account needs permission to connect to the database, use the configured schema, read/write the client and prestataire tables, update `TUtilisateur.modp`, and modify both prestataire relation tables. Other insurance categories remain outside this implementation.

CORS allows `http://localhost:3000` and `http://127.0.0.1:3000` by default. Set `CORS_ORIGINS` to a JSON array in `.env` if your frontend uses another origin.
