-- Run once against the database before using the expanded client contact form.
ALTER TABLE dbo."TClient"
    ADD COLUMN IF NOT EXISTS telephones_domicile jsonb,
    ADD COLUMN IF NOT EXISTS emails_domicile jsonb,
    ADD COLUMN IF NOT EXISTS telephones_bureau jsonb,
    ADD COLUMN IF NOT EXISTS emails_bureau jsonb;