"""History of the users' actions in dbo."TMouchard", the legacy software's log, with its wording and codes:
1 Consultation, 2 Ajout, 3 Modification, 4 Suppresion, 5 Impression, 6 Annulation, 7 Tentative.

Every add / change / delete the API accepts is recorded by the middleware below; a refused one (403 / 409)
is recorded as a "Tentative", as is a failed sign-in.
"""
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.background import BackgroundTask
from sqlalchemy import text

from app.auth import decode_access_token
from app.database import SessionLocal

logger = logging.getLogger(__name__)

MOVES = {"Consultation": 1, "Ajout": 2, "Modification": 3, "Suppresion": 4, "Impression": 5, "Annulation": 6, "Tentative": 7}
VERBS = {"POST": "Ajout", "PUT": "Modification", "PATCH": "Modification", "DELETE": "Suppresion"}

# Path after /api/ -> (what is acted on, how its number is written), most specific first.
ENTITIES = [
    (r"^clients", "client", "Code"),
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
    (r"^bordereaux/[^/]+/fusion", "Fusion bordereau", "N°"),
    (r"^bordereaux", "Bordereau", "N°"),
    (r"^reglements-clients", "Règlement client", "N°"),
    (r"^prestataires", "Prestataire", "Code"),
    (r"^documents", "Document", "N°"),
    (r"^access", "Accès aux modules de l'utilisateur", ""),
    (r"^admin/users", "Utilisateur", ""),
    (r"^sync", "Synchronisation", ""),
]
# Not user actions: reading a scan, the assistant, session refresh.
SKIPPED = re.compile(r"^(auth/(refresh|logout)|assistant/|.*\b(extract|ocr)[\w-]*$|records/)")
# Where the number of the record is found in the request / response body.
ID_KEYS = ("numero", "num_quittance", "nump", "num_int", "num_police", "code_client", "clientCode", "code_banque",
           "code_compagnie", "code_branche", "code_prestataire", "username", "id", "bordereau_id", "cmp")


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


def record(user: str, move: str, design: str) -> None:
    """Writes one line of history; a failure is logged and never breaks the user's action."""
    now = datetime.now()
    db = SessionLocal()
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.TMouchard.N'))"))
        db.execute(
            text(
                'INSERT INTO dbo."TMouchard" ("N", utilisateur, "Mvt", dmvt, "Hmvt", design, code) '
                'SELECT COALESCE(MAX("N"), 0) + 1, :u, :m, :d, :h, :design, :code FROM dbo."TMouchard"'
            ),
            {"u": user[:50], "m": move, "d": now.replace(hour=0, minute=0, second=0, microsecond=0),
             "h": datetime(1900, 1, 1, now.hour, now.minute, now.second), "design": design, "code": MOVES[move]},
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Historique (TMouchard) non enregistré : %s %s", move, design)
    finally:
        db.close()


async def audit_middleware(request: Request, call_next):
    method = request.method.upper()
    if method not in VERBS and not (method == "POST" and request.url.path.endswith("/api/auth/login")):
        return await call_next(request)
    if "/api/" not in request.url.path:
        return await call_next(request)
    raw_request = await request.body()
    response = await call_next(request)
    refused = response.status_code in (401, 403, 409) if request.url.path.endswith("/auth/login") else response.status_code in (403, 409)
    if not (200 <= response.status_code < 300 or refused):
        return response
    raw_response = b"".join([chunk async for chunk in response.body_iterator])
    rebuilt = Response(content=raw_response, status_code=response.status_code, headers=dict(response.headers), media_type=response.media_type)
    request_body = _parse(raw_request)
    described = describe(method, request.url.path, request_body, _parse(raw_response), refused)
    user = _user(request) or (request_body.get("username") if isinstance(request_body, dict) else None)
    if described and user:
        rebuilt.background = BackgroundTask(record, str(user), *described)  # written after the answer is sent
    return rebuilt
