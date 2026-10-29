-- Run once in the PostgreSQL database before using the Sinistres module.
-- The API lists, creates and updates dbo."Sinistre".nom_client (the client's name held on the claim);
-- databases migrated from the legacy software lack it. Existing claims fall back to dbo."TClient".
ALTER TABLE dbo."Sinistre"
    ADD COLUMN IF NOT EXISTS nom_client character varying(255);
