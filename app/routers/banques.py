"""Gestion des banques: the agency's own bank accounts (dbo."TBanque").

Type_Dep tells what an account is for: 'T' technique (collections for a company, Code_Compagnie),
'D' frais généraux of W. JEMMALI, 'F' frais généraux of M. JEMMALI.
"""
import json
import re
from datetime import date, datetime
from typing import Any, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.text_fields import label_values
from app.routers.assistant import FicheRequest, _gemini_generate, _ocr_space

router = APIRouter(prefix="/api/banques", tags=["Gestion des banques"], dependencies=[Depends(require_current_user)])

COLUMNS = (
    'TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque, TRIM(num_compte_b) AS num_compte_b, '
    'debit_ini, credit_ini, date_ini, debit, credit, date_maj, code_devise, "Code_Compagnie" AS code_compagnie, '
    '"Type_Dep" AS type_dep, COALESCE("Archive", FALSE) AS archive'
)
# Tables whose rows point to an account: such an account is archived, not deleted.
USED_IN = (("TRegCli", "règlements clients"), ("TDepense", "dépenses"))


class BanqueIn(BaseModel):
    code_banque: str = Field(min_length=1, max_length=50)
    nom_banque: str = Field(min_length=1, max_length=50)
    num_compte_b: str | None = Field(default=None, max_length=20)
    credit_ini: float = 0
    date_ini: date | None = None
    type_dep: Literal["T", "D", "F"] = "T"
    code_compagnie: int | None = None
    archive: bool = False


def _plain(row: Any) -> dict[str, Any]:
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in dict(row).items()}


def _get(db: Session, code: str) -> dict[str, Any] | None:
    row = db.execute(text(f'SELECT {COLUMNS} FROM dbo."TBanque" WHERE TRIM(code_banque) = :c'), {"c": code.strip()}).mappings().first()
    return _plain(row) if row else None


def _values(payload: BanqueIn, db: Session) -> dict[str, Any]:
    company = payload.code_compagnie if payload.type_dep == "T" else 0
    if payload.type_dep == "T":
        if not company:
            raise HTTPException(status_code=422, detail="Un compte technique doit être rattaché à une compagnie.")
        if not db.execute(text('SELECT 1 FROM dbo."TCompagnie" WHERE code_compagnie = :c'), {"c": company}).first():
            raise HTTPException(status_code=422, detail=f"La compagnie {company} n’existe pas.")
    return {
        "nom": payload.nom_banque.strip().upper(),
        "num": (payload.num_compte_b or "").strip() or None,
        "credit_ini": round(payload.credit_ini or 0, 3),
        "date_ini": payload.date_ini or date.today(),
        "type_dep": payload.type_dep,
        "company": company or 0,
        "archive": payload.archive,
    }


@router.get("")
def list_banques(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(text(f'SELECT {COLUMNS} FROM dbo."TBanque" ORDER BY COALESCE("Archive", FALSE), code_banque')).mappings().all()
    return [_plain(row) for row in rows]


@router.post("", status_code=201)
def create_banque(payload: BanqueIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    code = payload.code_banque.strip().upper()
    if _get(db, code):
        raise HTTPException(status_code=409, detail=f"Le code banque {code} existe déjà.")
    values = _values(payload, db)
    try:
        db.execute(
            text(
                'INSERT INTO dbo."TBanque" (code_banque, nom_banque, num_compte_b, debit_ini, credit_ini, date_ini, debit, credit, date_maj, '
                'code_devise, "Code_Compagnie", "Type_Dep", "Archive") '
                'VALUES (:code, :nom, :num, 0, :credit_ini, :date_ini, 0, 0, :date_ini, 0, :company, :type_dep, :archive)'
            ),
            {"code": code, **values},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Ajout de la banque impossible.") from exc
    return _get(db, code)


@router.put("/{code}")
def update_banque(code: str, payload: BanqueIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The code is the key used by règlements and dépenses: it cannot be changed. Débit / crédit en cours are
    maintained by the operations and are not edited here."""
    if not _get(db, code):
        raise HTTPException(status_code=404, detail="Banque introuvable.")
    values = _values(payload, db)
    try:
        db.execute(
            text(
                'UPDATE dbo."TBanque" SET nom_banque = :nom, num_compte_b = :num, credit_ini = :credit_ini, date_ini = :date_ini, '
                '"Code_Compagnie" = :company, "Type_Dep" = :type_dep, "Archive" = :archive WHERE TRIM(code_banque) = :code'
            ),
            {"code": code.strip(), **values},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Modification de la banque impossible.") from exc
    return _get(db, code)


@router.delete("/{code}", status_code=204)
def delete_banque(code: str, db: Session = Depends(get_db)) -> Response:
    if not _get(db, code):
        raise HTTPException(status_code=404, detail="Banque introuvable.")
    for table, label in USED_IN:
        used = db.execute(text(f'SELECT COUNT(*) FROM dbo."{table}" WHERE TRIM(code_banque) = :c'), {"c": code.strip()}).scalar()
        if used:
            raise HTTPException(status_code=409, detail=f"Ce compte est utilisé par {used} {label} : archivez-le au lieu de le supprimer.")
    db.execute(text('DELETE FROM dbo."TBanque" WHERE TRIM(code_banque) = :c'), {"c": code.strip()})
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------------------------
# Reading a scanned RIB / attestation de RIB / relevé bancaire (JPG, PNG or PDF)
# ---------------------------------------------------------------------------------------------
BANK_FIELDS = {
    "banque": "Name of the bank (e.g. ATTIJARI BANK, ATB, BIAT, STB, BNA, BH BANK, UIB, UBCI, AMEN BANK, ZITOUNA).",
    "rib": "The 20-digit RIB (relevé d'identité bancaire), digits only, without spaces.",
    "numero_compte": "Account number as printed (the 'N° de compte' / 'compte'), without the bank and agency codes when they are printed apart.",
    "titulaire": "Name of the account holder.",
    "agence": "Name of the bank branch (agence).",
    "solde": "Balance shown on a statement, as a number with a dot for decimals (3 decimals in dinars), negative when debit.",
    "date": "Date of the document or of the balance, dd/mm/yyyy.",
}
BANK_PROMPT = (
    "This is a photographed or scanned Tunisian bank document (RIB, attestation de RIB, relevé de compte, chéquier), in French "
    "and/or Arabic. Read it and fill the requested fields. Copy values exactly as printed; use null for anything not visible or "
    "not certain, never guess."
)
# First two digits of a Tunisian RIB: the bank.
RIB_BANKS = {
    "01": "ATB", "03": "BNA", "04": "ATTIJARI", "05": "BT", "07": "AMEN BANK", "08": "BIAT", "10": "STB",
    "11": "UBCI", "12": "UIB", "14": "BH", "17": "LA POSTE", "25": "ZITOUNA",
}


def _extract_ai(request: FicheRequest) -> dict[str, Any]:
    if settings.anthropic_api_key:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        schema = {
            "type": "object",
            "properties": {key: {"type": ["string", "null"], "description": hint} for key, hint in BANK_FIELDS.items()},
            "required": list(BANK_FIELDS),
            "additionalProperties": False,
        }
        response = client.messages.create(
            model=settings.assistant_model,
            max_tokens=2000,
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "medium"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document" if request.media_type == "application/pdf" else "image",
                        "source": {"type": "base64", "media_type": request.media_type, "data": request.image_base64},
                    },
                    {"type": "text", "text": BANK_PROMPT},
                ],
            }],
        )
        raw = next((block.text for block in response.content if block.type == "text"), "{}")
        return json.loads(raw)
    schema = {
        "type": "OBJECT",
        "properties": {key: {"type": "STRING", "nullable": True, "description": hint} for key, hint in BANK_FIELDS.items()},
        "required": list(BANK_FIELDS),
    }
    data = _gemini_generate({
        "contents": [{"parts": [{"text": BANK_PROMPT}, {"inline_data": {"mime_type": request.media_type, "data": request.image_base64}}]}],
        "generationConfig": {"response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
    })
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return json.loads("".join(part.get("text", "") for part in parts))


def _parse_text(raw: str) -> dict[str, Any]:
    """Rule-based reading of OCR text when no AI service is available."""
    flat = re.sub(r"[ \t]+", " ", raw)
    fields: dict[str, Any] = {key: None for key in BANK_FIELDS}
    rib = re.search(r"(?<!\d)(\d{2}[ .-]?\d{3}[ .-]?\d{13}[ .-]?\d{2})(?!\d)", flat) or re.search(r"(?<!\d)((?:\d[ .-]?){20})(?!\d)", flat)
    if rib:
        fields["rib"] = re.sub(r"\D", "", rib.group(1))[:20]
    account = re.search(r"(?:n[°o]\s*(?:de\s*)?compte|compte\s*n[°o]?)\s*[:.]?\s*([\d][\d ./-]{5,24}\d)", flat, re.IGNORECASE)
    if account:
        fields["numero_compte"] = account.group(1).strip()
    holder = re.search(r"(?:titulaire|intitul[ée](?:\s+du\s+compte)?|nom)\s*[:.]\s*([^\n]{3,60})", flat, re.IGNORECASE)
    if holder:
        fields["titulaire"] = holder.group(1).strip()
    lowered = flat.lower()
    names = {"attijari": "ATTIJARI", "biat": "BIAT", "amen": "AMEN BANK", "zitouna": "ZITOUNA", "ubci": "UBCI", "uib": "UIB",
             "stb": "STB", "bna": "BNA", "bh bank": "BH", "atb": "ATB", "arab tunisian": "ATB", "banque de l'habitat": "BH"}
    fields["banque"] = next((name for word, name in names.items() if re.search(rf"\b{re.escape(word)}\b", lowered)), None)
    date_match = re.search(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})\b", flat)
    if date_match:
        day, month, year = date_match.groups()
        fields["date"] = f"{int(day):02d}/{int(month):02d}/{year}"
    return fields


@router.post("/extract")
def extract_banque(request: FicheRequest) -> dict[str, Any]:
    """Reads a RIB / bank statement and proposes the account's values; the user checks them before saving."""
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
    return {"source": source, "fields": _finish(fields)}


def _finish(fields: dict[str, Any]) -> dict[str, Any]:
    """The values read, cleaned: the RIB as its 20 digits, the bank deduced from the RIB when not printed."""
    fields = {key: (str(fields.get(key)).strip() if fields.get(key) not in (None, "") else None) for key in BANK_FIELDS}
    rib = re.sub(r"\D", "", fields["rib"] or "")
    fields["rib"] = rib if len(rib) == 20 else (fields["rib"] or None)
    if not fields["banque"] and len(rib) == 20:
        fields["banque"] = RIB_BANKS.get(rib[:2])
    return fields


class TextIn(BaseModel):
    text: str = Field(min_length=10, max_length=300_000)


@router.post("/extract-text")
def extract_banque_from_text(payload: TextIn) -> dict[str, Any]:
    """The same reading from the text of the RIB / relevé recognised by OCR in the browser (no AI)."""
    return {"source": "ocr", "fields": _finish(_parse_text(payload.text)), "infos": label_values(payload.text)}
