-- Revalorisation dates entered on the policy form (Revalorisation checkbox).
-- The API also creates this table on first use; run it once by hand if the database user cannot create tables.
CREATE TABLE IF NOT EXISTS dbo."TPoliceRevalorisation" (
    num_int character varying(20) PRIMARY KEY,
    date_revalorisation date,
    date_envoi date
);
