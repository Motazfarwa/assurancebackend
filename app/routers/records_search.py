"""Search of policies and receipts across clients (filters of the "Polices du client" / "Quittances de la police" windows)."""
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.routers.client_records import serialize


router = APIRouter(prefix="/api/records", tags=["Recherche polices et quittances"], dependencies=[Depends(require_current_user)])

BRANCH_EXPRESSION = "COALESCE(NULLIF(TRIM(q.code_branche), ''), TRIM(p.code_branche))"


def _like(value: str) -> str:
    return f"%{value.strip()}%"


@router.get("/policies")
def search_policies(
    code_client: str | None = Query(default=None, max_length=8),
    nom_client: str | None = Query(default=None, max_length=60),
    num_police: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if code_client and code_client.strip():
        conditions.append("TRIM(p.code_client) = :code_client")
        params["code_client"] = code_client.strip()
    if nom_client and nom_client.strip():
        conditions.append("c.nom_client ILIKE :nom_client")
        params["nom_client"] = _like(nom_client)
    if num_police and num_police.strip():
        conditions.append("TRIM(p.num_police) ILIKE :num_police")
        params["num_police"] = _like(num_police)
    rows = db.execute(
        text(
            'SELECT p.*, TRIM(c.nom_client) AS nom_client FROM dbo."TPolice" p '
            'LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
            f"WHERE {' AND '.join(conditions)} ORDER BY p.effet DESC NULLS LAST, p.num_police LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return {"items": [serialize(row) for row in rows], "total": len(rows)}


@router.get("/receipts")
def search_receipts(
    code_client: str | None = Query(default=None, max_length=8),
    nom_client: str | None = Query(default=None, max_length=60),
    code_branche: str | None = Query(default=None, max_length=3),
    num_int: str | None = Query(default=None, max_length=15),
    num_quittance: str | None = Query(default=None, max_length=15),
    date_saisie: date | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if date_saisie:
        conditions.append("q.date_saisie >= :date_saisie AND q.date_saisie < :date_saisie_next")
        params["date_saisie"] = date_saisie
        params["date_saisie_next"] = date_saisie + timedelta(days=1)
    if num_quittance and num_quittance.strip():
        conditions.append("TRIM(q.num_quittance) ILIKE :num_quittance")
        params["num_quittance"] = _like(num_quittance)
    if num_int and num_int.strip():
        conditions.append("TRIM(q.num_int) = :num_int")
        params["num_int"] = num_int.strip()
    if code_client and code_client.strip():
        conditions.append("TRIM(q.code_client) = :code_client")
        params["code_client"] = code_client.strip()
    if nom_client and nom_client.strip():
        conditions.append("c.nom_client ILIKE :nom_client")
        params["nom_client"] = _like(nom_client)
    if code_branche and code_branche.strip():
        conditions.append(f"{BRANCH_EXPRESSION} = :code_branche")
        params["code_branche"] = code_branche.strip()
    rows = db.execute(
        text(
            f"SELECT q.*, TRIM(c.nom_client) AS nom_client, TRIM(p.num_police) AS num_police, {BRANCH_EXPRESSION} AS branche "
            'FROM dbo."TQuittance" q '
            'LEFT JOIN dbo."TPolice" p ON TRIM(p.num_int) = TRIM(q.num_int) '
            'LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client) '
            f"WHERE {' AND '.join(conditions)} ORDER BY q.date_emission DESC NULLS LAST, q.num_quittance LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return {"items": [serialize(row) for row in rows], "total": len(rows)}
