"""History of the users' actions in dbo."TMouchard", the legacy software's log, with its wording and codes:
1 Consultation, 2 Ajout, 3 Modification, 4 Suppresion, 5 Impression, 6 Annulation, 7 Tentative.

Every add / change / delete the API accepts is recorded by the middleware below; a refused one (403 / 409)
is recorded as a "Tentative", as is a failed sign-in. As in the legacy software, opening a record is a "Consultation"
and showing an état (aperçu / impression) an "Impression" (READS below; typing in a search list is not recorded, and
the same consultation repeated within a few minutes is recorded once).

What exactly was done is kept in dbo."TMouchardDetail" (same "N"): for a change, each field with its value before and
after (the record is read before the change and again after it); for an addition, the values of the new record; for a
deletion, the values of the deleted record; otherwise the data sent. Passwords and file contents are never kept.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from starlette.background import BackgroundTask
from sqlalchemy import text

from app.auth import decode_access_token
from app.database import SessionLocal
from app.routers.auth import users_table

logger = logging.getLogger(__name__)

# Tunisia is UTC+1 all year (no daylight saving time).
TUNIS = timezone(timedelta(hours=1))

MOVES = {"Consultation": 1, "Ajout": 2, "Modification": 3, "Suppresion": 4, "Impression": 5, "Annulation": 6, "Tentative": 7}
VERBS = {"POST": "Ajout", "PUT": "Modification", "PATCH": "Modification", "DELETE": "Suppresion"}

# Path after /api/ -> (what is acted on, how its number is written), most specific first.
ENTITIES = [
    (r"^clients", "client", "Code"),
    (r"^flotte/[^/]+/vehicules/[^/]+/sortie", "Sortie du véhicule de flotte police", "N°"),
    (r"^flotte/[^/]+/vehicules/[^/]+/remplacement", "Remplacement de véhicule, flotte police", "N°"),
    (r"^flotte/[^/]+/vehicules/import", "Import de véhicules, flotte police", "N°"),
    (r"^flotte/[^/]+/vehicules", "Véhicule de flotte police", "N°"),
    (r"^flotte", "Police flotte", "N°"),
    (r"^polices", "Police", "N°"),
    (r"^quittances", "Quittance", "N°"),
    (r"^sinistres/[^/]+/etapes", "Étape du sinistre", "N°"),
    (r"^sinistres/[^/]+/appels", "Appel du sinistre", "N°"),
    (r"^sinistres", "Sinistre", "N°"),
    (r"^cheques/verser", "Versement pièce", "N°"),
    (r"^cheques", "Pièce", "N°"),
    (r"^banques", "Banque", "Code"),
    (r"^compagnies/[^/]+/branches", "Branche", "Code"),
    (r"^compagnies", "Compagnie", "Code"),
    (r"^ristournes", "Ristourne", "N°"),
    (r"^frais-generaux", "Frais généraux", "N°"),
    (r"^journaux/encaissement", "Comptabilisation journal d'encaissement", ""),
    (r"^journaux/depense", "Comptabilisation journal de dépense", ""),
    (r"^bordereaux/fusion-periode", "Fusion bordereau", ""),
    (r"^bordereaux/[^/]+/fusion", "Fusion bordereau", "N°"),
    (r"^bordereaux", "Bordereau", "N°"),
    (r"^reglements-clients", "Règlement client", "N°"),
    (r"^prestataires", "Prestataire", "Code"),
    (r"^documents", "Document", "N°"),
    (r"^access", "Accès aux modules de l'utilisateur", ""),
    (r"^admin/users", "Utilisateur", ""),
    (r"^sync", "Synchronisation", ""),
]
# Not user actions: reading a scan, the assistant, session refresh, reading the notifications.
SKIPPED = re.compile(r"^(auth/(refresh|logout)|assistant/|notifications|ocr/|.*\b(extract|ocr)[\w-]*$|records/)")
# Where the number of the record is found in the request / response body.
ID_KEYS = ("numero", "num_quittance", "nump", "num_int", "num_police", "code_client", "clientCode", "code_banque",
           "code_compagnie", "code_branche", "code_prestataire", "username", "id", "bordereau_id", "cmp")


# Consultations and états: path after /api/ -> (move, wording built from the path groups and the query string).
def _period(query: dict[str, str]) -> str:
    def fr(value: str) -> str:
        return "/".join(reversed(value[:10].split("-"))) if value else ""
    debut = fr(query.get("debut") or query.get("du") or query.get("date_debut") or "")
    fin = fr(query.get("fin") or query.get("au") or query.get("date_fin") or "")
    if debut and fin:
        return f" du {debut} au {fin}"
    return f" du {debut or fin}" if debut or fin else ""


def _company(query: dict[str, str]) -> str:
    return query.get("compagnie") or query.get("code_compagnie") or ""


KINDS = {"police": "polices", "quittance": "quittances", "technique": "technique", "divers": "divers", "moins": "moins perçus", "trop": "trop perçus"}
READS: list[tuple[str, str, Any]] = [
    (r"^clients/(?P<c>(?!next-code$)[^/]+)$", "Consultation", lambda g, q: f"Consultation du client N° {g['c']}"),
    (r"^clients/(?P<c>[^/]+)/policies$", "Consultation", lambda g, q: f"Consultation des polices du client N° {g['c']}"),
    (r"^clients/(?P<c>[^/]+)/receipts$", "Consultation", lambda g, q: f"Consultation des quittances du client N° {g['c']}"),
    (r"^clients/(?P<c>[^/]+)/claims$", "Consultation", lambda g, q: f"Consultation des sinistres du client N° {g['c']}"),
    (r"^polices/detail$", "Consultation", lambda g, q: f"Consultation de la police N° {q['num_police']}" if q.get("num_police")
        else f"Consultation des polices du client N° {q.get('code_client', '')}"),
    (r"^polices/client/(?P<c>[^/]+)$", "Consultation", lambda g, q: f"Consultation des polices du client N° {g['c']}"),
    (r"^records/policies$", "Consultation", lambda g, q: f"Consultation des polices du client N° {q['code_client']}" if q.get("code_client")
        else f"Consultation de la police N° {q['num_police']}" if q.get("num_police") else None),
    (r"^records/receipts$", "Consultation", lambda g, q: f"Consultation des quittances de la police N° {q['num_int']}" if q.get("num_int") else None),
    (r"^sinistres/(?P<n>\d+)$", "Consultation", lambda g, q: f"Consultation du sinistre N° {g['n']}"),
    (r"^ristournes/(?P<n>(?!police$)[^/]+)$", "Consultation", lambda g, q: f"Consultation de la ristourne N° {g['n']}"),
    (r"^flotte/(?P<n>[^/]+)$", "Consultation", lambda g, q: f"Consultation de la flotte police N° {g['n']}"),
    (r"^flotte/(?P<n>[^/]+)/vehicules/(?P<o>[^/]+)/sinistres$", "Consultation",
     lambda g, q: f"Consultation des sinistres du véhicule {g['o']} de la flotte police N° {g['n']}"),
    (r"^reglements-clients/client/(?P<c>[^/]+)$", "Consultation", lambda g, q: f"Consultation des règlements du client N° {g['c']}"),
    (r"^reglements-clients/(?P<n>\d+)$", "Consultation", lambda g, q: f"Consultation du règlement N° {g['n']}"),
    (r"^cheques/client/(?P<c>[^/]+)$", "Consultation", lambda g, q: f"Consultation des pièces du client N° {g['c']}"),
    (r"^cheques/instance$", "Consultation", lambda g, q: "Consultation des pièces en instance"),
    (r"^bordereaux/(?P<n>\d+)$", "Consultation", lambda g, q: f"Consultation du bordereau N° {g['n']}"),
    (r"^bordereaux/confrontation$", "Consultation",
     lambda g, q: f"Consultation Confrontation bordereau compagnie {q.get('code_compagnie', '')} période {q.get('periode', '')}"),
    (r"^prestataires/(?P<c>(?!lookups$)[^/]+)$", "Consultation", lambda g, q: f"Consultation du prestataire N° {g['c']}"),
    (r"^journaux/encaissement$", "Consultation", lambda g, q: f"Consultation Journal d'encaissement compagnie {_company(q)}{_period(q)}"),
    (r"^journaux/depense$", "Consultation", lambda g, q: f"Consultation Journal de dépense compagnie {_company(q)}{_period(q)}"),
    (r"^journaux/feuille$", "Impression", lambda g, q: f"Impression Feuille de caisse compagnie {_company(q)}{_period(q)}"),
    (r"^journaux/percus$", "Impression", lambda g, q: f"Impression Etat des {KINDS.get(q.get('type', 'moins'), '')}{_period(q)}"),
    (r"^journaux/production$", "Impression",
     lambda g, q: f"Impression Etat des opérations journalières (production des {KINDS.get(q.get('type', 'police'), '')}){_period(q)}"),
    (r"^journaux/financier$", "Impression",
     lambda g, q: f"Impression Etat financier des opérations journalières ({KINDS.get(q.get('type', 'technique'), '')}){_period(q)}"),
    (r"^impayes-agence/etat$", "Impression", lambda g, q: f"Impression Etat des impayés{_period(q)}"),
    (r"^impayes-agence/acomptes$", "Impression", lambda g, q: "Impression Etat des acomptes"),
    (r"^impayes-agence$", "Consultation", lambda g, q: "Consultation des impayés agence"),
    (r"^historique$", "Consultation", lambda g, q: f"Consultation Journal des événements{_period(q)}"),
]
# The same consultation by the same user within this many seconds is one consultation (lists reloaded as filters change).
REPEAT_SECONDS = 120


def describe_read(path: str, query: dict[str, str]) -> tuple[str, str] | None:
    rest = path.split("/api/", 1)[-1].strip("/")
    for pattern, move, wording in READS:
        found = re.match(pattern, rest)
        if found:
            design = wording(found.groupdict(), query)
            return (move, re.sub(r"\s+", " ", design).strip()[:250]) if design else None
    return None


# Path after /api/ -> the record it acts on, read before and after the change (key values from the path, then the
# request's query string and body). Most specific first.
SNAPSHOTS = [
    (r"^sinistres/(?P<n>[^/]+)/etapes/(?P<c>\d+)$", 'SELECT * FROM dbo."Sinistre_Suivi" WHERE TRIM("Num_Sinistre_Agence") = :n AND "Cmp" = CAST(:c AS integer)'),
    (r"^sinistres/(?P<n>[^/]+)/appels/(?P<c>\d+)$", 'SELECT * FROM dbo."Sinistre_Appel" WHERE TRIM("Num_Sinistre_Agence") = :n AND "Cmp" = CAST(:c AS integer)'),
    (r"^sinistres/(?P<n>[^/]+)$", 'SELECT * FROM dbo."Sinistre" WHERE TRIM("Num_Sinistre_Agence") = :n'),
    (r"^clients/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TClient" WHERE TRIM(code_client) = :n'),
    (r"^flotte/(?P<n>[^/]+)/vehicules/(?P<c>[^/]+)(/sortie)?$", 'SELECT * FROM dbo."TPoliceFlotte" WHERE TRIM(num_int) = :n AND TRIM(ordre) = :c'),
    (r"^polices/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TPolice" WHERE TRIM(num_int) = :n ORDER BY date_creation DESC NULLS LAST'),
    (r"^(?:quittances|ristournes)/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :n'),
    (r"^banques/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TBanque" WHERE TRIM(code_banque) = :n'),
    (r"^compagnies/(?P<c>[^/]+)/branches/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TBranche" WHERE CAST(code_compagnie AS text) = :c AND TRIM(code_branche) = :n'),
    (r"^compagnies/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TCompagnie" WHERE CAST(code_compagnie AS text) = :n'),
    (r"^prestataires/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TPrestataire" WHERE CAST("Code_Prestataire" AS text) = :n'),
    (r"^cheques/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :code_client'),
    (r"^frais-generaux/(?P<c>[^/]+)/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TDepense" WHERE TRIM("Typ_dep") = :c AND CAST(num_dep AS text) = :n'),
    (r"^access/users/(?P<n>[^/]+)$", 'SELECT * FROM dbo."TUserModules" WHERE "Nom_Utilisateur" = :n'),
    (r"^admin/users/(?P<n>[^/]+)$", "USERS"),
]
# Never copied into the history.
SECRET = re.compile(r"pass|pwd|^modp$|token|secret|base64|image|fichier_contenu", re.IGNORECASE)
# Updated by the software itself on every save: not a change made by the user.
TECHNICAL = {"date_m", "update_date", "update_user", "create_date", "create_user", "date_maj"}
LABELS = {
    "num_int": "N° dossier", "num_police": "N° police", "num_quittance": "N° quittance", "code_client": "Code client",
    "nom_client": "Nom client", "code_compagnie": "Compagnie", "code_branche": "Branche", "prime_net": "Prime nette",
    "prime_total": "Prime totale", "montant_quittance": "Montant quittance", "solde_quittance": "Solde quittance",
    "num_imat": "Immatriculation", "obser": "Observation", "observation": "Observation", "date_emission": "Date émission",
    "echeance": "Échéance", "effet": "Date effet", "etat": "État", "Taxe": "Taxe", "commission": "Commission",
    "Nom": "Nom", "Prenom": "Prénom", "Nationnalite": "Nationalité", "code_locuteur": "Code de contact",
    "nom_locuteur": "Nom du contact", "adresse": "Adresse", "ville": "Ville", "tel": "Téléphone", "adresse_email": "Email",
    "Date_Sinistre": "Date sinistre", "Num_Sinistre_Agence": "N° sinistre", "Cloture": "Clôturé", "Regle": "Réglé",
    "Montant_Reglement": "Montant réglé", "Montant_Devis": "Montant devis", "Code_Expert1": "Expert",
    "nom_banque": "Nom banque", "num_compte_b": "N° compte", "credit_ini": "Crédit initial", "Modules": "Modules autorisés",
}
MAX_VALUE = 300


def _label(column: str) -> str:
    return LABELS.get(column) or column.replace("_", " ").strip().capitalize()


def _value(value: Any) -> str:
    """A value as shown in the history ('' for nothing)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Oui" if value else "Non"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M") if (value.hour, value.minute) != (0, 0) else value.strftime("%d/%m/%Y")
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, float) or value.__class__.__name__ == "Decimal":
        number = float(value)
        return f"{number:.3f}".rstrip("0").rstrip(".") if number != int(number) else str(int(number))
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    text_value = str(value).strip()
    return text_value if len(text_value) <= MAX_VALUE else text_value[:MAX_VALUE] + "…"


def _clean_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {key: value for key, value in row.items() if not SECRET.search(key)}


def _snapshot(rest: str, query: dict[str, str], body: Any) -> tuple[str, dict[str, Any] | None] | None:
    """(table, the record as stored now) for a path of SNAPSHOTS, else None."""
    for pattern, sql in SNAPSHOTS:
        match = re.match(pattern, rest)
        if not match:
            continue
        params = {key: value.strip() for key, value in match.groupdict().items()}
        if ":code_client" in sql:  # a piece is keyed by its number and its client (query string on delete, body otherwise)
            sent = body.get("code_client") if isinstance(body, dict) else None
            params["code_client"] = str(query.get("code_client") or sent or "").strip()
        db = SessionLocal()
        try:
            if sql == "USERS":
                table = users_table(db)
                row = db.execute(table.select().where(table.c.Nom_Utilisateur == params["n"]).limit(1)).mappings().first()
                return "Utilisateur", _clean_row(dict(row) if row else None)
            row = db.execute(text(sql + ("" if "LIMIT" in sql else " LIMIT 1")), params).mappings().first()
            table_name = re.search(r'dbo\."([^"]+)"', sql).group(1)
            return table_name, _clean_row(dict(row) if row else None)
        except Exception:  # noqa: BLE001 - the history must never stop the action
            logger.exception("Historique : lecture de %s impossible", rest)
            return None
        finally:
            db.close()
    return None


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    changes = []
    for key in after:
        if key.lower() in TECHNICAL or key not in before:
            continue
        old, new = _value(before.get(key)), _value(after.get(key))
        if old != new:
            changes.append({"champ": _label(key), "avant": old, "apres": new})
    return changes


def _values(row: dict[str, Any]) -> list[dict[str, str]]:
    return [{"champ": _label(key), "valeur": _value(value)} for key, value in row.items()
            if key.lower() not in TECHNICAL and _value(value) != ""]


def _sent(body: Any) -> list[dict[str, str]]:
    """The data sent with the request (secrets left out)."""
    if not isinstance(body, dict):
        return []
    return [{"champ": _label(key), "valeur": _value(value)} for key, value in body.items()
            if not SECRET.search(key) and _value(value) != ""][:80]


def details(method: str, rest: str, query: dict[str, str], request_body: Any, response_body: Any,
            before: tuple[str, dict[str, Any] | None] | None, refused: bool) -> dict[str, Any] | None:
    """What exactly was done, for dbo."TMouchardDetail"."""
    password = isinstance(request_body, dict) and bool(request_body.get("password"))
    if refused:
        sent = _sent(request_body)
        return {"type": "tentative", "valeurs": sent} if sent else None
    if method == "DELETE":
        if before and before[1]:
            return {"type": "suppression", "table": before[0], "valeurs": _values(before[1])}
        return None
    after = None
    if method == "POST":
        number = ""
        if isinstance(response_body, dict):
            number = str(response_body.get("Cmp") or "") or _first_id(response_body)
        found = _snapshot(f"{rest}/{number}", query, request_body) if number else None
        if found and found[1]:
            return {"type": "creation", "table": found[0], "valeurs": _values(found[1])}
        sent = _sent(request_body)
        return {"type": "donnees", "valeurs": sent} if sent else None
    if before:
        after = _snapshot(rest, query, request_body)
    if before and before[1] and after and after[1]:
        changes = _diff(before[1], after[1])
        if password:
            changes.append({"champ": "Mot de passe", "avant": "", "apres": "modifié"})
        return {"type": "modification", "table": before[0], "changements": changes}
    sent = _sent(request_body)
    return {"type": "donnees", "valeurs": sent} if sent else None


def _user(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        return str(decode_access_token(header[7:])["sub"])
    except Exception:  # noqa: BLE001 - an invalid token is answered by the route itself
        return None


def _first_id(body: Any) -> str:
    if isinstance(body, dict):
        for key in ID_KEYS:
            value = body.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value).strip()
        for value in body.values():
            if isinstance(value, dict):
                found = _first_id(value)
                if found:
                    return found
    return ""


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def describe(method: str, path: str, request_body: Any, response_body: Any, refused: bool) -> tuple[str, str] | None:
    """(Mvt, design) of an API call, or None when it is not a user action."""
    rest = path.split("/api/", 1)[-1].strip("/")
    if SKIPPED.match(rest):
        return None
    if rest == "auth/login":
        name = (request_body or {}).get("username") if isinstance(request_body, dict) else None
        return ("Tentative", f"Tentative de connexion {name or ''}".strip()) if refused else None
    verb = VERBS.get(method)
    if not verb:
        return None
    label, number_word = next(((label, word) for pattern, label, word in ENTITIES if re.match(pattern, rest)), (rest.split("/")[0], "N°"))
    parts = rest.split("/")
    # The record's number: in the path (/quittances/341175, /sinistres/2026001/etapes/3), else in the bodies.
    path_id = next((part for part in parts[1:] if re.search(r"\d", part) and not part.isalpha()), "")
    if not path_id and len(parts) == 2:
        path_id = parts[1]  # a code without digits (banque ZZTEST…)
    if rest.startswith("compagnies/") and len(parts) > 1:
        path_id = parts[-1] if "branches" in parts and len(parts) > 3 else parts[1]
    if rest.startswith(("access/", "admin/users/")):
        path_id = parts[-1]
    number = path_id or _first_id(response_body) or _first_id(request_body)
    action = "Suppression" if verb == "Suppresion" else verb
    target = f"{label} {number_word} {number}".strip() if number and number_word else f"{label} {number}".strip()
    if label.startswith(("Comptabilisation", "Fusion", "Versement")):
        action = ""  # the label already says what was done
    if refused:
        return "Tentative", f"Tentative de {(action or label).lower()} {target if action else ''}".strip()[:250]
    return verb, f"{action} {target}".strip()[:250]


DETAIL_DDL = 'CREATE TABLE IF NOT EXISTS dbo."TMouchardDetail" ("N" integer PRIMARY KEY, "Details" jsonb NOT NULL)'
_detail_ready = False


def record(user: str, move: str, design: str, detail: Any = None) -> None:
    """Writes one line of history (and what exactly was done); a failure is logged and never breaks the user's action.
    `detail` is the details, or a function computing them (run here, after the answer was sent)."""
    global _detail_ready
    now = datetime.now(TUNIS).replace(tzinfo=None)  # the server runs in UTC; the agency works in Tunisian time
    try:
        detail = detail() if callable(detail) else detail
    except Exception:  # noqa: BLE001
        logger.exception("Historique : détail non calculé pour %s", design)
        detail = None
    db = SessionLocal()
    try:
        if detail and not _detail_ready:
            db.execute(text(DETAIL_DDL))
            db.commit()
            _detail_ready = True
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.TMouchard.N'))"))
        if move in ("Consultation", "Impression"):
            since = now - timedelta(seconds=REPEAT_SECONDS)
            repeated = db.execute(
                text('SELECT 1 FROM dbo."TMouchard" WHERE utilisateur = :u AND design = :design AND dmvt = :d AND "Hmvt" >= :h LIMIT 1'),
                {"u": user[:50], "design": design, "d": now.replace(hour=0, minute=0, second=0, microsecond=0),
                 "h": datetime(1900, 1, 1, since.hour, since.minute, since.second) if since.date() == now.date() else datetime(1900, 1, 1)},
            ).first()
            if repeated:
                db.rollback()
                return
        number = db.execute(
            text(
                'INSERT INTO dbo."TMouchard" ("N", utilisateur, "Mvt", dmvt, "Hmvt", design, code) '
                'SELECT COALESCE(MAX("N"), 0) + 1, :u, :m, :d, :h, :design, :code FROM dbo."TMouchard" RETURNING "N"'
            ),
            {"u": user[:50], "m": move, "d": now.replace(hour=0, minute=0, second=0, microsecond=0),
             "h": datetime(1900, 1, 1, now.hour, now.minute, now.second), "design": design, "code": MOVES[move]},
        ).scalar()
        if detail:
            db.execute(
                text('INSERT INTO dbo."TMouchardDetail" ("N", "Details") VALUES (:n, CAST(:d AS jsonb)) ON CONFLICT ("N") DO UPDATE SET "Details" = EXCLUDED."Details"'),
                {"n": number, "d": json.dumps(detail, ensure_ascii=False, default=str)},
            )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Historique (TMouchard) non enregistré : %s %s", move, design)
    finally:
        db.close()


async def audit_middleware(request: Request, call_next):
    method = request.method.upper()
    if method == "GET" and "/api/" in request.url.path:
        read = describe_read(request.url.path, dict(request.query_params))
        if not read:
            return await call_next(request)
        response = await call_next(request)
        user = _user(request)
        if 200 <= response.status_code < 300 and user:
            response.background = BackgroundTask(record, str(user), *read)  # written after the answer is sent
        return response
    if method not in VERBS and not (method == "POST" and request.url.path.endswith("/api/auth/login")):
        return await call_next(request)
    if "/api/" not in request.url.path:
        return await call_next(request)
    raw_request = await request.body()
    request_body = _parse(raw_request)
    rest = request.url.path.split("/api/", 1)[-1].strip("/")
    query = dict(request.query_params)
    # The record as it is before a change / deletion, to show afterwards what exactly was changed.
    before = None
    if method in ("PUT", "PATCH", "DELETE") and not SKIPPED.match(rest):
        before = await run_in_threadpool(_snapshot, rest, query, request_body)
    response = await call_next(request)
    refused = response.status_code in (401, 403, 409) if request.url.path.endswith("/auth/login") else response.status_code in (403, 409)
    if not (200 <= response.status_code < 300 or refused):
        return response
    raw_response = b"".join([chunk async for chunk in response.body_iterator])
    rebuilt = Response(content=raw_response, status_code=response.status_code, headers=dict(response.headers), media_type=response.media_type)
    response_body = _parse(raw_response)
    described = describe(method, request.url.path, request_body, response_body, refused)
    user = _user(request) or (request_body.get("username") if isinstance(request_body, dict) else None)
    if described and user:
        detail = None if rest == "auth/login" else (lambda: details(method, rest, query, request_body, response_body, before, refused))
        rebuilt.background = BackgroundTask(record, str(user), *described, detail)  # written after the answer is sent
    return rebuilt
