from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, and_, func, or_, select
from sqlalchemy.exc import NoSuchTableError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/impayes-agence", tags=["Etat des impayés"], dependencies=[Depends(require_current_user)])


def unpaid_table(db: Session) -> Table:
    try:
        return Table(settings.db_unpaid_table, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL est indisponible. Vérifiez DATABASE_URL.") from exc
    except NoSuchTableError as exc:
        raise HTTPException(status_code=503, detail=f"La table {settings.db_schema}.{settings.db_unpaid_table} est introuvable.") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de lire la table des impayés.") from exc


@router.get("/compagnies")
def list_unpaid_companies(db: Session = Depends(get_db)) -> dict[str, Any]:
    table = unpaid_table(db)
    rows = db.execute(
        select(table.c.nom_compagnie)
        .where(table.c.nom_compagnie.is_not(None), table.c.nom_compagnie != "")
        .distinct()
        .order_by(table.c.nom_compagnie)
    ).all()
    return {"items": [row[0] for row in rows]}

def serialize(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in row.items()}


@router.get("")
def list_agency_unpaid(
    code_client: str = Query(default="", max_length=8),
    nom_client: str = Query(default="", max_length=30),
    date_debut: date | None = None,
    date_fin: date | None = None,
    type_client: str = Query(default="", pattern="^(|P|S)$"),
    compagnie: str = Query(default="", max_length=50),
    piece: str = Query(default="", pattern="^(|avec|sans)$"),
    mise_en_demeure: bool = False,
    statut: str = Query(default="", pattern="^(|impaye|paye)$"),
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if date_debut and date_fin and date_debut > date_fin:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    table = unpaid_table(db)
    query = select(table)
    conditions = []
    if code_client.strip():
        conditions.append(table.c.code_client == code_client.strip())
    if nom_client.strip():
        conditions.append(table.c.nom_client.ilike(f"%{nom_client.strip()}%"))
    if date_debut:
        conditions.append(table.c.echeance >= datetime.combine(date_debut, time.min))
    if date_fin:
        conditions.append(table.c.echeance < datetime.combine(date_fin + timedelta(days=1), time.min))
    if type_client:
        conditions.append(table.c.tc == type_client)
    if compagnie.strip():
        conditions.append(table.c.nom_compagnie.ilike(compagnie.strip()))
    if piece == "avec":
        conditions.append(or_(and_(table.c.Inf_Piece.is_not(None), table.c.Inf_Piece != ""), and_(table.c.Total_Piece.is_not(None), table.c.Total_Piece > 0)))
    elif piece == "sans":
        conditions.append(and_(or_(table.c.Inf_Piece.is_(None), table.c.Inf_Piece == ""), or_(table.c.Total_Piece.is_(None), table.c.Total_Piece <= 0)))
    # Total_Piece = 0 (or empty) means the client has paid; anything else is unpaid.
    if statut == "impaye":
        conditions.append(func.coalesce(table.c.Total_Piece, 0) != 0)
    elif statut == "paye":
        conditions.append(func.coalesce(table.c.Total_Piece, 0) == 0)
    if mise_en_demeure:
        conditions.append(table.c.observation.ilike("%mise en demeure%"))
    if conditions:
        query = query.where(*conditions)
    rows = db.execute(query.order_by(table.c.echeance, table.c.code_client).limit(limit)).mappings().all()
    items = [serialize(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit}


