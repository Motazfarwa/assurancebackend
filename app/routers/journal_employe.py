"""Journal du jour: each employee writes down what they did in the day; the administrator follows every employee's days.

An employee keeps one journal per day (dbo."TJournalEmploye", key: user + day): the tasks with their state (fait, en cours,
à faire), remarks and what is planned next. "Valider" locks the day and sends a short report to the administrator's
WhatsApp:
 - through the WhatsApp Business Cloud API when WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are set on the server
   (WHATSAPP_TEMPLATE: an approved template with one body parameter, needed outside the 24 h window of Meta);
 - otherwise a wa.me link is returned: it opens WhatsApp with the report written, the employee only presses "Envoyer".
The administrator's number is set in the module (dbo."TParamApp"), else ADMIN_WHATSAPP_NUMBER.
The report also counts what the employee did in the software that day (dbo."TMouchard").

Client requests:
 - the employee notes in the day the requests received (by phone, email or WhatsApp), the request and an observation
   ("Demandes" of the day's journal);
 - the administrator records a client request and assigns it to chosen users, or to every user of an agency (the "lieu"
   of the users: Tunis, Hammamet): dbo."TDemandeClient" and one dbo."TDemandeAffectation" line per user, who marks it
   "traitée" (with a reply) from their journal.
"""
import html
import json
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
import datetime as dt
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.routers.access import is_admin

log = logging.getLogger("app.journal")
router = APIRouter(prefix="/api/journal-employe", tags=["Journal du jour"])

DDL = [
    """CREATE TABLE IF NOT EXISTS dbo."TJournalEmploye" (
        "Nom_Utilisateur" varchar(50) NOT NULL,
        "Date_Jour" date NOT NULL,
        "Taches" jsonb NOT NULL DEFAULT '[]'::jsonb,
        "Remarques" text,
        "Demain" text,
        "Statut" varchar(10) NOT NULL DEFAULT 'brouillon',
        "Date_Maj" timestamp,
        "Date_Validation" timestamp,
        "Envoi" varchar(10),
        "Vu_Admin" timestamp,
        PRIMARY KEY ("Nom_Utilisateur", "Date_Jour")
    )""",
    'CREATE TABLE IF NOT EXISTS dbo."TParamApp" ("Cle" varchar(50) PRIMARY KEY, "Valeur" text)',
    'ALTER TABLE dbo."TJournalEmploye" ADD COLUMN IF NOT EXISTS "Envoi_Email" varchar(10)',
    'ALTER TABLE dbo."TJournalEmploye" ADD COLUMN IF NOT EXISTS "Demandes" jsonb NOT NULL DEFAULT \'[]\'::jsonb',
    """CREATE TABLE IF NOT EXISTS dbo."TDemandeClient" (
        "Id" serial PRIMARY KEY,
        "Client" varchar(150),
        "Canaux" jsonb NOT NULL DEFAULT '[]'::jsonb,
        "Demande" text NOT NULL,
        "Observation" text,
        "Cible_Type" varchar(12) NOT NULL,
        "Cible" varchar(150),
        "Cree_Par" varchar(50),
        "Date_Creation" timestamp NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS dbo."TDemandeAffectation" (
        "Demande_Id" integer NOT NULL REFERENCES dbo."TDemandeClient"("Id") ON DELETE CASCADE,
        "Nom_Utilisateur" varchar(50) NOT NULL,
        "Statut" varchar(10) NOT NULL DEFAULT 'a_traiter',
        "Reponse" text,
        "Date_Traitement" timestamp,
        PRIMARY KEY ("Demande_Id", "Nom_Utilisateur")
    )""",
]
CHANNELS = {"telephone": "Téléphone", "email": "Email", "whatsapp": "WhatsApp"}
NOW = "(now() AT TIME ZONE 'Africa/Tunis')"
STATES = {"fait": "Fait", "en_cours": "En cours", "a_faire": "À faire"}
ACTIONS = {"Ajout": "ajouts", "Modification": "modifications", "Suppresion": "suppressions", "Impression": "impressions",
           "Consultation": "consultations", "Annulation": "annulations", "Tentative": "tentatives refusées"}
_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if not _ready:
        for statement in DDL:
            db.execute(text(statement))
        db.commit()
        _ready = True


class Tache(BaseModel):
    texte: str = Field(min_length=1, max_length=300)
    statut: str = Field(default="fait", pattern="^(fait|en_cours|a_faire)$")
    # when the task was done: its day and time (HH:MM)
    date: dt.date | None = None
    heure: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def when(task: dict[str, Any]) -> str:
    """'11/11 à 14:30' (the day and time the task was done), or ''."""
    day = date.fromisoformat(task["date"]).strftime("%d/%m") if task.get("date") else ""
    return " à ".join(part for part in (day, task.get("heure") or "") if part)


class DemandeJour(BaseModel):
    canaux: list[str] = Field(default_factory=list, max_length=3)
    demande: str = Field(default="", max_length=500)
    observation: str = Field(default="", max_length=1500)


class JournalIn(BaseModel):
    date_jour: date
    taches: list[Tache] = Field(default_factory=list, max_length=60)
    demandes: list[DemandeJour] = Field(default_factory=list, max_length=40)
    remarques: str = Field(default="", max_length=1500)
    demain: str = Field(default="", max_length=1000)


class ParamsIn(BaseModel):
    whatsapp_admin: str | None = Field(default=None, max_length=30)
    email_admin: str | None = Field(default=None, max_length=120)


def _today() -> date:
    return date.today()


def _check_day(day: date) -> None:
    if day > _today() + timedelta(days=1):  # a day ahead at most: the server and the agency may not share the date
        raise HTTPException(status_code=422, detail="Le journal ne peut pas être saisi pour une date future.")


def _row(db: Session, name: str, day: date) -> dict[str, Any] | None:
    row = db.execute(text('SELECT * FROM dbo."TJournalEmploye" WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" = :d'),
                     {"u": name, "d": day}).mappings().first()
    return dict(row) if row else None


def _activity(db: Session, name: str, day: date) -> dict[str, Any]:
    """What the user did in the software that day: actions counted by kind, and the last ones."""
    params = {"u": name, "d": day, "n": day + timedelta(days=1)}
    where = 'LOWER(TRIM(utilisateur)) = LOWER(TRIM(:u)) AND dmvt >= :d AND dmvt < :n'
    counts = db.execute(text(f'SELECT TRIM("Mvt") AS mvt, COUNT(*) AS n FROM dbo."TMouchard" WHERE {where} GROUP BY 1 ORDER BY 2 DESC'), params).all()
    last = db.execute(text(f'SELECT "Hmvt" AS h, TRIM(design) AS design FROM dbo."TMouchard" WHERE {where} AND TRIM("Mvt") IN (\'Ajout\', \'Modification\', \'Suppresion\') ORDER BY "N" DESC LIMIT 12'), params).mappings().all()
    return {
        "total": sum(n for _, n in counts),
        "par_type": [{"type": mvt, "libelle": ACTIONS.get(mvt, (mvt or "autres").lower()), "nombre": n} for mvt, n in counts],
        "dernieres": [{"heure": r["h"].strftime("%H:%M") if r["h"] else None, "design": r["design"]} for r in last],
    }


def _param(db: Session, key: str) -> str:
    return db.execute(text('SELECT "Valeur" FROM dbo."TParamApp" WHERE "Cle" = :k'), {"k": key}).scalar() or ""


def _admin_number(db: Session) -> str:
    """The administrator's WhatsApp number in international form, digits only (8 digits: a Tunisian number, 216 added)."""
    digits = re.sub(r"\D", "", _param(db, "whatsapp_admin") or settings.admin_whatsapp_number or "")
    digits = digits[2:] if digits.startswith("00") else digits
    return "216" + digits if len(digits) == 8 else digits


def _out(row: dict[str, Any] | None, name: str, day: date) -> dict[str, Any]:
    if not row:
        return {"utilisateur": name, "date_jour": day.isoformat(), "taches": [], "demandes": [], "remarques": "", "demain": "", "statut": "vide",
                "date_maj": None, "date_validation": None, "envoi": None, "envoi_email": None, "vu_admin": None}
    taches = row["Taches"] if isinstance(row["Taches"], list) else json.loads(row["Taches"] or "[]")
    demandes = row.get("Demandes") or []
    demandes = demandes if isinstance(demandes, list) else json.loads(demandes)
    stamp = lambda value: value.isoformat(timespec="minutes") if value else None  # noqa: E731
    return {"utilisateur": row["Nom_Utilisateur"], "date_jour": row["Date_Jour"].isoformat(), "taches": taches, "demandes": demandes,
            "remarques": row["Remarques"] or "", "demain": row["Demain"] or "", "statut": row["Statut"],
            "date_maj": stamp(row["Date_Maj"]), "date_validation": stamp(row["Date_Validation"]), "envoi": row["Envoi"], "envoi_email": row.get("Envoi_Email"), "vu_admin": stamp(row["Vu_Admin"])}


def report_text(journal: dict[str, Any], activity: dict[str, Any]) -> str:
    """The short report sent to the administrator."""
    day = date.fromisoformat(journal["date_jour"]).strftime("%d/%m/%Y")
    lines = [f"📋 Journal du {day} — {journal['utilisateur']}"]
    for key, label in STATES.items():
        tasks = [t["texte"] + (f" ({when(t)})" if when(t) else "") for t in journal["taches"] if t.get("statut") == key]
        if tasks:
            lines.append(f"\n{'✅' if key == 'fait' else '⏳' if key == 'en_cours' else '📌'} {label} ({len(tasks)})")
            lines += [f"• {task}" for task in tasks]
    if not journal["taches"]:
        lines.append("\nAucune tâche saisie.")
    if journal.get("demandes"):
        lines.append(f"\n📞 Demandes clients ({len(journal['demandes'])})")
        for item in journal["demandes"]:
            line = f"• {item['demande']}" + (f" [{channels_text(item.get('canaux'))}]" if item.get("canaux") else "")
            lines.append(line + (f" — {item['observation']}" if item.get("observation") else ""))
    if journal["remarques"].strip():
        lines.append(f"\n📝 Remarques : {journal['remarques'].strip()}")
    if journal["demain"].strip():
        lines.append(f"\n➡️ Prévu ensuite : {journal['demain'].strip()}")
    if activity["total"]:
        detail = ", ".join(f"{t['nombre']} {t['libelle']}" for t in activity["par_type"])
        lines.append(f"\n🖥️ Dans le logiciel : {activity['total']} actions ({detail})")
    else:
        lines.append("\n🖥️ Aucune action enregistrée dans le logiciel ce jour.")
    report = "\n".join(lines)
    return report if len(report) <= 3500 else report[:3490].rstrip() + "\n…"


def _report_email(db: Session) -> str:
    """The address the submitted days are emailed to: set in the module, else JOURNAL_REPORT_EMAIL."""
    return (_param(db, "email_admin") or settings.journal_report_email or "").strip()


def _email_configured() -> bool:
    return bool(settings.brevo_api_key or (settings.smtp_host and settings.smtp_user and settings.smtp_password))


def _report_html(journal: dict[str, Any], activity: dict[str, Any]) -> str:
    """The same report as an email page: tasks by state, remarks, the activity in the software."""
    e = html.escape
    day = date.fromisoformat(journal["date_jour"]).strftime("%d/%m/%Y")
    colors = {"fait": "#17705f", "en_cours": "#8a5a10", "a_faire": "#2a5d9c"}
    rows = "".join(
        f'<tr><td style="padding:6px 10px;color:{colors.get(t.get("statut"), "#233c4b")};font-weight:700;white-space:nowrap">{e(STATES.get(t.get("statut"), ""))}</td>'
        f'<td style="padding:6px 10px">{e(t["texte"])}</td><td style="padding:6px 10px;color:#587079;white-space:nowrap">{e(when(t))}</td></tr>' for t in journal["taches"])
    parts = [f'<h2 style="margin:0 0 4px;color:#173247">Journal du {day}</h2><p style="margin:0 0 14px;color:#587079">{e(journal["utilisateur"])}</p>',
             f'<table style="border-collapse:collapse;font-size:14px;border:1px solid #e3eaec">{rows}</table>' if rows else '<p>Aucune tâche saisie.</p>']
    if journal.get("demandes"):
        items = ""
        for item in journal["demandes"]:
            via = " <span style='color:#587079'>(reçue par " + e(channels_text(item.get("canaux"))) + ")</span>" if item.get("canaux") else ""
            note = "<br><span style='color:#587079'>" + e(item["observation"]) + "</span>" if item.get("observation") else ""
            items += "<li style='margin:0 0 6px'><b>" + e(item["demande"]) + "</b>" + via + note + "</li>"
        parts.append(f'<h3 style="margin:16px 0 6px;color:#173247;font-size:15px">Demandes clients</h3><ul style="margin:0;padding-left:18px">{items}</ul>')
    if journal["remarques"].strip():
        parts.append(f'<p style="margin:14px 0 0"><b>Remarques :</b> {e(journal["remarques"].strip())}</p>')
    if journal["demain"].strip():
        parts.append(f'<p style="margin:8px 0 0"><b>Prévu ensuite :</b> {e(journal["demain"].strip())}</p>')
    detail = ", ".join(f"{t['nombre']} {t['libelle']}" for t in activity["par_type"])
    parts.append(f'<p style="margin:14px 0 0;color:#587079">Dans le logiciel : {activity["total"]} action(s){f" ({e(detail)})" if detail else ""}</p>')
    return f'<div style="font-family:Arial,sans-serif;color:#233c4b;font-size:14px">{"".join(parts)}</div>'


def _send_email(to: str, subject: str, body: str, body_html: str) -> None:
    """Sends the report (raises on failure): the Brevo API when its key is set, else SMTP."""
    sender = settings.smtp_from or settings.smtp_user or to
    if settings.brevo_api_key:
        answer = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=20, headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
                            json={"sender": {"name": "Gestion Assurances", "email": sender}, "to": [{"email": to}], "subject": subject,
                                  "textContent": body, "htmlContent": body_html})
        if answer.status_code >= 300:
            raise RuntimeError(f"Brevo {answer.status_code} : {answer.text[:200]}")
        return
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, f"Gestion Assurances <{sender}>", to
    message.set_content(body)
    message.add_alternative(body_html, subtype="html")
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)


def channels_text(channels: list[str] | None) -> str:
    return ", ".join(CHANNELS[c] for c in CHANNELS if c in (channels or []))


def _day_requests(items: list[DemandeJour]) -> list[dict[str, Any]]:
    """The client requests of the day that are written (a request, or at least an observation)."""
    out = []
    for item in items:
        demande, observation = item.demande.strip(), item.observation.strip()
        if not demande and not observation:
            continue
        if not demande:
            raise HTTPException(status_code=422, detail="Chaque demande client doit avoir sa demande (pas seulement une observation).")
        out.append({"canaux": [c for c in CHANNELS if c in item.canaux], "demande": demande, "observation": observation})
    return out


def _link(number: str, report: str) -> str | None:
    return f"https://wa.me/{number}?text={quote(report)}" if number else None


def _send_api(number: str, report: str) -> None:
    """Sends the report through the WhatsApp Business Cloud API (raises on failure)."""
    url = f"https://graph.facebook.com/v21.0/{settings.whatsapp_phone_number_id}/messages"
    if settings.whatsapp_template:
        # A template parameter may not hold line breaks: the report is written on one line.
        flat = re.sub(r"\s*\n+\s*", " | ", report).strip(" |")[:1000]
        body = {"messaging_product": "whatsapp", "to": number, "type": "template", "template": {
            "name": settings.whatsapp_template, "language": {"code": settings.whatsapp_template_language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}]}}
    else:
        body = {"messaging_product": "whatsapp", "to": number, "type": "text", "text": {"body": report, "preview_url": False}}
    answer = httpx.post(url, json=body, headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=20)
    if answer.status_code >= 300:
        try:
            reason = answer.json().get("error", {}).get("message") or answer.text
        except ValueError:
            reason = answer.text
        raise RuntimeError(reason[:300])


@router.get("/moi")
def my_journal(day: date | None = Query(default=None, alias="date"), user: dict[str, Any] = Depends(require_current_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure(db)
    name, day = str(user["sub"]), day or _today()
    journal = _out(_row(db, name, day), name, day)
    activity = _activity(db, name, day)
    report = report_text(journal, activity)
    return {"journal": journal, "activite": activity, "rapport": report,
            "whatsapp": {"numero": bool(_admin_number(db)), "lien": _link(_admin_number(db), report) if journal["statut"] == "valide" else None}}


def _save(db: Session, name: str, payload: JournalIn, validate: bool) -> None:
    current = _row(db, name, payload.date_jour)
    if current and current["Statut"] == "valide":
        raise HTTPException(status_code=409, detail="Ce journal est déjà validé : demandez à l’administrateur de le rouvrir pour le modifier.")
    taches = json.dumps([{"texte": t.texte.strip(), "statut": t.statut, "date": t.date.isoformat() if t.date else None, "heure": t.heure}
                         for t in payload.taches if t.texte.strip()], ensure_ascii=False)
    demandes = json.dumps(_day_requests(payload.demandes), ensure_ascii=False)
    db.execute(text(f'''
        INSERT INTO dbo."TJournalEmploye" ("Nom_Utilisateur", "Date_Jour", "Taches", "Demandes", "Remarques", "Demain", "Statut", "Date_Maj", "Date_Validation")
        VALUES (:u, :d, CAST(:t AS jsonb), CAST(:q AS jsonb), :r, :m, CAST(:s AS varchar), {NOW}, CASE WHEN CAST(:s AS varchar) = 'valide' THEN {NOW} END)
        ON CONFLICT ("Nom_Utilisateur", "Date_Jour") DO UPDATE SET "Taches" = EXCLUDED."Taches", "Demandes" = EXCLUDED."Demandes", "Remarques" = EXCLUDED."Remarques",
            "Demain" = EXCLUDED."Demain", "Statut" = EXCLUDED."Statut", "Date_Maj" = EXCLUDED."Date_Maj", "Date_Validation" = EXCLUDED."Date_Validation"
    '''), {"u": current["Nom_Utilisateur"] if current else name, "d": payload.date_jour, "t": taches, "q": demandes, "r": payload.remarques.strip(),
           "m": payload.demain.strip(), "s": "valide" if validate else "brouillon"})


@router.put("/moi")
def save_journal(payload: JournalIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure(db)
    _check_day(payload.date_jour)
    _save(db, str(user["sub"]), payload, validate=False)
    db.commit()
    return my_journal(payload.date_jour, user, db)


@router.post("/moi/valider")
def validate_journal(payload: JournalIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure(db)
    _check_day(payload.date_jour)
    if not any(t.texte.strip() for t in payload.taches) and not _day_requests(payload.demandes):
        raise HTTPException(status_code=422, detail="Ajoutez au moins une tâche ou une demande client avant de soumettre la journée.")
    name = str(user["sub"])
    _save(db, name, payload, validate=True)
    db.commit()
    result = my_journal(payload.date_jour, user, db)
    number = _admin_number(db)
    sent, error = False, None
    if number and settings.whatsapp_token and settings.whatsapp_phone_number_id:
        try:
            _send_api(number, result["rapport"])
            sent = True
        except (httpx.HTTPError, RuntimeError) as exc:
            error = f"Envoi automatique WhatsApp impossible ({exc}). Envoyez le rapport avec le bouton WhatsApp."
            log.warning("journal %s %s: WhatsApp %s", name, payload.date_jour, exc)
    db.execute(text('UPDATE dbo."TJournalEmploye" SET "Envoi" = :e WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" = :d'),
               {"e": "api" if sent else "lien" if number else None, "u": name, "d": payload.date_jour})
    db.commit()
    result["journal"]["envoi"] = "api" if sent else "lien" if number else None
    result["whatsapp"] = {"numero": bool(number), "envoye": sent, "erreur": error, "lien": None if sent else _link(number, result["rapport"])}
    # The report is also emailed to the administrator.
    to = _report_email(db)
    mailed, mail_error = False, None
    if to and _email_configured():
        day = payload.date_jour.strftime("%d/%m/%Y")
        try:
            _send_email(to, f"Journal du {day} — {name}", result["rapport"], _report_html(result["journal"], result["activite"]))
            mailed = True
        except (OSError, smtplib.SMTPException, httpx.HTTPError, RuntimeError) as exc:
            mail_error = f"L’email à l’administrateur n’a pas pu être envoyé ({str(exc)[:160]}) : le journal est bien enregistré."
            log.warning("journal %s %s: email %s", name, payload.date_jour, exc)
    elif to:
        mail_error = "Envoi d’email non configuré sur le serveur : le journal est bien enregistré, l’administrateur le voit dans ce module."
    db.execute(text('UPDATE dbo."TJournalEmploye" SET "Envoi_Email" = :e WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" = :d'),
               {"e": "envoye" if mailed else "echec" if to else None, "u": name, "d": payload.date_jour})
    db.commit()
    result["journal"]["envoi_email"] = "envoye" if mailed else "echec" if to else None
    result["email"] = {"destinataire": to, "envoye": mailed, "erreur": mail_error}
    return result


# ---- Administrator: every employee's day ----

def _require_admin(user: dict[str, Any]) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Accès réservé à l’administrateur.")


@router.get("/equipe")
def team_day(day: date | None = Query(default=None, alias="date"), user: dict[str, Any] = Depends(require_current_user),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every employee (the users of the agency, plus anyone who wrote a journal that day) and their journal of the day."""
    _require_admin(user)
    _ensure(db)
    day = day or _today()
    users = [r[0] for r in db.execute(text('''
        SELECT MIN(TRIM("Nom_Utilisateur")) FROM dbo."RUtilisateur_Depot" WHERE TRIM(COALESCE("Nom_Utilisateur", '')) <> ''
        GROUP BY LOWER(TRIM("Nom_Utilisateur"))''')).all()]
    rows = {r["Nom_Utilisateur"].strip().lower(): r for r in db.execute(text('SELECT * FROM dbo."TJournalEmploye" WHERE "Date_Jour" = :d'), {"d": day}).mappings()}
    names = sorted({*(u for u in users if u.lower() != "admin"), *(r["Nom_Utilisateur"].strip() for r in rows.values())}, key=str.lower)
    items = []
    for name in names:
        journal = _out(dict(rows[name.lower()]) if name.lower() in rows else None, name, day)
        items.append({**journal, "activite": _activity(db, name, day)})
    order = {"valide": 0, "brouillon": 1, "vide": 2}
    items.sort(key=lambda item: (order[item["statut"]], item["utilisateur"].lower()))
    return {"date_jour": day.isoformat(), "items": items,
            "compteurs": {k: sum(1 for i in items if i["statut"] == k) for k in order},
            "whatsapp": {"numero": _param(db, "whatsapp_admin") or settings.admin_whatsapp_number or "",
                         "api": bool(settings.whatsapp_token and settings.whatsapp_phone_number_id)},
            "email": {"adresse": _report_email(db), "configure": _email_configured()}}


@router.get("/equipe/{name}")
def employee_history(name: str, jours: int = Query(default=14, ge=1, le=90), user: dict[str, Any] = Depends(require_current_user),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    """One employee's last journals."""
    _require_admin(user)
    _ensure(db)
    rows = db.execute(text('SELECT * FROM dbo."TJournalEmploye" WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" > :since ORDER BY "Date_Jour" DESC'),
                      {"u": name.strip(), "since": _today() - timedelta(days=jours)}).mappings().all()
    return {"utilisateur": name.strip(), "items": [_out(dict(r), name, r["Date_Jour"]) for r in rows]}


@router.post("/equipe/{name}/{day}/vu")
def mark_seen(name: str, day: date, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_admin(user)
    _ensure(db)
    db.execute(text(f'UPDATE dbo."TJournalEmploye" SET "Vu_Admin" = {NOW} WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" = :d'), {"u": name.strip(), "d": day})
    db.commit()
    return _out(_row(db, name.strip(), day), name, day)


@router.post("/equipe/{name}/{day}/rouvrir")
def reopen(name: str, day: date, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The employee may correct a validated day again (it must be validated again)."""
    _require_admin(user)
    _ensure(db)
    db.execute(text('''UPDATE dbo."TJournalEmploye" SET "Statut" = 'brouillon', "Date_Validation" = NULL, "Envoi" = NULL, "Envoi_Email" = NULL, "Vu_Admin" = NULL
                       WHERE LOWER("Nom_Utilisateur") = LOWER(:u) AND "Date_Jour" = :d'''), {"u": name.strip(), "d": day})
    db.commit()
    return _out(_row(db, name.strip(), day), name, day)


@router.put("/parametres")
def set_params(payload: ParamsIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The administrator's WhatsApp number and email address, which receive the reports (only those sent are changed)."""
    _require_admin(user)
    _ensure(db)
    values = {}
    if payload.whatsapp_admin is not None:
        values["whatsapp_admin"] = payload.whatsapp_admin.strip()
        if values["whatsapp_admin"] and len(re.sub(r"\D", "", values["whatsapp_admin"])) < 8:
            raise HTTPException(status_code=422, detail="Numéro WhatsApp invalide : 8 chiffres (Tunisie) ou le numéro international.")
    if payload.email_admin is not None:
        values["email_admin"] = payload.email_admin.strip()
        if values["email_admin"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email_admin"]):
            raise HTTPException(status_code=422, detail="Adresse email invalide.")
    for key, value in values.items():
        db.execute(text('INSERT INTO dbo."TParamApp" ("Cle", "Valeur") VALUES (:k, :v) ON CONFLICT ("Cle") DO UPDATE SET "Valeur" = EXCLUDED."Valeur"'), {"k": key, "v": value})
    db.commit()
    return {"numero": _param(db, "whatsapp_admin"), "email": _report_email(db)}


# ---- Client requests assigned by the administrator ----

class DemandeIn(BaseModel):
    client: str = Field(default="", max_length=150)
    canaux: list[str] = Field(default_factory=list, max_length=3)
    demande: str = Field(min_length=1, max_length=1000)
    observation: str = Field(default="", max_length=1500)
    cible_type: str = Field(pattern="^(utilisateur|agence)$")
    utilisateurs: list[str] = Field(default_factory=list, max_length=100)
    agence: str = Field(default="", max_length=100)


class TraiterIn(BaseModel):
    reponse: str = Field(default="", max_length=1500)


def _staff(db: Session) -> list[dict[str, str]]:
    """The agency's users (not the administrator) and their agency (lieu)."""
    rows = db.execute(text('''
        SELECT MIN(TRIM("Nom_Utilisateur")) AS nom, MIN(TRIM(COALESCE(lieu, \'\'))) AS agence FROM dbo."RUtilisateur_Depot"
        WHERE TRIM(COALESCE("Nom_Utilisateur", \'\')) <> \'\' AND LOWER(TRIM("Nom_Utilisateur")) <> \'admin\'
        GROUP BY LOWER(TRIM("Nom_Utilisateur")) ORDER BY 1''')).mappings().all()
    return [dict(row) for row in rows]


def _request_out(row: dict[str, Any], assignments: list[dict[str, Any]]) -> dict[str, Any]:
    stamp = lambda value: value.isoformat(timespec="minutes") if value else None  # noqa: E731
    canaux = row["Canaux"] if isinstance(row["Canaux"], list) else json.loads(row["Canaux"] or "[]")
    return {"id": row["Id"], "client": row["Client"] or "", "canaux": canaux, "demande": row["Demande"], "observation": row["Observation"] or "",
            "cible_type": row["Cible_Type"], "cible": row["Cible"] or "", "cree_par": row["Cree_Par"], "date_creation": stamp(row["Date_Creation"]),
            "affectations": [{"utilisateur": a["Nom_Utilisateur"], "statut": a["Statut"], "reponse": a["Reponse"] or "",
                              "date_traitement": stamp(a["Date_Traitement"])} for a in assignments]}


@router.get("/demandes/destinataires")
def request_targets(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Who a request can be assigned to: the users, and the agencies with their users."""
    _require_admin(user)
    staff = _staff(db)
    agencies = sorted({s["agence"] for s in staff if s["agence"]}, key=str.lower)
    return {"utilisateurs": staff, "agences": [{"agence": a, "utilisateurs": [s["nom"] for s in staff if s["agence"] == a]} for a in agencies]}


@router.get("/demandes")
def list_requests(jours: int = Query(default=30, ge=1, le=365), user: dict[str, Any] = Depends(require_current_user),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """The requests recorded by the administrator in the last days, with each assigned user's state."""
    _require_admin(user)
    _ensure(db)
    rows = db.execute(text(f'SELECT * FROM dbo."TDemandeClient" WHERE "Date_Creation" >= {NOW} - make_interval(days => :j) ORDER BY "Id" DESC'), {"j": jours}).mappings().all()
    ids = [r["Id"] for r in rows]
    assignments: dict[int, list[dict[str, Any]]] = {}
    if ids:
        for a in db.execute(text('SELECT * FROM dbo."TDemandeAffectation" WHERE "Demande_Id" = ANY(:ids) ORDER BY LOWER("Nom_Utilisateur")'), {"ids": ids}).mappings():
            assignments.setdefault(a["Demande_Id"], []).append(dict(a))
    return {"items": [_request_out(dict(r), assignments.get(r["Id"], [])) for r in rows]}


@router.post("/demandes")
def create_request(payload: DemandeIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Records a client request and assigns it to the chosen users, or to every user of the agency."""
    _require_admin(user)
    _ensure(db)
    staff = _staff(db)
    if payload.cible_type == "agence":
        agence = payload.agence.strip()
        names = [s["nom"] for s in staff if s["agence"].lower() == agence.lower()]
        if not names:
            raise HTTPException(status_code=422, detail=f"Aucun utilisateur dans l’agence {agence or 'choisie'}.")
        target = agence
    else:
        known = {s["nom"].lower(): s["nom"] for s in staff}
        names = list(dict.fromkeys(known[n.strip().lower()] for n in payload.utilisateurs if n.strip().lower() in known))
        if not names:
            raise HTTPException(status_code=422, detail="Choisissez au moins un utilisateur.")
        target = ", ".join(names)
    demande = payload.demande.strip()
    if not demande:
        raise HTTPException(status_code=422, detail="Écrivez la demande du client.")
    new_id = db.execute(text(f'''
        INSERT INTO dbo."TDemandeClient" ("Client", "Canaux", "Demande", "Observation", "Cible_Type", "Cible", "Cree_Par", "Date_Creation")
        VALUES (:c, CAST(:k AS jsonb), :d, :o, :t, :g, :u, {NOW}) RETURNING "Id"'''),
        {"c": payload.client.strip(), "k": json.dumps([c for c in CHANNELS if c in payload.canaux]), "d": demande,
         "o": payload.observation.strip(), "t": payload.cible_type, "g": target[:150], "u": str(user["sub"])[:50]}).scalar()
    for name in names:
        db.execute(text('INSERT INTO dbo."TDemandeAffectation" ("Demande_Id", "Nom_Utilisateur") VALUES (:i, :u)'), {"i": new_id, "u": name[:50]})
    db.commit()
    row = db.execute(text('SELECT * FROM dbo."TDemandeClient" WHERE "Id" = :i'), {"i": new_id}).mappings().first()
    assignments = db.execute(text('SELECT * FROM dbo."TDemandeAffectation" WHERE "Demande_Id" = :i ORDER BY LOWER("Nom_Utilisateur")'), {"i": new_id}).mappings().all()
    return _request_out(dict(row), [dict(a) for a in assignments])


@router.delete("/demandes/{request_id}")
def delete_request(request_id: int, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_admin(user)
    _ensure(db)
    deleted = db.execute(text('DELETE FROM dbo."TDemandeClient" WHERE "Id" = :i'), {"i": request_id}).rowcount
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    return {"supprimee": request_id}


@router.get("/moi/demandes")
def my_requests(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """The requests assigned to me: those to handle, and those handled in the last 7 days."""
    _ensure(db)
    rows = db.execute(text(f'''
        SELECT d.*, a."Statut", a."Reponse", a."Date_Traitement" FROM dbo."TDemandeAffectation" a JOIN dbo."TDemandeClient" d ON d."Id" = a."Demande_Id"
        WHERE LOWER(a."Nom_Utilisateur") = LOWER(:u) AND (a."Statut" <> \'traitee\' OR a."Date_Traitement" >= {NOW} - INTERVAL \'7 days\')
        ORDER BY a."Statut" = \'traitee\', d."Id" DESC'''), {"u": str(user["sub"])}).mappings().all()
    items = []
    for r in rows:
        out = _request_out(dict(r), [])
        out.pop("affectations")
        out.update({"statut": r["Statut"], "reponse": r["Reponse"] or "",
                    "date_traitement": r["Date_Traitement"].isoformat(timespec="minutes") if r["Date_Traitement"] else None})
        items.append(out)
    return {"items": items}


@router.post("/moi/demandes/{request_id}/traiter")
def handle_request(request_id: int, payload: TraiterIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Marks a request assigned to me as handled, with a reply."""
    _ensure(db)
    done = db.execute(text(f'''UPDATE dbo."TDemandeAffectation" SET "Statut" = \'traitee\', "Reponse" = :r, "Date_Traitement" = {NOW}
                              WHERE "Demande_Id" = :i AND LOWER("Nom_Utilisateur") = LOWER(:u)'''),
                      {"i": request_id, "u": str(user["sub"]), "r": payload.reponse.strip()}).rowcount
    db.commit()
    if not done:
        raise HTTPException(status_code=404, detail="Cette demande ne vous est pas affectée.")
    return my_requests(user, db)
