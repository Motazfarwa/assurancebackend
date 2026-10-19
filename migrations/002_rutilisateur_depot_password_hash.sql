-- Run once in the PostgreSQL database before logging in with the new user table.
-- dbo."RUtilisateur_Depot"."ModP" is varchar(15): too short for a PBKDF2 hash.
-- Existing plaintext passwords remain usable for the first login; the API then replaces
-- each one with a salted PBKDF2-SHA256 hash.
ALTER TABLE dbo."RUtilisateur_Depot"
    ALTER COLUMN "ModP" TYPE character varying(255);
