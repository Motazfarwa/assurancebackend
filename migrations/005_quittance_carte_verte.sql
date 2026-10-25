
-- Carte verte validity of a receipt (created by the API on first use).
CREATE TABLE IF NOT EXISTS dbo."TQuittanceCarteVerte" (
    num_quittance character varying(15) PRIMARY KEY,
    date_debut date,
    date_fin date
);
