"""Saisie des frais généraux, as in the legacy software.

A frais général is a TDepense row of the agency: Typ_dep 'D' (frais généraux W.J, types D01..D16) or 'F' (M.J, F01..F17),
code_type from ttype_dep, mod_dep the payment mode (C chèque, V virement, E espèces, T traite), num_cheq the pièce,
objet_dep the observation and code_banque the agency account it is paid from. num_dep is numbered per Typ_dep.
"""
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db


# A normal module (400): every user given it in "Access users" may use it.
router = APIRouter(prefix="/api/frais-generaux", tags=["Frais généraux"], dependencies=[Depends(require_current_user)])

# Only the W.J frais are entered now; the M.J ones (Typ_dep 'F') stay in TDepense as history. As in the legacy software,
# the frais accounts also get the recettes ('E', the commissions of the companies' feuilles de caisse transferred to
# them: "COM FC GAT DU 15/10/2026", types E01..E10) and the sorties diverses ('S'): the état détaillé of the account
# lists them all (crédit 'E', débit 'D' and 'S').
NATURES = {"D": "Frais Généraux (W.J)", "E": "Recettes diverses", "S": "Sorties diverses"}
NATURE_CODES = "".join(NATURES)
MODES = {"C": "Chèque", "V": "Virement", "E": "Espèces", "T": "Traite"}


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Base de données indisponible : {str(getattr(exc, 'orig', exc))[:200]}")


class FraisCreate(BaseModel):
    nature: str = Field(pattern=f"^[{NATURE_CODES}]$")
    code_type: str = Field(min_length=1, max_length=10)
    mode: str = Field(pattern="^[CVET]$")
    date_dep: date
    num_piece: str | None = Field(default=None, max_length=20)
    date_piece: date | None = None
    code_banque: str | None = Field(default=None, max_length=15)
    montant: float = Field(gt=0)
    observation: str | None = Field(default=None, max_length=50)


@router.get("/options")
def options(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The natures with their types of frais, the payment modes, the agency accounts given to the frais généraux in
    Gestion des banques (Type_Dep 'D', not archived) and the last one of them used per nature."""
    try:
        types = db.execute(text(
            'SELECT TRIM(code_type) AS code_type, TRIM(libelle) AS libelle, TRIM(typ_dep) AS nature FROM dbo.ttype_dep '
            "WHERE TRIM(typ_dep) IN :natures ORDER BY code_type"
        ).bindparams(bindparam("natures", expanding=True)), {"natures": list(NATURES)}).mappings().all()
        banks = db.execute(text(
            'SELECT TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque FROM dbo."TBanque" '
            "WHERE TRIM(\"Type_Dep\") = 'D' AND COALESCE(\"Archive\", FALSE) = FALSE ORDER BY code_banque"
        )).mappings().all()
        last = db.execute(text(
            'SELECT DISTINCT ON ("Typ_dep") TRIM("Typ_dep") AS nature, TRIM(code_banque) AS code_banque FROM dbo."TDepense" '
            "WHERE TRIM(\"Typ_dep\") IN :natures AND TRIM(code_banque) IN (SELECT TRIM(code_banque) FROM dbo.\"TBanque\" "
            "WHERE TRIM(\"Type_Dep\") = 'D' AND COALESCE(\"Archive\", FALSE) = FALSE) ORDER BY \"Typ_dep\", num_dep DESC"
        ).bindparams(bindparam("natures", expanding=True)), {"natures": list(NATURES)}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {
        "natures": [{"code": code, "libelle": label, "types": [dict(t) for t in types if t["nature"] == code]} for code, label in NATURES.items()],
        "modes": [{"code": code, "libelle": label} for code, label in MODES.items()],
        "banques": [dict(row) for row in banks],
        "banque_par_nature": {row["nature"]: row["code_banque"] for row in last},
    }


@router.get("")
def list_frais(
    nature: str = Query("D", pattern=f"^[TF{NATURE_CODES}]$"), debut: date | None = None, fin: date | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The frais of a nature between two dates (by default the current month), newest first. T (tous): the dépenses
    (frais généraux, D) and the recettes (E) together, with the total of each."""
    today = date.today()
    debut = debut or today.replace(day=1)
    fin = fin or today
    try:
        rows = db.execute(text("""
            SELECT d.num_dep, d.date_dep, TRIM(d.code_type) AS code_type, TRIM(t.libelle) AS libelle, TRIM(d.mod_dep) AS mode,
                   TRIM(d.num_cheq) AS num_piece, d.date_cheq AS date_piece, TRIM(d.code_banque) AS code_banque,
                   d.mont_dep AS montant, TRIM(d.objet_dep) AS observation, COALESCE(d."Comptabiliser", 'N') = 'O' AS comptabilise,
                   TRIM(d."Typ_dep") AS nature
            FROM dbo."TDepense" d LEFT JOIN dbo.ttype_dep t ON TRIM(t.code_type) = TRIM(d.code_type)
            WHERE TRIM(d."Typ_dep") = ANY(:natures) AND d.date_dep >= :debut AND d.date_dep < :fin
            ORDER BY d.date_dep DESC, d.num_dep DESC
        """), {"natures": ["D", "E"] if nature == "T" else [nature], "debut": debut, "fin": fin + timedelta(days=1)}).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    items = [_plain(row) for row in rows]
    depenses = round(sum(float(row["montant"] or 0) for row in items if row["nature"] != "E"), 3)
    recettes = round(sum(float(row["montant"] or 0) for row in items if row["nature"] == "E"), 3)
    return {"items": items, "total": round(depenses + recettes, 3), "total_depenses": depenses, "total_recettes": recettes}


@router.post("", status_code=201)
def create_frais(payload: FraisCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        known = db.execute(
            text("SELECT 1 FROM dbo.ttype_dep WHERE TRIM(code_type) = :c AND TRIM(typ_dep) = :n"),
            {"c": payload.code_type.strip(), "n": payload.nature},
        ).first()
        if known is None:
            raise HTTPException(status_code=422, detail="Ce type de frais n’appartient pas à la nature choisie.")
        # No sequence in this database: the next number of the nature, under a lock so two entries cannot share it.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.TDepense.num_dep'))"))
        number = db.execute(
            text('SELECT COALESCE(MAX(num_dep), 0) + 1 FROM dbo."TDepense" WHERE "Typ_dep" = :n'), {"n": payload.nature}
        ).scalar_one()
        db.execute(text("""
            INSERT INTO dbo."TDepense" (num_dep, "Typ_dep", date_dep, mont_dep, objet_dep, code_banque, num_cheq, date_cheq,
                code_client, sinistre, mod_dep, code_type, "Comptabiliser", "Validation", "Create_Date")
            VALUES (:num, :nature, :date_dep, :montant, :objet, :banque, :piece, :date_piece,
                '', '', :mode, :code_type, 'N', 'N', :now)
        """), {
            "num": number, "nature": payload.nature, "date_dep": payload.date_dep, "montant": round(payload.montant, 3),
            "objet": (payload.observation or "").strip() or None, "banque": (payload.code_banque or "").strip() or None,
            "piece": (payload.num_piece or "").strip() or None, "date_piece": payload.date_piece or payload.date_dep,
            "mode": payload.mode, "code_type": payload.code_type.strip(), "now": datetime.now(),
        })
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"num_dep": number, "nature": payload.nature}


@router.delete("/{nature}/{number}", status_code=204)
def delete_frais(nature: str, number: int, db: Session = Depends(get_db)) -> Response:
    if nature not in NATURES:
        raise HTTPException(status_code=404, detail="Nature de frais inconnue.")
    try:
        row = db.execute(
            text('SELECT COALESCE("Comptabiliser", \'N\') AS c FROM dbo."TDepense" WHERE "Typ_dep" = :n AND num_dep = :num'),
            {"n": nature, "num": number},
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Frais n° {number} introuvable.")
        if row.c == "O":
            raise HTTPException(status_code=409, detail="Frais comptabilisé : il ne peut pas être supprimé.")
        db.execute(text('DELETE FROM dbo."TDepense" WHERE "Typ_dep" = :n AND num_dep = :num'), {"n": nature, "num": number})
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return Response(status_code=204)
