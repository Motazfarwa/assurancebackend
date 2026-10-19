from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.database import get_db
from app.routers.auth import users_table


router = APIRouter(prefix="/api/admin/users", tags=["Administration des utilisateurs"], dependencies=[Depends(require_admin)])


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=25)
    password: str = Field(min_length=1, max_length=256)
    lieu: str | None = Field(default=None, max_length=255)
    code_depot: str = Field(default="01", min_length=1, max_length=3)


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=1, max_length=256)
    lieu: str | None = Field(default=None, max_length=255)


def _is_admin_name(name: str) -> bool:
    return name.strip().casefold() == "admin"


@router.get("")
def list_users(db: Session = Depends(get_db)) -> dict[str, list[dict[str, Any]]]:
    table = users_table(db)
    try:
        rows = db.execute(select(table.c.Nom_Utilisateur, table.c.lieu, table.c.Code_Depot).where(table.c.Nom_Utilisateur.is_not(None)).order_by(table.c.Nom_Utilisateur)).all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de charger les utilisateurs.") from exc
    return {"items": [{"username": str(nom), "lieu": lieu, "code_depot": code_depot} for nom, lieu, code_depot in rows]}


@router.post("", status_code=201)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    table = users_table(db)
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="Le nom d'utilisateur est obligatoire.")
    try:
        if db.execute(select(table.c.Nom_Utilisateur).where(table.c.Nom_Utilisateur == username).limit(1)).first() is not None:
            raise HTTPException(status_code=409, detail="Ce nom d'utilisateur existe déjà.")
        db.execute(insert(table).values(Code_Depot=payload.code_depot, Nom_Utilisateur=username, ModP=hash_password(payload.password), lieu=payload.lieu))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Impossible de créer l'utilisateur.") from exc
    return {"username": username, "lieu": payload.lieu}


@router.put("/{username}")
def update_user(username: str, payload: UserUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    table = users_table(db)
    values: dict[str, Any] = {}
    if payload.password is not None:
        values["ModP"] = hash_password(payload.password)
    if "lieu" in payload.model_fields_set:
        values["lieu"] = payload.lieu
    if not values:
        raise HTTPException(status_code=422, detail="Aucune modification fournie.")
    try:
        result = db.execute(update(table).where(table.c.Nom_Utilisateur == username).values(**values))
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Impossible de modifier l'utilisateur.") from exc
    return {"username": username}


@router.delete("/{username}", status_code=204)
def delete_user(username: str, db: Session = Depends(get_db)) -> None:
    if _is_admin_name(username):
        raise HTTPException(status_code=400, detail="Le compte administrateur ne peut pas être supprimé.")
    table = users_table(db)
    try:
        result = db.execute(delete(table).where(table.c.Nom_Utilisateur == username))
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Impossible de supprimer l'utilisateur.") from exc
