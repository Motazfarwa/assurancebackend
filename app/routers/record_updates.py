import random
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import MetaData, Table, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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

    date_saisie: datetime | None = None
    date_emission: datetime | None = None
    echeance: datetime | None = None
    tc: str | None = None
    date_reglement: datetime | None = None
    date_m: datetime | None = None
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
QUITTANCE_COLUMNS = {"periode": "Periode", "taxe": "Taxe", "date_reglement": "Date_Reglement", "date_m": "Date_M"}


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


class QuittanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    num_int: str = Field(min_length=1, max_length=15)
    code_client: str = Field(min_length=1, max_length=8)
    num_quittance: str | None = Field(default=None, max_length=15)
    date_saisie: datetime | None = None
    date_emission: datetime | None = None
    echeance: datetime | None = None
    tc: str | None = Field(default=None, pattern="^[TC]$")
    prime_net: float | None = None
    commission: float | None = None
    montant_quittance: float | None = None
    solde_quittance: float | None = None
    date_reglement: datetime | None = None
    date_m: datetime | None = None


@router.post("/quittances", status_code=201)
def create_quittance(payload: QuittanceCreate, db: Session = Depends(get_db)) -> dict[str, str]:
    """Enters a receipt for an existing policy of the client (the policy / receipt creation flow is separate)."""
    policy = db.execute(
        text('SELECT TRIM(code_client) AS code_client, TRIM(code_branche) AS code_branche FROM dbo."TPolice" WHERE TRIM(num_int) = :n'),
        {"n": payload.num_int.strip()},
    ).mappings().first()
    if not policy:
        raise HTTPException(status_code=404, detail="Police introuvable.")
    if policy["code_client"] != payload.code_client.strip():
        raise HTTPException(status_code=422, detail="Cette police n'appartient pas à ce client.")

    # Without a typed number: Q + yymmddhhmmss + 2 random digits (15 characters, the column size).
    number = (payload.num_quittance or "").strip() or f"Q{datetime.now():%y%m%d%H%M%S}{random.randint(0, 99):02d}"
    period_source = payload.date_emission or payload.echeance or payload.date_saisie or datetime.now()
    try:
        db.execute(
            text(
                'INSERT INTO dbo."TQuittance" (num_quittance, num_int, code_client, code_branche, date_saisie, date_emission, echeance, '
                'tc, "Periode", prime_net, commission, montant_quittance, solde_quittance, "Date_Reglement", "Date_M", observation, type_quittance) '
                "VALUES (:num_quittance, :num_int, :code_client, :code_branche, :date_saisie, :date_emission, :echeance, "
                ":tc, :periode, :prime_net, :commission, :montant_quittance, :solde_quittance, :date_reglement, :date_m, '', 'P')"
            ),
            {
                "num_quittance": number, "num_int": payload.num_int.strip(), "code_client": payload.code_client.strip(),
                "code_branche": policy["code_branche"], "date_saisie": payload.date_saisie or datetime.now(),
                "date_emission": payload.date_emission, "echeance": payload.echeance, "tc": payload.tc,
                "periode": f"{period_source:%m/%Y}", "prime_net": payload.prime_net, "commission": payload.commission,
                "montant_quittance": payload.montant_quittance, "solde_quittance": payload.solde_quittance,
                "date_reglement": payload.date_reglement, "date_m": payload.date_m or datetime.now(),
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Le numéro de quittance {number} existe déjà.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Enregistrement impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"num_quittance": number}
