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



# A quittance of a company bordereau counts (état des impayés, taux de recouvrement) only once its bordereau is fused
# (module 550): its fusion cancelled, it leaves the état again. The bordereau is the legacy one (TBordereau, one row per
# quittance, "Fusion" O/N) or an imported one (TBordereauLigne of TBordereauCie, fusionne O/N); a quittance found in a
# fused bordereau is kept even if another, not fused, also has it.
FUSED_ONLY_SQL = """
  AND TRIM(q.num_quittance) NOT IN (
    SELECT n FROM (
      SELECT TRIM(bx.num_quittance) AS n FROM "{schema}"."TBordereau" bx
      WHERE UPPER(COALESCE(TRIM(bx."Fusion"), 'N')) <> 'O' AND bx.num_quittance IS NOT NULL
      UNION
      SELECT TRIM(lx.num_quittance) FROM "{schema}"."TBordereauLigne" lx JOIN "{schema}"."TBordereauCie" cx ON cx.id_bordereau = lx.id_bordereau
      WHERE COALESCE(cx.fusionne, 'N') <> 'O' AND lx.num_quittance IS NOT NULL
      EXCEPT
      SELECT TRIM(bx.num_quittance) FROM "{schema}"."TBordereau" bx
      WHERE UPPER(COALESCE(TRIM(bx."Fusion"), 'N')) = 'O' AND bx.num_quittance IS NOT NULL
      EXCEPT
      SELECT TRIM(lx.num_quittance) FROM "{schema}"."TBordereauLigne" lx JOIN "{schema}"."TBordereauCie" cx ON cx.id_bordereau = lx.id_bordereau
      WHERE cx.fusionne = 'O' AND lx.num_quittance IS NOT NULL
    ) unfused WHERE n IS NOT NULL
  )
"""


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
    # TC of the quittance: "C" comptant, "T" terme, "" both
    tc: str = Query(default="", pattern="^(|C|T)$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Etat des impayés, read live from TQuittance (TTempEtatImpaye is only a print buffer of the legacy software).
    Only unpaid primes are kept (type_quittance = 'P', solde_quittance > 0), cancelled ones left out; the pièces
    (cheques / traites) en instance on a quittance are its RPiece rows whose TPiece is not settled yet (regle <> 'O').
    Both statements list the same quittances: `piece=sans` with their solde_quittance, `piece=avec` with
    solde_quittance less the pièces en instance. `date_debut` / `date_fin` bound the échéance; each row tells whether its
    policy is automobile, for the mise en demeure groups: its branch name holds "AUTO". Many policies carry no branch
    code, or one their company does not list (LLOYD "200" for its "20", GAT "122" for its "012"); their branch is then
    read from the policy number, which starts with it (GAT 011… AUTO PARTICULIER, LLOYD 20… AUTO), and failing that a
    policy with a registration plate is automobile."""
    if date_debut and date_fin and date_debut > date_fin:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    schema = settings.db_schema
    sql = f"""
        WITH instance AS (
            SELECT TRIM(r."Num_Quittance") AS num_quittance, SUM(r."Montant_Reglement") AS montant,
                   STRING_AGG(DISTINCT TRIM(r."NumP"), ', ') AS pieces
            FROM "{schema}"."RPiece" r
            -- A cashed pièce (regle = 'O') has already lowered solde_quittance: only the ones en instance are subtracted.
            JOIN "{schema}"."TPiece" t ON TRIM(t.nump) = TRIM(r."NumP") AND TRIM(t.code_client) = TRIM(r."Code_Client")
            WHERE COALESCE(t.regle, 'N') <> 'O'
            GROUP BY 1
        ),
        -- The quittances kept, chosen before the client / policy / company lookups.
        q AS MATERIALIZED (
            SELECT q.*
            FROM "{schema}"."TQuittance" q
            WHERE TRIM(q.type_quittance) = 'P' AND q.solde_quittance > 0 AND COALESCE(q.annule, 'N') <> 'O'
              AND (:code = '' OR TRIM(q.code_client) = :code)
              AND (:tc = '' OR UPPER(TRIM(q.tc)) = :tc)
              AND (NOT :mise OR q.mised = 'O' OR q.observation ILIKE '%mise en demeure%')
              AND (CAST(:debut AS timestamp) IS NULL OR q.echeance >= CAST(:debut AS timestamp))
              AND (CAST(:fin AS timestamp) IS NULL OR q.echeance < CAST(:fin AS timestamp))
              {FUSED_ONLY_SQL.format(schema=schema)}
        ),
        -- The latest version of each policy, read once (a per-row lookup on TRIM(num_int) cannot use an index).
        pol AS (
            SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, risque, code_compagnie, TRIM(code_branche) AS code_branche,
                   NULLIF(TRIM(num_imat), '') AS num_imat
            FROM "{schema}"."TPolice"
            WHERE TRIM(num_int) IN (SELECT TRIM(num_int) FROM q)
            ORDER BY TRIM(num_int), effet DESC NULLS LAST
        ),
        -- What the client already paid on the quittance (RDetRegCli: TRDetRegCli is its copy frozen in 2012).
        reg AS (
            SELECT TRIM(d."Num_quitance") AS num_quittance, SUM(d."Montant") AS montant, MAX(t.date_reg_cli) AS date_reglement
            FROM "{schema}"."RDetRegCli" d LEFT JOIN "{schema}"."TRegCli" t ON t.num_reg_cli = d."Num_reg_cli"
            WHERE TRIM(d."Num_quitance") IN (SELECT TRIM(num_quittance) FROM q)
            GROUP BY 1
        )
        SELECT TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, TRIM(c.adresse) AS adresse,
               TRIM(c.ville) AS ville, c.cp, TRIM(c."Type_Client") AS type_client,
               TRIM(q.num_quittance) AS num_quittance, TRIM(q.num_int) AS num_int, TRIM(q.tc) AS tc, TRIM(q."Periode") AS periode,
               q.echeance, q.montant_quittance, q.solde_quittance, COALESCE(i.montant, 0) AS en_instance, i.pieces,
               COALESCE(g.montant, 0) AS regle, g.date_reglement,
               CASE WHEN :piece = 'avec' THEN q.solde_quittance - COALESCE(i.montant, 0) ELSE q.solde_quittance END AS solde,
               COALESCE(NULLIF(TRIM(p.risque), ''), '') AS risque, TRIM(q.observation) AS observation,
               p.code_compagnie, TRIM(k.nom_compagnie) AS nom_compagnie, TRIM(b.nom_branche) AS nom_branche,
               COALESCE(b.nom_branche ILIKE '%AUTO%', FALSE) OR (b.nom_branche IS NULL AND p.num_imat IS NOT NULL) AS automobile
        FROM q
        LEFT JOIN "{schema}"."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
        LEFT JOIN instance i ON i.num_quittance = TRIM(q.num_quittance)
        LEFT JOIN reg g ON g.num_quittance = TRIM(q.num_quittance)
        LEFT JOIN pol p ON p.num_int = TRIM(q.num_int)
        LEFT JOIN "{schema}"."TCompagnie" k ON k.code_compagnie = p.code_compagnie
        -- The policy's branch, else the company's longest branch code the policy number starts with.
        LEFT JOIN LATERAL (
            SELECT x.nom_branche FROM "{schema}"."TBranche" x
            WHERE x.code_compagnie = p.code_compagnie
              AND (TRIM(x.code_branche) = p.code_branche OR (LENGTH(TRIM(x.code_branche)) >= 2 AND p.num_int LIKE TRIM(x.code_branche) || '%'))
            ORDER BY TRIM(x.code_branche) = COALESCE(p.code_branche, '') DESC, LENGTH(TRIM(x.code_branche)) DESC
            LIMIT 1
        ) b ON TRUE
        WHERE (:nom = '' OR c.nom_client ILIKE :nom_like)
          AND (:type_client = '' OR TRIM(c."Type_Client") = :type_client)
          AND (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
        -- Clients by code, ascending as numbers (the codes are text: shorter first, then alphabetical).
        ORDER BY LENGTH(TRIM(q.code_client)), TRIM(q.code_client), k.nom_compagnie, q.echeance
        LIMIT :limit
    """
    params = {
        "piece": piece, "code": code_client.strip(), "nom": nom_client.strip(), "nom_like": f"%{nom_client.strip()}%",
        "type_client": type_client, "compagnie": code_compagnie, "mise": mise_en_demeure, "limit": limit, "tc": tc,
        "debut": datetime.combine(date_debut, time.min) if date_debut else None,
        "fin": datetime.combine(date_fin + timedelta(days=1), time.min) if date_fin else None,
    }
    try:
        rows = db.execute(text(sql), params).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de calculer l’état des impayés.") from exc
    items = [serialize(row) for row in rows]
    return {"items": items, "count": len(items), "limit": limit}


RECOVERY_SQL = """
WITH q AS MATERIALIZED (
    -- the premium quittances (type P, not cancelled) whose échéance falls in the period, of the statement's client / TC
    SELECT TRIM(q.num_quittance) AS num_quittance, TRIM(q.num_int) AS num_int, TRIM(q.code_client) AS code_client,
           COALESCE(q.montant_quittance, 0) AS montant, GREATEST(COALESCE(q.solde_quittance, 0), 0) AS solde
    FROM "{schema}"."TQuittance" q
    WHERE TRIM(q.type_quittance) = 'P' AND COALESCE(q.annule, 'N') <> 'O'
      AND q.echeance >= :debut AND q.echeance < :lendemain
      AND (:code = '' OR TRIM(q.code_client) = :code)
      AND (:tc = '' OR UPPER(TRIM(q.tc)) = :tc)
      {fused_only}
),
pol AS (
    SELECT DISTINCT ON (TRIM(num_int)) TRIM(num_int) AS num_int, code_compagnie
    FROM "{schema}"."TPolice" WHERE CAST(:compagnie AS integer) IS NOT NULL AND TRIM(num_int) IN (SELECT num_int FROM q)
    ORDER BY TRIM(num_int), effet DESC NULLS LAST
),
scope AS MATERIALIZED (
    -- of the statement's company / type of client
    SELECT q.* FROM q
    LEFT JOIN pol p ON p.num_int = q.num_int
    WHERE (CAST(:compagnie AS integer) IS NULL OR p.code_compagnie = CAST(:compagnie AS integer))
      AND (:type_client = '' OR q.code_client IN (SELECT TRIM(code_client) FROM "{schema}"."TClient" WHERE TRIM("Type_Client") = :type_client))
),
instance AS (
    -- "Avec pièce": the cheques / traites en instance (not cashed yet) cover part of the solde, as on the statement
    SELECT TRIM(r."Num_Quittance") AS num_quittance, SUM(r."Montant_Reglement") AS montant
    FROM "{schema}"."RPiece" r
    JOIN "{schema}"."TPiece" t ON TRIM(t.nump) = TRIM(r."NumP") AND TRIM(t.code_client) = TRIM(r."Code_Client")
    WHERE :piece = 'avec' AND COALESCE(t.regle, 'N') <> 'O' AND TRIM(r."Num_Quittance") IN (SELECT num_quittance FROM scope)
    GROUP BY 1
)
SELECT COALESCE(SUM(s.montant), 0) AS primes_exigibles,
       COALESCE(SUM(GREATEST(s.solde - COALESCE(i.montant, 0), 0)), 0) AS reste,
       COUNT(*) AS quittances_exigibles,
       COUNT(*) FILTER (WHERE s.solde - COALESCE(i.montant, 0) > 0.0005) AS quittances_impayees
FROM scope s LEFT JOIN instance i ON i.num_quittance = s.num_quittance
"""


@router.get("/taux-recouvrement")
def recovery_rate(
    code_client: str = Query(default="", max_length=8),
    type_client: str = Query(default="", pattern="^(|P|S)$"),
    code_compagnie: int | None = None,
    tc: str = Query(default="", pattern="^(|C|T)$"),
    piece: str = Query(default="sans", pattern="^(avec|sans)$"),
    date_debut: date | None = None,
    date_fin: date | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Taux de recouvrement = (primes exigibles - reste impayé) / primes exigibles, read from the quittances themselves:
    the premium quittances (type P, not cancelled) whose échéance falls in the period, and what is still unpaid on them
    (solde_quittance; "avec pièce": less the pièces en instance, as on the statement). A settlement the client made is
    counted whether or not it was entered as a client settlement (RDetRegCli): many quittances are settled (solde 0)
    without one. For the current year (1 January to today), the whole previous year, and the statement's own échéance
    interval when it has one; for the statement's client, company, type of client and TC when given."""
    if date_debut and date_fin and date_debut > date_fin:
        raise HTTPException(status_code=422, detail="La date de début doit précéder la date de fin.")
    try:
        today = db.execute(text("SELECT (now() AT TIME ZONE 'Africa/Tunis')::date")).scalar()
        start = today.replace(month=1, day=1)
        db.execute(text("SET LOCAL enable_nestloop = off"))  # TRIM joins on legacy tables: hash joins only
        def period(first: date, last: date) -> Any:
            return db.execute(text(RECOVERY_SQL.format(schema=settings.db_schema, fused_only=FUSED_ONLY_SQL.format(schema=settings.db_schema))), {
                "code": code_client.strip(), "type_client": type_client, "compagnie": code_compagnie, "tc": tc, "piece": piece,
                "debut": datetime.combine(first, time.min), "lendemain": datetime.combine(last + timedelta(days=1), time.min),
            }).mappings().one()
        row = period(start, today)
        before = period(start.replace(year=start.year - 1), start - timedelta(days=1))
        chosen = None
        if date_debut or date_fin:
            first = date_debut or date(1900, 1, 1)
            last = date_fin or today
            chosen = {"du": date_debut.isoformat() if date_debut else None, "au": last.isoformat(), **_summary(period(first, last))}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de calculer le taux de recouvrement.") from exc
    finally:
        db.rollback()  # ends the read transaction, and with it the SET LOCAL
    return {
        "annee": today.year, "du": start.isoformat(), "au": today.isoformat(), **_summary(row),
        "precedente": {"annee": today.year - 1, "taux": _summary(before)["taux"]},
        "periode": chosen,
    }


def _summary(row: Any) -> dict[str, Any]:
    due, rest = float(row["primes_exigibles"] or 0), float(row["reste"] or 0)
    return {
        "primes_exigibles": round(due, 3), "reste": round(rest, 3), "encaissements": round(due - rest, 3),
        "quittances_exigibles": int(row["quittances_exigibles"] or 0), "quittances_impayees": int(row["quittances_impayees"] or 0),
        "taux": round((due - rest) / due * 100, 2) if due > 0.0005 else None,
    }


@router.get("/etat/liens/{code_client}")
def unpaid_statement_links(code_client: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The clients linked to a client, for its Etat des impayés: the client of its own "Lien" (code_locuteur, as stored
    on the client) and the clients whose "Lien" is this client. Only codes that are real clients are given."""
    schema = settings.db_schema
    code = code_client.strip()
    client = db.execute(
        text(f'SELECT TRIM(code_client) AS code_client, TRIM(nom_client) AS nom_client, NULLIF(TRIM(code_locuteur), \'\') AS code_locuteur '
             f'FROM "{schema}"."TClient" WHERE TRIM(code_client) = :c LIMIT 1'),
        {"c": code},
    ).mappings().first()
    if not client:
        raise HTTPException(status_code=404, detail=f"Client {code} introuvable.")
    rows = db.execute(
        text(
            f'SELECT TRIM(c.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, \'lien\' AS relation FROM "{schema}"."TClient" c '
            "WHERE TRIM(c.code_client) = :lien "
            "UNION ALL "
            f'SELECT TRIM(c.code_client), TRIM(c.nom_client), \'lie\' FROM "{schema}"."TClient" c WHERE TRIM(c.code_locuteur) = :c'
        ),
        {"c": code, "lien": client["code_locuteur"] or ""},
    ).mappings().all()
    links: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["code_client"] != code:
            links.setdefault(row["code_client"], dict(row))
    ordered = sorted(links.values(), key=lambda row: (row["relation"] != "lien", len(row["code_client"]), row["code_client"]))
    return {"client": {"code_client": client["code_client"], "nom_client": client["nom_client"]}, "liens": ordered}


# Compagnies the agency does not work with: left out of the Etat des impayés choice.
HIDDEN_COMPANIES = ("MAE", "EL AMANA TAKAFUL", "SALIM", "STAR")


@router.get("/etat/compagnies")
def unpaid_statement_companies(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        text(f'SELECT code_compagnie, TRIM(nom_compagnie) FROM "{settings.db_schema}"."TCompagnie" '
             'WHERE UPPER(TRIM(nom_compagnie)) NOT IN :hidden ORDER BY nom_compagnie').bindparams(bindparam("hidden", expanding=True)),
        {"hidden": list(HIDDEN_COMPANIES)},
    ).all()
    return {"items": [{"id": row[0], "label": row[1]} for row in rows]}
