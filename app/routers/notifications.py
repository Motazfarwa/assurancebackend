"""Notifications (the bell of the workspace): the latest actions of dbo."TMouchard".

The administrator is notified of every user's actions; any other user only of their own. What a user has
already seen is kept in dbo."TNotifLu" (the last "N" seen), so the unread count follows them on any computer.
"""
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.routers.access import is_admin

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])

SEEN_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TNotifLu" (
        "Nom_Utilisateur" varchar(50) PRIMARY KEY,
        "Dernier_N" integer NOT NULL DEFAULT 0
    )
"""
_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if not _ready:
        db.execute(text(SEEN_DDL))
        db.commit()
        _ready = True


def _scope(user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """SQL condition limiting the history to what this user is notified of."""
    if is_admin(user):
        return "TRUE", {}
    return "LOWER(TRIM(utilisateur)) = LOWER(TRIM(:me))", {"me": str(user["sub"])}


def _last_seen(db: Session, name: str, scope: str, params: dict[str, Any]) -> int:
    row = db.execute(text('SELECT "Dernier_N" FROM dbo."TNotifLu" WHERE "Nom_Utilisateur" = :u'), {"u": name}).first()
    if row:
        return int(row[0])
    # First time: the past history is not "new"; only what happens from now on is.
    latest = db.execute(text(f'SELECT COALESCE(MAX("N"), 0) FROM dbo."TMouchard" WHERE {scope}'), params).scalar() or 0
    db.execute(text('INSERT INTO dbo."TNotifLu" ("Nom_Utilisateur", "Dernier_N") VALUES (:u, :n) ON CONFLICT DO NOTHING'), {"u": name, "n": latest})
    db.commit()
    return int(latest)


@router.get("")
def my_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    user: dict[str, Any] = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ensure(db)
    name = str(user["sub"])
    scope, params = _scope(user)
    seen = _last_seen(db, name, scope, params)
    unread = db.execute(text(f'SELECT COUNT(*) FROM dbo."TMouchard" WHERE {scope} AND "N" > :seen'), {**params, "seen": seen}).scalar()
    rows = db.execute(
        text(f'SELECT "N" AS n, utilisateur, "Mvt" AS mvt, dmvt, "Hmvt" AS hmvt, design, code FROM dbo."TMouchard" WHERE {scope} ORDER BY "N" DESC LIMIT :limit'),
        {**params, "limit": limit},
    ).mappings().all()
    items = [{
        "n": row["n"], "utilisateur": row["utilisateur"], "mvt": row["mvt"], "design": row["design"], "code": row["code"], "unread": row["n"] > seen,
        "date": row["dmvt"].date().isoformat() if row["dmvt"] else None,
        "heure": row["hmvt"].strftime("%H:%M") if row["hmvt"] else None,
    } for row in rows]
    return {"unread": unread, "items": items}


@router.put("/lu")
def mark_read(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything up to now has been seen."""
    _ensure(db)
    scope, params = _scope(user)
    latest = db.execute(text(f'SELECT COALESCE(MAX("N"), 0) FROM dbo."TMouchard" WHERE {scope}'), params).scalar() or 0
    db.execute(
        text(
            'INSERT INTO dbo."TNotifLu" ("Nom_Utilisateur", "Dernier_N") VALUES (:u, :n) '
            'ON CONFLICT ("Nom_Utilisateur") DO UPDATE SET "Dernier_N" = GREATEST(dbo."TNotifLu"."Dernier_N", :n)'
        ),
        {"u": str(user["sub"]), "n": latest},
    )
    db.commit()
    return {"unread": 0}
