import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import MetaData, Table, func, select, update
from sqlalchemy.exc import NoSuchTableError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import ACCESS_TOKEN_LIFETIME_SECONDS, create_access_token, create_refresh_token, decode_refresh_token, hash_password, require_current_user, verify_password
from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=25)
    password: str = Field(min_length=1, max_length=256)


def users_table(db: Session) -> Table:
    try:
        table = Table(settings.db_user_table, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL is unreachable. Check the host, port, database name, username, and password in DATABASE_URL.",
        ) from exc
    except NoSuchTableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f'Table {settings.db_schema}."{settings.db_user_table}" was not found. Check DB_SCHEMA and DB_USER_TABLE.',
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Could not inspect the configured PostgreSQL user table.") from exc
    required = {"Nom_Utilisateur", "ModP", "lieu"}
    if not required.issubset(table.c.keys()):
        raise HTTPException(status_code=503, detail="La table utilisateur doit contenir Nom_Utilisateur, ModP et lieu.")
    return table


def _is_predefined_admin(username: str) -> bool:
    return username.strip().casefold() == settings.admin_username.casefold()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


def _session_payload(username: str, lieu: Any) -> dict[str, Any]:
    lieu_value = str(lieu).strip() if lieu is not None else None
    return {
        "access_token": create_access_token(username, lieu_value),
        "refresh_token": create_refresh_token(username, lieu_value),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
        "user": {"username": username, "lieu": lieu_value},
    }


@router.get("/users")
def list_login_users(lieu: str = Query(default="", max_length=255), db: Session = Depends(get_db)) -> dict[str, list[str]]:
    """Usernames for the login page, filtered by the selected lieu (agency) when given."""
    table = users_table(db)
    query = select(table.c.Nom_Utilisateur).where(table.c.Nom_Utilisateur.is_not(None))
    if lieu.strip():
        query = query.where(func.lower(func.trim(table.c.lieu)) == lieu.strip().lower())
    try:
        rows = db.execute(query.distinct().order_by(table.c.Nom_Utilisateur)).scalars().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de charger les utilisateurs.") from exc
    return {"items": [str(name) for name in rows if str(name).strip()]}

@router.post("/login")
def login(credentials: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if _is_predefined_admin(credentials.username):
        if not hmac.compare_digest(credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Nom d'utilisateur ou mot de passe incorrect.")
        return _session_payload(settings.admin_username, None)
    table = users_table(db)
    row = db.execute(select(table).where(table.c.Nom_Utilisateur == credentials.username).limit(1)).mappings().first()
    if row is None or row["ModP"] is None:
        raise HTTPException(status_code=401, detail="Nom d'utilisateur ou mot de passe incorrect.")
    matches, needs_upgrade = verify_password(credentials.password, str(row["ModP"]))
    if not matches:
        raise HTTPException(status_code=401, detail="Nom d'utilisateur ou mot de passe incorrect.")
    if needs_upgrade:
        try:
            db.execute(update(table).where(table.c.Nom_Utilisateur == credentials.username).values(ModP=hash_password(credentials.password)))
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail="Impossible de sécuriser le mot de passe. Appliquez la migration SQL fournie.") from exc
    return _session_payload(str(row["Nom_Utilisateur"]), row["lieu"])


@router.post("/refresh")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    claims = decode_refresh_token(payload.refresh_token)
    if _is_predefined_admin(str(claims["sub"])):
        return _session_payload(settings.admin_username, None)
    table = users_table(db)
    row = db.execute(select(table).where(table.c.Nom_Utilisateur == claims["sub"]).limit(1)).mappings().first()
    if row is None:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.")
    return _session_payload(str(row["Nom_Utilisateur"]), row["lieu"])


@router.get("/me")
def me(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    return {"username": user["sub"], "lieu": user.get("lieu")}
