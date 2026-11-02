"""Gestion des ristournes, as in the legacy software.

A ristourne (rebate given back to a client, after a cancellation or a reduction) is a TQuittance row of type 'R':
ristourne nette = prime_net, décommission = commission, détaxe = Taxe, ristourne totale = montant_quittance
(nette + détaxe) and solde = what is still to pay to the client (lowered by the rebate payments, TDepense).
"""
import json
import re
from datetime import date, datetime
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
from app.routers.assistant import FicheRequest, _gemini_generate, _ocr_space


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
    "nette": ("number", "Ristourne nette: the net premium refunded (prime nette / prime HT ristournée), positive."),
    "decommission": ("number", "Décommission: the commission taken back from the agent (commission / décommission), positive."),
    "detaxe": ("number", "Détaxe: the taxes refunded (taxes / TVA / FG / détaxe; the total of the tax columns), positive."),
    "totale": ("number", "Total refunded to the client (prime totale / TTC / montant de la ristourne), positive."),
    "motif": ("string", "Reason printed (annulation, résiliation, suspension, réduction de garantie, vente du véhicule…)."),
    "remarque": ("string", "Anything on this line that is unclear, crossed out, handwritten or hard to read; null when all is clear."),
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
    "Do not return sub-total or grand-total rows as items.\n"
    "Rules: copy every value exactly as printed. Never compute, deduce or guess a value: if an amount is not printed for a line, "
    "use null (do not derive it from other columns). Amounts are in Tunisian dinars with 3 decimals: '1.234,567' or '1 234,567' "
    "means 1234.567; give them as plain positive numbers. Dates as dd/mm/yyyy. Use 'remarque' to report anything uncertain."
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


def _amount(value: Any) -> float | None:
    """A printed amount: 1234.567, '1 234,567', '1.234,567' -> 1234.567; anything unreadable -> None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(abs(float(value)), 3)
    textual = re.sub(r"[^\d,.]", "", str(value))
    if "," in textual:
        textual = textual.replace(".", "").replace(",", ".")
    elif textual.count(".") > 1:
        textual = textual.replace(".", "")
    try:
        return round(float(textual), 3)
    except ValueError:
        return None


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
        "motif": _clean(raw.get("motif")), "remarque": _clean(raw.get("remarque")),
    }
    warnings: list[str] = []
    if line["remarque"]:
        warnings.append(f"Lecture incertaine : {line['remarque']}")
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
    texts: list[str] = []
    for language, engine in (("fre", 2), ("ara", 1)):
        try:
            value = _ocr_space(request.image_base64, request.media_type, language, engine).strip()
        except Exception:  # noqa: BLE001 - any OCR failure just means "no text from this pass"
            continue
        if value:
            texts.append(value)
    if not texts:
        raise HTTPException(status_code=503, detail="Lecture du document impossible (service OCR indisponible ou texte illisible).")
    found = re.findall(r"[A-Z0-9][A-Z0-9/.-]{4,19}", "\n".join(texts).upper())
    tokens = dict.fromkeys(token for token in found if sum(ch.isdigit() for ch in token) >= 4)
    lines = [{"num_police": token, "remarque": "Lu par OCR : montants et dates à saisir."} for token in tokens if _find_policies(db, token)]
    return {"compagnie": None, "type_document": None, "date_document": None, "lignes": lines}


@router.post("/extract")
def extract_ristournes(request: FicheRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reads every ristourne of a company document and checks each one against the database. Nothing is saved:
    the user loads a line into the form, checks it and saves it."""
    source = "ai"
    document: dict[str, Any] | None = None
    if settings.anthropic_api_key or settings.gemini_api_key:
        try:
            document = _extract_lines(request)
        except (HTTPException, anthropic.APIError, ValueError):
            document = None  # the AI service is unavailable: fall back on OCR
    try:
        if document is None:
            source = "ocr"
            document = _ocr_lines(db, request)
        lines = [_check_line(db, raw) for raw in (document.get("lignes") or []) if isinstance(raw, dict)]
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    lines = [line for line in lines if any(line[key] for key in ("num_ristourne", "num_police", "nette", "totale"))]
    return {
        "source": source,
        "compagnie": _clean(document.get("compagnie")),
        "type_document": _clean(document.get("type_document")),
        "date_document": _iso(document.get("date_document")),
        "lignes": lines,
    }
