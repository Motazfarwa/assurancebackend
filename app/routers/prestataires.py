from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import MetaData, Table, cast, select, String
from sqlalchemy.exc import IntegrityError, NoSuchTableError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.prestataire_schemas import PrestataireInput


router = APIRouter(prefix="/api/prestataires", tags=["Prestataires"], dependencies=[Depends(require_current_user)])


def tables(db: Session) -> dict[str, Table]:
    names = (
        settings.db_prestataire_table,
        settings.db_prestataire_branch_table,
        settings.db_prestataire_company_table,
    )
    try:
        return {name: Table(name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind()) for name in names}
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL is unreachable. Check DATABASE_URL.") from exc
    except NoSuchTableError as exc:
        raise HTTPException(status_code=503, detail=f"Required prestataire table is missing in schema {settings.db_schema}.") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Could not inspect the prestataire tables.") from exc


def lookup_table(db: Session, name: str) -> Table:
    try:
        return Table(name, MetaData(), schema=settings.db_schema, autoload_with=db.get_bind())
    except (OperationalError, NoSuchTableError, SQLAlchemyError) as exc:
        raise HTTPException(status_code=503, detail=f"Required lookup table {settings.db_schema}.{name} is unavailable.") from exc


def serialize(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}


def relation_ids(db: Session, relation: Table, code: int, column: str) -> list[int]:
    return list(db.scalars(select(relation.c[column]).where(relation.c.Code_Prestataire == code).order_by(relation.c[column])))


def fetch_one(db: Session, main: Table, branch_rel: Table, company_rel: Table, code: int) -> dict[str, Any] | None:
    row = db.execute(select(main).where(main.c.Code_Prestataire == code)).mappings().first()
    if row is None:
        return None
    result = serialize(row)
    result["Branches"] = relation_ids(db, branch_rel, code, "Code_Type_Branche")
    result["Compagnies"] = relation_ids(db, company_rel, code, "Code_Compagnie")
    return result


def ensure_reference_values(db: Session, data: PrestataireInput) -> None:
    family = lookup_table(db, settings.db_family_prestataire_table)
    region = lookup_table(db, settings.db_region_table)
    company = lookup_table(db, settings.db_company_table)
    branch = lookup_table(db, settings.db_branch_type_table)
    if db.execute(select(family).where(family.c.Code_Famille == data.Code_Famille)).first() is None:
        raise HTTPException(status_code=422, detail="Famille de prestataire invalide.")
    if data.Code_Region is not None and db.execute(select(region).where(region.c.Code_Region == data.Code_Region)).first() is None:
        raise HTTPException(status_code=422, detail="Région invalide.")
    if data.Compagnies:
        valid = set(db.scalars(select(company.c.code_compagnie).where(company.c.code_compagnie.in_(set(data.Compagnies)))))
        if valid != set(data.Compagnies):
            raise HTTPException(status_code=422, detail="Une compagnie sélectionnée est invalide.")
    if data.Branches:
        valid = set(db.scalars(select(branch.c.Code_Type_Branche).where(branch.c.Code_Type_Branche.in_(set(data.Branches)))))
        if valid != set(data.Branches):
            raise HTTPException(status_code=422, detail="Une branche sélectionnée est invalide.")


@router.get("/lookups")
def get_lookups(db: Session = Depends(get_db)) -> dict[str, Any]:
    family = lookup_table(db, settings.db_family_prestataire_table)
    region = lookup_table(db, settings.db_region_table)
    company = lookup_table(db, settings.db_company_table)
    branch = lookup_table(db, settings.db_branch_type_table)
    main = lookup_table(db, settings.db_prestataire_table)
    families = db.execute(select(family.c.Code_Famille, family.c.Libelle_Famille).order_by(family.c.Libelle_Famille)).all()
    regions = db.execute(select(region.c.Code_Region, region.c.Libelle_Region).order_by(region.c.Libelle_Region)).all()
    companies = db.execute(select(company.c.code_compagnie, company.c.nom_compagnie).order_by(company.c.nom_compagnie)).all()
    branches = db.execute(select(branch.c.Code_Type_Branche, branch.c.Libelle_Type_Branche).order_by(branch.c.Libelle_Type_Branche)).all()
    notes = db.scalars(select(main.c.Note).where(main.c.Note.is_not(None), main.c.Note != "").distinct().order_by(main.c.Note)).all()
    return {
        "families": [{"id": r[0], "label": r[1]} for r in families],
        "regions": [{"id": r[0], "label": r[1]} for r in regions],
        "companies": [{"id": r[0], "label": r[1]} for r in companies],
        "branches": [{"id": r[0], "label": r[1]} for r in branches],
        "notes": [{"value": value, "label": value.strip()} for value in notes],
    }


@router.get("")
def list_prestataires(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    main = tables(db)[settings.db_prestataire_table]
    query = select(main)
    if q.strip():
        term = f"%{q.strip()}%"
        query = query.where(main.c.Nom_Prestataire.ilike(term) | cast(main.c.Code_Prestataire, String).ilike(term))
    rows = db.execute(query.order_by(main.c.Nom_Prestataire).limit(limit).offset(offset)).mappings().all()
    return {"items": [serialize(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/{code_prestataire}")
def get_prestataire(code_prestataire: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    main, branch_rel, company_rel = tables(db).values()
    row = fetch_one(db, main, branch_rel, company_rel, code_prestataire)
    if row is None:
        raise HTTPException(status_code=404, detail="Prestataire introuvable.")
    return row


@router.post("", status_code=201)
def create_prestataire(data: PrestataireInput, db: Session = Depends(get_db)) -> dict[str, Any]:
    main, branch_rel, company_rel = tables(db).values()
    try:
        if db.execute(select(main.c.Code_Prestataire).where(main.c.Code_Prestataire == data.Code_Prestataire)).first():
            raise HTTPException(status_code=409, detail="Ce code prestataire existe déjà.")
        ensure_reference_values(db, data)
        values = data.model_dump(exclude={"Compagnies", "Branches"})
        values["Date_Heure_Systeme"] = datetime.now()
        db.execute(main.insert().values(**values))
        for company_id in set(data.Compagnies):
            db.execute(company_rel.insert().values(Code_Prestataire=data.Code_Prestataire, Code_Compagnie=company_id))
        for branch_id in set(data.Branches):
            db.execute(branch_rel.insert().values(Code_Prestataire=data.Code_Prestataire, Code_Type_Branche=branch_id))
        db.commit()
        return fetch_one(db, main, branch_rel, company_rel, data.Code_Prestataire) or {}
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Code prestataire déjà utilisé ou référence invalide.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de l'enregistrement du prestataire.") from exc


@router.put("/{code_prestataire}")
def update_prestataire(code_prestataire: int, data: PrestataireInput, db: Session = Depends(get_db)) -> dict[str, Any]:
    main, branch_rel, company_rel = tables(db).values()
    if data.Code_Prestataire != code_prestataire:
        raise HTTPException(status_code=422, detail="Le code prestataire ne peut pas être modifié.")
    try:
        if db.execute(select(main.c.Code_Prestataire).where(main.c.Code_Prestataire == code_prestataire)).first() is None:
            raise HTTPException(status_code=404, detail="Prestataire introuvable.")
        ensure_reference_values(db, data)
        values = data.model_dump(exclude={"Code_Prestataire", "Compagnies", "Branches"})
        db.execute(main.update().where(main.c.Code_Prestataire == code_prestataire).values(**values))
        db.execute(company_rel.delete().where(company_rel.c.Code_Prestataire == code_prestataire))
        db.execute(branch_rel.delete().where(branch_rel.c.Code_Prestataire == code_prestataire))
        for company_id in set(data.Compagnies):
            db.execute(company_rel.insert().values(Code_Prestataire=code_prestataire, Code_Compagnie=company_id))
        for branch_id in set(data.Branches):
            db.execute(branch_rel.insert().values(Code_Prestataire=code_prestataire, Code_Type_Branche=branch_id))
        db.commit()
        return fetch_one(db, main, branch_rel, company_rel, code_prestataire) or {}
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Une référence sélectionnée est invalide.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la modification du prestataire.") from exc


@router.delete("/{code_prestataire}", status_code=204)
def delete_prestataire(code_prestataire: int, db: Session = Depends(get_db)) -> Response:
    main, branch_rel, company_rel = tables(db).values()
    try:
        if db.execute(select(main.c.Code_Prestataire).where(main.c.Code_Prestataire == code_prestataire)).first() is None:
            raise HTTPException(status_code=404, detail="Prestataire introuvable.")
        db.execute(company_rel.delete().where(company_rel.c.Code_Prestataire == code_prestataire))
        db.execute(branch_rel.delete().where(branch_rel.c.Code_Prestataire == code_prestataire))
        db.execute(main.delete().where(main.c.Code_Prestataire == code_prestataire))
        db.commit()
        return Response(status_code=204)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Suppression impossible : ce prestataire est utilisé par d'autres données.") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Erreur lors de la suppression du prestataire.") from exc
