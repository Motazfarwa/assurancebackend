from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api/sinistres", tags=["Sinistres"])


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


@router.get("")
def list_sinistres(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return a page of records, reflecting the existing table's columns."""
    try:
        table = Table(settings.db_table, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
        rows = db.execute(select(table).limit(limit).offset(offset)).mappings().all()
        return {
            "items": [{key: _json_value(value) for key, value in row.items()} for row in rows],
            "limit": limit,
            "offset": offset,
        }
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not read {settings.db_schema}.{settings.db_table}. Check the database connection and table settings.",
        ) from exc
