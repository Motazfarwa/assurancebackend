"""Mémoire de règlement of a client (Etat des impayés, one client searched by code), as a PDF, and sent to the client:

- by email, the PDF attached (Brevo API or SMTP, the settings of the "Journal du jour");
- by WhatsApp: through the WhatsApp Business Cloud API when WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are set (the PDF is
  sent as a document with the message); otherwise a wa.me link with the message is returned and the PDF is downloaded by
  the browser, to attach in WhatsApp.

The quittances are read again from the database with the criteria of the statement shown (the same as /etat), so the PDF
is what the screen shows. The client's addresses offered are only those stored on its file (TClient)."""
import base64
import logging
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.pdf_simple import PAGE_H, PAGE_W, Pdf, fit, text_width as pdf_width
from app.routers.impayes import unpaid_statement

router = APIRouter(prefix="/api/impayes-agence/memoire", tags=["Mémoire de règlement"], dependencies=[Depends(require_current_user)])
logger = logging.getLogger(__name__)

# The agency printed at the top of the reports (as src/data/agencyInfo.js of the frontend).
AGENCY = ["Cabinet d’Assurance Jemmali", "4, Rue Ohod - Mutuelleville", "1082 - Tunis", "Tél : 71 893 000", "Cell : 93 893 000"]
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _money(value: Any) -> str:
    return f"{float(value or 0):,.3f}".replace(",", " ").replace(".", ",")


def _date(value: Any) -> str:
    text_value = str(value or "")[:10]
    return "/".join(reversed(text_value.split("-"))) if text_value else ""


def _term(row: dict[str, Any]) -> str:
    tc, periode = (row.get("tc") or "").strip(), (row.get("periode") or "").strip()
    return f"T{periode}" if tc == "T" and periode else tc


def _rows(db: Session, code: str, piece: str, date_debut: date | None, date_fin: date | None, code_compagnie: int | None) -> list[dict[str, Any]]:
    body = unpaid_statement(code_client=code, nom_client="", piece=piece, date_debut=date_debut, date_fin=date_fin, type_client="",
                            code_compagnie=code_compagnie, mise_en_demeure=False, limit=5000, db=db)
    return body["items"]


def _client(db: Session, code: str) -> dict[str, Any]:
    row = db.execute(text(f'SELECT * FROM "{settings.db_schema}"."TClient" WHERE TRIM(code_client) = :c LIMIT 1'), {"c": code}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Client {code} introuvable.")
    return dict(row)


def _values(client: dict[str, Any], json_keys: tuple[str, ...], columns: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for key in json_keys:
        for item in client.get(key) or []:
            if isinstance(item, dict):
                out.append(str(item.get("value") or "").strip())
    out += [str(client.get(column) or "").strip() for column in columns]
    return out


def contacts_of(client: dict[str, Any]) -> dict[str, list[str]]:
    """The emails and phone numbers stored on the client file (its contacts, then the legacy columns); placeholders
    ("neant", ".") and invalid values are left out, nothing is made up."""
    emails, phones = [], []
    for value in _values(client, ("emails_domicile", "emails_bureau"), ("adresse_email", "email1", "email2", "email3", "emailb2", "emailb3")):
        if EMAIL_RE.match(value) and value.lower() not in (e.lower() for e in emails):
            emails.append(value)
    for value in _values(client, ("telephones_domicile", "telephones_bureau"), ("mobile", "tel", "tel1", "tel2", "tel3", "tel_b", "tel_b2", "tel_b3", "tel_b4")):
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 8 and digits not in (re.sub(r"\D", "", p) for p in phones):
            phones.append(value)
    return {"emails": emails, "telephones": phones}


def memoire_pdf(client: dict[str, Any], rows: list[dict[str, Any]]) -> bytes:
    """The Mémoire de règlement as on screen: the agency, the client, its unpaid quittances by compagnie, the totals."""
    pdf = Pdf()
    left, right = 40, PAGE_W - 40
    # columns: N° Police, TC, Échéance, Prime, Solde, Risque, Observation (x, width, alignment)
    cols = [("N° Police", 40, 78, "left"), ("TC", 118, 42, "left"), ("Échéance", 160, 54, "left"), ("Prime", 214, 62, "right"),
            ("Solde", 276, 62, "right"), ("Risque", 344, 110, "left"), ("Observation", 454, 101, "left")]

    def header() -> float:
        for index, line in enumerate(AGENCY):
            pdf.text(left, 50 + index * 12, line, 9, color=(0.12, 0.29, 0.53))
        today = date.today().strftime("%d/%m/%Y")
        pdf.text(right, 50, today, 9, bold=True, align="right")
        pdf.text(right - pdf_width(today, 9, True) - 3, 50, "Date :", 9, align="right")
        pdf.box(PAGE_W / 2 - 115, 118, 230, 30, stroke=0.2)
        pdf.box(PAGE_W / 2 - 112, 121, 224, 24, stroke=0.2)
        pdf.text(PAGE_W / 2, 137, "MÉMOIRE DE RÈGLEMENT", 13, bold=True, align="center")
        return 170

    def table_head(y: float) -> float:
        pdf.box(left, y, right - left, 16, fill=(0.94, 0.96, 0.97), stroke=0.75)
        for label, x, width, align in cols:
            pdf.text(x + width - 4 if align == "right" else x + 4, y + 11, label, 8, bold=True, align=align)
        return y + 16

    y = header()
    name = str(client.get("nom_client") or "").strip()
    for label, value in (("Client :", f"{str(client.get('code_client') or '').strip()}  {name}"), ("Adresse :", client.get("adresse")),
                         ("Ville :", client.get("ville")), ("Code postal :", client.get("cp"))):
        pdf.text(left + 12, y, label, 9, bold=True)
        pdf.text(left + 110, y, fit(str(value if value is not None else "—").strip() or "—", 380, 9), 9)
        y += 14
    y += 8
    companies: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        companies.setdefault(row.get("nom_compagnie") or "Sans compagnie", []).append(row)
    for company, items in companies.items():
        if y > PAGE_H - 120:
            pdf.new_page()
            y = 50
        pdf.text(left + 12, y + 4, f"Compagnie : {company}", 10, bold=True)
        y = table_head(y + 12)
        for row in items:
            if y > PAGE_H - 60:
                pdf.new_page()
                y = table_head(50)
            values = [row.get("num_int") or "", _term(row), _date(row.get("echeance")), _money(row.get("montant_quittance")),
                      _money(row.get("solde")), row.get("risque") or "", row.get("observation") or ""]
            for (label, x, width, align), value in zip(cols, values):
                shown = fit(str(value), width - 8, 8)
                pdf.text(x + width - 4 if align == "right" else x + 4, y + 11, shown, 8, align=align)
            pdf.line(left, y + 16, right, y + 16, gray=0.85)
            y += 16
        pdf.box(left, y, right - left, 16, fill=(0.97, 0.98, 0.98), stroke=0.75)
        pdf.text(left + 4, y + 11, "Total compagnie", 8, bold=True)
        pdf.text(338 - 4, y + 11, _money(sum(float(r.get("solde") or 0) for r in items)), 8, bold=True, align="right")
        y += 30
    if y > PAGE_H - 50:
        pdf.new_page()
        y = 50
    pdf.text(right, y, f"Total client  {_money(sum(float(r.get('solde') or 0) for r in rows))} TND", 11, bold=True, align="right")
    return pdf.output()


def _file_name(code: str) -> str:
    return f"memoire_reglement_{code}_{date.today():%Y%m%d}.pdf"


@router.get("/{code_client}/contacts")
def client_contacts(code_client: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    client = _client(db, code_client.strip())
    return {"code_client": str(client.get("code_client") or "").strip(), "nom_client": str(client.get("nom_client") or "").strip(), **contacts_of(client)}


@router.get("/{code_client}/pdf")
def memoire_download(
    code_client: str,
    piece: str = Query(default="avec", pattern="^(avec|sans)$"),
    date_debut: date | None = None,
    date_fin: date | None = None,
    code_compagnie: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    code = code_client.strip()
    client = _client(db, code)
    rows = _rows(db, code, piece, date_debut, date_fin, code_compagnie)
    return Response(memoire_pdf(client, rows), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_file_name(code)}"'})


class Envoi(BaseModel):
    canal: str = Field(pattern="^(email|whatsapp)$")
    destinataire: str = Field(min_length=3, max_length=120)
    sujet: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=4000)
    piece: str = Field(default="avec", pattern="^(avec|sans)$")
    date_debut: date | None = None
    date_fin: date | None = None
    code_compagnie: int | None = None

    # The criteria of the statement come as they are on screen: an empty one ("") is no criterion.
    @field_validator("date_debut", "date_fin", "code_compagnie", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


def _send_email(to: str, subject: str, body: str, pdf: bytes, name: str) -> None:
    """The message with the PDF attached (raises on failure): the Brevo API when its key is set, else SMTP."""
    sender = settings.smtp_from or settings.smtp_user or to
    if settings.brevo_api_key:
        answer = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=30, headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
                            json={"sender": {"name": AGENCY[0], "email": sender}, "to": [{"email": to}], "subject": subject, "textContent": body,
                                  "attachment": [{"content": base64.b64encode(pdf).decode(), "name": name}]})
        if answer.status_code >= 300:
            raise RuntimeError(f"Brevo {answer.status_code} : {answer.text[:200]}")
        return
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        raise RuntimeError("L’envoi d’email n’est pas configuré sur le serveur (BREVO_API_KEY ou SMTP_HOST / SMTP_USER / SMTP_PASSWORD).")
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, f"{AGENCY[0]} <{sender}>", to
    message.set_content(body)
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=name)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)


def whatsapp_number(value: str) -> str:
    """Digits only, in international form (8 digits: a Tunisian number, 216 added)."""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    return "216" + digits if len(digits) == 8 else digits


def _send_whatsapp(number: str, message: str, pdf: bytes, name: str) -> None:
    """The PDF as a document, the message as its caption, through the WhatsApp Business Cloud API (raises on failure)."""
    base = f"https://graph.facebook.com/v21.0/{settings.whatsapp_phone_number_id}"
    auth = {"Authorization": f"Bearer {settings.whatsapp_token}"}
    upload = httpx.post(f"{base}/media", headers=auth, timeout=30, data={"messaging_product": "whatsapp", "type": "application/pdf"},
                        files={"file": (name, pdf, "application/pdf")})
    if upload.status_code >= 300:
        raise RuntimeError(upload.text[:300])
    media = upload.json()["id"]
    if settings.whatsapp_memoire_template:
        # A template parameter may not hold line breaks: the message is written on one line.
        flat = re.sub(r"\s*\n+\s*", " ", message).strip()[:1000] or " "
        body = {"messaging_product": "whatsapp", "to": number, "type": "template", "template": {
            "name": settings.whatsapp_memoire_template, "language": {"code": settings.whatsapp_template_language},
            "components": [
                {"type": "header", "parameters": [{"type": "document", "document": {"id": media, "filename": name}}]},
                {"type": "body", "parameters": [{"type": "text", "text": flat}]},
            ]}}
    else:
        body = {"messaging_product": "whatsapp", "to": number, "type": "document",
                "document": {"id": media, "filename": name, "caption": message[:1000]}}
    answer = httpx.post(f"{base}/messages", headers=auth, timeout=30, json=body)
    if answer.status_code >= 300:
        try:
            reason = answer.json().get("error", {}).get("message") or answer.text
        except ValueError:
            reason = answer.text
        raise RuntimeError(reason[:300])


@router.post("/{code_client}/envoyer")
def send_memoire(code_client: str, payload: Envoi, db: Session = Depends(get_db)) -> dict[str, Any]:
    code = code_client.strip()
    client = _client(db, code)
    rows = _rows(db, code, payload.piece, payload.date_debut, payload.date_fin, payload.code_compagnie)
    if not rows:
        raise HTTPException(status_code=422, detail="Aucune quittance impayée : il n’y a pas de mémoire de règlement à envoyer.")
    pdf, name = memoire_pdf(client, rows), _file_name(code)
    to = payload.destinataire.strip()
    if payload.canal == "email":
        if not EMAIL_RE.match(to):
            raise HTTPException(status_code=422, detail="Adresse email invalide.")
        try:
            _send_email(to, payload.sujet.strip() or f"Mémoire de règlement – {AGENCY[0]}", payload.message, pdf, name)
        except Exception as exc:  # noqa: BLE001 - the reason is shown to the user
            logger.warning("mémoire %s: email to %s failed: %s", code, to, exc)
            raise HTTPException(status_code=502, detail=f"L’email n’a pas pu être envoyé : {exc}") from exc
        return {"canal": "email", "destinataire": to, "envoye": True, "fichier": name}
    number = whatsapp_number(to)
    if len(number) < 10:
        raise HTTPException(status_code=422, detail="Numéro WhatsApp invalide.")
    if settings.whatsapp_token and settings.whatsapp_phone_number_id:
        try:
            _send_whatsapp(number, payload.message, pdf, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mémoire %s: WhatsApp to %s failed: %s", code, number, exc)
            raise HTTPException(status_code=502, detail=f"Le message WhatsApp n’a pas pu être envoyé : {exc}") from exc
        return {"canal": "whatsapp", "destinataire": number, "envoye": True, "fichier": name}
    # No WhatsApp API on the server: WhatsApp opens with the message written; the PDF (downloaded) is attached there.
    return {"canal": "whatsapp", "destinataire": number, "envoye": False, "fichier": name,
            "lien": f"https://wa.me/{number}?text={quote(payload.message)}"}
