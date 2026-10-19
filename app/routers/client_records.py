from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, select
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


@router.get("/{client_code}/policies")
def list_client_policies(
    client_code: str,
    limit: int = Query(default=500, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return client_records(db, client_code, "TPolice", "effet", limit)


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
