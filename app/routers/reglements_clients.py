"""Règlements clients: the settlements a client made (dbo."TRegCli", the legacy software's table) and the receipts each
one paid (dbo."RDetRegCli"). Read only: settlements are entered with the Recette Technique.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db


router = APIRouter(prefix="/api/reglements-clients", tags=["Règlements clients"], dependencies=[Depends(require_current_user)])

# TRegCli.type_reg_cli: C = chèque, T = traite, V = virement, E = espèce; R = small balances settled without a piece.
TYPE_NAMES = {"C": "Chèque", "T": "Traite", "V": "Virement", "E": "Espèce", "R": "Sans pièce"}

REGLEMENT_COLUMNS = (
    'r.num_reg_cli, r.date_reg_cli, TRIM(r.code_client) AS code_client, TRIM(r.type_reg_cli) AS type_reg_cli, TRIM(r.num_piece) AS num_piece, '
    'r.mont_reg_cli, TRIM(r.code_banque) AS code_banque, TRIM(b.nom_banque) AS nom_banque, COALESCE(TRIM(r."Fectif"), \'N\') = \'O\' AS fictif, '
    '(SELECT COUNT(*) FROM dbo."RDetRegCli" d WHERE d."Num_reg_cli" = r.num_reg_cli) AS nb_quittances, '
    '(SELECT COALESCE(SUM(d."Montant"), 0) FROM dbo."RDetRegCli" d WHERE d."Num_reg_cli" = r.num_reg_cli) AS montant_affecte '
    'FROM dbo."TRegCli" r LEFT JOIN dbo."TBanque" b ON TRIM(b.code_banque) = TRIM(r.code_banque) '
)


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


def _reglement(row: Any) -> dict[str, Any]:
    item = _plain(row)
    item["type_libelle"] = TYPE_NAMES.get(item.get("type_reg_cli") or "", item.get("type_reg_cli") or "")
    return item


def _client(db: Session, code: str) -> dict[str, Any] | None:
    row = db.execute(
        text('SELECT TRIM(code_client) AS code_client, TRIM(nom_client) AS nom_client FROM dbo."TClient" WHERE TRIM(code_client) = :c'),
        {"c": code.strip()},
    ).mappings().first()
    return _plain(row) if row else None


@router.get("/client/{code_client}")
def client_reglements(code_client: str, limit: int = Query(default=500, ge=1, le=3000), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The client, its settlements (most recent first) and their totals."""
    try:
        client = _client(db, code_client)
        if client is None:
            raise HTTPException(status_code=404, detail=f"Client {code_client.strip()} introuvable.")
        params = {"c": code_client.strip(), "limit": limit}
        totals = db.execute(
            text('SELECT COUNT(*) AS nombre, COALESCE(SUM(mont_reg_cli), 0) AS montant FROM dbo."TRegCli" WHERE TRIM(code_client) = :c'), params
        ).mappings().first()
        rows = db.execute(
            text(f"SELECT {REGLEMENT_COLUMNS} WHERE TRIM(r.code_client) = :c ORDER BY r.date_reg_cli DESC NULLS LAST, r.num_reg_cli DESC LIMIT :limit"),
            params,
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Lecture des règlements impossible.") from exc
    return {"client": client, "reglements": [_reglement(row) for row in rows], "total": _plain(totals)}


@router.get("/{num_reg_cli}")
def get_reglement(num_reg_cli: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One settlement, its client and the receipts it paid (amount of the receipt, amount paid, balance left)."""
    try:
        row = db.execute(text(f"SELECT {REGLEMENT_COLUMNS} WHERE r.num_reg_cli = :n"), {"n": num_reg_cli}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Règlement n° {num_reg_cli} introuvable.")
        reglement = _reglement(row)
        details = db.execute(
            text(
                'SELECT d."Num_Recu" AS num_recu, TRIM(d."Num_quitance") AS num_quittance, TRIM(q.num_int) AS num_int, q.echeance, '
                'q.montant_quittance, d."Montant" AS montant, d."Solde_Quittance" AS solde_quittance, TRIM(d."Acompte_Solde") AS acompte_solde, '
                'TRIM(d."Observation") AS observation '
                'FROM dbo."RDetRegCli" d LEFT JOIN dbo."TQuittance" q ON TRIM(q.num_quittance) = TRIM(d."Num_quitance") '
                'WHERE d."Num_reg_cli" = :n ORDER BY d."Num_Recu"'
            ),
            {"n": num_reg_cli},
        ).mappings().all()
        client = _client(db, reglement["code_client"] or "")
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Lecture du règlement impossible.") from exc
    return {"reglement": reglement, "client": client, "details": [_plain(detail) for detail in details]}
