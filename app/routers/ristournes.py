"""Gestion des ristournes, as in the legacy software.

A ristourne (rebate given back to a client, after a cancellation or a reduction) is a TQuittance row of type 'R':
ristourne nette = prime_net, décommission = commission, détaxe = Taxe, ristourne totale = montant_quittance
(nette + détaxe) and solde = what is still to pay to the client (lowered by the rebate payments, TDepense).
"""
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db


router = APIRouter(prefix="/api/ristournes", tags=["Ristournes"], dependencies=[Depends(require_current_user)])

EPS = 0.0005

# The latest row of each policy (the policy table has no primary key and can repeat a policy).
POLICY_SQL = """
SELECT DISTINCT ON (TRIM(p.num_int)) TRIM(p.num_int) AS num_int, TRIM(p.num_police) AS num_police,
       TRIM(p.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, p.code_compagnie,
       TRIM(co.nom_compagnie) AS nom_compagnie, TRIM(p.code_branche) AS code_branche, TRIM(b.nom_branche) AS nom_branche
FROM dbo."TPolice" p
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client)
LEFT JOIN dbo."TCompagnie" co ON co.code_compagnie = p.code_compagnie
LEFT JOIN dbo."TBranche" b ON b.code_compagnie = p.code_compagnie AND TRIM(b.code_branche) = TRIM(p.code_branche)
WHERE {where}
ORDER BY TRIM(p.num_int), p.date_creation DESC NULLS LAST
"""

RISTOURNE_SQL = """
SELECT TRIM(q.num_quittance) AS num_ristourne, TRIM(q.num_int) AS num_int, pol.num_police,
       TRIM(q.code_client) AS code_client, TRIM(c.nom_client) AS nom_client,
       pol.code_compagnie, pol.nom_compagnie, pol.code_branche, pol.nom_branche,
       q.date_emission AS date_du, q.echeance AS date_au, q.date_saisie, q."Date_Reglement" AS date_reglement,
       q."Date_Comptabiliser" AS date_comptabilise, COALESCE(q."Comptabiliser", 'N') = 'O' AS comptabilise,
       q.prime_net AS nette, q.commission AS decommission, q."Taxe" AS detaxe,
       q.montant_quittance AS totale, q.solde_quittance AS solde, TRIM(q.observation) AS observation,
       TRIM(q.infoquittance) AS informations
FROM dbo."TQuittance" q
LEFT JOIN LATERAL ({policy}) pol ON TRUE
LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client)
WHERE TRIM(q.type_quittance) = 'R' AND {where}
"""


def _ristourne_sql(where: str) -> str:
    return RISTOURNE_SQL.format(policy=POLICY_SQL.format(where="TRIM(p.num_int) = TRIM(q.num_int)"), where=where)


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key] = value
    return out


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Base de données indisponible : {str(getattr(exc, 'orig', exc))[:200]}")


class RistournePayload(BaseModel):
    num_police: str = Field(min_length=1, max_length=20)
    date_du: date | None = None
    date_au: date | None = None
    date_saisie: date | None = None
    nette: float = 0
    decommission: float = 0
    detaxe: float = 0
    observation: str | None = Field(default=None, max_length=50)
    informations: str | None = Field(default=None, max_length=200)


class RistourneCreate(RistournePayload):
    num_ristourne: str = Field(min_length=1, max_length=15)


def _policy(db: Session, number: str) -> dict[str, Any]:
    row = db.execute(
        text(POLICY_SQL.format(where="(TRIM(p.num_police) = :n OR TRIM(p.num_int) = :n)")), {"n": number.strip()}
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Police {number} introuvable.")
    return _plain(row)


def _get(db: Session, number: str) -> dict[str, Any]:
    row = db.execute(text(_ristourne_sql("TRIM(q.num_quittance) = :n")), {"n": number.strip()}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ristourne {number} introuvable.")
    return _plain(row)


def _paid(db: Session, number: str) -> float:
    """What was already paid to the client on the ristourne (rebate payments of the journal de dépense)."""
    return float(db.execute(
        text("SELECT COALESCE(SUM(mont_dep), 0) FROM dbo.\"TDepense\" WHERE TRIM(\"Typ_dep\") = 'T' AND TRIM(num_quittance) = :n"),
        {"n": number},
    ).scalar_one() or 0)


def _amounts(payload: RistournePayload) -> dict[str, Any]:
    if min(payload.nette, payload.decommission, payload.detaxe) < 0:
        raise HTTPException(status_code=422, detail="Les montants de la ristourne doivent être positifs.")
    if payload.date_du and payload.date_au and payload.date_au < payload.date_du:
        raise HTTPException(status_code=422, detail="La date « Au » précède la date « Du ».")
    return {"totale": round(payload.nette + payload.detaxe, 3)}


@router.get("")
def list_ristournes(q: str = "", limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The ristournes, newest first, searched by number, policy, client code or name."""
    term = q.strip()
    where = "TRUE"
    if term:
        where = ("(TRIM(q.num_quittance) ILIKE :like OR pol.num_police ILIKE :like OR TRIM(q.code_client) = :term "
                 "OR c.nom_client ILIKE :like)")
    try:
        rows = db.execute(
            text(_ristourne_sql(where) + " ORDER BY q.date_saisie DESC NULLS LAST, q.num_quittance DESC LIMIT :limit"),
            {"term": term, "like": f"%{term}%", "limit": limit},
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": [_plain(row) for row in rows]}


@router.get("/police/{number}")
def policy_for_ristourne(number: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The client, company and branch of a policy, filled in when a ristourne is entered."""
    try:
        return _policy(db, number)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc


@router.get("/{number}")
def get_ristourne(number: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return _get(db, number)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc


@router.post("", status_code=201)
def create_ristourne(payload: RistourneCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    number = payload.num_ristourne.strip()
    amounts = _amounts(payload)
    try:
        if db.execute(text('SELECT 1 FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :n'), {"n": number}).first():
            raise HTTPException(status_code=409, detail=f"Le n° {number} existe déjà (quittance ou ristourne).")
        policy = _policy(db, payload.num_police)
        saisie = payload.date_saisie or date.today()
        db.execute(
            text("""
                INSERT INTO dbo."TQuittance" (
                    num_quittance, num_int, code_client, code_branche, date_saisie, date_emission, echeance, "Periode",
                    prime_net, prime_total, commission, "Taxe", montant_quittance, solde_quittance,
                    timbre, cout_attestation, transport, frais_quit, observation, infoquittance, type_quittance,
                    "Feuille_Caisse", "Comptabiliser", "Validation", "RE", "AV", "Date_M", "Create_Date"
                ) VALUES (
                    :n, :num_int, :code_client, :code_branche, :saisie, :du, :au, :periode,
                    :nette, :totale, :decommission, :detaxe, :totale, :totale,
                    0, 0, 0, 0, COALESCE(:observation, ''), :informations, 'R',
                    'N', 'N', 'N', 'N', 'N', :saisie, :now
                )
            """),
            {
                "n": number, "num_int": policy["num_int"], "code_client": policy["code_client"],
                "code_branche": policy["code_branche"], "saisie": saisie, "du": payload.date_du, "au": payload.date_au,
                "periode": f"{saisie:%m/%Y}", "nette": payload.nette, "decommission": payload.decommission,
                "detaxe": payload.detaxe, "totale": amounts["totale"], "observation": payload.observation,
                "informations": payload.informations, "now": datetime.now(),
            },
        )
        db.commit()
        return _get(db, number)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc


@router.put("/{number}")
def update_ristourne(number: str, payload: RistournePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    amounts = _amounts(payload)
    try:
        current = _get(db, number)
        if current["comptabilise"]:
            raise HTTPException(status_code=409, detail="Ristourne comptabilisée : elle ne peut plus être modifiée.")
        paid = _paid(db, current["num_ristourne"])
        if amounts["totale"] < paid - EPS:
            raise HTTPException(status_code=409, detail=f"La ristourne totale ne peut pas être inférieure au montant déjà payé ({paid:.3f}).")
        policy = _policy(db, payload.num_police)
        db.execute(
            text("""
                UPDATE dbo."TQuittance" SET num_int = :num_int, code_client = :code_client, code_branche = :code_branche,
                    date_saisie = COALESCE(:saisie, date_saisie), date_emission = :du, echeance = :au,
                    prime_net = :nette, prime_total = :totale, commission = :decommission, "Taxe" = :detaxe,
                    montant_quittance = :totale, solde_quittance = :solde, observation = COALESCE(:observation, ''),
                    infoquittance = :informations, "Date_M" = :now
                WHERE TRIM(num_quittance) = :n AND TRIM(type_quittance) = 'R'
            """),
            {
                "n": current["num_ristourne"], "num_int": policy["num_int"], "code_client": policy["code_client"],
                "code_branche": policy["code_branche"], "saisie": payload.date_saisie, "du": payload.date_du,
                "au": payload.date_au, "nette": payload.nette, "decommission": payload.decommission,
                "detaxe": payload.detaxe, "totale": amounts["totale"], "solde": round(amounts["totale"] - paid, 3),
                "observation": payload.observation, "informations": payload.informations, "now": datetime.now(),
            },
        )
        db.commit()
        return _get(db, current["num_ristourne"])
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc


@router.delete("/{number}", status_code=204)
def delete_ristourne(number: str, db: Session = Depends(get_db)) -> Response:
    try:
        current = _get(db, number)
        if current["comptabilise"]:
            raise HTTPException(status_code=409, detail="Ristourne comptabilisée : elle ne peut pas être supprimée.")
        if _paid(db, current["num_ristourne"]) > EPS:
            raise HTTPException(status_code=409, detail="Ristourne déjà payée au client : supprimez d’abord son règlement.")
        db.execute(
            text("DELETE FROM dbo.\"TQuittance\" WHERE TRIM(num_quittance) = :n AND TRIM(type_quittance) = 'R'"),
            {"n": current["num_ristourne"]},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return Response(status_code=204)
