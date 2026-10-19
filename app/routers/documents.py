from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import IntegrityError, NoSuchTableError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/documents", tags=["Documents"], dependencies=[Depends(require_current_user)])
DOCUMENT_FIELDS = ("Code_Document", "Code_Compagnie", "Type_Document", "Num_Document", "Etat")


def reflected_table(db: Session, name: str) -> Table:
    try:
        return Table(name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL est indisponible. Vérifiez DATABASE_URL.") from exc
    except NoSuchTableError as exc:
        raise HTTPException(status_code=503, detail=f"La table {settings.db_schema}.{name} est introuvable.") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=f"Impossible de lire la structure de {name}.") from exc


def serialize(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


@router.get("")
def list_documents(db: Session = Depends(get_db)) -> dict[str, Any]:
    documents = reflected_table(db, settings.db_document_table)
    rows = db.execute(select(documents).order_by(documents.c.Code_Document).limit(500)).mappings().all()
    return {"items": [serialize(row) for row in rows]}


@router.post("", status_code=201)
def create_document(values: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    documents = reflected_table(db, settings.db_document_table)
    companies = reflected_table(db, settings.db_company_table)
    required = ("Code_Document", "Code_Compagnie", "Type_Document", "Num_Document", "Etat")
    cleaned = {key: str(values.get(key, "")).strip() if key != "Code_Compagnie" else values.get(key) for key in required}
    for key in ("Code_Document", "Type_Document", "Num_Document", "Etat"):
        if not cleaned[key] or len(cleaned[key]) > 50:
            raise HTTPException(status_code=422, detail=f"{key} est obligatoire et doit contenir au plus 50 caractères.")
    try:
        cleaned["Code_Compagnie"] = int(cleaned["Code_Compagnie"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Code compagnie invalide.") from exc
    if db.execute(select(companies.c.code_compagnie).where(companies.c.code_compagnie == cleaned["Code_Compagnie"])).first() is None:
        raise HTTPException(status_code=422, detail="La compagnie sélectionnée est introuvable.")
    try:
        db.execute(documents.insert().values(**cleaned))
        db.commit()
        return cleaned
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Le document ne respecte pas les contraintes de la base.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de l'enregistrement du document.") from exc
