-- Bordereaux received from the insurers and their lines (Réception bordereau / Confrontation).
-- The API also creates these tables on first use; run this once by hand if the database user cannot create tables.
CREATE TABLE IF NOT EXISTS dbo."TBordereauCie" (
    id_bordereau bigserial PRIMARY KEY,
    code_compagnie integer NOT NULL,
    tc character varying(1) NOT NULL,
    periode character varying(7) NOT NULL,
    date_reception date,
    fichier character varying(200),
    nb_lignes integer NOT NULL DEFAULT 0,
    total_prime numeric(16, 3) NOT NULL DEFAULT 0,
    created_by character varying(60),
    created_at timestamp NOT NULL DEFAULT now(),
    UNIQUE (code_compagnie, tc, periode)
);

CREATE TABLE IF NOT EXISTS dbo."TBordereauLigne" (
    id_ligne bigserial PRIMARY KEY,
    id_bordereau bigint NOT NULL REFERENCES dbo."TBordereauCie" (id_bordereau) ON DELETE CASCADE,
    ligne integer NOT NULL DEFAULT 0,
    num_police character varying(30) NOT NULL,
    code_client character varying(12),
    nom_client character varying(120),
    echeance date,
    num_quittance character varying(30),
    prime_net numeric(14, 3),
    commission numeric(14, 3),
    prime_total numeric(14, 3)
);

CREATE INDEX IF NOT EXISTS ix_bordereau_ligne_bordereau ON dbo."TBordereauLigne" (id_bordereau);
