from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, and_, func, or_, select, text
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


@router.get("/reglements/clients")
def list_settlement_clients(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Clients having at least one settlement, for the filter dropdown."""
    schema = settings.db_schema
    sql = f"""
        SELECT DISTINCT q.code_client, c.nom_client
        FROM "{schema}"."TQuittance" q
        JOIN "{schema}"."TRDetRegCli" d ON d."Num_quitance" = q.num_quittance
        LEFT JOIN "{schema}"."TClient" c ON c.code_client = q.code_client
        WHERE q.code_client IS NOT NULL
        ORDER BY c.nom_client, q.code_client
    """
    try:
        rows = db.execute(text(sql)).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de lire la liste des clients.") from exc
    return {"items": [{"code_client": row["code_client"], "nom_client": row["nom_client"]} for row in rows]}


@router.get("/reglements")
def list_clients_by_settlement(
    code_client: str = Query(default="", max_length=8),
    nom_client: str = Query(default="", max_length=30),
    acompte_solde: str = Query(default="", pattern="^(|S|A)$"),
    statut: str = Query(default="impaye", pattern="^(impaye|paye)$"),
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Paid / unpaid receipts per client, based on the client settlements (TRDetRegCli)."""
    schema = settings.db_schema
    paid = 'r.nq IS NOT NULL AND (COALESCE(r.solde_flag, FALSE) OR COALESCE(r.solde, 1) <= 0)'
    sql = f"""
        SELECT q.code_client, c.nom_client, q.num_quittance, q."Periode", q.echeance, q.prime_total,
               COALESCE(r.regle, 0) AS montant_regle, COALESCE(r.solde, q.solde_quittance) AS solde_quittance,
               r.acompte_solde
        FROM "{schema}"."TQuittance" q
        LEFT JOIN "{schema}"."TClient" c ON c.code_client = q.code_client
        LEFT JOIN (
            SELECT "Num_quitance" AS nq, SUM("Montant") AS regle, MIN("Solde_Quittance") AS solde,
                   BOOL_OR("Acompte_Solde" = 'S') AS solde_flag,
                   CASE WHEN BOOL_OR("Acompte_Solde" = 'S') THEN 'S' WHEN BOOL_OR("Acompte_Solde" = 'A') THEN 'A' END AS acompte_solde
            FROM "{schema}"."TRDetRegCli" GROUP BY "Num_quitance"
        ) r ON r.nq = q.num_quittance
        WHERE {'(' + paid + ')' if statut == 'paye' else 'NOT (' + paid + ')'}
          AND (:code = '' OR q.code_client = :code)
          AND (:nom = '' OR c.nom_client ILIKE :nom_like)
          AND (:acompte = '' OR r.acompte_solde = :acompte)
        ORDER BY q.echeance, q.code_client
        LIMIT :limit
    """
    try:
        rows = db.execute(
            text(sql),
            {"code": code_client.strip(), "nom": nom_client.strip(), "nom_like": f"%{nom_client.strip()}%", "acompte": acompte_solde, "limit": limit},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de lire les règlements clients (TRDetRegCli).") from exc
    items = [serialize(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit}


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


