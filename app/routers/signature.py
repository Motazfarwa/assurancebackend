"""Electronic signature of each user (administrator or employee): drawn once in « Ma signature », kept with the user,
and put on the documents the user prints (the quittance's « Pour la Compagnie »)."""
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db

router = APIRouter(prefix="/api/signature", tags=["Signature électronique"])

SIGNATURE_DDL = """
CREATE TABLE IF NOT EXISTS dbo."TSignatureUtilisateur" (
    utilisateur varchar(60) PRIMARY KEY,
    image text NOT NULL,
    date_maj timestamp DEFAULT now()
)
"""
# A PNG drawn on the pad, as a data URL; 400 KB at most.
PNG_DATA_URL = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/=]+$")


class SignatureIn(BaseModel):
    image: str = Field(min_length=30, max_length=400_000)


def _user(user: dict[str, Any]) -> str:
    name = str(user.get("sub") or "").strip()[:60]
    if not name:
        raise HTTPException(status_code=401, detail="Utilisateur inconnu.")
    return name


def _ensure(db: Session) -> None:
    db.execute(text(SIGNATURE_DDL))
    db.commit()


@router.get("")
def my_signature(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure(db)
    row = db.execute(
        text('SELECT image, date_maj FROM dbo."TSignatureUtilisateur" WHERE utilisateur = :u'), {"u": _user(user)}
    ).mappings().first()
    return {"utilisateur": _user(user), "image": row["image"] if row else None,
            "date_maj": row["date_maj"].isoformat(timespec="minutes") if row and row["date_maj"] else None}


@router.put("")
def save_signature(payload: SignatureIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not PNG_DATA_URL.match(payload.image):
        raise HTTPException(status_code=422, detail="Signature illisible : dessinez-la à nouveau.")
    _ensure(db)
    try:
        db.execute(text(
            'INSERT INTO dbo."TSignatureUtilisateur" (utilisateur, image, date_maj) VALUES (:u, :i, now()) '
            "ON CONFLICT (utilisateur) DO UPDATE SET image = EXCLUDED.image, date_maj = now()"
        ), {"u": _user(user), "i": payload.image})
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Enregistrement de la signature impossible.") from exc
    return {"utilisateur": _user(user), "enregistree": True}


@router.delete("", status_code=204)
def delete_signature(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> Response:
    _ensure(db)
    db.execute(text('DELETE FROM dbo."TSignatureUtilisateur" WHERE utilisateur = :u'), {"u": _user(user)})
    db.commit()
    return Response(status_code=204)
