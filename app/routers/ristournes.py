"""Gestion des ristournes, as in the legacy software.

A ristourne (rebate given back to a client, after a cancellation or a reduction) is a TQuittance row of type 'R':
ristourne nette = prime_net, décommission = commission, détaxe = Taxe, ristourne totale = montant_quittance
(nette + détaxe) and solde = what is still to pay to the client (lowered by the rebate payments, TDepense).
"""
import json
import logging
import re
from datetime import date, datetime
from itertools import combinations
from typing import Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.text_fields import label_values
from app.routers.assistant import FicheRequest, _gemini_generate, _ocr_space


logger = logging.getLogger(__name__)
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
    num_int: str | None = Field(default=None, max_length=20)  # the exact policy, when its number is shared
    date_du: date | None = None
    date_au: date | None = None
    date_saisie: date | None = None
    nette: float = 0
    decommission: float = 0
    detaxe: float = 0
    # The Prime Totale printed on the company's quittance, when the ristourne comes from one: kept as printed
    # (nette + détaxe may differ from it by a rounding millime). Otherwise the total is nette + détaxe.
    totale: float | None = None
    observation: str | None = Field(default=None, max_length=50)
    informations: str | None = Field(default=None, max_length=200)


class RistourneCreate(RistournePayload):
    num_ristourne: str = Field(min_length=1, max_length=15)


def _policies(db: Session, number: str) -> list[dict[str, Any]]:
    """The policies a number designates: those with this policy number, else the one with this internal number.
    A policy number can equal another policy's internal number, and a few policy numbers belong to several clients."""
    for column in ("num_police", "num_int"):
        rows = db.execute(text(POLICY_SQL.format(where=f"TRIM(p.{column}) = :n")), {"n": number.strip()}).mappings().all()
        if rows:
            return [_plain(row) for row in rows]
    return []


def _policy(db: Session, number: str, num_int: str | None = None) -> dict[str, Any]:
    """The policy of a ristourne: the exact one chosen (num_int) or the only one with this number."""
    if num_int:
        row = db.execute(text(POLICY_SQL.format(where="TRIM(p.num_int) = :n")), {"n": num_int.strip()}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Police {number} introuvable.")
        return _plain(row)
    found = _policies(db, number)
    if not found:
        raise HTTPException(status_code=404, detail=f"Police {number} introuvable.")
    if len(found) > 1:
        clients = ", ".join(f"{item['nom_client'] or item['code_client']} ({item['nom_compagnie'] or item['code_compagnie']})" for item in found)
        raise HTTPException(status_code=409, detail=f"Plusieurs polices portent le n° {number} : {clients}. Choisissez la bonne police.")
    return found[0]


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
    computed = round(payload.nette + payload.detaxe, 3)
    if payload.totale is None:
        return {"totale": computed}
    if payload.totale < 0:
        raise HTTPException(status_code=422, detail="La ristourne totale doit être positive.")
    if abs(payload.totale - computed) > 1:
        raise HTTPException(status_code=422, detail=f"La ristourne totale ({payload.totale:.3f}) ne correspond pas à nette + détaxe ({computed:.3f}).")
    return {"totale": round(payload.totale, 3)}


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
    """The client, company and branch of a policy, filled in when a ristourne is entered. When several policies
    carry this number, all of them are listed in "candidates" for the user to choose (the first is not assumed)."""
    try:
        found = _policies(db, number)
        if not found:
            raise HTTPException(status_code=404, detail=f"Police {number} introuvable.")
        return {**found[0], "candidates": found} if len(found) > 1 else found[0]
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
        policy = _policy(db, payload.num_police, payload.num_int)
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
        policy = _policy(db, payload.num_police, payload.num_int)
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


# ---------------------------------------------------------------------------------------------
# Reading a company document (avis / bordereau de ristourne, avenant d'annulation…), PDF or image
# ---------------------------------------------------------------------------------------------
LINE_FIELDS = {
    "num_ristourne": ("string", "Number of the ristourne / avis / quittance de ristourne / avenant as printed, else null."),
    "num_police": ("string", "Policy number (N° police / contrat) exactly as printed, keeping letters, slashes and dashes."),
    "client": ("string", "Name of the insured / souscripteur / assuré as printed."),
    "date_du": ("string", "Start of the period refunded (du / effet / date d'annulation), dd/mm/yyyy."),
    "date_au": ("string", "End of the period refunded (au / échéance), dd/mm/yyyy."),
    "date_document": ("string", "Date the document or the line was issued (date d'émission / date de l'avis), dd/mm/yyyy."),
    "nette": ("number", "Only if a column / label literally named 'ristourne nette', 'prime nette' or 'prime HT' is printed: its value; else null."),
    "decommission": ("number", "Commission / décommission printed (e.g. 'Commission : 2,177DT'), else null."),
    "detaxe": ("number", "Only if a column literally named 'détaxe' or 'total taxes' is printed: its value; else null."),
    "totale": ("number", "Total refunded printed (Prime Totale / TTC / montant de la ristourne)."),
    # The columns of the companies' quittances de ristourne (CARTE, STAR…), copied one by one with their sign.
    "prime_base": ("number", "Column 'Prime de Base' (or 'Prime RC'), with its sign as printed."),
    "prime_annexe": ("number", "Column 'Prime Annexe' (or 'Primes garanties annexes'), with its sign as printed."),
    "frais": ("number", "Column 'Frais' / 'Accessoires' / 'Coût de police', with its sign as printed (e.g. -50,000DT -> -50)."),
    "taxe_base": ("number", "Column 'Taxe Base' (taxe sur la prime de base), with its sign as printed (e.g. -4,188DT -> -4.188)."),
    "taxes_annexes": ("number", "Column 'Taxes Annexes', with its sign as printed."),
    "fpcsr": ("number", "Column 'F.P.C.S.R' when a value is printed in it, else null."),
    "fpac": ("number", "Column 'FPAC' when a value is printed in it, else null."),
    "fga": ("number", "Column 'FGA' when a value is printed in it, else null."),
    "timbre": ("number", "Column 'Timbre' when printed, else null."),
    "immatriculation": ("string", "Vehicle registration (Immatriculation) as printed, else null."),
    "motif": ("string", "Reason printed (annulation, résiliation, suspension, réduction de garantie, vente du véhicule…)."),
    "remarque": ("string", "Anything on this line that is unclear, crossed out, handwritten or hard to read; null when all is clear."),
}
# Columns that make up the ristourne nette (the premium and costs) and the détaxe (the taxes and funds).
# The agency's rule for a company's quittance de ristourne:
#   Ristourne nette = Prime de Base + Prime Annexe + Frais ; Détaxe = Taxe Base + Taxes Annexes ;
#   Ristourne totale = Prime Totale (as printed). FPCSR / FPAC / FGA / Timbre are shown, not added.
NET_COLUMNS = ("prime_base", "prime_annexe", "frais")
TAX_COLUMNS = ("taxe_base", "taxes_annexes")
OTHER_COLUMNS = ("fpcsr", "fpac", "fga", "timbre")
COLUMN_LABELS = {
    "prime_base": "Prime de base", "prime_annexe": "Prime annexe", "frais": "Frais", "taxe_base": "Taxe base",
    "taxes_annexes": "Taxes annexes", "fpcsr": "F.P.C.S.R", "fpac": "FPAC", "fga": "FGA", "timbre": "Timbre",
}
DOC_FIELDS = {
    "compagnie": "Insurance company that issued the document (STAR, COMAR, GAT, ASTREE, MAGHREBIA, CARTE, BIAT ASSURANCES, LLOYD, AMI, ZITOUNA TAKAFUL…).",
    "type_document": "Kind of document as printed (avis de ristourne, bordereau de ristournes, avenant, relevé…).",
    "date_document": "Date of the document, dd/mm/yyyy.",
}
EXTRACT_PROMPT = (
    "This is a scanned or photographed document from a Tunisian insurance company (French and/or Arabic) about ristournes: "
    "premiums refunded to clients after a cancellation, a suspension or a reduction (avis de ristourne, bordereau de ristournes, "
    "avenant d'annulation, relevé). Analyse the WHOLE document carefully, every page and every table row, including rows that "
    "continue on the next page. Return one item per ristourne / policy line; do not merge lines and do not skip any. "
    "Do not return sub-total or grand-total rows as items. The same ristourne printed twice on a page (for example the "
    "'QUITTANCE DE RISTOURNE' and its 'AVIS D'ENCAISSEMENT' with the same number) is ONE item: take the commission from "
    "whichever copy prints it.\n"
    "Rules: copy every value exactly as printed, into the field whose description matches the printed column name. Never "
    "compute, deduce or guess a value: an empty cell or a value that is not printed is null. Amounts are in Tunisian dinars "
    "with 3 decimals: '1.234,567', '1 234,567' or '20,087DT' means 1234.567 / 20.087; keep the minus sign of negative "
    "amounts. Dates as dd/mm/yyyy. Use 'remarque' to report anything uncertain."
)


def _extract_lines(request: FicheRequest) -> dict[str, Any]:
    if settings.anthropic_api_key:
        def prop(kind: str, hint: str) -> dict[str, Any]:
            return {"type": [kind, "null"], "description": hint}
        line = {"type": "object", "properties": {key: prop(*spec) for key, spec in LINE_FIELDS.items()},
                "required": list(LINE_FIELDS), "additionalProperties": False}
        schema = {
            "type": "object",
            "properties": {**{key: prop("string", hint) for key, hint in DOC_FIELDS.items()}, "lignes": {"type": "array", "items": line}},
            "required": [*DOC_FIELDS, "lignes"],
            "additionalProperties": False,
        }
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.assistant_model,
            max_tokens=16000,
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "high"},
            messages=[{"role": "user", "content": [
                {"type": "document" if request.media_type == "application/pdf" else "image",
                 "source": {"type": "base64", "media_type": request.media_type, "data": request.image_base64}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ]}],
        )
        raw = next((block.text for block in response.content if block.type == "text"), "{}")
        return json.loads(raw)
    gemini_type = {"string": "STRING", "number": "NUMBER"}
    line = {"type": "OBJECT", "properties": {key: {"type": gemini_type[kind], "nullable": True, "description": hint} for key, (kind, hint) in LINE_FIELDS.items()},
            "required": list(LINE_FIELDS)}
    schema = {"type": "OBJECT", "properties": {**{key: {"type": "STRING", "nullable": True, "description": hint} for key, hint in DOC_FIELDS.items()},
                                               "lignes": {"type": "ARRAY", "items": line}}, "required": [*DOC_FIELDS, "lignes"]}
    data = _gemini_generate({
        "contents": [{"parts": [{"text": EXTRACT_PROMPT}, {"inline_data": {"mime_type": request.media_type, "data": request.image_base64}}]}],
        "generationConfig": {"response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
    })
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return json.loads("".join(part.get("text", "") for part in parts))


def _signed(value: Any) -> float | None:
    """A printed amount with its sign: 1234.567, '1 234,567', '-50,000DT' -> 1234.567 / -50.0; unreadable -> None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    textual = str(value).strip()
    negative = textual.startswith("-") or textual.endswith("-") or (textual.startswith("(") and textual.endswith(")"))
    textual = re.sub(r"[^\d,.]", "", textual)
    if "," in textual:
        textual = textual.replace(".", "").replace(",", ".")
    elif textual.count(".") > 1:
        textual = textual.replace(".", "")
    try:
        number = round(float(textual), 3)
    except ValueError:
        return None
    return -number if negative else number


def _amount(value: Any) -> float | None:
    """A printed amount as a positive value (the form's amounts are positive)."""
    number = _signed(value)
    return None if number is None else abs(number)


def _iso(value: Any) -> str | None:
    match = re.search(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})", str(value or ""))
    if not match:
        return None
    day_, month, year = (int(part) for part in match.groups())
    year += 2000 if year < 100 else 0
    try:
        return date(year, month, day_).isoformat()
    except ValueError:
        return None


def _words(name: Any) -> set[str]:
    return {word for word in re.split(r"[^A-Z0-9]+", str(name or "").upper()) if len(word) > 2}


def _find_policies(db: Session, number: str) -> list[dict[str, Any]]:
    """The policies in the database for the number as printed (also tried without its spaces)."""
    for candidate in dict.fromkeys([number.strip(), re.sub(r"\s+", "", number)]):
        found = _policies(db, candidate) if candidate else []
        if found:
            return found
    return []


def _pick_policy(found: list[dict[str, Any]], client: str | None) -> dict[str, Any] | None:
    """The one policy meant: the only match, or among several the only one whose client is the document's."""
    if len(found) == 1:
        return found[0]
    same_client = [item for item in found if client and _words(client) & _words(item.get("nom_client"))]
    return same_client[0] if len(same_client) == 1 else None


def _clean(value: Any) -> str | None:
    return str(value).strip() or None if value is not None else None


def _check_line(db: Session, raw: dict[str, Any]) -> dict[str, Any]:
    line = {
        "num_ristourne": _clean(raw.get("num_ristourne")), "num_police": _clean(raw.get("num_police")), "client": _clean(raw.get("client")),
        "date_du": _iso(raw.get("date_du")), "date_au": _iso(raw.get("date_au")), "date_document": _iso(raw.get("date_document")),
        "nette": _amount(raw.get("nette")), "decommission": _amount(raw.get("decommission")),
        "detaxe": _amount(raw.get("detaxe")), "totale": _amount(raw.get("totale")),
        "immatriculation": _clean(raw.get("immatriculation")),
        "motif": _clean(raw.get("motif")), "remarque": _clean(raw.get("remarque")),
    }
    warnings: list[str] = []
    if line["remarque"]:
        warnings.append(f"Lecture incertaine : {line['remarque']}")
    # The columns printed on the document, kept as they are (sign included) to be shown with the line.
    columns = {key: _signed(raw.get(key)) for key in (*NET_COLUMNS, *TAX_COLUMNS, *OTHER_COLUMNS)}
    line["colonnes"] = [{"libelle": COLUMN_LABELS[key], "valeur": value} for key, value in columns.items() if value is not None]
    # No "ristourne nette" / "détaxe" printed as such (CARTE, STAR… quittances): their sums of the printed columns.
    computed: list[str] = []
    if line["nette"] is None and any(columns[key] is not None for key in NET_COLUMNS):
        line["nette"] = abs(round(sum(columns[key] or 0 for key in NET_COLUMNS), 3))
        computed.append(f"nette = {' + '.join(COLUMN_LABELS[key] for key in NET_COLUMNS if columns[key] is not None)}")
    if line["detaxe"] is None and any(columns[key] is not None for key in TAX_COLUMNS):
        line["detaxe"] = abs(round(sum(columns[key] or 0 for key in TAX_COLUMNS), 3))
        computed.append(f"détaxe = {' + '.join(COLUMN_LABELS[key] for key in TAX_COLUMNS if columns[key] is not None)}")
    if computed:
        warnings.append("Calculé à partir des colonnes du document : " + " ; ".join(computed) + ".")
    found = _find_policies(db, line["num_police"]) if line["num_police"] else []
    policy = _pick_policy(found, line["client"])
    if not line["num_police"]:
        warnings.append("N° de police non lu sur le document.")
    elif not found:
        warnings.append(f"Police {line['num_police']} introuvable dans la base.")
    elif policy is None:
        warnings.append(f"Plusieurs polices portent le n° {line['num_police']} : choisissez la bonne police.")
    elif line["client"] and policy.get("nom_client") and not (_words(line["client"]) & _words(policy["nom_client"])):
        warnings.append(f"Le client du document ({line['client']}) ne correspond pas au client de la police ({policy['nom_client']}).")
    exists = False
    if line["num_ristourne"]:
        exists = bool(db.execute(text('SELECT 1 FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :n'), {"n": line["num_ristourne"]}).first())
        if exists:
            warnings.append(f"Le n° {line['num_ristourne']} existe déjà dans la base.")
    else:
        warnings.append("N° de ristourne non lu : à saisir.")
    if line["nette"] is None:
        warnings.append("Ristourne nette non lue sur le document.")
    if line["nette"] is not None and line["totale"] is not None:
        expected = round(line["nette"] + (line["detaxe"] or 0), 3)
        if abs(expected - line["totale"]) > 0.01:
            warnings.append(f"Total imprimé {line['totale']:.3f} ≠ nette + détaxe ({expected:.3f}) : à vérifier.")
    if line["date_du"] and line["date_au"] and line["date_au"] < line["date_du"]:
        warnings.append("La date « Au » précède la date « Du ».")
    return {**line, "policy": policy, "candidates": found if len(found) > 1 else [], "exists": exists, "warnings": warnings}


def _ocr_lines(db: Session, request: FicheRequest) -> dict[str, Any]:
    """Without an AI service: OCR the document and keep only the policy numbers found in the database (amounts are not read)."""
    if not settings.ocr_space_api_key or settings.ocr_space_api_key == "helloworld":
        # ocr.space's shared demo key is nearly always refused (429): no waiting for it, the browser reads the document.
        raise HTTPException(status_code=503, detail="pas de clé OCR.space personnelle (OCR_SPACE_API_KEY)")
    texts: list[str] = []
    for language, engine in (("fre", 2), ("ara", 1)):
        try:
            value = _ocr_space(request.image_base64, request.media_type, language, engine).strip()
        except Exception:  # noqa: BLE001 - any OCR failure just means "no text from this pass"
            continue
        if value:
            texts.append(value)
    if not texts:
        raise HTTPException(status_code=503, detail="le service OCR de secours (ocr.space) est indisponible")
    raw = "\n".join(texts)
    quittance = _parse_quittance(raw)
    if quittance:
        return {"compagnie": quittance.pop("compagnie", None), "type_document": "Quittance de ristourne", "date_document": quittance.get("date_document"), "lignes": [quittance]}
    found = re.findall(r"[A-Z0-9][A-Z0-9/.-]{4,19}", raw.upper())
    tokens = dict.fromkeys(token for token in found if sum(ch.isdigit() for ch in token) >= 4)
    lines = [{"num_police": token, "remarque": "Lu par OCR : montants et dates à saisir."} for token in tokens if _find_policies(db, token)]
    return {"compagnie": None, "type_document": None, "date_document": None, "lignes": lines}


def _parse_quittance(raw: str) -> dict[str, Any] | None:
    """OCR text of one company "QUITTANCE DE RISTOURNE N° …" (CARTE layout): its labelled values and its row of amounts."""
    flat = re.sub(r"[ \t]+", " ", raw)
    number = re.search(r"RISTOURNE\s*N[°o]?\s*[:.]?\s*(\d{6,})", flat, re.IGNORECASE)
    if not number:
        return None
    pick = lambda pattern: (re.search(pattern, flat, re.IGNORECASE) or [None, None])[1]  # noqa: E731
    line: dict[str, Any] = {
        "num_ristourne": number.group(1),
        "num_police": pick(r"N[°o]?\s*CONTRAT\s*[:.]?\s*([A-Z0-9/.-]{5,20})"),
        "client": (pick(r"ASSURE\s*[:.]?\s*([^\n]{2,60})") or pick(r"SOUSCRIPTEUR\s*[:.]?\s*([^\n]{2,60})") or "").strip() or None,
        "date_du": pick(r"EFFET\s*DU\s*[:.]?\s*(\d{1,2}/\d{1,2}/\d{2,4})"),
        "date_au": pick(r"JUSQU.?AU\s*[:.]?\s*(\d{1,2}/\d{1,2}/\d{2,4})"),
        "date_document": pick(r"(?:\b|TUNIS)LE\s+(\d{1,2}/\d{1,2}/\d{2,4})"),  # "TUNIS LE 06/05/2026", read "TUNISLE"
        # The commission of the avis d'encaissement; the OCR garbles its label ("Coñimtéaton::") and its "DT" ("2,1770T").
        "decommission": _labelled_amounts(raw).get("decommission")
        or pick(r"\bCo\w{0,3}m\w{2,10}\s*:{1,2}\s*(\d{1,3}(?:[ .]\d{3})*,\d{3})"),
        # A Tunisian plate (4637 TU 86, RS 283583), else the few characters before the next label.
        "immatriculation": (pick(r"Immatriculation\s*[:.]?\s*(\d{1,4}\s*TU\s*\d{1,4}|RS\s*\d{3,7}|[^\n]{3,12}?)(?=\s{2,}|\s+(?:TUNIS|Marque|Type)\b|\n|$)") or "").strip() or None,
        "remarque": "Lu par OCR (service IA indisponible) : vérifiez chaque montant avec le document.",
    }
    if re.search(r"\bCARTE\b", flat):
        line["compagnie"] = "CARTE"
    # The row of amounts under "Prime de Base | Prime Annexe | Frais | Taxe Base | Taxes Annexes | F.P.C.S.R | FPAC |
    # FGA | Prime Totale". Every amount of the text, in reading order: an amount with 3 decimals followed by its "DT",
    # which the OCR often misreads ("0T", "DI", "D1", a lone "D") or loses.
    every = [cell.replace(" ", "") for cell in re.findall(r"(?<![\d,])(-?\s?\d{1,3}(?:[ .]\d{3})*,\d{3})(?=\s*(?:D\s?T|[D0O]\s?[T1I|l]|D\b|\s|\||\]|$))", raw)]
    checked = _consistent_row(every)
    if checked:
        # The row whose amounts add up: Base + Annexe + Frais + Taxe Base + Taxes Annexes (+ FPCSR / FPAC / FGA) = Totale.
        line.update(checked)
        return line
    # No row adds up: the amounts are not guessed from their position (a cell lost by the OCR shifts every other one).
    labelled = _labelled_amounts(raw).get("totale")
    if labelled:
        line["totale"] = labelled
    line["remarque"] = "Lu par OCR : le tableau des montants est illisible, saisissez la ristourne nette, la détaxe et la totale depuis le document."
    return line


# Labelled amounts of a document's text: the company's commission is the ristourne's décommission, and its Prime Totale
# (or ristourne totale / montant de la ristourne) is the ristourne totale. Only a value printed right after its label.
_AMOUNT = r"(-?[ ]?\d{1,3}(?:[ .]\d{3})*,\d{3}|-?[ ]?\d+[.,]\d{3})(?![\d,])"  # 1.234,567 / 1 234,567 / 20,087 / 4.188
_LABELLED = {
    "decommission": r"(?:D[ée]\s?commission|Commission|Comm\.)",
    "totale": r"(?:Prime\s+Totale|Ristourne\s+Totale|Total\s+(?:de\s+la\s+)?ristourne|Montant\s+(?:de\s+la\s+)?ristourne|Net\s+[àa]\s+(?:payer|rembourser))",
}


def _labelled_amounts(raw: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, label in _LABELLED.items():
        match = re.search(label + r"[ \t]*(?:\(?[ \t]*DT[ \t]*\)?)?[ \t]*[:.]?[ \t]*" + _AMOUNT, raw, re.IGNORECASE)
        if match:
            found[key] = match.group(1).replace(" ", "")
    return found


def _adds_up(parts: list[float], total: float) -> list[float] | None:
    """The cells (Base, Annexe, Frais, Taxe Base, Taxes Annexes…) when they add up to the Prime Totale. The OCR often
    drops the minus sign of Frais and Taxe Base (refunds are negative there): both signs are tried."""
    for frais_sign in (1, -1):
        for taxe_sign in (1, -1):
            cells = list(parts)
            cells[2] = abs(cells[2]) * frais_sign if frais_sign == -1 or cells[2] >= 0 else cells[2]
            cells[3] = abs(cells[3]) * taxe_sign if taxe_sign == -1 or cells[3] >= 0 else cells[3]
            if total > 0 and abs(sum(cells) - total) <= 0.011:
                return cells
    return None


def _consistent_row(amounts: list[str]) -> dict[str, Any] | None:
    """Among the amounts read in order, the row of the quittance's table whose cells add up to its Prime Totale.
    First a row read in order (5 cells + Prime Totale, plus up to 3 filled FPCSR / FPAC / FGA cells); else, as the
    scattered-text OCR may read the Prime Totale before the last cells or slip a stray value in, any 5 cells kept in
    their order within 8 neighbouring amounts, the Prime Totale being one of the others."""
    values = [_signed(value) for value in amounts]

    def row(cells: list[float], total: float) -> dict[str, Any]:
        found = dict(zip(("prime_base", "prime_annexe", "frais", "taxe_base", "taxes_annexes"), cells[:5]))
        found.update(dict(zip(("fpcsr", "fpac", "fga"), cells[5:])))
        return {**found, "totale": total}

    for width in (6, 7, 8, 9):
        for start in range(0, len(values) - width + 1):
            window = values[start:start + width]
            if any(value is None for value in window):
                continue
            cells = _adds_up(window[:-1], window[-1])
            if cells:
                return row(cells, window[-1])
    # The tightest match wins (fewest amounts skipped): with signs to try, a loose one can add up by chance.
    best: tuple[int, int, dict[str, Any]] | None = None
    for start in range(len(values)):
        window = [value for value in values[start:start + 8] if value is not None]
        for index, total in enumerate(window[1:], start=1):  # the window's first amount is the Prime de Base
            others = window[1:index] + window[index + 1:]
            for chosen in combinations(range(len(others)), 4):
                cells = _adds_up([window[0], *(others[position] for position in chosen)], total)
                if cells:
                    used = [0, index, *(position + 1 if position + 1 < index else position + 2 for position in chosen)]
                    skipped = max(used) + 1 - len(used)
                    if best is None or (skipped, start) < best[:2]:
                        best = (skipped, start, row(cells, total))
    return best[2] if best else None


def _dedupe(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per ristourne: a quittance and its avis d'encaissement (same number, same policy) are the same ristourne;
    the copy that prints more values is kept, completed with what only the other one prints (the commission)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str] | int] = []
    for index, raw in enumerate(lines):
        key = (str(raw.get("num_ristourne") or "").strip(), str(raw.get("num_police") or "").strip())
        if not key[0]:
            order.append(index)
            merged[(str(index), "")] = raw
            continue
        if key not in merged:
            merged[key] = dict(raw)
            order.append(key)
        else:
            kept = merged[key]
            for field, value in raw.items():
                if kept.get(field) in (None, "") and value not in (None, ""):
                    kept[field] = value
    return [merged[(str(item), "")] if isinstance(item, int) else merged[item] for item in order]


@router.post("/extract")
def extract_ristournes(request: FicheRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reads every ristourne of a company document and checks each one against the database. Nothing is saved:
    the user loads a line into the form, checks it and saves it."""
    source = "ai"
    document: dict[str, Any] | None = None
    ai_problem = "aucune clé de service IA n’est configurée sur le serveur (ANTHROPIC_API_KEY ou GEMINI_API_KEY)"
    if settings.anthropic_api_key or settings.gemini_api_key:
        try:
            document = _extract_lines(request)
        except HTTPException as exc:  # the AI service refused or is unreachable: fall back on OCR
            ai_problem = f"service IA : {exc.detail}"
        except anthropic.APIError as exc:
            ai_problem = f"service IA : {getattr(exc, 'message', None) or exc.__class__.__name__}"[:300]
        except ValueError:
            ai_problem = "service IA : réponse illisible"
        if document is not None:
            logger.info("Ristournes : document lu par l'IA")
        else:
            logger.warning("Ristournes : lecture IA impossible (%s), essai OCR", ai_problem)
    try:
        if document is None:
            source = "ocr"
            try:
                document = _ocr_lines(db, request)
            except HTTPException as exc:
                # Both readings failed: say why, so the cause can be fixed (key, quota, service down).
                raise HTTPException(status_code=503, detail=f"Lecture du document impossible : {ai_problem} ; {exc.detail}. Réessayez dans un moment ou saisissez la ristourne.") from exc
        return _answer(db, source, document)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc


def _answer(db: Session, source: str, document: dict[str, Any]) -> dict[str, Any]:
    """The lines of a document read (one per ristourne), each checked against the database."""
    raw_lines = _dedupe([raw for raw in (document.get("lignes") or []) if isinstance(raw, dict)])
    lines = [_check_line(db, raw) for raw in raw_lines]
    return {
        "source": source,
        "compagnie": _clean(document.get("compagnie")),
        "type_document": _clean(document.get("type_document")),
        "date_document": _iso(document.get("date_document")),
        "lignes": [line for line in lines if any(line[key] for key in ("num_ristourne", "num_police", "nette", "totale"))],
    }


class TextIn(BaseModel):
    text: str = Field(min_length=20, max_length=300_000)


@router.post("/extract-text")
def extract_ristournes_from_text(payload: TextIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The same reading from the text of the document recognised in the browser (when no AI service answers):
    the company's quittance de ristourne (CARTE layout), else the policy numbers found in the database."""
    try:
        quittance = _parse_quittance(payload.text)
        if quittance:
            document = {"compagnie": quittance.pop("compagnie", None), "type_document": "Quittance de ristourne",
                        "date_document": quittance.get("date_document"), "lignes": [quittance]}
        else:
            found = re.findall(r"[A-Z0-9][A-Z0-9/.-]{4,19}", payload.text.upper())
            tokens = dict.fromkeys(token for token in found if sum(ch.isdigit() for ch in token) >= 4)
            lines = [{"num_police": token, "remarque": "Lu par OCR : montants et dates à saisir."} for token in tokens if _find_policies(db, token)]
            # One policy on the document: its commission (décommission) and Prime Totale, when printed with their labels.
            if len(lines) == 1:
                lines[0].update(_labelled_amounts(payload.text))
            document = {"lignes": lines}
        return {**_answer(db, "ocr-local", document), "infos": label_values(payload.text)}
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
