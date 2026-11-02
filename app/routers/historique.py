"""Historique des actions (administrator): reading dbo."TMouchard", the history written by app.audit
and by the legacy software."""
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit import DETAIL_DDL
from app.auth import require_admin
from app.database import get_db
from app.routers.auth import users_table

router = APIRouter(prefix="/api/historique", tags=["Historique des actions"], dependencies=[Depends(require_admin)])


@router.get("")
def list_history(
    du: date | None = None,
    au: date | None = None,
    utilisateur: str | None = Query(default=None, max_length=50),
    mvt: str | None = Query(default=None, max_length=20),
    q: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    du = du or date.today()
    au = au or du
    where = ['dmvt >= :du', 'dmvt < :au']
    params: dict[str, Any] = {"du": du, "au": au + timedelta(days=1), "offset": offset, "limit": limit}
    if utilisateur:
        where.append("utilisateur = :u")
        params["u"] = utilisateur
    if mvt:
        where.append('"Mvt" = :m')
        params["m"] = mvt
    if q:
        where.append("design ILIKE :q")
        params["q"] = f"%{q.strip()}%"
    clause = " AND ".join(where)
    total = db.execute(text(f'SELECT COUNT(*) FROM dbo."TMouchard" WHERE {clause}'), params).scalar()
    db.execute(text(DETAIL_DDL))
    rows = db.execute(
        text(
            f'SELECT "N" AS n, utilisateur, "Mvt" AS mvt, dmvt, "Hmvt" AS hmvt, design, code, '
            f'EXISTS (SELECT 1 FROM dbo."TMouchardDetail" d WHERE d."N" = dbo."TMouchard"."N") AS has_details FROM dbo."TMouchard" '
            f'WHERE {clause} ORDER BY dmvt DESC, "Hmvt" DESC, "N" DESC OFFSET :offset LIMIT :limit'
        ),
        params,
    ).mappings().all()
    items = [{
        "n": row["n"], "utilisateur": row["utilisateur"], "mvt": row["mvt"], "design": row["design"], "code": row["code"],
        "has_details": row["has_details"],
        "date": row["dmvt"].date().isoformat() if row["dmvt"] else None,
        "heure": row["hmvt"].strftime("%H:%M") if row["hmvt"] else None,
    } for row in rows]
    return {"total": total, "items": items}


@router.get("/{n:int}")
def history_detail(n: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """One action and what exactly was done (fields changed with their old and new values, values added or deleted)."""
    db.execute(text(DETAIL_DDL))
    row = db.execute(
        text('SELECT m."N" AS n, m.utilisateur, m."Mvt" AS mvt, m.dmvt, m."Hmvt" AS hmvt, m.design, m.code, d."Details" AS details '
             'FROM dbo."TMouchard" m LEFT JOIN dbo."TMouchardDetail" d ON d."N" = m."N" WHERE m."N" = :n'),
        {"n": n},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Action introuvable dans l’historique.")
    return {
        "n": row["n"], "utilisateur": row["utilisateur"], "mvt": row["mvt"], "design": row["design"], "code": row["code"],
        "date": row["dmvt"].date().isoformat() if row["dmvt"] else None,
        "heure": row["hmvt"].strftime("%H:%M:%S") if row["hmvt"] else None,
        "details": row["details"],
    }


@router.get("/utilisateurs")
def history_users(db: Session = Depends(get_db)) -> list[str]:
    """The users who exist now (the users table), not every name found in the legacy history."""
    table = users_table(db)
    rows = db.execute(select(table.c.Nom_Utilisateur).where(table.c.Nom_Utilisateur.is_not(None)).order_by(table.c.Nom_Utilisateur)).all()
    names = [str(row[0]).strip() for row in rows if str(row[0]).strip()]
    return list(dict.fromkeys(["Admin", *(name for name in names if name.casefold() != "admin")]))  # the administrator's actions are logged too
