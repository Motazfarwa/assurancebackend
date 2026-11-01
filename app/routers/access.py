"""Access users: the modules each user may see in the workspace, set by the administrator.

A user without a row in dbo."TUserModules" sees every module (the behaviour before this screen existed);
a row lists the module numbers the user is limited to. The administrator always sees everything.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_admin, require_current_user
from app.database import get_db

router = APIRouter(prefix="/api/access", tags=["Accès des utilisateurs"])

ACCESS_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TUserModules" (
        "Nom_Utilisateur" varchar(25) PRIMARY KEY,
        "Modules" text NOT NULL DEFAULT '',
        "Update_User" varchar(25),
        "Update_Date" timestamp DEFAULT now()
    )
"""
_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if not _ready:
        db.execute(text(ACCESS_DDL))
        db.commit()
        _ready = True


def is_admin(user: dict[str, Any]) -> bool:
    return str(user.get("sub", "")).strip().casefold() == "admin"


def _split(value: str | None) -> list[str]:
    return [item for item in (value or "").split(",") if item]


class AccessIn(BaseModel):
    # None: every module (the restriction is removed); a list: only those modules.
    modules: list[str] | None = Field(default=None, max_length=200)


@router.get("/me")
def my_modules(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if is_admin(user):
        return {"modules": None}
    _ensure(db)
    row = db.execute(text('SELECT "Modules" FROM dbo."TUserModules" WHERE "Nom_Utilisateur" = :u'), {"u": user["sub"]}).first()
    return {"modules": _split(row[0]) if row else None}


@router.get("/users", dependencies=[Depends(require_admin)])
def list_access(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _ensure(db)
    rows = db.execute(text('SELECT "Nom_Utilisateur", "Modules" FROM dbo."TUserModules"')).all()
    return [{"username": name, "modules": _split(modules)} for name, modules in rows]


@router.put("/users/{username}")
def set_access(username: str, payload: AccessIn, admin: dict[str, Any] = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if username.strip().casefold() == "admin":
        raise HTTPException(status_code=422, detail="L’administrateur a toujours accès à tous les modules.")
    _ensure(db)
    try:
        if payload.modules is None:
            db.execute(text('DELETE FROM dbo."TUserModules" WHERE "Nom_Utilisateur" = :u'), {"u": username})
        else:
            modules = ",".join(sorted({item.strip() for item in payload.modules if item.strip()}))
            db.execute(
                text(
                    'INSERT INTO dbo."TUserModules" ("Nom_Utilisateur", "Modules", "Update_User", "Update_Date") VALUES (:u, :m, :a, now()) '
                    'ON CONFLICT ("Nom_Utilisateur") DO UPDATE SET "Modules" = :m, "Update_User" = :a, "Update_Date" = now()'
                ),
                {"u": username, "m": modules, "a": admin["sub"]},
            )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Impossible d’enregistrer les accès.") from exc
    return {"username": username, "modules": payload.modules}
