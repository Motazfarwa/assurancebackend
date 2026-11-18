-- Moins perçu of 18/11/2026, client 249 (pièce BT9168333/2): the rest of the quittance of the pièce whose solde is still
-- 0.117 (prime 426.232, paid 426.115) is written off as the legacy software does — a client settlement of type 'R'
-- (no pièce, Fectif 'N', same day and account as the pièce) with one line closing the quittance (Acompte_Solde 'S', solde 0).
-- Safe to run once: it stops without changing anything unless exactly one such quittance is found, and does nothing if
-- the moins perçu is already there.
BEGIN;
DO $$
DECLARE
    piece record;
    quit record;
    found_count int;
    numero int;
    recu int;
BEGIN
    SELECT r.num_reg_cli, TRIM(r.code_banque) AS banque INTO piece
    FROM dbo."TRegCli" r
    WHERE TRIM(r.code_client) = '249' AND TRIM(r.num_piece) = 'BT9168333/2' AND r.date_reg_cli::date = DATE '2026-11-18'
    ORDER BY r.num_reg_cli LIMIT 1;
    IF piece IS NULL THEN RAISE EXCEPTION 'Pièce BT9168333/2 du 18/11/2026 (client 249) introuvable : rien modifié.'; END IF;

    IF EXISTS (SELECT 1 FROM dbo."TRegCli" WHERE TRIM(code_client) = '249' AND TRIM(type_reg_cli) = 'R' AND date_reg_cli::date = DATE '2026-11-18') THEN
        RAISE NOTICE 'Moins perçu déjà enregistré : rien modifié.'; RETURN;
    END IF;

    SELECT COUNT(*) INTO found_count
    FROM dbo."RDetRegCli" d JOIN dbo."TQuittance" q ON TRIM(q.num_quittance) = TRIM(d."Num_quitance")
    WHERE d."Num_reg_cli" = piece.num_reg_cli AND ROUND(COALESCE(q.solde_quittance, 0)::numeric, 3) = 0.117;
    IF found_count <> 1 THEN RAISE EXCEPTION 'Attendu 1 quittance avec un reste de 0.117, trouvé % : rien modifié.', found_count; END IF;

    SELECT TRIM(q.num_quittance) AS num_quittance, q.montant_quittance INTO quit
    FROM dbo."RDetRegCli" d JOIN dbo."TQuittance" q ON TRIM(q.num_quittance) = TRIM(d."Num_quitance")
    WHERE d."Num_reg_cli" = piece.num_reg_cli AND ROUND(COALESCE(q.solde_quittance, 0)::numeric, 3) = 0.117;

    PERFORM pg_advisory_xact_lock(hashtext('dbo.TRegCli.num_reg_cli'));
    PERFORM pg_advisory_xact_lock(hashtext('dbo.RDetRegCli.Num_Recu'));
    SELECT COALESCE(MAX(num_reg_cli), 0) + 1 INTO numero FROM dbo."TRegCli";
    SELECT COALESCE(MAX("Num_Recu"), 0) + 1 INTO recu FROM dbo."RDetRegCli";

    INSERT INTO dbo."TRegCli" (num_reg_cli, date_reg_cli, code_client, type_reg_cli, num_piece, mont_reg_cli, code_banque, "Fectif", "Create_User", "Create_Date")
    VALUES (numero, DATE '2026-11-18', '249', 'R', '', 0.117, piece.banque, 'N', 'correction', now());
    INSERT INTO dbo."RDetRegCli" ("Num_Recu", "Num_reg_cli", "Num_quitance", "Ncpt", "Montant", "Validation", "Comptabiliser",
                                  "Acompte_Solde", "Solde_Quittance", create_user, create_date)
    VALUES (recu, numero, quit.num_quittance, '249', 0.117, 'N', 'N', 'S', 0, 'correction', now());
    UPDATE dbo."TQuittance" SET solde_quittance = 0, "Date_Reglement" = DATE '2026-11-18' WHERE TRIM(num_quittance) = quit.num_quittance;

    RAISE NOTICE 'Moins perçu 0.117 enregistré : règlement %, quittance % (prime %), compte %.', numero, quit.num_quittance, quit.montant_quittance, piece.banque;
END $$;
COMMIT;
