from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import MetaData, Table, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api", tags=["Modification polices et quittances"], dependencies=[Depends(require_current_user)])


class PoliceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effet: datetime | None = None
    expiration: datetime | None = None
    echeance: str | None = None
    duree: int | None = None
    etat: str | None = None
    risque: str | None = None
    num_imat: str | None = None
    marque: str | None = None
    garantie: str | None = None
    franchise: int | None = None
    capital: float | None = None
    obser: str | None = None
    prime_net: float | None = None
    commission: float | None = None
    prime_total: float | None = None


class QuittanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_emission: datetime | None = None
    echeance: datetime | None = None
    periode: str | None = None
    prime_net: float | None = None
    prime_total: float | None = None
    commission: float | None = None
    montant_quittance: float | None = None
    solde_quittance: float | None = None
    taxe: float | None = None
    timbre: float | None = None
    cout_attestation: float | None = None
    observation: str | None = None


# API field -> actual column name where the column is mixed-case.
QUITTANCE_COLUMNS = {"periode": "Periode", "taxe": "Taxe"}


def _update_row(db: Session, table_name: str, key_column: str, key_value: str, values: dict[str, Any], column_names: dict[str, str] | None = None) -> None:
    if not values:
        raise HTTPException(status_code=422, detail="Aucune modification fournie.")
    try:
        table = Table(table_name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
        columns = {(column_names or {}).get(field, field): value for field, value in values.items()}
        missing = [name for name in columns if name not in table.c]
        if missing:
            raise HTTPException(status_code=503, detail=f"Colonnes absentes de {settings.db_schema}.{table_name} : {', '.join(missing)}.")
        result = db.execute(update(table).where(table.c[key_column] == key_value).values(**columns))
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Enregistrement introuvable.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Impossible d'enregistrer les modifications.") from exc


@router.put("/polices/{num_int}")
def update_police(num_int: str, payload: PoliceUpdate, db: Session = Depends(get_db)) -> dict[str, str]:
    _update_row(db, "TPolice", "num_int", num_int, payload.model_dump(exclude_unset=True))
    return {"num_int": num_int}


@router.put("/quittances/{num_quittance}")
def update_quittance(num_quittance: str, payload: QuittanceUpdate, db: Session = Depends(get_db)) -> dict[str, str]:
    _update_row(db, "TQuittance", "num_quittance", num_quittance, payload.model_dump(exclude_unset=True), QUITTANCE_COLUMNS)
    return {"num_quittance": num_quittance}
