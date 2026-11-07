"""Réconciliation de police, as in the legacy software (every signed-in user):

- a police (TPolice) goes to another client, with the quittances not yet comptabilisées that are chosen (TQuittance);
  the client's settlements (TRegCli, RDetRegCli) stay as they were paid;
- a wrong N° police, N° quittance or N° attestation is replaced by the right one, here and in every table that
  refers to it (settlements, pièces, dépenses, attestations, avenants, sinistres, bordereaux, tarifs…).

The dbo tables have no keys: each number is matched trimmed, and a number already used is refused.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db


router = APIRouter(prefix="/api/reconciliation", tags=["Réconciliation de police"], dependencies=[Depends(require_current_user)])

# The tables (and their column) holding each number, besides TPolice / TQuittance themselves. Archive copies (TT…,
# TRDetRegCli, TRPiece) and the print buffers (TTemp…) are left as they are.
POLICE_REFS = [
    ("TDepense", "num_police"), ("STDepense", "num_police"), ("RQuittance_Attestation", "Num_Police"),
    ("TPoliceFlotte", "num_police"), ("TNoteHonoraire", "Num_Police"), ("TBordereauLigne", "num_police"),
    ("RTarif_Assistance", "Num_Police"), ("RTarif_Auto_STAR", "Num_Police"), ("RTarif_MultiRisque", "Num_Police"),
    ("RTarif_Transport", "Num_Police"), ("RTarif_Vie", "Num_Police"),
]
NUM_INT_REFS = [
    ("TQuittance", "num_int"), ("TQuittance_Suivi", "num_int"), ("TPoliceFlotte", "num_int"), ("TPoliceGarantie", "num_int"),
    ("TPoliceRevalorisation", "num_int"), ("TAvenant", "Num_int"), ("Sinistre", "Num_Int"), ("TBordereau", "num_int"),
    ("TBordereauweb", "num_int"), ("TRecours", "num_int"),
]
QUITTANCE_REFS = [
    ("TQuittance", "num_quittance_Acompte"), ("TQuittance_Suivi", "num_quittance"), ("RDetRegCli", "Num_quitance"),
    ("RPiece", "Num_Quittance"), ("TDepense", "num_quittance"), ("STDepense", "num_quittance"),
    ("RQuittance_Attestation", "Num_Quittance"), ("TAvenant", "Num_quittance"), ("TBordereau", "num_quittance"),
    ("TBordereauweb", "num_quittance"), ("TBordereauLigne", "num_quittance"), ("TRecuSauter", "Num_Quittance"),
    ("TQuittanceCarteVerte", "num_quittance"), ("TPieceRetour", "num_quittance"), ("TVersement", "num_quittance"),
    ("RTarif_Assistance", "Num_Quittance"), ("RTarif_Auto_STAR", "Num_Quittance"), ("RTarif_MultiRisque", "Num_Quittance"),
    ("RTarif_Transport", "Num_Quittance"), ("RTarif_Vie", "Num_Quittance"),
]
ATTESTATION_REFS = [
    ("TQuittance", "Num_Attestation"), ("TQuittance_Suivi", "Num_Attestation"), ("RQuittance_Attestation", "Num_Attestation"),
    ("TAvenant", "Num_Attestation"), ("RTarif_Auto_STAR", "Num_Attestation"),
]


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Base de données indisponible : {exc.__class__.__name__}.")


def _user(user: dict[str, Any]) -> str:
    return str(user.get("sub", ""))[:20]


def _existing(db: Session) -> set[tuple[str, str]]:
    """The (table, column) pairs of dbo, so a table missing from a database is skipped."""
    rows = db.execute(text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'dbo'")).all()
    return {(table, column) for table, column in rows}


def _cascade(db: Session, refs: list[tuple[str, str]], old: str, new: str, existing: set[tuple[str, str]]) -> dict[str, int]:
    """Replaces old by new in each (table, column); the lines changed per table."""
    changed: dict[str, int] = {}
    for table, column in refs:
        if (table, column) not in existing:
            continue
        count = db.execute(text(f'UPDATE dbo."{table}" SET "{column}" = :new WHERE TRIM("{column}") = :old'), {"old": old, "new": new}).rowcount
        if count:
            changed[table] = changed.get(table, 0) + count
    return changed


# ---------------------------------------------------------------------------------------------
# The police, its client and its quittances not yet comptabilisées
# ---------------------------------------------------------------------------------------------

@router.get("/police")
def find_police(numero: str = Query(..., min_length=1, max_length=30), db: Session = Depends(get_db)) -> dict[str, Any]:
    number = numero.strip()
    try:
        polices = db.execute(
            text(
                'SELECT TRIM(p.num_int) AS num_int, TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client, '
                'TRIM(c.nom_client) AS nom_client, TRIM(k.nom_compagnie) AS nom_compagnie, TRIM(p.etat) AS etat, p.effet '
                'FROM dbo."TPolice" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
                'LEFT JOIN dbo."TCompagnie" k ON k.code_compagnie = p.code_compagnie '
                'WHERE TRIM(p.num_police) = :n OR TRIM(p.num_int) = :n ORDER BY p.effet DESC NULLS LAST'
            ),
            {"n": number},
        ).mappings().all()
        items = []
        for police in polices:
            quittances = db.execute(
                text(
                    'SELECT TRIM(q.num_quittance) AS num_quittance, TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, '
                    'q.date_saisie, q.echeance, TRIM(q.tc) AS tc, COALESCE(q.prime_net, 0) AS prime_net, COALESCE(q.prime_total, 0) AS prime_total, '
                    'COALESCE(q.solde_quittance, 0) AS solde_quittance, TRIM(q.type_quittance) AS type_quittance '
                    'FROM dbo."TQuittance" q LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client) '
                    'WHERE TRIM(q.num_int) = :i AND COALESCE(q."Comptabiliser", \'N\') <> \'O\' ORDER BY q.echeance, TRIM(q.num_quittance)'
                ),
                {"i": police["num_int"]},
            ).mappings().all()
            items.append({**_plain(police), "quittances": [_plain(row) for row in quittances]})
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": items}


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


@router.get("/client/{code_client}")
def find_client(code_client: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        row = db.execute(
            text('SELECT TRIM(code_client) AS code_client, TRIM(nom_client) AS nom_client FROM dbo."TClient" WHERE TRIM(code_client) = :c LIMIT 1'),
            {"c": code_client.strip()},
        ).mappings().first()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    if not row:
        raise HTTPException(status_code=404, detail=f"Aucun client de code {code_client.strip()}.")
    return dict(row)


class ClientChange(BaseModel):
    num_int: str = Field(min_length=1, max_length=30)
    code_client: str = Field(min_length=1, max_length=8)
    quittances: list[str] = Field(default_factory=list, max_length=2000)


@router.put("/police/client")
def change_police_client(payload: ClientChange, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The police goes to the new client, with the chosen quittances not yet comptabilisées."""
    num_int, code = payload.num_int.strip(), payload.code_client.strip()
    numbers = sorted({number.strip() for number in payload.quittances if number.strip()})
    try:
        police = db.execute(
            text('SELECT TRIM(num_police) AS num_police, TRIM(code_client) AS code_client FROM dbo."TPolice" WHERE TRIM(num_int) = :n'),
            {"n": num_int},
        ).mappings().all()
        if len(police) != 1:
            raise HTTPException(status_code=404 if not police else 409, detail=f"Police N° {num_int} introuvable." if not police
                                else f"Plusieurs polices portent le N° {num_int} : corrigez-les d'abord.")
        if police[0]["code_client"] == code:
            raise HTTPException(status_code=409, detail=f"La police appartient déjà au client {code}.")
        if not db.execute(text('SELECT 1 FROM dbo."TClient" WHERE TRIM(code_client) = :c'), {"c": code}).first():
            raise HTTPException(status_code=404, detail=f"Aucun client de code {code}.")
        params = {"n": num_int, "c": code, "u": _user(user), "q": numbers}
        db.execute(text('UPDATE dbo."TPolice" SET code_client = :c, "Update_User" = :u, "Update_Date" = now() WHERE TRIM(num_int) = :n'), params)
        flotte = db.execute(text('UPDATE dbo."TPoliceFlotte" SET code_client = :c WHERE TRIM(num_int) = :n'), params).rowcount
        moved = db.execute(
            text(
                'UPDATE dbo."TQuittance" SET code_client = :c, "Update_User" = :u, "Update_Date" = now() '
                'WHERE TRIM(num_int) = :n AND TRIM(num_quittance) = ANY(:q) AND COALESCE("Comptabiliser", \'N\') <> \'O\''
            ),
            params,
        ).rowcount if numbers else 0
        if moved != len(numbers):
            db.rollback()
            raise HTTPException(status_code=409, detail="Une quittance choisie a été comptabilisée ou modifiée entre-temps : rechargez la police.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"numero": police[0]["num_police"] or num_int, "ancien_client": police[0]["code_client"], "code_client": code,
            "quittances": moved, "vehicules_flotte": flotte}


# ---------------------------------------------------------------------------------------------
# Rectification of a N° police, N° quittance or N° attestation
# ---------------------------------------------------------------------------------------------

class NumberChange(BaseModel):
    ancien: str = Field(min_length=1, max_length=30)
    nouveau: str = Field(min_length=1, max_length=30)


def _rename_police(db: Session, old: str, new: str, user: str, existing: set[tuple[str, str]]) -> dict[str, int]:
    polices = db.execute(
        text('SELECT DISTINCT TRIM(num_int) AS num_int, TRIM(num_police) AS num_police FROM dbo."TPolice" WHERE TRIM(num_police) = :o'),
        {"o": old},
    ).mappings().all()
    if not polices:
        raise HTTPException(status_code=404, detail=f"Aucune police N° {old}.")
    if len(polices) > 1:
        raise HTTPException(status_code=409, detail=f"Plusieurs polices portent le N° {old} : impossible de savoir laquelle corriger.")
    if db.execute(text('SELECT 1 FROM dbo."TPolice" WHERE TRIM(num_police) = :n OR TRIM(num_int) = :n'), {"n": new}).first():
        raise HTTPException(status_code=409, detail=f"Le N° police {new} existe déjà.")
    num_int = polices[0]["num_int"]
    # Most policies have their N° police as internal number: then the internal number, which links the quittances,
    # sinistres, bordereaux… to the police, changes too.
    with_int = num_int == old
    changed = {"TPolice": db.execute(
        text(
            'UPDATE dbo."TPolice" SET num_police = :new, num_int = CASE WHEN :with_int THEN :new ELSE num_int END, '
            '"Update_User" = :u, "Update_Date" = now() WHERE TRIM(num_int) = :i'
        ),
        {"new": new, "with_int": with_int, "u": user, "i": num_int},
    ).rowcount}
    for refs in ([POLICE_REFS, NUM_INT_REFS] if with_int else [POLICE_REFS]):
        for table, count in _cascade(db, refs, old, new, existing).items():
            changed[table] = changed.get(table, 0) + count
    return changed


def _rename_quittance(db: Session, old: str, new: str, user: str, existing: set[tuple[str, str]]) -> dict[str, int]:
    found = db.execute(text('SELECT COUNT(*) FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :o'), {"o": old}).scalar()
    if not found:
        raise HTTPException(status_code=404, detail=f"Aucune quittance N° {old}.")
    if found > 1:
        raise HTTPException(status_code=409, detail=f"Plusieurs quittances portent le N° {old} : impossible de savoir laquelle corriger.")
    if db.execute(text('SELECT 1 FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :n'), {"n": new}).first():
        raise HTTPException(status_code=409, detail=f"Le N° quittance {new} existe déjà.")
    changed = {"TQuittance": db.execute(
        text('UPDATE dbo."TQuittance" SET num_quittance = :new, "Update_User" = :u, "Update_Date" = now() WHERE TRIM(num_quittance) = :o'),
        {"new": new, "u": user, "o": old},
    ).rowcount}
    changed.update(_cascade(db, QUITTANCE_REFS, old, new, existing))
    return changed


def _rename_attestation(db: Session, old: str, new: str, user: str, existing: set[tuple[str, str]]) -> dict[str, int]:
    used = 'SELECT 1 FROM dbo."TQuittance" WHERE TRIM("Num_Attestation") = :n UNION ALL SELECT 1 FROM dbo."RQuittance_Attestation" WHERE TRIM("Num_Attestation") = :n'
    if not db.execute(text(used), {"n": old}).first():
        raise HTTPException(status_code=404, detail=f"Aucune attestation N° {old}.")
    if db.execute(text(used), {"n": new}).first():
        raise HTTPException(status_code=409, detail=f"L'attestation N° {new} existe déjà.")
    db.execute(text('UPDATE dbo."TQuittance" SET "Update_User" = :u, "Update_Date" = now() WHERE TRIM("Num_Attestation") = :o'), {"u": user, "o": old})
    return _cascade(db, ATTESTATION_REFS, old, new, existing)


RENAMES = {"police": _rename_police, "quittance": _rename_quittance, "attestation": _rename_attestation}


@router.put("/numero/{kind}")
def rename_number(kind: str, payload: NumberChange, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rename = RENAMES.get(kind)
    if not rename:
        raise HTTPException(status_code=404, detail="Rectification inconnue.")
    old, new = payload.ancien.strip(), payload.nouveau.strip()
    if old == new:
        raise HTTPException(status_code=422, detail="Le nouveau numéro est identique à l'ancien.")
    try:
        changed = rename(db, old, new, _user(user), _existing(db))
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"numero": f"{old} → {new}", "ancien": old, "nouveau": new, "tables": changed}
