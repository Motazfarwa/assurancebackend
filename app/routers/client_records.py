from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api/clients", tags=["Client records"], dependencies=[Depends(require_current_user)])


def record_table(db: Session, name: str) -> Table:
    try:
        return Table(name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=f"Impossible de lire {settings.db_schema}.{name}. Vérifiez la connexion et la table.") from exc


def serialize(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


def client_records(db: Session, client_code: str, table_name: str, order_column: str, limit: int, client_column: str = "code_client", extra_filters: dict[str, str] | None = None) -> dict[str, Any]:
    table = record_table(db, table_name)
    if client_column not in table.c:
        raise HTTPException(status_code=503, detail=f"La colonne {client_column} est absente de {settings.db_schema}.{table_name}.")
    query = select(table).where(table.c[client_column] == client_code)
    for column, value in (extra_filters or {}).items():
        if column not in table.c:
            raise HTTPException(status_code=503, detail=f"La colonne {column} est absente de {settings.db_schema}.{table_name}.")
        query = query.where(table.c[column] == value)
    if order_column in table.c:
        query = query.order_by(table.c[order_column].desc().nullslast())
    rows = db.execute(query.limit(limit)).mappings().all()
    return {"items": [serialize(row) for row in rows], "total": len(rows)}


# The prime shown for a police (as the legacy software): its last prime quittance when that one is a terme (T), otherwise
# its first comptant (C) quittance. Ristournes (type R) never count.
DISPLAY_PRIMES_SQL = """
WITH q AS (
    SELECT TRIM(num_int) AS num_int, TRIM(num_quittance) AS num_quittance, TRIM(COALESCE(tc, '')) AS tc, echeance, date_emission,
           prime_net, commission, prime_total
    FROM dbo."TQuittance"
    WHERE TRIM(num_int) = ANY(:ids) AND UPPER(COALESCE(TRIM(type_quittance), 'P')) <> 'R' AND TRIM(COALESCE(tc, '')) IN ('T', 'C')
), last AS (
    SELECT DISTINCT ON (num_int) num_int, tc FROM q ORDER BY num_int, echeance DESC NULLS LAST, date_emission DESC NULLS LAST, num_quittance DESC
)
SELECT DISTINCT ON (q.num_int) q.num_int, q.num_quittance, q.tc, q.prime_net, q.commission, q.prime_total
FROM q JOIN last l ON l.num_int = q.num_int AND q.tc = CASE WHEN l.tc = 'T' THEN 'T' ELSE 'C' END
ORDER BY q.num_int,
         CASE WHEN l.tc = 'T' THEN q.echeance END DESC NULLS LAST,
         CASE WHEN l.tc = 'T' THEN q.date_emission END DESC NULLS LAST,
         q.echeance ASC NULLS LAST, q.date_emission ASC NULLS LAST, q.num_quittance
"""


def with_display_primes(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds to each police the prime shown (prime_net_aff, commission_aff, prime_total_aff, tc_aff, quittance_aff)."""
    ids = sorted({str(item.get("num_int") or "").strip() for item in items} - {""})
    if not ids:
        return items
    primes = {row["num_int"]: row for row in db.execute(text(DISPLAY_PRIMES_SQL), {"ids": ids}).mappings().all()}
    for item in items:
        row = primes.get(str(item.get("num_int") or "").strip())
        if row:
            item.update({"prime_net_aff": row["prime_net"], "commission_aff": row["commission"], "prime_total_aff": row["prime_total"],
                         "tc_aff": row["tc"], "quittance_aff": row["num_quittance"]})
    return items


@router.get("/{client_code}/policies")
def list_client_policies(
    client_code: str,
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    records = client_records(db, client_code, "TPolice", "effet", limit)
    with_display_primes(db, records["items"])
    return records


@router.get("/{client_code}/receipts")
def list_client_receipts(
    client_code: str,
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return client_records(db, client_code, "TQuittance", "date_emission", limit)


@router.get("/{client_code}/claims")
def list_client_claims(
    client_code: str,
    num_int: str | None = Query(default=None, max_length=15),
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    filters = {"Num_Int": num_int} if num_int else None
    return client_records(db, client_code, settings.db_table, "Date_Sinistre", limit, client_column="Code_Client", extra_filters=filters)
