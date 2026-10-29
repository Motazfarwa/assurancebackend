from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, and_, bindparam, func, or_, select, text
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




@router.get("/acomptes")
def list_partial_payments(
    code_client: str = Query(default="", max_length=8),
    nom_client: str = Query(default="", max_length=30),
    date_debut: date | None = None,
    date_fin: date | None = None,
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Etat des acomptes: receipts of type P partly paid, i.e. 0.01 < solde_quittance < montant_quittance, with what was paid
    and the Acompte (A) / Solde (S) mark of their last client settlement (RDetRegCli)."""
    if date_debut and date_fin and date_debut > date_fin:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    schema = settings.db_schema
    sql = f"""
        SELECT TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, TRIM(q.num_quittance) AS num_quittance,
               TRIM(p.num_police) AS num_police, TRIM(k.nom_compagnie) AS nom_compagnie, q."Periode", q.echeance,
               q.montant_quittance, q.montant_quittance - q.solde_quittance AS montant_regle, q.solde_quittance, q."Date_Reglement",
               COALESCE(NULLIF(TRIM(d."Acompte_Solde"), ''), 'A') AS acompte_solde
        FROM "{schema}"."TQuittance" q
        LEFT JOIN "{schema}"."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
        LEFT JOIN LATERAL (
            SELECT num_police, code_compagnie FROM "{schema}"."TPolice" WHERE TRIM(num_int) = TRIM(q.num_int) ORDER BY effet DESC NULLS LAST LIMIT 1
        ) p ON TRUE
        LEFT JOIN "{schema}"."TCompagnie" k ON k.code_compagnie = p.code_compagnie
        LEFT JOIN LATERAL (
            SELECT "Acompte_Solde" FROM "{schema}"."RDetRegCli" WHERE TRIM("Num_quitance") = TRIM(q.num_quittance) ORDER BY "Num_Recu" DESC NULLS LAST LIMIT 1
        ) d ON TRUE
        WHERE q.solde_quittance > 0.01 AND q.solde_quittance < q.montant_quittance
          AND q.type_quittance = 'P' AND COALESCE(q.annule, 'N') <> 'O'
          AND (:code = '' OR TRIM(q.code_client) = :code)
          AND (:nom = '' OR c.nom_client ILIKE :nom_like)
          AND (CAST(:debut AS timestamp) IS NULL OR q.echeance >= CAST(:debut AS timestamp))
          AND (CAST(:fin AS timestamp) IS NULL OR q.echeance < CAST(:fin AS timestamp))
        ORDER BY q.echeance DESC NULLS LAST, q.code_client
        LIMIT :limit
    """
    params = {
        "code": code_client.strip(), "nom": nom_client.strip(), "nom_like": f"%{nom_client.strip()}%", "limit": limit,
        "debut": datetime.combine(date_debut, time.min) if date_debut else None,
        "fin": datetime.combine(date_fin + timedelta(days=1), time.min) if date_fin else None,
    }
    try:
        rows = db.execute(text(sql), params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de lire les quittances en acompte.") from exc
    items = [serialize(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit}


@router.get("/etat")
def unpaid_statement(
    code_client: str = Query(default="", max_length=8),
    nom_client: str = Query(default="", max_length=30),
    piece: str = Query(default="avec", pattern="^(avec|sans)$"),
    date_debut: date | None = None,
    date_fin: date | None = None,
    type_client: str = Query(default="", pattern="^(|P|S)$"),
    code_compagnie: int | None = None,
    mise_en_demeure: bool = False,
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Etat des impayés, read live from TQuittance (TTempEtatImpaye is only a print buffer of the legacy software).
    A quittance is unpaid while solde_quittance > 0; the cheques / traites still "en instance" on it are the RPiece
    reservations of a TPiece not settled yet (regle <> 'O'). `piece=avec` keeps the quittances with such a cheque, their
    solde being what the cheques leave (0 when they cover it); `piece=sans` the ones without, solde > 0. Only the
    quittances whose type_quittance contains P are kept (no ristournes R, no C), cancelled ones are left out;
    `date_debut` / `date_fin` bound the échéance."""
    if date_debut and date_fin and date_debut > date_fin:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    schema = settings.db_schema
    sql = f"""
        WITH instance AS (
            SELECT TRIM(r."Num_Quittance") AS num_quittance, SUM(r."Montant_Reglement") AS montant,
                   STRING_AGG(DISTINCT TRIM(r."NumP"), ', ') AS pieces
            FROM "{schema}"."RPiece" r
            JOIN "{schema}"."TPiece" t ON TRIM(t.nump) = TRIM(r."NumP") AND TRIM(t.code_client) = TRIM(r."Code_Client")
            WHERE COALESCE(t.regle, 'N') <> 'O'
            GROUP BY 1
        )
        SELECT TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, TRIM(c.adresse) AS adresse,
               TRIM(c.ville) AS ville, c.cp, TRIM(c."Type_Client") AS type_client,
               TRIM(q.num_quittance) AS num_quittance, TRIM(q.num_int) AS num_int, TRIM(q.tc) AS tc, TRIM(q."Periode") AS periode,
               q.echeance, q.montant_quittance, q.solde_quittance, COALESCE(i.montant, 0) AS en_instance, i.pieces,
               GREATEST(q.solde_quittance - COALESCE(i.montant, 0), 0) AS solde,
               COALESCE(NULLIF(TRIM(p.risque), ''), '') AS risque, TRIM(q.observation) AS observation,
               p.code_compagnie, TRIM(k.nom_compagnie) AS nom_compagnie
        FROM "{schema}"."TQuittance" q
        LEFT JOIN "{schema}"."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
        LEFT JOIN instance i ON i.num_quittance = TRIM(q.num_quittance)
        LEFT JOIN LATERAL (
            SELECT risque, code_compagnie FROM "{schema}"."TPolice" WHERE TRIM(num_int) = TRIM(q.num_int) ORDER BY effet DESC NULLS LAST LIMIT 1
        ) p ON TRUE
        LEFT JOIN "{schema}"."TCompagnie" k ON k.code_compagnie = p.code_compagnie
        WHERE q.solde_quittance > 0.001 AND q.type_quittance ILIKE '%P%' AND COALESCE(q.annule, 'N') <> 'O'
          AND (CASE WHEN :piece = 'avec' THEN COALESCE(i.montant, 0) > 0 ELSE COALESCE(i.montant, 0) = 0 END)
          AND (:code = '' OR TRIM(q.code_client) = :code)
          AND (:nom = '' OR c.nom_client ILIKE :nom_like)
          AND (:type_client = '' OR TRIM(c."Type_Client") = :type_client)
          AND (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
          AND (NOT :mise OR q.mised = 'O' OR q.observation ILIKE '%mise en demeure%')
          AND (CAST(:debut AS timestamp) IS NULL OR q.echeance >= CAST(:debut AS timestamp))
          AND (CAST(:fin AS timestamp) IS NULL OR q.echeance < CAST(:fin AS timestamp))
        ORDER BY c.nom_client, q.code_client, k.nom_compagnie, q.echeance
        LIMIT :limit
    """
    params = {
        "piece": piece, "code": code_client.strip(), "nom": nom_client.strip(), "nom_like": f"%{nom_client.strip()}%",
        "type_client": type_client, "compagnie": code_compagnie, "mise": mise_en_demeure, "limit": limit,
        "debut": datetime.combine(date_debut, time.min) if date_debut else None,
        "fin": datetime.combine(date_fin + timedelta(days=1), time.min) if date_fin else None,
    }
    try:
        rows = db.execute(text(sql), params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de calculer l’état des impayés.") from exc
    items = [serialize(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit}


# Compagnies the agency does not work with: left out of the Etat des impayés choice.
HIDDEN_COMPANIES = ("MAE", "EL AMANA TAKAFUL", "SALIM", "STAR", "BH ASSURANCES")


@router.get("/etat/compagnies")
def unpaid_statement_companies(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        text(f'SELECT code_compagnie, TRIM(nom_compagnie) FROM "{settings.db_schema}"."TCompagnie" '
             'WHERE UPPER(TRIM(nom_compagnie)) NOT IN :hidden ORDER BY nom_compagnie').bindparams(bindparam("hidden", expanding=True)),
        {"hidden": list(HIDDEN_COMPANIES)},
    ).all()
    return {"items": [{"id": row[0], "label": row[1]} for row in rows]}
