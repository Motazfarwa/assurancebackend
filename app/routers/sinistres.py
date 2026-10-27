"""Sinistres: claim files stored in dbo."Sinistre" (the legacy software's table), and the reading of a scanned
constat amiable / déclaration de sinistre (image or PDF) to pre-fill a claim.

The claim number (Num_Sinistre_Agence) follows the legacy rule: year and month of the claim date, then a sequence
of three digits within that month (202610003 = 3rd claim of October 2026).
"""
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Any

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.routers.assistant import FicheRequest, _gemini_generate, _ocr_space


router = APIRouter(prefix="/api/sinistres", tags=["Sinistres"])

# Editable columns of dbo."Sinistre" and their kind ("text" columns carry their maximum length).
TEXT_COLUMNS = {
    "Num_Sinistre_Agence": 10, "Lieu_Sinistre": 50, "Branche_Sinistre": 20, "Code_Client": 8, "Num_Int": 15, "Num_Imat": 12,
    "Risque": 50, "Num_Sinistre_Compagnie_Assure": 20, "Num_Sinistre_Compagnie_Advers1": 20, "Tiers1": 50,
    "Num_Sinistre_Compagnie_Advers2": 20, "Tiers2": 50, "Type_Sinistre": 50, "Garantie": 50, "Type_Degats": 50, "Degats": 50,
    "Agence": 50, "Observation": 200, "reference": 50, "code_garage": 50, "Num_Int1": 15, "Num_Imat1": 12, "Num_Int2": 15,
    "Num_Imat2": 12, "code_branche": 3, "PC": 1, "classer": 1, "code_assure": 50, "nom_assure": 200, "nom_client": 255, "Code_Agence": 4,
}
INT_COLUMNS = {
    "Code_Type_Branche", "Code_Compagnie_Assure", "Code_Compagnie_Advers1", "Code_Compagnie_Advers2", "Code_Responsabilite",
    "Code_Expert1", "Code_Expert2", "Code_Expert3", "idfamille",
}
BOOL_COLUMNS = {"IDA", "FTUSA", "Regle", "CXP", "Cloture", "Prescription"}
DATE_COLUMNS = {"Date_Saisie", "Date_Sinistre", "Date_FTUSA", "Date_Reglement", "Date_CXP", "Date_Cloture", "Date_Prescription"}
FLOAT_COLUMNS = {
    "Montant_Devis", "Montant_Facture", "Montant_Reglement", "Montant_Rapport", "mont_cie", "solde", "MTVA_Devis", "MTVA_Facture", "MTVA_Rapport",
}
EDITABLE = set(TEXT_COLUMNS) | INT_COLUMNS | BOOL_COLUMNS | DATE_COLUMNS | FLOAT_COLUMNS

# The client name and the risk, when the claim does not hold them, come from the client file and the policy.
LIST_COLUMNS = (
    '"Num_Sinistre_Agence", "Date_Saisie", "Date_Sinistre", "Lieu_Sinistre", TRIM("Code_Client") AS "Code_Client", '
    'COALESCE(NULLIF(TRIM(nom_client), \'\'), (SELECT TRIM(c.nom_client) FROM dbo."TClient" c '
    'WHERE TRIM(c.code_client) = TRIM(dbo."Sinistre"."Code_Client") LIMIT 1)) AS nom_client, '
    'COALESCE(NULLIF(TRIM("Risque"), \'\'), (SELECT TRIM(p.risque) FROM dbo."TPolice" p '
    'WHERE TRIM(p.num_int) = TRIM(dbo."Sinistre"."Num_Int") ORDER BY p.effet DESC NULLS LAST LIMIT 1)) AS "Risque", '
    '"Num_Int", "Num_Imat", "Type_Sinistre", "Garantie", "Tiers1", "Code_Compagnie_Assure", "Regle", "Cloture", "Montant_Reglement", '
    '"Montant_Devis"'
)

# Values the legacy software offers for these free-text columns (completed with what the table already holds).
GARANTIES = ["(Aucun)", "RC", "TR", "COLL", "Dom.Véhicule", "BG", "Vol", "Incendie", "AUTRE"]
TYPES_DEGATS = ["(Aucun)", "Matériel", "Corporel", "Matériel et corporel"]

# Branch name keywords -> TType_Branche code.
BRANCH_TYPES = [
    ("MALADIE", 12), ("EAUX", 11), ("BRIS", 9), ("ELEC", 8), ("HABITATION", 6), ("MULTIRISQUE", 6), ("ASSISTANCE", 5),
    ("VIE", 4), ("R.C", 3), ("RESPONSABILITE", 3), ("TRANSPORT", 2), ("INCENDIE", 10), ("VOL", 7), ("AUTO", 1),
]


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        elif isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFD", value or "").encode("ascii", "ignore").decode().upper()


def _clean_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Form values -> column values, with a French message for anything invalid."""
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        if value == "" or value is None:
            clean[key] = None
            continue
        try:
            if key in TEXT_COLUMNS:
                clean[key] = str(value).strip()[: TEXT_COLUMNS[key]] or None
            elif key in INT_COLUMNS:
                clean[key] = int(value)
            elif key in FLOAT_COLUMNS:
                clean[key] = float(str(value).replace(",", "."))
            elif key in BOOL_COLUMNS:
                clean[key] = value in (True, 1, "1", "true", "O", "on")
            elif key in DATE_COLUMNS:
                clean[key] = datetime.fromisoformat(str(value)[:19])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Valeur invalide pour {key} : {value}") from exc
    return clean


def _next_number(db: Session, claim_date: date | None) -> str:
    prefix = (claim_date or date.today()).strftime("%Y%m")
    last = db.execute(
        text('SELECT MAX("Num_Sinistre_Agence") FROM dbo."Sinistre" WHERE "Num_Sinistre_Agence" LIKE :p AND length(TRIM("Num_Sinistre_Agence")) = 9'),
        {"p": f"{prefix}%"},
    ).scalar()
    sequence = int(str(last).strip()[6:]) + 1 if last and str(last).strip()[6:].isdigit() else 1
    return f"{prefix}{sequence:03d}"


def _branch_type(db: Session, code_compagnie: Any, code_branche: Any) -> int | None:
    if not code_compagnie or not code_branche:
        return None
    row = db.execute(
        text('SELECT nom_branche, "Automobile", "Nature_Tarif" FROM dbo."TBranche" WHERE code_compagnie = :c AND TRIM(code_branche) = :b'),
        {"c": code_compagnie, "b": str(code_branche).strip()},
    ).first()
    if row is None:
        return None
    upper = _ascii(row[0] or "")
    found = next((code for keyword, code in BRANCH_TYPES if keyword in upper), None)
    # Branches flagged as motor (or with the motor tariff) are "Auto" whatever their name.
    return found or (1 if (row[1] or "").strip() == "O" or (row[2] or "").strip() == "A" else None)


def _find_policy(db: Session, numero: str | None = None, immat: str | None = None) -> dict[str, Any] | None:
    """Agency policy by internal / policy number, else by registration plate."""
    select_policy = (
        'SELECT TRIM(p.num_int) AS num_int, TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client, c.nom_client, '
        'p.code_compagnie, TRIM(p.code_branche) AS code_branche, TRIM(p.num_imat) AS num_imat, TRIM(p.risque) AS risque, '
        "UPPER(TRIM(p.etat)) AS etat "
        'FROM dbo."TPolice" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
    )
    row = None
    digits = re.sub(r"\D", "", numero or "")
    if len(digits) >= 5:
        row = db.execute(
            text(select_policy + "WHERE TRIM(p.num_int) = :n OR TRIM(p.num_police) = :n ORDER BY p.effet DESC NULLS LAST LIMIT 1"), {"n": digits}
        ).mappings().first()
    plate = re.sub(r"\s", "", _ascii((immat or "").replace("تونس", "TU")))
    if row is None and len(plate) >= 4:
        row = db.execute(
            text(select_policy + "WHERE REPLACE(UPPER(TRIM(p.num_imat)), ' ', '') = :i ORDER BY (UPPER(TRIM(p.etat)) = 'AN') DESC, p.effet DESC NULLS LAST LIMIT 1"),
            {"i": plate},
        ).mappings().first()
    if row is None:
        return None
    policy = _plain(row)
    policy["code_type_branche"] = _branch_type(db, policy["code_compagnie"], policy["code_branche"])
    return policy


def _company_code(db: Session, name: str | None) -> int | None:
    wanted = _ascii(name or "").strip()
    if len(wanted) < 2:
        return None
    companies = [(row[0], _ascii(row[1] or "").strip()) for row in db.execute(text('SELECT code_compagnie, nom_compagnie FROM dbo."TCompagnie"')).all()]
    for code, company in sorted(companies, key=lambda item: -len(item[1])):
        if company and (re.search(rf"\b{re.escape(company)}\b", wanted) or wanted in company):
            return code
    return None


# ---------------------------------------------------------------------------------------------
# Lookups, list, CRUD
# ---------------------------------------------------------------------------------------------
@router.get("/lookups")
def lookups(db: Session = Depends(get_db)) -> dict[str, Any]:
    def rows(sql: str) -> list[Any]:
        return [tuple(row) for row in db.execute(text(sql)).all()]

    used_garanties = [row[0] for row in rows('SELECT DISTINCT TRIM("Garantie") FROM dbo."Sinistre" WHERE TRIM("Garantie") <> \'\'')]
    used_degats = [row[0] for row in rows('SELECT DISTINCT TRIM("Type_Degats") FROM dbo."Sinistre" WHERE TRIM("Type_Degats") <> \'\'')]
    merge = lambda base, extra: base + sorted({value for value in extra if value and value.lower() not in {item.lower() for item in base}})  # noqa: E731
    return {
        "responsabilites": [{"id": r[0], "label": r[1].strip()} for r in rows('SELECT "Code_Responsabilite", "Libelle_Responsabilite" FROM dbo."TResponsabilite" ORDER BY 1')],
        "types_sinistre": [{"id": r[0].strip(), "label": r[0].strip(), "recours": r[1] == "O"} for r in rows('SELECT "TypeSin", "Reco" FROM dbo."TType_Sinistre"')],
        "branches": [{"id": r[0], "label": r[1].strip()} for r in rows('SELECT "Code_Type_Branche", "Libelle_Type_Branche" FROM dbo."TType_Branche" ORDER BY 1')],
        "compagnies": [{"id": r[0], "label": r[1].strip()} for r in rows('SELECT code_compagnie, nom_compagnie FROM dbo."TCompagnie" ORDER BY nom_compagnie')],
        "experts": [{"id": r[0], "label": r[1].strip()} for r in rows('SELECT "Code_Prestataire", "Nom_Prestataire" FROM dbo."TPrestataire" WHERE "Code_Famille" = 1 ORDER BY "Nom_Prestataire"')],
        "garages": [{"id": r[0].strip(), "label": r[1].strip()} for r in rows('SELECT "Code_Garage", "Nom_Garage" FROM dbo."TGarage" ORDER BY "Nom_Garage"')],
        "garanties": merge(GARANTIES, used_garanties),
        "types_degats": merge(TYPES_DEGATS, used_degats),
    }


@router.get("")
def list_sinistres(
    q: str = Query(default="", max_length=60),
    code_client: str = Query(default="", max_length=8),
    statut: str = Query(default="", pattern="^(|en_cours|regle|cloture)$"),
    non_regle: bool = False,
    non_cloture: bool = False,
    date_max: date | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Claims, most recent first; `q` searches the number, client, policy, plate and third party,
    `statut` keeps the open files (en_cours), the settled ones (regle) or the closed ones (cloture),
    `non_regle` / `non_cloture` keep the files whose "Regle" / "Cloture" column is false (both: both false), and `date_max`
    the claims that happened on or before that day. `counts` gives, for the same search and date,
    how many files are not settled and not closed."""
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if statut == "en_cours":
        conditions.append('COALESCE("Cloture", FALSE) = FALSE AND COALESCE("Regle", FALSE) = FALSE')
    elif statut == "regle":
        conditions.append('COALESCE("Regle", FALSE) = TRUE AND COALESCE("Cloture", FALSE) = FALSE')
    elif statut == "cloture":
        conditions.append('COALESCE("Cloture", FALSE) = TRUE')
    if code_client.strip():
        conditions.append('TRIM("Code_Client") = :code')
        params["code"] = code_client.strip()
    if q.strip():
        conditions.append(
            '("Num_Sinistre_Agence" ILIKE :q OR TRIM("Code_Client") = :exact OR nom_client ILIKE :q OR "Num_Int" ILIKE :q '
            'OR REPLACE("Num_Imat", \' \', \'\') ILIKE :plate OR "Tiers1" ILIKE :q OR "Num_Sinistre_Compagnie_Assure" ILIKE :q)'
        )
        params.update({"q": f"%{q.strip()}%", "exact": q.strip(), "plate": f"%{re.sub(r'[ ]', '', q.strip())}%"})
    if date_max is not None:
        conditions.append('"Date_Sinistre" < :date_after')
        params["date_after"] = date.fromordinal(date_max.toordinal() + 1)  # the whole day, whatever the time
    # The counts follow the search and the date, not the status filters.
    counted = " AND ".join(conditions)
    if non_regle:
        conditions.append('"Regle" = FALSE')
    if non_cloture:
        conditions.append('"Cloture" = FALSE')
    where = " AND ".join(conditions)
    try:
        counts = db.execute(
            text(
                'SELECT COUNT(*) FILTER (WHERE "Regle" = FALSE) AS non_regle, '
                'COUNT(*) FILTER (WHERE "Cloture" = FALSE) AS non_cloture '
                f'FROM dbo."Sinistre" WHERE {counted}'
            ),
            params,
        ).mappings().first()
        total = db.execute(text(f'SELECT COUNT(*) FROM dbo."Sinistre" WHERE {where}'), params).scalar()
        rows = db.execute(
            text(f'SELECT {LIST_COLUMNS} FROM dbo."Sinistre" WHERE {where} ORDER BY "Date_Saisie" DESC NULLS LAST, "Num_Sinistre_Agence" DESC LIMIT :limit OFFSET :offset'),
            params,
        ).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Lecture des sinistres impossible.") from exc
    return {
        "items": [_plain(row) for row in rows], "total": total, "limit": limit, "offset": offset,
        "counts": {"non_regle": counts["non_regle"], "non_cloture": counts["non_cloture"]},
    }


@router.get("/next-number")
def next_number(date_sinistre: date | None = None, db: Session = Depends(get_db)) -> dict[str, str]:
    return {"numero": _next_number(db, date_sinistre)}


@router.get("/police")
def policy_for_claim(numero: str = Query(default="", max_length=20), immat: str = Query(default="", max_length=20), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The insured's policy (by number or plate): client, company, branch and risk to fill the claim."""
    policy = _find_policy(db, numero, immat)
    if policy is None:
        raise HTTPException(status_code=404, detail="Aucune police trouvée pour ce numéro / cette immatriculation.")
    return policy


@router.get("/{numero}")
def get_sinistre(numero: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.execute(text('SELECT * FROM dbo."Sinistre" WHERE TRIM("Num_Sinistre_Agence") = :n'), {"n": numero.strip()}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Sinistre introuvable.")
    return _plain(row)


@router.post("", status_code=201)
def create_sinistre(payload: dict[str, Any], user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    values = _clean_payload(payload)
    if not values.get("Date_Sinistre"):
        raise HTTPException(status_code=422, detail="La date du sinistre est obligatoire.")
    now = datetime.now().replace(microsecond=0)
    login = str(user.get("sub", ""))[:20]
    values.setdefault("Date_Saisie", now)
    values["Date_Saisie"] = values["Date_Saisie"] or now
    values.update({"Date_Systeme": now, "Create_User": login, "Create_Date": now})
    chosen = values.get("Num_Sinistre_Agence")
    for _attempt in range(3):
        values["Num_Sinistre_Agence"] = chosen or _next_number(db, values["Date_Sinistre"].date())
        columns = ", ".join(f'"{key}"' for key in values)
        try:
            db.execute(text(f'INSERT INTO dbo."Sinistre" ({columns}) VALUES ({", ".join(f":{key}" for key in values)})'), values)
            db.commit()
            return {"numero": values["Num_Sinistre_Agence"]}
        except IntegrityError as exc:
            db.rollback()
            if chosen:
                raise HTTPException(status_code=409, detail=f"Le sinistre {chosen} existe déjà.") from exc
            # Two claims numbered at the same time: take the next number.
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail=f"Enregistrement impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    raise HTTPException(status_code=409, detail="Numérotation du sinistre impossible, réessayez.")


@router.put("/{numero}")
def update_sinistre(numero: str, payload: dict[str, Any], user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    values = _clean_payload(payload)
    values.pop("Num_Sinistre_Agence", None)
    if "Date_Sinistre" in values and not values["Date_Sinistre"]:
        raise HTTPException(status_code=422, detail="La date du sinistre est obligatoire.")
    values.update({"Update_User": str(user.get("sub", ""))[:20], "Update_Date": datetime.now().replace(microsecond=0)})
    assignments = ", ".join(f'"{key}" = :{key}' for key in values)
    try:
        result = db.execute(text(f'UPDATE dbo."Sinistre" SET {assignments} WHERE TRIM("Num_Sinistre_Agence") = :numero_key'), {**values, "numero_key": numero.strip()})
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Sinistre introuvable.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Modification impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"numero": numero.strip()}


@router.delete("/{numero}", status_code=204)
def delete_sinistre(numero: str, db: Session = Depends(get_db)) -> None:
    try:
        result = db.execute(text('DELETE FROM dbo."Sinistre" WHERE TRIM("Num_Sinistre_Agence") = :n'), {"n": numero.strip()})
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Sinistre introuvable.")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Suppression impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc


# ---------------------------------------------------------------------------------------------
# Reading a scanned constat amiable / déclaration de sinistre (JPG, PNG or PDF)
# ---------------------------------------------------------------------------------------------
_VEHICLE = {
    "nom_assure": "name of the insured (assuré / souscripteur)",
    "conducteur": "name of the driver",
    "compagnie": "insurance company name (e.g. GAT, STAR, COMAR, ASTREE, LLOYD, MAGHREBIA, CARTE, BIAT, AMI, BH)",
    "num_police": "policy / contract number, digits exactly as printed",
    "immatriculation": "registration plate exactly as printed (e.g. '209 TU 2222', 'RS 209848')",
    "marque": "make and model of the vehicle",
    "degats": "visible damage / point of impact (damaged parts), short",
}
SINISTRE_FIELDS: dict[str, str] = {
    "type_document": "'constat' for a constat amiable d'accident automobile, 'declaration' for a déclaration de sinistre, otherwise 'autre'.",
    "date_sinistre": "Date of the accident or loss, dd/mm/yyyy.",
    "heure_sinistre": "Time of the accident, hh:mm.",
    "lieu_sinistre": "Place of the accident (street, city).",
    "blesses": "'oui' if the document says people were injured (blessés), 'non' if it says no one was, otherwise null.",
    **{f"a_{key}": f"Vehicle A: {hint}." for key, hint in _VEHICLE.items()},
    **{f"b_{key}": f"Vehicle B: {hint}." for key, hint in _VEHICLE.items()},
    "num_sinistre_compagnie": "Claim number given by an insurance company, when printed.",
    "circonstances": "Short summary of the circumstances and observations, at most 200 characters.",
}
SINISTRE_PROMPT = (
    "This document is a scanned or photographed Tunisian insurance claim: usually a 'constat amiable d'accident automobile' "
    "(two vehicles A and B) or a 'déclaration de sinistre', in French and/or Arabic. Read it and fill the requested fields. "
    "Copy values exactly as printed; use null for anything not visible or not certain, never guess. "
    "A déclaration with a single insured goes in the vehicle A fields. Dates as dd/mm/yyyy."
)


def _extract_ai(request: FicheRequest) -> dict[str, Any]:
    if settings.anthropic_api_key:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        schema = {
            "type": "object",
            "properties": {key: {"type": ["string", "null"], "description": hint} for key, hint in SINISTRE_FIELDS.items()},
            "required": list(SINISTRE_FIELDS),
            "additionalProperties": False,
        }
        response = client.messages.create(
            model=settings.assistant_model,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "medium"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document" if request.media_type == "application/pdf" else "image",
                        "source": {"type": "base64", "media_type": request.media_type, "data": request.image_base64},
                    },
                    {"type": "text", "text": SINISTRE_PROMPT},
                ],
            }],
        )
        raw = next((block.text for block in response.content if block.type == "text"), "{}")
        return json.loads(raw)
    schema = {
        "type": "OBJECT",
        "properties": {key: {"type": "STRING", "nullable": True, "description": hint} for key, hint in SINISTRE_FIELDS.items()},
        "required": list(SINISTRE_FIELDS),
    }
    data = _gemini_generate({
        "contents": [{"parts": [{"text": SINISTRE_PROMPT}, {"inline_data": {"mime_type": request.media_type, "data": request.image_base64}}]}],
        "generationConfig": {"response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
    })
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return json.loads("".join(part.get("text", "") for part in parts))


PLATE_RE = re.compile(r"\b(\d{1,4})\s*(TU|RS|تونس)\s*(\d{1,4})\b|\bRS\s*(\d{4,6})\b", re.IGNORECASE)


def _parse_text(raw: str) -> dict[str, Any]:
    """Rule-based reading of OCR text when no AI service is available."""
    flat = re.sub(r"\s+", " ", raw)
    fields: dict[str, Any] = {key: None for key in SINISTRE_FIELDS}
    fields["type_document"] = "constat" if re.search(r"constat", flat, re.IGNORECASE) else "declaration" if re.search(r"d[ée]claration", flat, re.IGNORECASE) else "autre"
    match = re.search(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})\b", flat)
    if match:
        day, month, year = match.groups()
        fields["date_sinistre"] = f"{int(day):02d}/{int(month):02d}/{year if len(year) == 4 else '20' + year}"
    hour = re.search(r"\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)\b", flat)
    if hour:
        fields["heure_sinistre"] = f"{int(hour.group(1)):02d}:{hour.group(2)}"
    place = re.search(r"lieu[^:\n]{0,20}:[ \t]*([^\n]{3,})", raw, re.IGNORECASE)
    if place:
        fields["lieu_sinistre"] = re.split(r"\s{2,}|\t", place.group(1).strip())[0][:50]
    plates = [m.group(0).upper().replace("تونس", "TU") for m in PLATE_RE.finditer(flat)]
    policies = re.findall(r"(?:police|contrat)\s*n?[°o]?\s*[:.]?\s*(\d{6,14})", flat, re.IGNORECASE)
    for side, index in (("a", 0), ("b", 1)):
        if len(plates) > index:
            fields[f"{side}_immatriculation"] = re.sub(r"\s+", " ", plates[index])
        if len(policies) > index:
            fields[f"{side}_num_police"] = policies[index]
    fields["blesses"] = "oui" if re.search(r"bless[ée]s?\s*[:.]?\s*(oui|x)", flat, re.IGNORECASE) else None
    return fields


def _to_iso(value: str | None) -> str | None:
    match = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", value or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    year = year + 2000 if year < 100 else year
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _suggest(db: Session, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Extracted fields -> claim columns. The vehicle whose policy is ours is the insured; the other one is the third party."""
    value = lambda key: (fields.get(key) or "").strip() if isinstance(fields.get(key), str) else fields.get(key)  # noqa: E731
    policies = {side: _find_policy(db, value(f"{side}_num_police"), value(f"{side}_immatriculation")) for side in ("a", "b")}
    ours = "b" if policies["b"] and not policies["a"] else "a"
    other = "b" if ours == "a" else "a"
    policy = policies[ours]

    claim_date = _to_iso(value("date_sinistre"))
    hour = value("heure_sinistre")
    suggestion: dict[str, Any] = {
        "Date_Sinistre": f"{claim_date}T{hour}:00" if claim_date and hour and re.fullmatch(r"\d{2}:\d{2}", hour) else claim_date,
        "Lieu_Sinistre": (value("lieu_sinistre") or "")[:50] or None,
        "Num_Int": (re.sub(r"\D", "", value(f"{ours}_num_police") or "") or None),
        "Num_Imat": (value(f"{ours}_immatriculation") or "")[:12] or None,
        "nom_assure": value(f"{ours}_nom_assure") or value(f"{ours}_conducteur"),
        "Degats": (value(f"{ours}_degats") or "")[:50] or None,
        "Code_Compagnie_Assure": _company_code(db, value(f"{ours}_compagnie")),
        "Tiers1": (value(f"{other}_nom_assure") or value(f"{other}_conducteur") or "")[:50] or None,
        "Code_Compagnie_Advers1": _company_code(db, value(f"{other}_compagnie")),
        "Num_Int1": (re.sub(r"\D", "", value(f"{other}_num_police") or "") or None),
        "Num_Imat1": (value(f"{other}_immatriculation") or "")[:12] or None,
        "Num_Sinistre_Compagnie_Assure": (value("num_sinistre_compagnie") or "")[:20] or None,
        "Type_Degats": "Corporel" if (value("blesses") or "").lower() == "oui" else "Matériel" if value("type_document") == "constat" else None,
        "Observation": (value("circonstances") or "")[:200] or None,
    }
    if policy:
        suggestion.update({
            "Num_Int": policy["num_int"], "Num_Imat": suggestion["Num_Imat"] or (policy["num_imat"] or None), "Code_Client": policy["code_client"],
            "nom_client": policy["nom_client"], "Code_Compagnie_Assure": policy["code_compagnie"], "code_branche": policy["code_branche"],
            "Code_Type_Branche": policy["code_type_branche"], "Risque": (policy["risque"] or "")[:50] or None,
        })
    return {key: item for key, item in suggestion.items() if item not in (None, "")}, policy, ours.upper()


@router.post("/extract")
def extract_sinistre(request: FicheRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reads a constat / déclaration (image or PDF) and proposes the claim's values; the user checks them before saving."""
    source = "ai"
    fields: dict[str, Any] | None = None
    if settings.anthropic_api_key or settings.gemini_api_key:
        try:
            fields = _extract_ai(request)
        except (HTTPException, anthropic.APIError, ValueError):
            fields = None  # the AI service is unavailable: fall back on OCR
    if fields is None:
        source = "ocr"
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
        fields = _parse_text("\n".join(texts))
    sinistre, policy, side = _suggest(db, fields)
    return {"source": source, "fields": fields, "sinistre": sinistre, "police": policy, "vehicule_assure": side}
