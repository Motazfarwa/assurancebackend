"""Journaux techniques, as in the legacy software:

- Journal d'encaissement (JEQ): the premium receipts (TQuittance, type P) of a company not yet declared to it. The ones
  chosen are "comptabilisées" on the journal date: TQuittance and their client settlement lines (RDetRegCli) get
  Comptabiliser = 'O' and Date_Comptabiliser = that date. A receipt only partly paid is declared for its new acomptes
  (the RDetRegCli lines), the receipt itself staying to declare.
- Journal de dépense: the claim and rebate payments (TDepense, Typ_dep 'T' whose type has Nat_quit S or R) of a company,
  comptabilisées the same way.

The journal number is the date (yyyymmdd), Validation stays 'N' (validated by the company later), like the legacy software.

Encaissements validated one by one ("Valider" in the journal d'encaissement) are declared on the day of the validation;
dbo."TEncaissementValide" keeps which client settlement lines were validated and when, so the feuille de caisse counts
them on that day instead of the day the client paid (the client's payment itself, TRegCli, is not changed).

"Valider" in the feuille de caisse of a company on a day (the 15th or the end of the month) declares on that day every
encaissement still to declare (or the ones chosen in the journal d'encaissement) and every claim and rebate payment
still to comptabiliser (dbo."TDepenseValide": counted in that day's feuille); dbo."TFeuilleCaisseValide" keeps the
validated feuilles (one per company and day).
"""
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.routers.access import is_admin


router = APIRouter(prefix="/api/journaux", tags=["Journaux techniques"], dependencies=[Depends(require_current_user)])

EPS = 0.0005


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        elif isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Base de données indisponible : {exc.__class__.__name__}.")


# Companies the agency keeps no journal or feuille de caisse for.
EXCLUDED_COMPANIES = ["STAR", "SALIM", "MAE", "EL AMANA TAKAFUL", "BH ASSURANCES", "EUROP ASSISTANCE"]


@router.get("/compagnies")
def companies(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """The companies, with the agent code and address printed on the journals."""
    try:
        rows = db.execute(
            text(
                'SELECT code_compagnie, TRIM(nom_compagnie) AS nom_compagnie, TRIM("Code_Agent") AS code_agent, '
                'TRIM("Adresse_Compagnie") AS adresse, TRIM("Ville_Compagnie") AS ville FROM dbo."TCompagnie" '
                "WHERE UPPER(TRIM(nom_compagnie)) <> ALL(:excluded) ORDER BY nom_compagnie"
            ),
            {"excluded": EXCLUDED_COMPANIES},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return [_plain(row) for row in rows]


# ---------------------------------------------------------------------------------------------
# Journal d'encaissement
# ---------------------------------------------------------------------------------------------

# Receipts of the company still to declare: every quittance a client paid something on (dbo."RDetRegCli", one line per
# quittance settled, with the TRegCli date) by the journal date and not declared yet, whatever its échéance; with `done`
# the ones declared on that date.
# declared_before / at_day / pending: the settlement lines declared before the date, on the date, not yet.
RECEIPTS_SQL = """
WITH paid AS MATERIALIZED (
    -- the client settlement lines not declared yet, paid by the journal date (since 1 January of the year before: older
    -- lines never declared are leftovers of 2005-2006 test quittances, not encaissements to declare now)
    SELECT DISTINCT TRIM(d."Num_quitance") AS num_quittance
    FROM dbo."RDetRegCli" d JOIN dbo."TRegCli" t ON t.num_reg_cli = d."Num_reg_cli"
    WHERE COALESCE(d."Comptabiliser", 'N') <> 'O' AND t.date_reg_cli < :next_day
      AND t.date_reg_cli >= date_trunc('year', CAST(:day AS date)) - INTERVAL '1 year'
),
q AS MATERIALIZED (
    SELECT q.* FROM dbo."TQuittance" q
    WHERE TRIM(q.type_quittance) = 'P' AND COALESCE(q.annule, 'N') <> 'O'
      AND (
        (COALESCE(q."Comptabiliser", 'N') <> 'O' AND TRIM(q.num_quittance) IN (SELECT num_quittance FROM paid))
        OR (:done AND q."Date_Comptabiliser" >= :done_from AND q."Date_Comptabiliser" < :next_day)
        OR (:done AND EXISTS (
            SELECT 1 FROM dbo."RDetRegCli" d WHERE TRIM(d."Num_quitance") = TRIM(q.num_quittance)
              AND d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" >= :done_from AND d."Date_Comptabiliser" < :next_day))
      )
      AND (CAST(:numbers AS text[]) IS NULL OR TRIM(q.num_quittance) = ANY(CAST(:numbers AS text[])))
),
pol AS (
    SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, TRIM(num_police) AS num_police, code_compagnie
    FROM dbo."TPolice" WHERE TRIM(num_int) IN (SELECT TRIM(num_int) FROM q)
    ORDER BY TRIM(num_int), effet DESC NULLS LAST
),
reg AS (
    SELECT TRIM(d."Num_quitance") AS num_quittance,
           SUM(CASE WHEN d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" < :done_from THEN d."Montant" ELSE 0 END) AS declared_before,
           SUM(CASE WHEN d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" >= :done_from AND d."Date_Comptabiliser" < :next_day THEN d."Montant" ELSE 0 END) AS at_day,
           MAX(CASE WHEN d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" >= :done_from AND d."Date_Comptabiliser" < :next_day THEN d."Date_Comptabiliser" END) AS declared_on,
           SUM(CASE WHEN COALESCE(d."Comptabiliser", 'N') <> 'O' THEN d."Montant" ELSE 0 END) AS pending
    FROM dbo."RDetRegCli" d
    WHERE TRIM(d."Num_quitance") IN (SELECT TRIM(num_quittance) FROM q)
    GROUP BY 1
),
-- What the clients paid on each quittance (RDetRegCli, with the TRegCli date and pièce; TRDetRegCli is its copy
-- frozen on 02/03/2012), shown in the journal for information.
treg AS (
    SELECT TRIM(d."Num_quitance") AS num_quittance, SUM(d."Montant") AS regle, MAX(t.date_reg_cli) AS date_reglement,
           BOOL_OR(t.date_reg_cli >= CAST(:reg_debut AS date) AND t.date_reg_cli < CAST(:reg_fin AS date) + 1) AS in_period,
           STRING_AGG(DISTINCT TRIM(t.num_piece), ', ') FILTER (WHERE TRIM(t.num_piece) <> '') AS pieces_reglement
    FROM dbo."RDetRegCli" d LEFT JOIN dbo."TRegCli" t ON t.num_reg_cli = d."Num_reg_cli"
    WHERE TRIM(d."Num_quitance") IN (SELECT TRIM(num_quittance) FROM q)
    GROUP BY 1
)
SELECT TRIM(q.num_quittance) AS num_quittance, TRIM(q.tc) AS tc, q.echeance, p.num_police, TRIM(q."Code_Agence") AS code_agence,
       p.code_compagnie, TRIM(k.nom_compagnie) AS nom_compagnie,
       COALESCE(tr.regle, 0) AS regle, tr.date_reglement, tr.pieces_reglement,
       TRIM(q.code_client) AS code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = TRIM(q.code_client) LIMIT 1) AS assure,
       COALESCE(q.prime_net, 0) AS prime_net, COALESCE(q.frais_quit, 0) AS frais_quit, COALESCE(q.montant_quittance, 0) AS montant_quittance,
       COALESCE(q.solde_quittance, 0) AS solde_quittance, COALESCE(q.commission, 0) AS commission, COALESCE(q.timbre, 0) AS timbre,
       COALESCE(q.cout_attestation, 0) AS frais, q."Comptabiliser" = 'O' AND q."Date_Comptabiliser" >= :done_from AND q."Date_Comptabiliser" < :next_day AS declared_today,
       COALESCE(r.declared_on, q."Date_Comptabiliser") AS date_comptabilise,
       (SELECT MAX(TRIM(v."Utilisateur")) FROM dbo."TEncaissementValide" v WHERE TRIM(v."Num_quitance") = TRIM(q.num_quittance)
          AND v."Date_Validation" >= :done_from AND v."Date_Validation" < :next_day) AS valide_par,
       COALESCE(r.declared_before, 0) AS declared_before, COALESCE(r.at_day, 0) AS at_day, COALESCE(r.pending, 0) AS pending
FROM q
JOIN pol p ON p.num_int = TRIM(q.num_int)
JOIN dbo."TCompagnie" k ON k.code_compagnie = p.code_compagnie
LEFT JOIN reg r ON r.num_quittance = TRIM(q.num_quittance)
LEFT JOIN treg tr ON tr.num_quittance = TRIM(q.num_quittance)
-- One company, or all those the agency keeps journals for.
WHERE (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
  AND UPPER(TRIM(k.nom_compagnie)) <> ALL(:excluded)
  -- With `regles`, only the quittances with a client settlement (and, between two dates, settled in them).
  AND (NOT :regles OR (tr.date_reglement IS NOT NULL AND COALESCE(tr.regle, 0) > 0.0005
                       AND (CAST(:reg_debut AS date) IS NULL OR tr.in_period)))
ORDER BY q.echeance, TRIM(q.num_quittance)
"""


def _receipt(row: Any) -> dict[str, Any]:
    """One journal line: declared on the date (comptabilise) or to declare, for its balance (S) or its new acomptes (A)."""
    item = _plain(row)
    paid = item["solde_quittance"] <= EPS
    if item.pop("declared_today"):
        done, kind, amount = True, "S", item["montant_quittance"] - item["declared_before"]
    elif item["at_day"] > EPS:
        done, kind, amount = True, "A", item["at_day"]
    elif not paid and item["pending"] > EPS:
        done, kind, amount = False, "A", item["pending"]
    else:
        done, kind, amount = False, "S", item["montant_quittance"] - item["declared_before"]
    if not done:
        item["date_comptabilise"] = None
    item.update({
        "comptabilise": done,
        "a_s": kind,
        "montant": round(amount, 3),
        "prime_cout": round(item["prime_net"] + item["frais_quit"], 3),
        # The commission is declared with the balance, not with the acomptes.
        "comm": item["commission"] if kind == "S" else 0,
        "paye": paid,
    })
    return item


def _receipts(db: Session, compagnie: int | None, day: date, done: bool, numbers: list[str] | None = None,
              regles: bool = False, reg_debut: date | None = None, reg_fin: date | None = None,
              done_from: date | None = None) -> list[dict[str, Any]]:
    """The lines to declare on `day`, and with `done` those declared from `done_from` (default: that day) to `day`."""
    _ensure_validated(db)
    rows = db.execute(
        text(RECEIPTS_SQL),
        {"compagnie": compagnie, "day": day, "next_day": day + timedelta(days=1), "done": done, "numbers": numbers,
         "done_from": done_from or day,
         "excluded": EXCLUDED_COMPANIES, "regles": regles, "reg_debut": reg_debut, "reg_fin": reg_fin},
    ).mappings().all()
    return [_receipt(row) for row in rows]


@router.get("/encaissement")
def receipts_journal(
    compagnie: int | None = Query(default=None, description="Without it, every company"),
    date_journal: date = Query(..., alias="date"),
    comptabilises: bool = Query(default=False, description="Also the receipts declared on that date"),
    regles: bool = Query(default=False, description="Only the receipts with a client settlement"),
    debut: date | None = Query(default=None, description="With fin: settled between the two dates"),
    fin: date | None = None,
    periode: bool = Query(default=False, description="Declared lines of the half month of the date (1-15, 16-end), not only of the date"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if bool(debut) != bool(fin):
        raise HTTPException(status_code=422, detail="Indiquez les deux dates.")
    if debut and fin and fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    try:
        done_from = _period_start(date_journal) if periode else None
        items = _receipts(db, compagnie, date_journal, comptabilises, regles=regles, reg_debut=debut, reg_fin=fin, done_from=done_from)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": items, "num_jeq": date_journal.strftime("%Y%m%d"), "debut_periode": (done_from or date_journal).isoformat()}


def _period_start(day: date) -> date:
    """The journals are made on the 15th and at the end of the month: the period of a date starts on the 1st or the 16th."""
    return day.replace(day=1 if day.day <= 15 else 16)


VALIDATED_DDL = """
CREATE TABLE IF NOT EXISTS dbo."TEncaissementValide" (
    "Num_reg_cli" integer NOT NULL,
    "Num_quitance" varchar(30) NOT NULL,
    "Date_Validation" date NOT NULL,
    "Montant" double precision,
    "Utilisateur" varchar(50),
    "Date_Saisie" timestamp,
    PRIMARY KEY ("Num_reg_cli", "Num_quitance")
)
"""
CASH_VALIDATED_DDL = [
    """CREATE TABLE IF NOT EXISTS dbo."TDepenseValide" (
        num_dep integer PRIMARY KEY,
        jour date NOT NULL,
        utilisateur varchar(50),
        date_saisie timestamp
    )""",
    """CREATE TABLE IF NOT EXISTS dbo."TFeuilleCaisseValide" (
        code_compagnie integer NOT NULL,
        jour date NOT NULL,
        quittances integer NOT NULL,
        encaissements numeric(14, 3) NOT NULL,
        depenses integer NOT NULL,
        montant_depenses numeric(14, 3) NOT NULL,
        utilisateur varchar(50),
        date_saisie timestamp NOT NULL,
        PRIMARY KEY (code_compagnie, jour)
    )""",
]
_validated_ready = False


def _ensure_validated(db: Session) -> None:
    global _validated_ready
    if not _validated_ready:
        db.execute(text(VALIDATED_DDL))
        for statement in CASH_VALIDATED_DDL:
            db.execute(text(statement))
        db.commit()
        _validated_ready = True


def _declare_receipt(db: Session, line: dict[str, Any], day: date, name: str) -> int:
    """Declares one encaissement on `day`: its client settlement lines (and the quittance when it is paid in full), kept in
    TEncaissementValide so the feuille de caisse counts them on that day. Returns the number of settlement lines."""
    number = line["num_quittance"]
    settlements = db.execute(text(
        'SELECT d."Num_reg_cli" AS reg, COALESCE(d."Montant", 0) AS montant FROM dbo."RDetRegCli" d '
        'WHERE TRIM(d."Num_quitance") = :n AND COALESCE(d."Comptabiliser", \'N\') <> \'O\''
    ), {"n": number}).all()
    params = {"d": day, "n": number}
    if line["a_s"] == "S":
        db.execute(text(
            'UPDATE dbo."TQuittance" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
            'WHERE TRIM(num_quittance) = :n AND COALESCE("Comptabiliser", \'N\') <> \'O\''
        ), params)
    db.execute(text(
        'UPDATE dbo."RDetRegCli" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
        'WHERE TRIM("Num_quitance") = :n AND COALESCE("Comptabiliser", \'N\') <> \'O\''
    ), params)
    for reg, montant in settlements:
        db.execute(text(
            'INSERT INTO dbo."TEncaissementValide" ("Num_reg_cli", "Num_quitance", "Date_Validation", "Montant", "Utilisateur", "Date_Saisie") '
            "VALUES (:r, :n, :d, :m, :u, now() AT TIME ZONE 'Africa/Tunis') ON CONFLICT DO NOTHING"
        ), {"r": reg, "n": number, "d": day, "m": montant, "u": name})
    return len(settlements)


class ValiderEncaissement(BaseModel):
    num_quittance: str = Field(min_length=1, max_length=30)


@router.post("/encaissement/valider")
def validate_receipt(payload: ValiderEncaissement, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validates one encaissement on the day of the validation (agency time): its client settlement lines are declared
    on that day (and the quittance when it is paid in full) and counted in that day's feuille de caisse."""
    _ensure_validated(db)
    number = payload.num_quittance.strip()
    name = str(user.get("sub", ""))[:50]
    try:
        day = db.execute(text("SELECT (now() AT TIME ZONE 'Africa/Tunis')::date")).scalar()
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('encaissement:' || :n))"), {"n": number})  # one validation at a time
        lines = [line for line in _receipts(db, None, day, False, [number], regles=True) if not line["comptabilise"]]
        if not lines:
            raise HTTPException(status_code=409, detail=f"L’encaissement de la quittance {number} est déjà validé (ou n’a pas de règlement client à déclarer).")
        line = lines[0]
        if not line["paye"] and line["a_s"] != "A":
            raise HTTPException(status_code=409, detail=f"Quittance {number} : le client n’a pas encore payé ce reste, il n’y a pas d’encaissement à valider.")
        settlements = _declare_receipt(db, line, day, name)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"num_quittance": number, "date_validation": day.isoformat(), "montant": line["montant"], "a_s": line["a_s"],
            "code_compagnie": line["code_compagnie"], "valide_par": name, "lignes_reglement": settlements}


class ComptabiliserQuittances(BaseModel):
    compagnie: int
    date: date
    quittances: list[str] = Field(min_length=1, max_length=2000)


@router.post("/encaissement/comptabiliser")
def post_receipts(payload: ComptabiliserQuittances, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Declares the chosen receipts on the journal date (only those still to declare for this company)."""
    numbers = sorted({number.strip() for number in payload.quittances if number.strip()})
    try:
        lines = [line for line in _receipts(db, payload.compagnie, payload.date, False, numbers) if not line["comptabilise"]]
        params = {"d": payload.date}
        for line in lines:
            params["n"] = line["num_quittance"]
            if line["a_s"] == "S":
                db.execute(text(
                    'UPDATE dbo."TQuittance" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
                    'WHERE TRIM(num_quittance) = :n AND COALESCE("Comptabiliser", \'N\') <> \'O\''
                ), params)
            db.execute(text(
                'UPDATE dbo."RDetRegCli" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
                'WHERE TRIM("Num_quitance") = :n AND COALESCE("Comptabiliser", \'N\') <> \'O\''
            ), params)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"comptabilisees": len(lines), "num_jeq": payload.date.strftime("%Y%m%d")}


# ---------------------------------------------------------------------------------------------
# Journal de dépense
# ---------------------------------------------------------------------------------------------

EXPENSES_SQL = """
WITH d AS MATERIALIZED (
    SELECT d.*, TRIM(t."Nat_quit") AS nature, TRIM(t.libelle) AS libelle FROM dbo."TDepense" d
    JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
    WHERE TRIM(d."Typ_dep") = 'T' AND TRIM(t."Nat_quit") IN ('S', 'R')
      AND (CAST(:nature AS text) IS NULL OR TRIM(t."Nat_quit") = CAST(:nature AS text))
      AND (
        (COALESCE(d."Comptabiliser", 'N') <> 'O' AND d.date_dep < :next_day)
        OR (:done AND d."Date_Comptabiliser" >= :day AND d."Date_Comptabiliser" < :next_day)
      )
      AND (CAST(:ids AS int[]) IS NULL OR d.num_dep = ANY(CAST(:ids AS int[])))
),
pol AS (
    SELECT DISTINCT ON (TRIM(num_police)) TRIM(num_police) AS num_police, code_compagnie, TRIM(num_imat) AS num_imat
    FROM dbo."TPolice" WHERE TRIM(num_police) IN (SELECT TRIM(num_police) FROM d)
    ORDER BY TRIM(num_police), effet DESC NULLS LAST
)
SELECT d.num_dep, d.nature, d.libelle, d.date_dep, COALESCE(d.mont_dep, 0) AS montant, TRIM(d.objet_dep) AS objet,
       COALESCE(NULLIF(TRIM(d.sinistre), ''), TRIM(d.num_quittance)) AS numero, TRIM(d.num_police) AS num_police, p.num_imat,
       TRIM(d.code_client) AS code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = TRIM(d.code_client) LIMIT 1) AS assure,
       TRIM(d.num_cheq) AS num_cheq, TRIM(d.code_banque) AS code_banque,
       COALESCE(d."Comptabiliser", 'N') = 'O' AS comptabilise, d."Date_Comptabiliser" AS date_comptabilise,
       p.code_compagnie, TRIM(k.nom_compagnie) AS nom_compagnie,
       -- D.Com: on a rebate, the commission of the rebated receipt
       CASE WHEN d.nature = 'R' THEN ABS(COALESCE((
           SELECT q.commission FROM dbo."TQuittance" q WHERE TRIM(q.num_quittance) = TRIM(d.num_quittance) LIMIT 1), 0)) ELSE 0 END AS d_com
FROM d
JOIN pol p ON p.num_police = TRIM(d.num_police)
JOIN dbo."TCompagnie" k ON k.code_compagnie = p.code_compagnie
-- One company, or all those the agency keeps journals for.
WHERE (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
  AND UPPER(TRIM(k.nom_compagnie)) <> ALL(:excluded)
ORDER BY TRIM(k.nom_compagnie), TRIM(d.num_police), d.num_dep  -- as the legacy "Liste des dépenses": by police
"""


def _expenses(db: Session, compagnie: int | None, day: date, done: bool, nature: str | None = None, ids: list[int] | None = None) -> list[dict[str, Any]]:
    rows = db.execute(
        text(EXPENSES_SQL),
        {"compagnie": compagnie, "day": day, "next_day": day + timedelta(days=1), "done": done, "nature": nature, "ids": ids,
         "excluded": EXCLUDED_COMPANIES},
    ).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/depense")
def expenses_journal(
    compagnie: int | None = Query(default=None, description="Without it, every company"),
    date_journal: date = Query(..., alias="date"),
    nature: str = Query(default="", pattern="^[SR]?$", description="S = sinistres, R = ristournes, empty = both"),
    comptabilises: bool = Query(default=False, description="Also the expenses comptabilisées on that date"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        items = _expenses(db, compagnie, date_journal, comptabilises, nature or None)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": items}


class ComptabiliserDepenses(BaseModel):
    compagnie: int
    date: date
    depenses: list[int] = Field(min_length=1, max_length=2000)


@router.post("/depense/comptabiliser")
def post_expenses(payload: ComptabiliserDepenses, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Comptabilise the chosen claim and rebate payments of the company on the journal date."""
    try:
        ids = [line["num_dep"] for line in _expenses(db, payload.compagnie, payload.date, False, None, sorted(set(payload.depenses))) if not line["comptabilise"]]
        if ids:
            db.execute(text(
                'UPDATE dbo."TDepense" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
                'WHERE TRIM("Typ_dep") = \'T\' AND num_dep = ANY(CAST(:ids AS int[])) AND COALESCE("Comptabiliser", \'N\') <> \'O\''
            ), {"d": payload.date, "ids": ids})
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"comptabilisees": len(ids)}


# ---------------------------------------------------------------------------------------------
# Etat des moins perçus & trop perçus
# ---------------------------------------------------------------------------------------------
# Moins perçu: the small rest of a receipt the agency writes off, a client settlement of type 'R'.
# Trop perçu: what a client paid over the receipts, put on the placeholder receipt 9999999999.
OVERPAID_RECEIPT = "9999999999"

UNDERPAID_SQL = """
WITH pol AS (
    SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, TRIM(num_police) AS num_police
    FROM dbo."TPolice" ORDER BY TRIM(num_int), num_police
)
SELECT r.date_reg_cli AS date_mvt, TRIM(r.code_client) AS code_client, TRIM(c.nom_client) AS nom_client,
       pol.num_police, TRIM(d."Num_quitance") AS num_quittance, q.echeance,
       TRIM(q.tc) || COALESCE(SPLIT_PART(TRIM(q."Periode"), '/', 1) || '-' || SPLIT_PART(TRIM(q."Periode"), '/', 2), '') AS tc,
       TRIM(r.code_banque) AS banque, d."Montant" AS montant
FROM dbo."TRegCli" r
JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = r.num_reg_cli
LEFT JOIN dbo."TQuittance" q ON q.num_quittance = d."Num_quitance"
LEFT JOIN pol ON pol.num_int = TRIM(q.num_int)
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(r.code_client)
WHERE TRIM(r.type_reg_cli) = 'R' AND r.date_reg_cli >= :debut AND r.date_reg_cli < :fin
ORDER BY r.date_reg_cli, r.num_reg_cli
"""

OVERPAID_SQL = """
SELECT r.date_reg_cli AS date_mvt, TRIM(r.code_client) AS code_client, TRIM(c.nom_client) AS nom_client,
       TRIM(r.code_banque) AS banque, d."Montant" AS montant
FROM dbo."TRegCli" r
JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = r.num_reg_cli
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(r.code_client)
WHERE TRIM(d."Num_quitance") = :overpaid AND r.date_reg_cli >= :debut AND r.date_reg_cli < :fin
ORDER BY TRIM(r.code_client), r.date_reg_cli
"""


@router.get("/percus")
def received_gaps(
    debut: date,
    fin: date,
    type: str = Query("moins", pattern="^(moins|trop)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Moins perçus (written-off rests) or trop perçus (overpayments) of the client settlements dated debut..fin."""
    if fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    params = {"debut": debut, "fin": fin + timedelta(days=1), "overpaid": OVERPAID_RECEIPT}
    try:
        rows = db.execute(text(UNDERPAID_SQL if type == "moins" else OVERPAID_SQL), params).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [_plain(row) for row in rows]
    return {"items": items, "total": round(sum(float(row["montant"] or 0) for row in items), 3)}


# ---------------------------------------------------------------------------------------------
# Feuille de caisse over a period
# ---------------------------------------------------------------------------------------------
# Per day of the period, the client settlements of the company's quittances (TRegCli dated that day, one RDetRegCli
# line per quittance paid; the 'R' moins perçus are not cash): primes = what the clients paid, and the commission of a
# quittance on the line that settles it (Acompte_Solde 'S'; an acompte carries none). Without dates: every day.
CASH_RECEIPTS_SQL = """
WITH v AS MATERIALIZED (
    -- the settlement lines validated in the journal d'encaissement: counted on the day of their validation
    SELECT "Num_reg_cli" AS num_reg_cli, TRIM("Num_quitance") AS num_quittance, "Date_Validation" AS jour FROM dbo."TEncaissementValide"
),
r AS MATERIALIZED (
    SELECT r.num_reg_cli, r.date_reg_cli::date AS jour
    FROM dbo."TRegCli" r
    WHERE COALESCE(TRIM(r.type_reg_cli), '') <> 'R'
      AND (CAST(:debut AS date) IS NULL OR r.date_reg_cli >= CAST(:debut AS date))
      AND (CAST(:fin AS date) IS NULL OR r.date_reg_cli < CAST(:fin AS date) + 1)
),
lines AS MATERIALIZED (
    SELECT r.jour, TRIM(d."Num_quitance") AS num_quittance, COALESCE(d."Montant", 0) AS montant,
           COALESCE(NULLIF(TRIM(d."Acompte_Solde"), ''), 'S') = 'S' AS settles
    FROM r JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = r.num_reg_cli
    WHERE NOT EXISTS (SELECT 1 FROM v WHERE v.num_reg_cli = d."Num_reg_cli" AND v.num_quittance = TRIM(d."Num_quitance"))
    UNION ALL
    SELECT v.jour, v.num_quittance, COALESCE(d."Montant", 0) AS montant,
           COALESCE(NULLIF(TRIM(d."Acompte_Solde"), ''), 'S') = 'S' AS settles
    FROM v JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = v.num_reg_cli AND TRIM(d."Num_quitance") = v.num_quittance
    JOIN dbo."TRegCli" t ON t.num_reg_cli = v.num_reg_cli AND COALESCE(TRIM(t.type_reg_cli), '') <> 'R'
    WHERE (CAST(:debut AS date) IS NULL OR v.jour >= CAST(:debut AS date))
      AND (CAST(:fin AS date) IS NULL OR v.jour <= CAST(:fin AS date))
),
q AS (
    SELECT DISTINCT ON (TRIM(q.num_quittance)) TRIM(q.num_quittance) AS num_quittance, TRIM(q.num_int) AS num_int,
           COALESCE(q.commission, 0) AS comm
    FROM dbo."TQuittance" q WHERE TRIM(q.num_quittance) IN (SELECT num_quittance FROM lines)
    ORDER BY TRIM(q.num_quittance)
),
pol AS (
    SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, code_compagnie
    FROM dbo."TPolice" WHERE TRIM(num_int) IN (SELECT num_int FROM q)
    ORDER BY TRIM(num_int), effet DESC NULLS LAST
)
SELECT l.jour, COUNT(*) AS quittances, ROUND(SUM(l.montant)::numeric, 3) AS primes, 0 AS tq,
       ROUND(SUM(CASE WHEN l.settles THEN q.comm ELSE 0 END)::numeric, 3) AS commission, 0 AS frais
FROM lines l JOIN q ON q.num_quittance = l.num_quittance JOIN pol p ON p.num_int = q.num_int
WHERE p.code_compagnie = :compagnie
GROUP BY l.jour ORDER BY l.jour
"""

CASH_EXPENSES_SQL = """
WITH pol AS (
    SELECT DISTINCT ON (TRIM(num_police)) TRIM(num_police) AS num_police, code_compagnie
    FROM dbo."TPolice" ORDER BY TRIM(num_police), effet DESC NULLS LAST
), d AS MATERIALIZED (
    -- the claims and rebates paid that day (date of the dépense), like the client settlements
    SELECT d.num_dep, TRIM(t."Nat_quit") AS nature, d.date_dep::date AS jour, d.date_dep, COALESCE(d.mont_dep, 0) AS montant,
           COALESCE(NULLIF(TRIM(d.sinistre), ''), TRIM(d.num_quittance)) AS numero, TRIM(d.num_quittance) AS num_quittance
    FROM dbo."TDepense" d
    JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
    JOIN pol p ON p.num_police = TRIM(d.num_police)
    WHERE p.code_compagnie = :compagnie AND TRIM(d."Typ_dep") = 'T' AND TRIM(t."Nat_quit") IN ('S', 'R')
      AND NOT EXISTS (SELECT 1 FROM dbo."TDepenseValide" v WHERE v.num_dep = d.num_dep)
      AND (CAST(:debut AS date) IS NULL OR d.date_dep >= CAST(:debut AS date))
      AND (CAST(:fin AS date) IS NULL OR d.date_dep < CAST(:fin AS date) + 1)
    UNION ALL
    -- the ones comptabilisées by the validation of a feuille de caisse: counted on the day of that feuille
    SELECT d.num_dep, TRIM(t."Nat_quit"), v.jour, d.date_dep, COALESCE(d.mont_dep, 0),
           COALESCE(NULLIF(TRIM(d.sinistre), ''), TRIM(d.num_quittance)), TRIM(d.num_quittance)
    FROM dbo."TDepenseValide" v
    JOIN dbo."TDepense" d ON d.num_dep = v.num_dep
    JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
    JOIN pol p ON p.num_police = TRIM(d.num_police)
    WHERE p.code_compagnie = :compagnie
      AND (CAST(:debut AS date) IS NULL OR v.jour >= CAST(:debut AS date))
      AND (CAST(:fin AS date) IS NULL OR v.jour <= CAST(:fin AS date))
), rebated AS (
    -- D.Com: on a rebate, the commission of the rebated receipt
    SELECT TRIM(q.num_quittance) AS num_quittance, MAX(ABS(COALESCE(q.commission, 0))) AS d_com
    FROM dbo."TQuittance" q WHERE TRIM(q.num_quittance) IN (SELECT num_quittance FROM d WHERE nature = 'R')
    GROUP BY 1
)
SELECT d.num_dep, d.nature, d.jour, d.date_dep, d.montant, d.numero,
       CASE WHEN d.nature = 'R' THEN COALESCE(r.d_com, 0) ELSE 0 END AS d_com
FROM d LEFT JOIN rebated r ON r.num_quittance = d.num_quittance
ORDER BY d.jour, d.date_dep, d.num_dep
"""


@router.get("/feuille")
def cash_sheet(
    compagnie: int = Query(...),
    debut: date | None = Query(default=None, description="Empty with fin: every day"),
    fin: date | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The client settlements of each day of the period (primes paid, commission of the quittances settled) and the
    claims and rebates paid in it."""
    if debut and fin and fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    params = {"compagnie": compagnie, "debut": debut, "fin": fin}
    _ensure_validated(db)
    try:
        # The legacy tables have no statistics-friendly keys (TRIM joins): the planner underestimates them and picks
        # nested loops (minutes over all dates). Hash joins only, for this read-only transaction.
        db.execute(text("SET LOCAL enable_nestloop = off"))
        journals = [_plain(row) for row in db.execute(text(CASH_RECEIPTS_SQL), params).mappings().all()]
        expenses = [_plain(row) for row in db.execute(text(CASH_EXPENSES_SQL), params).mappings().all()]
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    finally:
        db.rollback()  # ends the transaction, and with it the SET LOCAL
    return {"journaux": journals, "depenses": expenses}


# The "Détails" of the feuille de caisse of a company on a day: the solde and the montant à déclarer may be rounded by
# the user, by at most one dinar each way, with a remarque; the adjusted values are kept per company and day.
ADJUSTMENT_DDL = """
CREATE TABLE IF NOT EXISTS dbo."TFeuilleCaisseAjust" (
    code_compagnie integer NOT NULL,
    jour date NOT NULL,
    solde_calcule numeric(14, 3) NOT NULL,
    solde numeric(14, 3) NOT NULL,
    a_declarer_calcule numeric(14, 3) NOT NULL,
    a_declarer numeric(14, 3) NOT NULL,
    remarque varchar(200),
    update_user varchar(50),
    update_date timestamp NOT NULL DEFAULT now(),
    PRIMARY KEY (code_compagnie, jour)
)
"""
MAX_ADJUSTMENT = 1.0  # dinars, up or down


def _adjustment(db: Session, compagnie: int, jour: date) -> dict[str, Any] | None:
    db.execute(text(ADJUSTMENT_DDL))
    row = db.execute(
        text('SELECT * FROM dbo."TFeuilleCaisseAjust" WHERE code_compagnie = :c AND jour = :j'), {"c": compagnie, "j": jour},
    ).mappings().first()
    return _plain(row) if row else None


@router.get("/feuille/ajustement")
def cash_adjustment(compagnie: int, jour: date, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The solde and montant à déclarer kept for the feuille of a company on a day (null: the computed ones)."""
    try:
        found = _adjustment(db, compagnie, jour)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"ajustement": found}


class AdjustmentIn(BaseModel):
    compagnie: int
    jour: date
    solde_calcule: float
    solde: float
    a_declarer_calcule: float
    a_declarer: float
    remarque: str | None = Field(default=None, max_length=200)


@router.put("/feuille/ajustement")
def save_cash_adjustment(payload: AdjustmentIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Keeps the solde and montant à déclarer of the feuille, each within one dinar of the computed value; a remarque is
    required when one of them differs. Back to the computed values: the adjustment is removed."""
    for label, value, computed in (("solde", payload.solde, payload.solde_calcule), ("montant à déclarer", payload.a_declarer, payload.a_declarer_calcule)):
        if abs(round(value - computed, 3)) > MAX_ADJUSTMENT:
            raise HTTPException(status_code=422, detail=f"Le {label} ne peut pas s'écarter de plus de 1,000 DT du montant calculé ({computed:.3f}).")
    remarque = (payload.remarque or "").strip()
    changed = abs(payload.solde - payload.solde_calcule) > 0.0005 or abs(payload.a_declarer - payload.a_declarer_calcule) > 0.0005
    if changed and not remarque:
        raise HTTPException(status_code=422, detail="Indiquez une remarque pour expliquer la modification.")
    key = {"c": payload.compagnie, "j": payload.jour}
    try:
        db.execute(text(ADJUSTMENT_DDL))
        db.execute(text('DELETE FROM dbo."TFeuilleCaisseAjust" WHERE code_compagnie = :c AND jour = :j'), key)
        if changed:
            db.execute(
                text(
                    'INSERT INTO dbo."TFeuilleCaisseAjust" (code_compagnie, jour, solde_calcule, solde, a_declarer_calcule, a_declarer, '
                    'remarque, update_user) VALUES (:c, :j, :sc, :s, :ac, :a, :r, :u)'
                ),
                {**key, "sc": round(payload.solde_calcule, 3), "s": round(payload.solde, 3), "ac": round(payload.a_declarer_calcule, 3),
                 "a": round(payload.a_declarer, 3), "r": remarque, "u": str(user.get("sub", ""))[:50]},
            )
        found = _adjustment(db, payload.compagnie, payload.jour)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"ajustement": found}


# ---------------------------------------------------------------------------------------------
# Opérations journalières (production)
# ---------------------------------------------------------------------------------------------
# As the legacy report: the history lines (TMouchard) "Ajout / Modification / Suppression Police N° x" (or Quittance)
# dated in the period, one per day, movement and record, with the record as it is now.
PRODUCTION_MOVES = r"""
SELECT DISTINCT dmvt::date AS date_mvt, TRIM("Mvt") AS mvt, substring(design FROM 'N°\s*(\S+)') AS numero
FROM dbo."TMouchard"
WHERE dmvt >= :debut AND dmvt < :fin AND TRIM("Mvt") IN ('Ajout', 'Modification', 'Suppresion', 'Suppression', 'Annulation')
  AND design ~* :pattern
"""
PRODUCTION_SQL = {
    "police": f"""
WITH m AS ({PRODUCTION_MOVES})
SELECT m.date_mvt, m.mvt, m.numero AS num_int, TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client,
       TRIM(c.nom_client) AS nom_client, TRIM(p.echeance) AS echeance, UPPER(TRIM(p.etat)) AS etat, TRIM(p.risque) AS risque
FROM m
LEFT JOIN LATERAL (SELECT * FROM dbo."TPolice" p WHERE TRIM(p.num_int) = m.numero ORDER BY p.effet DESC NULLS LAST LIMIT 1) p ON TRUE
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client)
ORDER BY m.date_mvt, m.numero, m.mvt
""",
    "quittance": f"""
WITH m AS ({PRODUCTION_MOVES})
SELECT m.date_mvt, m.mvt, m.numero AS num_quittance, TRIM(q.num_int) AS num_int, TRIM(q.code_client) AS code_client,
       TRIM(c.nom_client) AS nom_client, q.echeance, TRIM(q.tc) AS tc, TRIM(q."Periode") AS periode,
       TRIM(q.type_quittance) AS type_quittance, q.prime_total
FROM m
LEFT JOIN dbo."TQuittance" q ON TRIM(q.num_quittance) = m.numero
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
ORDER BY m.date_mvt, m.numero, m.mvt
""",
}
PRODUCTION_PATTERNS = {"police": r"^\s*\S+\s+Police\s+N°", "quittance": r"^\s*\S+\s+Quittance\s+N°"}


@router.get("/production")
def daily_production(
    debut: date,
    fin: date,
    type: str = Query("police", pattern="^(police|quittance)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Opérations journalières: the policies (or quittances) added, modified or deleted between debut and fin."""
    if fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    try:
        rows = db.execute(
            text(PRODUCTION_SQL[type]), {"debut": debut, "fin": fin + timedelta(days=1), "pattern": PRODUCTION_PATTERNS[type]},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [{**_plain(row), "mvt": "Suppression" if row["mvt"] == "Suppresion" else row["mvt"]} for row in rows]
    return {"items": items}


# ---------------------------------------------------------------------------------------------
# Opérations journalières (financières)
# ---------------------------------------------------------------------------------------------
# Technique: the client payments of the period (TRegCli with a piece, one line per quittance paid, Opé R) and the
# technical payments (TDepense 'T': claims, rebates, experts; Opé D). Divers: the sorties diverses (TDepense 'S', Opé D)
# and entrées diverses (TDepense 'E', Opé R).
FINANCIAL_SQL = {
    "technique": """
SELECT 'R' AS ope, r.date_reg_cli::date AS date_op, r.num_reg_cli AS numero, d."Num_Recu" AS ligne,
       TRIM(r.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, TRIM(q.num_int) AS num_police, q.echeance,
       CASE WHEN TRIM(q.tc) = 'T' THEN 'T' || COALESCE(TRIM(q."Periode"), '') ELSE TRIM(q.tc) END AS tc,
       q.montant_quittance AS prime, d."Montant" AS reglement,
       CASE WHEN ROW_NUMBER() OVER (PARTITION BY r.num_reg_cli ORDER BY d."Num_Recu") = 1 THEN r.mont_reg_cli END AS total,
       TRIM(r.num_piece) AS num_cheq, p.datep::date AS date_cheq, TRIM(r.code_banque) AS compte,
       NULL AS sin_rist, NULL AS beneficiaire
FROM dbo."TRegCli" r
JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = r.num_reg_cli
LEFT JOIN dbo."TQuittance" q ON TRIM(q.num_quittance) = TRIM(d."Num_quitance")
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(r.code_client)
LEFT JOIN LATERAL (SELECT datep FROM dbo."TPiece" p WHERE TRIM(p.nump) = TRIM(r.num_piece) ORDER BY (TRIM(p.code_client) = TRIM(r.code_client)) DESC LIMIT 1) p ON TRUE
WHERE r.date_reg_cli >= :debut AND r.date_reg_cli < :fin AND COALESCE(TRIM(r.type_reg_cli), '') <> 'R'
UNION ALL
SELECT 'D', d.date_dep::date, d.num_dep, 0, TRIM(d.code_client), TRIM(c.nom_client), TRIM(d.num_police), NULL, NULL,
       NULL, d.mont_dep, NULL, TRIM(d.num_cheq), d.date_cheq::date, TRIM(d.code_banque),
       COALESCE(NULLIF(TRIM(d.sinistre), ''), NULLIF(TRIM(d.num_quittance), '')), TRIM(d.objet_dep)
FROM dbo."TDepense" d
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(d.code_client)
WHERE TRIM(d."Typ_dep") = 'T' AND d.date_dep >= :debut AND d.date_dep < :fin
ORDER BY date_op, ope, numero, ligne
""",
    "divers": """
SELECT CASE WHEN TRIM(d."Typ_dep") = 'E' THEN 'R' ELSE 'D' END AS ope, d.date_dep::date AS date_op, d.num_dep AS numero,
       d.mont_dep AS reglement, TRIM(d.num_cheq) AS num_cheq, d.date_cheq::date AS date_cheq, TRIM(d.code_banque) AS compte,
       TRIM(d.objet_dep) AS beneficiaire
FROM dbo."TDepense" d
WHERE TRIM(d."Typ_dep") IN ('S', 'E') AND d.date_dep >= :debut AND d.date_dep < :fin
ORDER BY date_op, ope, numero
""",
}


@router.get("/financier")
def daily_financial(
    debut: date,
    fin: date,
    type: str = Query("technique", pattern="^(technique|divers)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Opérations journalières financières between debut and fin, with the totals of the recettes (R) and dépenses (D)."""
    if fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    try:
        rows = db.execute(text(FINANCIAL_SQL[type]), {"debut": debut, "fin": fin + timedelta(days=1)}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [_plain(row) for row in rows]
    total = lambda ope: round(sum(float(row["reglement"] or 0) for row in items if row["ope"] == ope), 3)  # noqa: E731
    return {"items": items, "total_recette": total("R"), "total_depense": total("D")}


# ---------------------------------------------------------------------------------------------
# Etat des comptes client
# ---------------------------------------------------------------------------------------------
# Per client, for the quittances due between two dates (or all of them, the returned ones left out): the primes, the ristournes (quittances of type R),
# what the client paid on them (RDetRegCli), the commissions, and the claims paid to the client in the period (TDepense 'T'
# of nature S). Optional filters: one company, one client, one police; état: tous, comptabilisé, en instance, ristournes.
ACCOUNTS_SQL = """
WITH pol AS MATERIALIZED (  -- the policies of the company / the police asked for (none asked: not used)
    SELECT TRIM(num_int) AS num_int, TRIM(num_police) AS num_police FROM dbo."TPolice"
    WHERE (CAST(:compagnie AS int) IS NOT NULL OR CAST(:police AS text) IS NOT NULL)
      AND (CAST(:compagnie AS int) IS NULL OR code_compagnie = CAST(:compagnie AS int))
      AND (CAST(:police AS text) IS NULL OR TRIM(num_int) = CAST(:police AS text) OR TRIM(num_police) = CAST(:police AS text))
),
q AS MATERIALIZED (
    SELECT q.num_quittance, TRIM(q.code_client) AS code_client, TRIM(COALESCE(q.type_quittance, '')) = 'R' AS ristourne,
           COALESCE(q.prime_total, 0) AS prime_total, COALESCE(q.commission, 0) AS commission
    FROM dbo."TQuittance" q
    WHERE COALESCE(q.annule, 'N') <> 'O'
      AND q."Date_Retour" IS NULL  -- a quittance returned to the company is no longer the client's
      AND (CAST(:debut AS date) IS NULL OR (q.echeance >= CAST(:debut AS date) AND q.echeance < CAST(:fin AS date)))
      AND (CAST(:client AS text) IS NULL OR TRIM(q.code_client) = CAST(:client AS text))
      AND (CAST(:compagnie AS int) IS NULL AND CAST(:police AS text) IS NULL OR TRIM(q.num_int) IN (SELECT num_int FROM pol))
      AND CASE CAST(:etat AS text)
            WHEN 'comptabilise' THEN COALESCE(q."Comptabiliser", 'N') = 'O'
            WHEN 'instance' THEN COALESCE(q."Comptabiliser", 'N') <> 'O'
            WHEN 'ristourne' THEN TRIM(COALESCE(q.type_quittance, '')) = 'R'
            ELSE TRUE END
),
reg AS (
    SELECT d."Num_quitance" AS num_quittance, SUM(d."Montant") AS montant
    FROM dbo."RDetRegCli" d WHERE d."Num_quitance" IN (SELECT num_quittance FROM q) GROUP BY 1
),
quit AS (
    SELECT q.code_client,
           SUM(q.prime_total) FILTER (WHERE NOT q.ristourne) AS prime,
           ABS(SUM(q.prime_total) FILTER (WHERE q.ristourne)) AS ristourne,
           SUM(reg.montant) FILTER (WHERE NOT q.ristourne) AS reglement,
           SUM(q.commission) AS commission
    FROM q LEFT JOIN reg ON reg.num_quittance = q.num_quittance
    GROUP BY 1
),
sin AS (
    SELECT TRIM(d.code_client) AS code_client, SUM(d.mont_dep) AS sinistre
    FROM dbo."TDepense" d JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
    WHERE TRIM(d."Typ_dep") = 'T' AND TRIM(t."Nat_quit") = 'S' AND CAST(:etat AS text) <> 'ristourne'
      AND (CAST(:debut AS date) IS NULL OR (d.date_dep >= CAST(:debut AS date) AND d.date_dep < CAST(:fin AS date)))
      AND (CAST(:client AS text) IS NULL OR TRIM(d.code_client) = CAST(:client AS text))
      AND (CAST(:compagnie AS int) IS NULL AND CAST(:police AS text) IS NULL OR TRIM(d.num_police) IN (SELECT num_police FROM pol))
      AND CASE CAST(:etat AS text)
            WHEN 'comptabilise' THEN COALESCE(d."Comptabiliser", 'N') = 'O'
            WHEN 'instance' THEN COALESCE(d."Comptabiliser", 'N') <> 'O'
            ELSE TRUE END
    GROUP BY 1
)
SELECT COALESCE(quit.code_client, sin.code_client) AS code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = COALESCE(quit.code_client, sin.code_client) LIMIT 1) AS nom_client,
       ROUND(COALESCE(quit.prime, 0)::numeric, 3) AS prime, ROUND(COALESCE(quit.ristourne, 0)::numeric, 3) AS ristourne,
       ROUND(COALESCE(sin.sinistre, 0)::numeric, 3) AS sinistre, ROUND(COALESCE(quit.reglement, 0)::numeric, 3) AS reglement,
       ROUND(COALESCE(quit.commission, 0)::numeric, 3) AS commission
FROM quit FULL JOIN sin ON sin.code_client = quit.code_client
ORDER BY COALESCE(quit.prime, 0) DESC, 1
"""
ACCOUNT_TOTALS = ("prime", "ristourne", "sinistre", "reglement", "commission")

# The detail (what an employee sees): every quittance of the same selection, with its company, échéance, police and
# prime; the ristournes only when they are asked for.
ACCOUNT_LINES_SQL = """
WITH pol AS MATERIALIZED (
    SELECT TRIM(num_int) AS num_int, TRIM(num_police) AS num_police FROM dbo."TPolice"
    WHERE (CAST(:compagnie AS int) IS NOT NULL OR CAST(:police AS text) IS NOT NULL)
      AND (CAST(:compagnie AS int) IS NULL OR code_compagnie = CAST(:compagnie AS int))
      AND (CAST(:police AS text) IS NULL OR TRIM(num_int) = CAST(:police AS text) OR TRIM(num_police) = CAST(:police AS text))
),
q AS MATERIALIZED (
    SELECT TRIM(q.code_client) AS code_client, q.echeance::date AS echeance, TRIM(q.num_int) AS num_int,
           TRIM(q.num_quittance) AS num_quittance, COALESCE(q.prime_total, 0) AS prime
    FROM dbo."TQuittance" q
    WHERE COALESCE(q.annule, 'N') <> 'O'
      AND q."Date_Retour" IS NULL
      AND (CAST(:debut AS date) IS NULL OR (q.echeance >= CAST(:debut AS date) AND q.echeance < CAST(:fin AS date)))
      AND (CAST(:client AS text) IS NULL OR TRIM(q.code_client) = CAST(:client AS text))
      AND (CAST(:compagnie AS int) IS NULL AND CAST(:police AS text) IS NULL OR TRIM(q.num_int) IN (SELECT num_int FROM pol))
      AND (TRIM(COALESCE(q.type_quittance, '')) = 'R') = (CAST(:etat AS text) = 'ristourne')
      AND CASE CAST(:etat AS text)
            WHEN 'comptabilise' THEN COALESCE(q."Comptabiliser", 'N') = 'O'
            WHEN 'instance' THEN COALESCE(q."Comptabiliser", 'N') <> 'O'
            ELSE TRUE END
),
p AS (
    SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, TRIM(num_police) AS num_police, code_compagnie
    FROM dbo."TPolice" WHERE TRIM(num_int) IN (SELECT num_int FROM q)
    ORDER BY TRIM(num_int), effet DESC NULLS LAST
)
SELECT q.code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = q.code_client LIMIT 1) AS nom_client,
       p.code_compagnie, (SELECT TRIM(k.nom_compagnie) FROM dbo."TCompagnie" k WHERE k.code_compagnie = p.code_compagnie) AS nom_compagnie,
       q.echeance, COALESCE(p.num_police, q.num_int) AS num_police, q.num_quittance, ROUND(q.prime::numeric, 3) AS prime
FROM q LEFT JOIN p ON p.num_int = q.num_int
ORDER BY CASE WHEN q.code_client ~ '^[0-9]+$' THEN CAST(q.code_client AS bigint) END NULLS LAST, q.code_client,
         nom_compagnie NULLS LAST, q.echeance, q.num_quittance
"""


@router.get("/comptes-clients")
def client_accounts(
    debut: date | None = None,
    fin: date | None = None,
    code_compagnie: int | None = None,
    code_client: str | None = Query(default=None, max_length=20),
    police: str | None = Query(default=None, max_length=30),
    etat: str = Query("tous", pattern="^(tous|comptabilise|instance|ristourne)$"),
    presentation: str = Query("synthese", pattern="^(synthese|detail)$"),
    user: dict[str, Any] = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Etat des comptes client. Administrator: one line per client with its primes, ristournes, sinistres, règlements
    and commissions (or the detail). Any other user: the detail only, client by client and company by company, the
    quittances and their primes (no commission), as in the legacy software."""
    if not is_admin(user):
        presentation = "detail"
    if (debut is None) != (fin is None):
        raise HTTPException(status_code=422, detail="Donnez la date de début et la date de fin.")
    if debut and fin and fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    params = {
        "debut": debut, "fin": fin + timedelta(days=1) if fin else None, "compagnie": code_compagnie,
        "client": (code_client or "").strip() or None, "police": (police or "").strip() or None, "etat": etat,
    }
    try:
        rows = db.execute(text(ACCOUNT_LINES_SQL if presentation == "detail" else ACCOUNTS_SQL), params).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [_plain(row) for row in rows]
    if presentation == "detail":
        return {"presentation": "detail", "lines": items, "total": round(sum(float(row["prime"] or 0) for row in items), 3)}
    return {"presentation": "synthese", "items": items, "totals": {key: round(sum(float(row[key] or 0) for row in items), 3) for key in ACCOUNT_TOTALS}}


# ---------------------------------------------------------------------------------------------
# Etat de la recette technique
# ---------------------------------------------------------------------------------------------
# The client payments of the period (TRegCli, except the rests settled without a piece, type R), one line per quittance
# paid (RDetRegCli), with the company of the quittance's policy and the feuille de caisse (JEQ) that declared it.
RECETTE_SQL = """
SELECT r.date_reg_cli::date AS date_reg, r.num_reg_cli, d."Num_Recu" AS num_recu, TRIM(r.type_reg_cli) AS type_reg, TRIM(r.num_piece) AS num_piece,
       TRIM(r.code_banque) AS banque, TRIM(r.code_client) AS code_client, TRIM(c.nom_client) AS nom_client,
       TRIM(q.num_int) AS num_int, TRIM(p.num_police) AS num_police, TRIM(d."Num_quitance") AS num_quittance, q.echeance::date AS echeance,
       p.code_compagnie, TRIM(cp.nom_compagnie) AS nom_compagnie, d."Montant" AS montant,
       COALESCE(d."Comptabiliser", 'N') = 'O' AS comptabilise, d."Date_Comptabiliser"::date AS date_comptabilise
FROM dbo."TRegCli" r
JOIN dbo."RDetRegCli" d ON d."Num_reg_cli" = r.num_reg_cli
LEFT JOIN dbo."TQuittance" q ON q.num_quittance = d."Num_quitance"
LEFT JOIN LATERAL (SELECT num_police, code_compagnie FROM dbo."TPolice" p WHERE TRIM(p.num_int) = TRIM(q.num_int) LIMIT 1) p ON TRUE
LEFT JOIN dbo."TCompagnie" cp ON cp.code_compagnie = p.code_compagnie
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(r.code_client)
WHERE r.date_reg_cli >= :debut AND r.date_reg_cli < :fin AND COALESCE(TRIM(r.type_reg_cli), '') <> 'R'
  AND (CAST(:compagnie AS int) IS NULL OR p.code_compagnie = CAST(:compagnie AS int))
ORDER BY r.date_reg_cli, r.num_reg_cli, d."Num_Recu"
"""


@router.get("/recette")
def recette_technique(debut: date, fin: date, code_compagnie: int | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Etat de la recette technique between debut and fin (optionally of one company); the grouping is done by the page."""
    if fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    try:
        rows = db.execute(text(RECETTE_SQL), {"debut": debut, "fin": fin + timedelta(days=1), "compagnie": code_compagnie}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [_plain(row) for row in rows]
    return {"items": items, "total": round(sum(float(row["montant"] or 0) for row in items), 3)}


# ---------------------------------------------------------------------------------------------
# Retour des quittances
# ---------------------------------------------------------------------------------------------
# The quittances of a type (T/C) and period, of one company or all; ticking « Retour » gives them back to the company:
# TQuittance "Date_Retour" is set (as the legacy software did), unticking clears it. With `retournees`, the quittances
# returned during the period's month (by their return date, whatever their own period).
RETURNS_SQL = """
SELECT TRIM(q.num_int) AS num_police, TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, q.echeance::date AS echeance,
       TRIM(q.num_quittance) AS num_quittance, q.prime_net, q.commission, q.prime_total, q.solde_quittance, q."Date_Retour" AS date_retour,
       TRIM(q."Periode") AS periode, TRIM(k.nom_compagnie) AS nom_compagnie
FROM dbo."TQuittance" q
JOIN dbo."TPolice" p ON TRIM(p.num_int) = TRIM(q.num_int)
JOIN dbo."TCompagnie" k ON k.code_compagnie = p.code_compagnie
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
WHERE q.tc = :tc AND TRIM(COALESCE(q.type_quittance, '')) <> 'R'
  AND (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
  AND UPPER(TRIM(k.nom_compagnie)) <> ALL(:excluded)
  AND CASE WHEN :retournees THEN q."Date_Retour" >= :debut AND q."Date_Retour" < :fin ELSE TRIM(q."Periode") = :periode END
ORDER BY CASE WHEN :retournees THEN q."Date_Retour" END, q.echeance, TRIM(q.num_quittance)
"""
# Companies the agency does not return quittances to (as in Réception comptant, Gestion des quittances, Etat du terme).
RETURN_EXCLUDED = ["STAR", "SALIM", "MAE", "EL AMANA TAKAFUL", "EUROP ASSISTANCE", "EUROPE ASSISTANCE"]


@router.get("/retours")
def quittance_returns(
    code_compagnie: int | None = None,
    periode: str = Query(pattern=r"^[0-9]{2}/[0-9]{4}$"),
    tc: str = Query("C", pattern="^[TC]$"),
    retournees: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Quittances of a type and period (every company without code_compagnie) with their return date; with retournees,
    those returned during the period's month."""
    month, year = int(periode[:2]), int(periode[3:])
    if not 1 <= month <= 12:
        raise HTTPException(status_code=422, detail="Période invalide.")
    debut = date(year, month, 1)
    fin = date(year + month // 12, month % 12 + 1, 1)
    try:
        rows = db.execute(
            text(RETURNS_SQL),
            {"compagnie": code_compagnie, "tc": tc, "periode": periode, "retournees": retournees, "debut": debut, "fin": fin,
             "excluded": RETURN_EXCLUDED},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": [_plain(row) for row in rows]}


class ReturnIn(BaseModel):
    num_quittance: str = Field(min_length=1, max_length=15)
    num_police: str = Field(min_length=1, max_length=20)
    retour: bool


@router.put("/retours/quittance")
def set_quittance_return(payload: ReturnIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Returns a quittance to the company (Date_Retour = now) or takes the return back (Date_Retour emptied)."""
    keys = {"q": payload.num_quittance.strip(), "n": payload.num_police.strip()}
    try:
        result = db.execute(
            text(
                'UPDATE dbo."TQuittance" SET "Date_Retour" = CASE WHEN :retour THEN LOCALTIMESTAMP(0) END, "Update_User" = :u, "Update_Date" = now() '
                'WHERE TRIM(num_quittance) = :q AND TRIM(num_int) = :n AND ("Date_Retour" IS NULL) = :retour'
            ),
            {**keys, "retour": payload.retour, "u": str(user.get("sub", ""))[:20]},
        )
        if result.rowcount != 1:
            db.rollback()
            state = "déjà retournée" if payload.retour else "pas retournée"
            raise HTTPException(status_code=409, detail=f"La quittance N° {payload.num_quittance} est {state} : rechargez la liste.")
        returned = db.execute(text('SELECT "Date_Retour" FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :q AND TRIM(num_int) = :n'), keys).scalar()
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"num_quittance": keys["q"], "date_retour": returned.isoformat() if returned else None}


# ---------------------------------------------------------------------------------------------
# Etat du terme
# ---------------------------------------------------------------------------------------------
# The policies en cours (etat AN) whose terme falls in the month, as the legacy "Etat de terme du mois": the month of
# their échéance ("dd/mm"), or for a policy paid by months (Unite M, durée 3 or 6) every durée months from it (01/09
# by 3 months: September, December, March, June). A policy with an expiration date is temporary.
# Its branche: TPolice.code_branche often holds the start of the police number ("001", "252") rather than a TBranche
# code, or nothing. So, within the company: the exact code, else its first two characters, else the branche name
# that the risque starts with (the longest, "CHAMEL M1+M5" is CHAMEL). None of them: no branche.
TERM_SQL = """
WITH p AS (
    SELECT TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client, TRIM(p.echeance) AS echeance,
           UPPER(TRIM(p."Unite")) AS unite, p.duree, UPPER(COALESCE(TRIM(p."TousRisque"), '')) = 'O' AS tous_risque,
           UPPER(TRIM(p.etat)) AS etat, p.expiration, p.code_compagnie, NULLIF(TRIM(p.code_branche), '') AS code_branche,
           UPPER(TRIM(p.risque)) AS risque,
           CAST(SUBSTRING(TRIM(p.echeance) FROM '^[0-9]{1,2}[/-]([0-9]{1,2})') AS integer) AS mois_echeance
    FROM dbo."TPolice" p
    WHERE UPPER(TRIM(p.etat)) = 'AN'
      AND (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
)
SELECT p.num_police, p.code_client, TRIM(c.nom_client) AS nom_client, p.echeance, p.unite, p.duree, p.tous_risque, p.etat,
       p.expiration, TRIM(k.nom_compagnie) AS nom_compagnie,
       COALESCE(
           (SELECT NULLIF(TRIM(b.nom_branche), '') FROM dbo."TBranche" b
            WHERE b.code_compagnie = p.code_compagnie AND TRIM(b.code_branche) = p.code_branche LIMIT 1),
           (SELECT NULLIF(TRIM(b.nom_branche), '') FROM dbo."TBranche" b
            WHERE b.code_compagnie = p.code_compagnie AND TRIM(b.code_branche) = LEFT(p.code_branche, 2) LIMIT 1),
           (SELECT NULLIF(TRIM(b.nom_branche), '') FROM dbo."TBranche" b
            WHERE b.code_compagnie = p.code_compagnie AND LEFT(p.risque, LENGTH(TRIM(b.nom_branche))) = UPPER(TRIM(b.nom_branche))
            ORDER BY LENGTH(TRIM(b.nom_branche)) DESC LIMIT 1)
       ) AS nom_branche,
       CAST(EXTRACT(YEAR FROM c."DateNaissance") AS integer) AS annee_naissance
FROM p
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = p.code_client
JOIN dbo."TCompagnie" k ON k.code_compagnie = p.code_compagnie
WHERE p.mois_echeance BETWEEN 1 AND 12
  AND UPPER(TRIM(k.nom_compagnie)) <> ALL(:excluded)
  AND (p.mois_echeance = :mois
       OR (p.unite = 'M' AND p.duree BETWEEN 1 AND 11 AND MOD(:mois - p.mois_echeance + 12, p.duree) = 0))
ORDER BY p.num_police
"""


@router.get("/terme")
def term_statement(
    mois: int = Query(..., ge=1, le=12),
    code_compagnie: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        rows = db.execute(text(TERM_SQL), {"mois": mois, "compagnie": code_compagnie, "excluded": RETURN_EXCLUDED}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [{**_plain(row), "temporaire": row["expiration"] is not None} for row in rows]
    return {"items": items}


# ---------------------------------------------------------------------------------------------
# Etat détaillé des comptes (bank accounts of the agency)
# ---------------------------------------------------------------------------------------------
# The movements of an agency account (TBanque) between two dates, as the legacy "Etat détaillé des comptes":
#   crédit: the client settlements paid into it (TRegCli.code_banque; neither ristourne 'R' nor fictive ones), and the
#           entrées diverses (TDepense 'E');
#   débit:  every other payment made from it (TDepense: technique 'T' — claims, rebates, experts —, frais généraux 'D',
#           sorties diverses 'S', …).
# Solde: the account's opening balance (credit_ini − debit_ini at date_ini) plus the credits less the debits since then.
BANK_MOVES_SQL = """
SELECT 'C' AS sens, r.date_reg_cli::date AS date_mvt, 'R' || r.num_reg_cli AS id, TRIM(r.num_piece) AS reference,
       TRIM(r.type_reg_cli) AS mode, r.mont_reg_cli AS montant, TRIM(r.code_client) AS code_client, TRIM(c.nom_client) AS nom_client,
       'Règlement client' AS libelle
FROM dbo."TRegCli" r
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(r.code_client)
WHERE TRIM(r.code_banque) = :banque AND r.date_reg_cli >= :debut AND r.date_reg_cli < :fin
  AND COALESCE(TRIM(r.type_reg_cli), '') <> 'R' AND COALESCE(TRIM(r."Fectif"), 'N') <> 'O' AND COALESCE(r.mont_reg_cli, 0) <> 0
UNION ALL
SELECT CASE WHEN TRIM(d."Typ_dep") = 'E' THEN 'C' ELSE 'D' END, d.date_dep::date, 'D' || TRIM(d."Typ_dep") || d.num_dep,
       TRIM(d.num_cheq), TRIM(d.mod_dep), d.mont_dep, TRIM(d.code_client), TRIM(c.nom_client),
       COALESCE(NULLIF(TRIM(d.objet_dep), ''),
                CASE TRIM(d."Typ_dep") WHEN 'T' THEN 'Dépense technique' WHEN 'D' THEN 'Frais généraux'
                     WHEN 'S' THEN 'Sortie diverse' WHEN 'E' THEN 'Entrée diverse' ELSE 'Dépense' END)
FROM dbo."TDepense" d
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(d.code_client)
WHERE TRIM(d.code_banque) = :banque AND d.date_dep >= :debut AND d.date_dep < :fin AND COALESCE(d.mont_dep, 0) <> 0
ORDER BY date_mvt, sens DESC, id
"""

BANK_TOTALS_SQL = """
SELECT COALESCE(SUM(montant) FILTER (WHERE sens = 'C'), 0) AS credit, COALESCE(SUM(montant) FILTER (WHERE sens = 'D'), 0) AS debit
FROM (""" + BANK_MOVES_SQL.split("ORDER BY")[0] + """) m
"""


@router.get("/comptes-banque/banques")
def bank_accounts(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """The agency accounts, those in use first."""
    try:
        rows = db.execute(text(
            'SELECT TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque, TRIM(num_compte_b) AS num_compte, '
            'COALESCE("Archive", FALSE) AS archive FROM dbo."TBanque" ORDER BY COALESCE("Archive", FALSE), code_banque'
        )).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return [_plain(row) for row in rows]


@router.get("/comptes-banque")
def bank_statement(
    code_banque: str = Query(..., min_length=1, max_length=15),
    debut: date = Query(...),
    fin: date = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Etat détaillé des comptes: the opening balance at debut, the movements of debut..fin, and the closing balance."""
    if fin < debut:
        raise HTTPException(status_code=422, detail="La date de fin précède la date de début.")
    banque = code_banque.strip()
    try:
        account = db.execute(text(
            'SELECT TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque, TRIM(num_compte_b) AS num_compte, '
            'COALESCE(credit_ini, 0) - COALESCE(debit_ini, 0) AS solde_initial, date_ini FROM dbo."TBanque" WHERE TRIM(code_banque) = :b'
        ), {"b": banque}).mappings().first()
        if account is None:
            raise HTTPException(status_code=404, detail=f"Compte {banque} introuvable.")
        start = account["date_ini"].date() if account["date_ini"] else date(1900, 1, 1)
        before = db.execute(text(BANK_TOTALS_SQL), {"banque": banque, "debut": start, "fin": debut}).mappings().one() if debut > start else {"credit": 0, "debit": 0}
        rows = db.execute(text(BANK_MOVES_SQL), {"banque": banque, "debut": max(debut, start), "fin": fin + timedelta(days=1)}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    solde = round(float(account["solde_initial"] or 0) + float(before["credit"] or 0) - float(before["debit"] or 0), 3)
    depart = solde
    items = []
    for row in rows:
        amount = float(row["montant"] or 0)
        solde = round(solde + (amount if row["sens"] == "C" else -amount), 3)
        items.append({**_plain(row), "montant": round(amount, 3), "solde": solde})
    credit = round(sum(item["montant"] for item in items if item["sens"] == "C"), 3)
    debit = round(sum(item["montant"] for item in items if item["sens"] == "D"), 3)
    return {
        "banque": {key: account[key] for key in ("code_banque", "nom_banque", "num_compte")},
        "solde_depart": depart, "total_credit": credit, "total_debit": debit, "solde_final": solde, "items": items,
    }


# ---------------------------------------------------------------------------------------------
# Etat des dépenses techniques
# ---------------------------------------------------------------------------------------------
# The technical payments (TDepense 'T': claims, rebates, experts) of a company's policies dated in a month, as the legacy
# "ETAT DES DEPENSES TECHNIQUES" (ImpDepTech). Its Observation column gives the feuille de caisse the payment went into:
# the day it was comptabilisé (Journal de dépense), or "en instance" while it is not.
TECH_EXPENSES_SQL = """
WITH d AS MATERIALIZED (
    SELECT d.*, TRIM(t.libelle) AS libelle, TRIM(t."Nat_quit") AS nature
    FROM dbo."TDepense" d JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
    WHERE TRIM(d."Typ_dep") = 'T' AND d.date_dep >= :debut AND d.date_dep < :fin
),
pol AS (
    SELECT DISTINCT ON (TRIM(num_police)) TRIM(num_police) AS num_police, code_compagnie
    FROM dbo."TPolice" WHERE TRIM(num_police) IN (SELECT TRIM(num_police) FROM d)
    ORDER BY TRIM(num_police), effet DESC NULLS LAST
)
SELECT d.num_dep, d.date_dep::date AS date_dep, TRIM(d.num_police) AS num_police, TRIM(d.code_client) AS code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = TRIM(d.code_client) LIMIT 1) AS assure,
       d.libelle, d.nature, ROUND(COALESCE(d.mont_dep, 0)::numeric, 3) AS montant, TRIM(d.num_cheq) AS num_cheq,
       TRIM(d.code_banque) AS banque, NULLIF(TRIM(d.sinistre), '') AS sinistre,
       COALESCE(d."Comptabiliser", 'N') = 'O' AS comptabilise, d."Date_Comptabiliser"::date AS date_feuille
FROM d JOIN pol p ON p.num_police = TRIM(d.num_police)
WHERE p.code_compagnie = :compagnie
ORDER BY d.date_dep, d.num_dep
"""


@router.get("/depenses-techniques")
def technical_expenses(
    compagnie: int = Query(...),
    mois: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="AAAA-MM"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    year, month = (int(part) for part in mois.split("-"))
    if not 1 <= month <= 12:
        raise HTTPException(status_code=422, detail="Mois invalide.")
    debut = date(year, month, 1)
    fin = date(year + month // 12, month % 12 + 1, 1)
    try:
        db.execute(text("SET LOCAL enable_nestloop = off"))  # same planner issue as the feuille de caisse
        items = [_plain(row) for row in db.execute(text(TECH_EXPENSES_SQL), {"compagnie": compagnie, "debut": debut, "fin": fin}).mappings().all()]
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    finally:
        db.rollback()
    return {"items": items, "total": round(sum(float(row["montant"] or 0) for row in items), 3)}


# ---------------------------------------------------------------------------------------------
# Validation of the feuille de caisse
# ---------------------------------------------------------------------------------------------

def _cash_validation(db: Session, compagnie: int, jour: date) -> dict[str, Any] | None:
    row = db.execute(text('SELECT * FROM dbo."TFeuilleCaisseValide" WHERE code_compagnie = :c AND jour = :j'), {"c": compagnie, "j": jour}).mappings().first()
    if not row:
        return None
    item = _plain(row)
    item["date_saisie"] = row["date_saisie"].isoformat(timespec="minutes") if row["date_saisie"] else None
    return item


@router.get("/feuille/validation")
def cash_validation(compagnie: int, jour: date, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Whether the feuille de caisse of the company on the day is validated (by whom, when, what it declared)."""
    _ensure_validated(db)
    try:
        return {"validation": _cash_validation(db, compagnie, jour)}
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc


class ValiderFeuille(BaseModel):
    compagnie: int
    jour: date
    # The lines chosen in the journal d'encaissement / de dépense; null: every line still to declare.
    quittances: list[str] | None = Field(default=None, max_length=5000)
    depenses: list[int] | None = Field(default=None, max_length=5000)


@router.post("/feuille/valider")
def validate_cash_sheet(payload: ValiderFeuille, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validates the feuille de caisse of the company on the day: the encaissements paid by then and not declared yet (a
    quittance paid in full, or its new acomptes) and the claim and rebate payments not comptabilisées yet are declared on
    that day, and the feuille is kept as validated (once per company and day)."""
    _ensure_validated(db)
    name = str(user.get("sub", ""))[:50]
    jour = payload.jour
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('feuille:' || :c || ':' || :j))"), {"c": str(payload.compagnie), "j": jour.isoformat()})
        if _cash_validation(db, payload.compagnie, jour):
            raise HTTPException(status_code=409, detail=f"La feuille de caisse du {jour.strftime('%d/%m/%Y')} de cette compagnie est déjà validée.")
        numbers = sorted({n.strip() for n in payload.quittances if n.strip()}) if payload.quittances is not None else None
        receipts = [] if numbers == [] else [
            line for line in _receipts(db, payload.compagnie, jour, False, numbers, regles=True)
            if not line["comptabilise"] and (line["paye"] or line["a_s"] == "A")
        ]
        for line in receipts:
            _declare_receipt(db, line, jour, name)
        ids = sorted(set(payload.depenses)) if payload.depenses is not None else None
        expenses = [] if ids == [] else [line for line in _expenses(db, payload.compagnie, jour, False, None, ids) if not line["comptabilise"]]
        if expenses:
            expense_ids = [line["num_dep"] for line in expenses]
            db.execute(text(
                'UPDATE dbo."TDepense" SET "Comptabiliser" = \'O\', "Date_Comptabiliser" = :d '
                'WHERE TRIM("Typ_dep") = \'T\' AND num_dep = ANY(CAST(:ids AS int[])) AND COALESCE("Comptabiliser", \'N\') <> \'O\''
            ), {"d": jour, "ids": expense_ids})
            for num_dep in expense_ids:
                db.execute(text(
                    'INSERT INTO dbo."TDepenseValide" (num_dep, jour, utilisateur, date_saisie) '
                    "VALUES (:n, :j, :u, now() AT TIME ZONE 'Africa/Tunis') ON CONFLICT DO NOTHING"
                ), {"n": num_dep, "j": jour, "u": name})
        db.execute(text(
            'INSERT INTO dbo."TFeuilleCaisseValide" (code_compagnie, jour, quittances, encaissements, depenses, montant_depenses, utilisateur, date_saisie) '
            "VALUES (:c, :j, :q, :e, :n, :m, :u, now() AT TIME ZONE 'Africa/Tunis')"
        ), {"c": payload.compagnie, "j": jour, "q": len(receipts), "e": round(sum(line["montant"] for line in receipts), 3),
            "n": len(expenses), "m": round(sum(float(line["montant"] or 0) for line in expenses), 3), "u": name})
        validation = _cash_validation(db, payload.compagnie, jour)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"validation": validation}
