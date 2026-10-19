-- Run once in the PostgreSQL database before starting password hash upgrades.
-- Existing plaintext passwords remain usable for the first login; the API replaces
-- each password with a PBKDF2-SHA256 hash after successful authentication.
ALTER TABLE dbo."TUtilisateur"
    ALTER COLUMN modp TYPE character varying(255);
