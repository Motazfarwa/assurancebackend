from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import IntegrityError, NoSuchTableError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/compagnies", tags=["Compagnies et branches"], dependencies=[Depends(require_current_user)])

COMPANY_KEY = "code_compagnie"
BRANCH_KEY = "code_branche"
COMPANY_COLUMNS = {
    "nom_compagnie", "contre_ass_spe", "Capital_CAS", "Taux_Majoration", "Code_Agent",
    "Nbr_Chiffre_Code", "Num_Quittance_Auto", "Assistance_Voyage", "Adresse_Agence",
    "Ville_Agence", "CP_Agence", "Pays_Agence", "Tel_Agence", "Fax_Agence",
    "Adresse_Compagnie", "Ville_Compagnie", "Nom_Agence",
}
BRANCH_COLUMNS = {
    "nom_branche", "frais_quitt", "taux_comm", "Taux_Comm2", "Montant_Comm", "Timbre",
    "Cout_Attestation", "Transport", "Taxe", "Taxe2", "Automobile", "Nature_Tarif",
}


def table(db: Session, name: str) -> Table:
    try:
        return Table(name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL est indisponible. Vérifiez DATABASE_URL.") from exc
    except NoSuchTableError as exc:
        raise HTTPException(status_code=503, detail=f"La table {settings.db_schema}.{name} est introuvable.") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=f"Impossible de lire la structure de {name}.") from exc


def row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in result.items()}


@router.get("")
def list_companies(db: Session = Depends(get_db)) -> dict[str, Any]:
    companies = table(db, settings.db_company_table)
    rows = db.execute(select(companies).order_by(companies.c[COMPANY_KEY])).mappings().all()
    return {"items": [row_dict(row) for row in rows]}


@router.put("/{company_code}")
def update_company(company_code: int, values: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    companies = table(db, settings.db_company_table)
    if db.execute(select(companies.c[COMPANY_KEY]).where(companies.c[COMPANY_KEY] == company_code)).first() is None:
        raise HTTPException(status_code=404, detail="Compagnie introuvable.")
    changes = {key: value for key, value in values.items() if key in COMPANY_COLUMNS and key in companies.c}
    if not changes:
        raise HTTPException(status_code=422, detail="Aucune information de compagnie à modifier.")
    try:
        db.execute(companies.update().where(companies.c[COMPANY_KEY] == company_code).values(**changes))
        db.commit()
        row = db.execute(select(companies).where(companies.c[COMPANY_KEY] == company_code)).mappings().one()
        return row_dict(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Modification impossible : une valeur ne respecte pas les contraintes de la base.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la modification de la compagnie.") from exc


@router.delete("/{company_code}", status_code=204)
def delete_company(company_code: int, db: Session = Depends(get_db)) -> Response:
    companies = table(db, settings.db_company_table)
    branches = table(db, settings.db_branch_table)
    prestataire_companies = table(db, settings.db_prestataire_company_table)
    if db.execute(select(companies.c[COMPANY_KEY]).where(companies.c[COMPANY_KEY] == company_code)).first() is None:
        raise HTTPException(status_code=404, detail="Compagnie introuvable.")
    if db.execute(select(branches.c[BRANCH_KEY]).where(branches.c[COMPANY_KEY] == company_code).limit(1)).first():
        raise HTTPException(status_code=409, detail="Supprimez d'abord les branches rattachées à cette compagnie.")
    if db.execute(select(prestataire_companies.c.Code_Prestataire).where(prestataire_companies.c.Code_Compagnie == company_code).limit(1)).first():
        raise HTTPException(status_code=409, detail="Cette compagnie est rattachée à un prestataire et ne peut pas être supprimée.")
    try:
        db.execute(companies.delete().where(companies.c[COMPANY_KEY] == company_code))
        db.commit()
        return Response(status_code=204)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Suppression impossible : cette compagnie est utilisée par d'autres données.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la suppression de la compagnie.") from exc


@router.get("/{company_code}/branches")
def list_branches(company_code: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    companies = table(db, settings.db_company_table)
    branches = table(db, settings.db_branch_table)
    if db.execute(select(companies.c[COMPANY_KEY]).where(companies.c[COMPANY_KEY] == company_code)).first() is None:
        raise HTTPException(status_code=404, detail="Compagnie introuvable.")
    rows = db.execute(select(branches).where(branches.c[COMPANY_KEY] == company_code).order_by(branches.c[BRANCH_KEY])).mappings().all()
    return {"items": [row_dict(row) for row in rows]}


@router.post("/{company_code}/branches", status_code=201)
def create_branch(company_code: int, values: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    companies = table(db, settings.db_company_table)
    branches = table(db, settings.db_branch_table)
    if db.execute(select(companies.c[COMPANY_KEY]).where(companies.c[COMPANY_KEY] == company_code)).first() is None:
        raise HTTPException(status_code=404, detail="Compagnie introuvable.")
    code = str(values.get(BRANCH_KEY, "")).strip()
    name = str(values.get("nom_branche", "")).strip()
    if not code or len(code) > 4 or not name:
        raise HTTPException(status_code=422, detail="Le code (4 caractères maximum) et le nom de la branche sont obligatoires.")
    payload = {key: value for key, value in values.items() if key in BRANCH_COLUMNS and key in branches.c}
    payload.update({BRANCH_KEY: code, COMPANY_KEY: company_code, "nom_branche": name})
    try:
        if db.execute(select(branches.c[BRANCH_KEY]).where(branches.c[COMPANY_KEY] == company_code, branches.c[BRANCH_KEY] == code)).first():
            raise HTTPException(status_code=409, detail="Ce code de branche existe déjà pour cette compagnie.")
        db.execute(branches.insert().values(**payload))
        db.commit()
        row = db.execute(select(branches).where(branches.c[COMPANY_KEY] == company_code, branches.c[BRANCH_KEY] == code)).mappings().one()
        return row_dict(row)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Code de branche déjà utilisé ou référence invalide.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de l'ajout de la branche.") from exc


@router.put("/{company_code}/branches/{branch_code}")
def update_branch(company_code: int, branch_code: str, values: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    branches = table(db, settings.db_branch_table)
    if not branch_code or len(branch_code) > 4:
        raise HTTPException(status_code=422, detail="Code de branche invalide.")
    changes = {key: value for key, value in values.items() if key in BRANCH_COLUMNS and key in branches.c}
    if "nom_branche" in changes and not str(changes["nom_branche"] or "").strip():
        raise HTTPException(status_code=422, detail="Le nom de la branche est obligatoire.")
    if not changes:
        raise HTTPException(status_code=422, detail="Aucune information de branche à modifier.")
    where = (branches.c[COMPANY_KEY] == company_code) & (branches.c[BRANCH_KEY] == branch_code)
    if db.execute(select(branches.c[BRANCH_KEY]).where(where)).first() is None:
        raise HTTPException(status_code=404, detail="Branche introuvable pour cette compagnie.")
    try:
        db.execute(branches.update().where(where).values(**changes))
        db.commit()
        row = db.execute(select(branches).where(where)).mappings().one()
        return row_dict(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Modification impossible : une valeur ne respecte pas les contraintes de la base.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la modification de la branche.") from exc


@router.delete("/{company_code}/branches/{branch_code}", status_code=204)
def delete_branch(company_code: int, branch_code: str, db: Session = Depends(get_db)) -> Response:
    branches = table(db, settings.db_branch_table)
    where = (branches.c[COMPANY_KEY] == company_code) & (branches.c[BRANCH_KEY] == branch_code)
    if db.execute(select(branches.c[BRANCH_KEY]).where(where)).first() is None:
        raise HTTPException(status_code=404, detail="Branche introuvable pour cette compagnie.")
    try:
        db.execute(branches.delete().where(where))
        db.commit()
        return Response(status_code=204)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Suppression impossible : cette branche est utilisée par d'autres données.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la suppression de la branche.") from exc

