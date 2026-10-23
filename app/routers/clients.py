import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import BigInteger, MetaData, Table, cast, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.schemas import ClientInput


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clients", tags=["Clients"], dependencies=[Depends(require_current_user)])


_contact_columns_ready = False


def ensure_contact_columns(db: Session) -> None:
    """Adds the phone / email columns of the client form when the database does not have them yet (migration 002)."""
    global _contact_columns_ready
    if _contact_columns_ready:
        return
    table = f'"{settings.db_schema}"."{settings.db_client_table}"'
    try:
        db.execute(text(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS telephones_domicile jsonb, ADD COLUMN IF NOT EXISTS emails_domicile jsonb, "
            "ADD COLUMN IF NOT EXISTS telephones_bureau jsonb, ADD COLUMN IF NOT EXISTS emails_bureau jsonb"
        ))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Could not add the client contact columns; run migrations/002_client_contact_details.sql", exc_info=True)
        return
    _contact_columns_ready = True


def client_table(db: Session) -> Table:
    try:
        return Table(settings.db_client_table, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Impossible de lire {settings.db_schema}.{settings.db_client_table}. Vérifiez la connexion et la table.",
        ) from exc


def serialize(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


def complete_client_name(values: dict[str, Any]) -> dict[str, Any]:
    if not values.get("nom_client"):
        full_name = " ".join(part.strip() for part in (values.get("Nom"), values.get("Prenom")) if part and part.strip())
        values["nom_client"] = full_name or None
    return values


@router.get("")
def list_clients(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    table = client_table(db)
    if "code_client" not in table.c or "nom_client" not in table.c:
        raise HTTPException(status_code=503, detail="La table client doit contenir code_client et nom_client.")
    query = select(table)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(or_(table.c.code_client.ilike(term), table.c.nom_client.ilike(term)))
    try:
        rows = db.execute(query.order_by(table.c.code_client).limit(limit).offset(offset)).mappings().all()
        return {"items": [serialize(row) for row in rows], "limit": limit, "offset": offset}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Erreur lors de la lecture des clients.") from exc


@router.get("/next-code")
def next_client_code(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Last numeric client code + 1, so a new client can be created with an incremented code."""
    table = client_table(db)
    try:
        last = db.execute(
            select(func.max(cast(table.c.code_client, BigInteger))).where(table.c.code_client.op("~")("^[0-9]+$"))
        ).scalar()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Impossible de calculer le prochain code client.") from exc
    return {"last_code": last, "next_code": str((last or 0) + 1)}


@router.get("/{code_client}")
def get_client(code_client: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    table = client_table(db)
    row = db.execute(select(table).where(table.c.code_client == code_client)).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    return serialize(row)


@router.post("", status_code=201)
def create_client(client: ClientInput, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_contact_columns(db)
    table = client_table(db)
    values = complete_client_name(client.model_dump())
    unknown = set(values) - set(table.c.keys())
    if unknown:
        raise HTTPException(status_code=503, detail=f"Colonnes absentes de la table client : {', '.join(sorted(unknown))}")
    try:
        db.execute(table.insert().values(**values))
        db.commit()
        row = db.execute(select(table).where(table.c.code_client == client.code_client)).mappings().one()
        return serialize(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ce code client existe déjà ou certaines données sont invalides.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Client insert failed for code_client=%s", client.code_client)
        raise HTTPException(status_code=503, detail=f"Erreur lors de l'enregistrement du client : {str(getattr(exc, 'orig', exc))[:250]}") from exc


@router.put("/{code_client}")
def update_client(code_client: str, client: ClientInput, db: Session = Depends(get_db)) -> dict[str, Any]:
    ensure_contact_columns(db)
    table = client_table(db)
    if client.code_client != code_client:
        raise HTTPException(status_code=422, detail="Le code client ne peut pas être changé pendant la modification.")
    values = complete_client_name(client.model_dump(exclude={"code_client"}))
    unknown = set(values) - set(table.c.keys())
    if unknown:
        raise HTTPException(status_code=503, detail=f"Colonnes absentes de la table client : {', '.join(sorted(unknown))}")
    try:
        result = db.execute(table.update().where(table.c.code_client == code_client).values(**values))
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Client introuvable.")
        db.commit()
        row = db.execute(select(table).where(table.c.code_client == code_client)).mappings().one()
        return serialize(row)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Client update failed for code_client=%s", code_client)
        raise HTTPException(status_code=503, detail=f"Erreur lors de la modification du client : {str(getattr(exc, 'orig', exc))[:250]}") from exc


@router.delete("/{code_client}", status_code=204)
def delete_client(code_client: str, db: Session = Depends(get_db)) -> Response:
    table = client_table(db)
    try:
        result = db.execute(table.delete().where(table.c.code_client == code_client))
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Client introuvable.")
        db.commit()
        return Response(status_code=204)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Suppression impossible : ce client est peut-être utilisé par d'autres données.") from exc
