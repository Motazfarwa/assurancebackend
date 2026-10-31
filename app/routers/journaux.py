"""Journaux techniques, as in the legacy software:

- Journal d'encaissement (JEQ): the premium receipts (TQuittance, type P) of a company not yet declared to it. The ones
  chosen are "comptabilisées" on the journal date: TQuittance and their client settlement lines (RDetRegCli) get
  Comptabiliser = 'O' and Date_Comptabiliser = that date. A receipt only partly paid is declared for its new acomptes
  (the RDetRegCli lines), the receipt itself staying to declare.
- Journal de dépense: the claim and rebate payments (TDepense, Typ_dep 'T' whose type has Nat_quit S or R) of a company,
  comptabilisées the same way.

The journal number is the date (yyyymmdd), Validation stays 'N' (validated by the company later), like the legacy software.
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

# Receipts of the company still to declare (due by the journal date), and with `done` the ones declared on that date.
# declared_before / at_day / pending: the settlement lines declared before the date, on the date, not yet.
RECEIPTS_SQL = """
WITH q AS MATERIALIZED (
    SELECT q.* FROM dbo."TQuittance" q
    WHERE TRIM(q.type_quittance) = 'P' AND COALESCE(q.annule, 'N') <> 'O'
      AND (
        (COALESCE(q."Comptabiliser", 'N') <> 'O' AND q.echeance < :next_day)
        OR (:done AND q."Date_Comptabiliser" >= :day AND q."Date_Comptabiliser" < :next_day)
        OR (:done AND EXISTS (
            SELECT 1 FROM dbo."RDetRegCli" d WHERE TRIM(d."Num_quitance") = TRIM(q.num_quittance)
              AND d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" >= :day AND d."Date_Comptabiliser" < :next_day))
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
           SUM(CASE WHEN d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" < :day THEN d."Montant" ELSE 0 END) AS declared_before,
           SUM(CASE WHEN d."Comptabiliser" = 'O' AND d."Date_Comptabiliser" >= :day AND d."Date_Comptabiliser" < :next_day THEN d."Montant" ELSE 0 END) AS at_day,
           SUM(CASE WHEN COALESCE(d."Comptabiliser", 'N') <> 'O' THEN d."Montant" ELSE 0 END) AS pending
    FROM dbo."RDetRegCli" d WHERE TRIM(d."Num_quitance") IN (SELECT TRIM(num_quittance) FROM q)
    GROUP BY 1
)
SELECT TRIM(q.num_quittance) AS num_quittance, TRIM(q.tc) AS tc, q.echeance, p.num_police, TRIM(q."Code_Agence") AS code_agence,
       TRIM(q.code_client) AS code_client,
       (SELECT TRIM(c.nom_client) FROM dbo."TClient" c WHERE TRIM(c.code_client) = TRIM(q.code_client) LIMIT 1) AS assure,
       COALESCE(q.prime_net, 0) AS prime_net, COALESCE(q.frais_quit, 0) AS frais_quit, COALESCE(q.montant_quittance, 0) AS montant_quittance,
       COALESCE(q.solde_quittance, 0) AS solde_quittance, COALESCE(q.commission, 0) AS commission, COALESCE(q.timbre, 0) AS timbre,
       COALESCE(q.cout_attestation, 0) AS frais, q."Comptabiliser" = 'O' AND q."Date_Comptabiliser" >= :day AND q."Date_Comptabiliser" < :next_day AS declared_today,
       COALESCE(r.declared_before, 0) AS declared_before, COALESCE(r.at_day, 0) AS at_day, COALESCE(r.pending, 0) AS pending
FROM q
JOIN pol p ON p.num_int = TRIM(q.num_int)
LEFT JOIN reg r ON r.num_quittance = TRIM(q.num_quittance)
WHERE p.code_compagnie = :compagnie
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


def _receipts(db: Session, compagnie: int, day: date, done: bool, numbers: list[str] | None = None) -> list[dict[str, Any]]:
    rows = db.execute(
        text(RECEIPTS_SQL),
        {"compagnie": compagnie, "day": day, "next_day": day + timedelta(days=1), "done": done, "numbers": numbers},
    ).mappings().all()
    return [_receipt(row) for row in rows]


@router.get("/encaissement")
def receipts_journal(
    compagnie: int = Query(...),
    date_journal: date = Query(..., alias="date"),
    comptabilises: bool = Query(default=False, description="Also the receipts declared on that date"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        items = _receipts(db, compagnie, date_journal, comptabilises)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": items, "num_jeq": date_journal.strftime("%Y%m%d")}


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
       -- D.Com: on a rebate, the commission of the rebated receipt
       CASE WHEN d.nature = 'R' THEN ABS(COALESCE((
           SELECT q.commission FROM dbo."TQuittance" q WHERE TRIM(q.num_quittance) = TRIM(d.num_quittance) LIMIT 1), 0)) ELSE 0 END AS d_com
FROM d
JOIN pol p ON p.num_police = TRIM(d.num_police)
WHERE p.code_compagnie = :compagnie
ORDER BY d.date_dep, d.num_dep
"""


def _expenses(db: Session, compagnie: int, day: date, done: bool, nature: str | None = None, ids: list[int] | None = None) -> list[dict[str, Any]]:
    rows = db.execute(
        text(EXPENSES_SQL),
        {"compagnie": compagnie, "day": day, "next_day": day + timedelta(days=1), "done": done, "nature": nature, "ids": ids},
    ).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/depense")
def expenses_journal(
    compagnie: int = Query(...),
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
