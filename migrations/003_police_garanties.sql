-- Vehicle guarantees entered on the policy form (Vol, Incendie, BDG, ...).
-- The API also creates this table on first use; run it once by hand if the database user cannot create tables.
CREATE TABLE IF NOT EXISTS dbo."TPoliceGarantie" (
    num_int character varying(20) PRIMARY KEY,
    vol numeric(14, 3),
    incendie numeric(14, 3),
    bdg numeric(14, 3),
    personnes_transportees numeric(14, 3),
    ind_conducteur numeric(14, 3),
    remarquage numeric(14, 3)
);
