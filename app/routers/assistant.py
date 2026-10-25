"""AI assistant and task agent grounded in the live database.

Retrieval is done with tools (schema lookup + read-only SQL) rather than a static index, so answers always
reflect the current data. Every query runs in a read-only transaction, and the user table (password hashes)
is never reachable.
"""
import json
import re
from typing import Any, Literal

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db


router = APIRouter(prefix="/api/assistant", tags=["Assistant IA"], dependencies=[Depends(require_current_user)])

MAX_ROWS = 100
MAX_RESULT_CHARS = 14000
BLOCKED_TABLES = {settings.db_user_table.lower(), "rutilisateur_depot"}
BLOCKED_COLUMNS = {"modp"}
FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|do|execute|vacuum|analyze|"
    r"set|reset|listen|notify|lock|pg_sleep|pg_read_file|pg_ls_dir|lo_import|lo_export|dblink|nextval|setval)\b",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are the built-in assistant of "Gestion Assurances", the back-office of an insurance agency in Tunisia (Assurance Jemmali). Users are agency staff. Answer in the language of the user's message (French by default, Arabic if they write Arabic).

You can read the agency database through two tools:
- list_tables: lists the tables and columns you may query.
- run_sql: runs ONE read-only PostgreSQL SELECT (max {max_rows} rows). Tables live in schema dbo with mixed-case names, so quote them: dbo."TClient", dbo."TPolice", dbo."TQuittance", dbo."TRDetRegCli" (client settlements), dbo."TBranche", dbo."TCompagnie", dbo."Sinistre". Quote mixed-case columns too (e.g. "Periode").

How to work:
- Never invent data. If a question concerns clients, policies, receipts (quittances), settlements or claims, look it up with the tools; call list_tables first if you are unsure of a column name.
- Prefer targeted queries with WHERE and LIMIT; aggregate in SQL (COUNT, SUM) instead of pulling many rows.
- Money is in Tunisian dinars (TND, 3 decimals). Dates in answers: dd/mm/yyyy.
- You cannot modify data. If the user wants a change (create a client, a policy, a receipt), explain precisely which screen and which values to use instead.
- Never reveal or look for passwords, tokens or user accounts.
- Be concise: lead with the answer, then the key figures; use a short table or list when it helps.
{mode_rules}"""

MODE_RULES = {
    "ask": "",
    "agent": (
        "\nYou are in TASK mode: the user gives you a goal to accomplish end to end. Briefly plan, run as many queries "
        "as needed, cross-check the numbers, and finish with a structured result: a one-line summary, the findings "
        "(table or bullet list with codes/numbers so staff can act on them), and the recommended next steps. "
        "State clearly what you could not determine."
    ),
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": "List the database tables you can query with their columns and types. Optionally filter by a table name fragment.",
        "input_schema": {
            "type": "object",
            "properties": {"name_contains": {"type": "string", "description": "Optional fragment of a table name, e.g. 'Quittance'."}},
        },
    },
    {
        "name": "run_sql",
        "description": "Run a single read-only PostgreSQL SELECT (or WITH ... SELECT) and return the rows as JSON. At most 100 rows are returned.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "One SELECT statement, no trailing semicolon."}},
            "required": ["query"],
        },
    },
]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=24)
    mode: Literal["ask", "agent"] = "ask"
    client_code: str | None = Field(default=None, max_length=8)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _tool_list_tables(db: Session, name_contains: str = "") -> str:
    rows = db.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND (:frag = '' OR table_name ILIKE :like) "
            "ORDER BY table_name, ordinal_position"
        ),
        {"schema": settings.db_schema, "frag": name_contains.strip(), "like": f"%{name_contains.strip()}%"},
    ).all()
    tables: dict[str, list[str]] = {}
    for table, column, data_type in rows:
        if table.lower() in BLOCKED_TABLES or column.lower() in BLOCKED_COLUMNS:
            continue
        tables.setdefault(table, []).append(f"{column} ({data_type})")
    return _json({"schema": settings.db_schema, "tables": tables})


def _check_sql(query: str) -> str:
    sql = query.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("Empty query.")
    if ";" in sql or "--" in sql or "/*" in sql:
        raise ValueError("Only one statement without comments is allowed.")
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT queries are allowed.")
    if FORBIDDEN_SQL.search(sql):
        raise ValueError("This statement contains a forbidden keyword; only read-only queries are allowed.")
    lowered = sql.lower()
    if any(name in lowered for name in BLOCKED_TABLES) or any(name in lowered for name in BLOCKED_COLUMNS):
        raise ValueError("This table is not accessible.")
    if "information_schema" in lowered or "pg_catalog" in lowered or "pg_shadow" in lowered:
        raise ValueError("Use list_tables to inspect the schema.")
    return sql


def _tool_run_sql(db: Session, query: str) -> str:
    sql = _check_sql(query)
    db.execute(text("SET TRANSACTION READ ONLY"))
    db.execute(text("SET LOCAL statement_timeout = 8000"))
    rows = db.execute(text(f"SELECT * FROM ({sql}) AS q LIMIT {MAX_ROWS + 1}")).mappings().all()
    truncated = len(rows) > MAX_ROWS
    payload = {"row_count": min(len(rows), MAX_ROWS), "truncated": truncated, "rows": [dict(row) for row in rows[:MAX_ROWS]]}
    result = _json(payload)
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + '... [truncated: select fewer columns/rows or aggregate]'
    return result


def _run_tool(db: Session, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Returns (content, is_error). Errors go back to the model so it can correct itself."""
    try:
        if name == "list_tables":
            return _tool_list_tables(db, str(tool_input.get("name_contains", ""))), False
        if name == "run_sql":
            return _tool_run_sql(db, str(tool_input.get("query", ""))), False
        return f"Unknown tool {name}.", True
    except ValueError as exc:
        return str(exc), True
    except SQLAlchemyError as exc:
        db.rollback()
        return f"Database error: {str(getattr(exc, 'orig', exc))[:300]}", True
    finally:
        # Every tool call is its own short read-only transaction.
        db.rollback()


GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name": "list_tables",
            "description": TOOLS[0]["description"],
            "parameters": {
                "type": "OBJECT",
                "properties": {"name_contains": {"type": "STRING", "description": "Optional fragment of a table name, e.g. 'Quittance'."}},
            },
        },
        {
            "name": "run_sql",
            "description": TOOLS[1]["description"],
            "parameters": {
                "type": "OBJECT",
                "properties": {"query": {"type": "STRING", "description": "One SELECT statement, no trailing semicolon."}},
                "required": ["query"],
            },
        },
    ]
}]


def _gemini_generate(body: dict[str, Any]) -> dict[str, Any]:
    """One Gemini call (Google AI Studio key, free tier). Errors become readable HTTP errors."""
    key = settings.gemini_api_key or ""
    # "AIza..." keys use the Generative Language API; "AQ." keys are Vertex AI express keys.
    base = "https://aiplatform.googleapis.com/v1/publishers/google" if key.startswith("AQ.") else "https://generativelanguage.googleapis.com/v1beta"
    try:
        reply = httpx.post(f"{base}/models/{settings.gemini_model}:generateContent", headers={"x-goog-api-key": key}, json=body, timeout=90)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Impossible de joindre le service IA.") from exc
    if reply.status_code >= 400:
        try:
            reason = str(reply.json().get("error", {}).get("message", ""))[:200]
        except ValueError:
            reason = ""
        if reply.status_code == 429:
            raise HTTPException(status_code=429, detail="Quota gratuit atteint, réessayez dans une minute.")
        raise HTTPException(status_code=503 if reply.status_code in (400, 401, 403, 404) else 502, detail=f"Service IA ({reply.status_code}) : {reason or 'erreur'}")
    try:
        return reply.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Réponse illisible du service IA.") from exc


def _chat_gemini(request: ChatRequest, db: Session, system: str) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {"role": "user" if m.role == "user" else "model", "parts": [{"text": m.content}]} for m in request.messages
    ]
    steps: list[dict[str, Any]] = []
    max_turns = 14 if request.mode == "agent" else 7

    for _ in range(max_turns):
        data = _gemini_generate({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": GEMINI_TOOLS,
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000},
        })
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        calls = [part["functionCall"] for part in parts if "functionCall" in part]

        if not calls:
            answer = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
            if not answer and (data.get("promptFeedback") or {}).get("blockReason"):
                return {"answer": "Je ne peux pas répondre à cette demande.", "steps": steps}
            if candidate.get("finishReason") == "MAX_TOKENS":
                answer += "\n\n(Réponse tronquée : demandez-moi de continuer.)"
            return {"answer": answer or "Aucune réponse.", "steps": steps}

        # The model turn goes back unchanged (it may carry thought signatures), followed by the tool results.
        contents.append({"role": "model", "parts": parts})
        results = []
        for call in calls:
            name = str(call.get("name", ""))
            args = dict(call.get("args") or {})
            content, is_error = _run_tool(db, name, args)
            steps.append({"tool": name, "input": args, "error": is_error})
            results.append({"functionResponse": {"name": name, "response": {"error" if is_error else "result": content}}})
        contents.append({"role": "user", "parts": results})

    return {
        "answer": "J'ai atteint la limite d'étapes avant de finir. Précisez la demande ou découpez-la en plusieurs questions.",
        "steps": steps,
    }


@router.post("/chat")
def chat(request: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    system = SYSTEM_PROMPT.format(max_rows=MAX_ROWS, mode_rules=MODE_RULES[request.mode])
    if request.client_code:
        system += f'\nThe user currently has client file "{request.client_code}" open; questions like "this client" refer to code_client = \'{request.client_code}\'.'

    if not settings.anthropic_api_key:
        if settings.gemini_api_key:
            return _chat_gemini(request, db, system)
        raise HTTPException(
            status_code=503,
            detail="Assistant non configuré : définissez GEMINI_API_KEY (gratuit, Google AI Studio) ou ANTHROPIC_API_KEY sur le serveur.",
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in request.messages]
    steps: list[dict[str, Any]] = []
    max_turns = 14 if request.mode == "agent" else 7

    try:
        for _ in range(max_turns):
            response = client.messages.create(
                model=settings.assistant_model,
                max_tokens=16000,
                system=system,
                tools=TOOLS,
                output_config={"effort": "high" if request.mode == "agent" else "medium"},
                messages=messages,
            )

            if response.stop_reason == "refusal":
                return {"answer": "Je ne peux pas répondre à cette demande.", "steps": steps}

            if response.stop_reason != "tool_use":
                answer = "".join(block.text for block in response.content if block.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    answer += "\n\n(Réponse tronquée : demandez-moi de continuer.)"
                return {"answer": answer or "Aucune réponse.", "steps": steps}

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                content, is_error = _run_tool(db, block.name, dict(block.input))
                steps.append({"tool": block.name, "input": block.input, "error": is_error})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})

        return {
            "answer": "J'ai atteint la limite d'étapes avant de finir. Précisez la demande ou découpez-la en plusieurs questions.",
            "steps": steps,
        }
    except anthropic.AuthenticationError as exc:
        raise HTTPException(status_code=503, detail="Clé API de l'assistant invalide.") from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(status_code=429, detail="Assistant surchargé, réessayez dans un instant.") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=503, detail="Impossible de joindre le service IA.") from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Erreur du service IA ({exc.status_code}).") from exc


# ---------------------------------------------------------------------------------------------
# Reading a scanned / photographed client or contract file (French or Arabic)
# ---------------------------------------------------------------------------------------------
FICHE_FIELDS: dict[str, str] = {
    "last_name": "Family name of the insured / subscriber (Nom). If the document gives one combined name (Nom / RS), put the family name here.",
    "first_name": "Given name (Prénom); empty if the insured is a company or not separable.",
    "company_name": "Raison sociale when the insured is a company, else null.",
    "birth_date": "Date of birth, dd/mm/yyyy.",
    "birth_place": "Place of birth.",
    "nationality": "Nationality.",
    "profession": "Profession.",
    "id_number": "National ID card (CIN), passport or unique identifier number exactly as printed.",
    "id_date": "Issue date of the ID, dd/mm/yyyy.",
    "id_place": "Issue place of the ID.",
    "license_number": "Driving licence number.",
    "address": "Street address of the insured.",
    "city": "City.",
    "postal_code": "Postal code (4 digits in Tunisia).",
    "phone": "Phone number, digits only.",
    "email": "Email address.",
    "policy_number": "Contract / policy number (Contrat N°, عدد العقد) exactly as printed, digits only.",
    "effect_date": "Effective date (Date d'effet), dd/mm/yyyy.",
    "expiry_date": "Expiry or due date (Date d'échéance / expiration), dd/mm/yyyy.",
    "vehicle_registration": "Vehicle registration plate (Immatriculation), e.g. '209 TU 222'.",
    "net_premium": "Net contribution / prime nette for the period, number with dot decimals.",
    "taxes": "Taxes (Taxes), number with dot decimals.",
    "stamp": "Stamp duty (Timbre), number with dot decimals.",
    "commission": "Commission, number with dot decimals.",
    "total_premium": "Total amount to pay (Total / Contribution au comptant / Prime totale), number with dot decimals.",
    "period": "Period or instalment (Période, Fractionnement), as printed.",
}

FICHE_SCHEMA = {
    "type": "object",
    "properties": {key: {"type": ["string", "null"], "description": hint} for key, hint in FICHE_FIELDS.items()},
    "required": list(FICHE_FIELDS),
    "additionalProperties": False,
}

FICHE_PROMPT = (
    "This image is a photographed or scanned insurance document from Tunisia (client file, contract, quotation or receipt), "
    "in French and/or Arabic. It may be skewed, dark or partly cut off. Read it carefully and fill the requested fields. "
    "Rules: copy values exactly as printed (do not translate names or addresses); use null for anything that is not visible "
    "or that you are not confident about - never guess; dates as dd/mm/yyyy; amounts as plain numbers with a dot for decimals "
    "(e.g. '233,745DT' -> '233.745'); Arabic-Indic digits converted to Western digits."
)


class FicheRequest(BaseModel):
    image_base64: str = Field(min_length=100, max_length=9_000_000)
    media_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"] = "image/jpeg"


def _extract_fiche_gemini(request: FicheRequest) -> dict[str, Any]:
    """Free-tier alternative: Google Gemini vision (key from https://aistudio.google.com/apikey)."""
    schema = {
        "type": "OBJECT",
        "properties": {key: {"type": "STRING", "nullable": True, "description": hint} for key, hint in FICHE_FIELDS.items()},
        "required": list(FICHE_FIELDS),
    }
    body = {
        "contents": [{"parts": [
            {"text": FICHE_PROMPT},
            {"inline_data": {"mime_type": request.media_type, "data": request.image_base64}},
        ]}],
        "generationConfig": {"response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
    }
    # Google AI Studio keys ("AIza...") use the Generative Language API; keys starting with "AQ." are Vertex AI
    # express-mode keys and only work on the Vertex endpoint.
    key = settings.gemini_api_key or ""
    if key.startswith("AQ."):
        url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{settings.gemini_model}:generateContent"
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    try:
        reply = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Impossible de joindre le service de lecture.") from exc
    if reply.status_code >= 400:
        try:
            reason = str(reply.json().get("error", {}).get("message", ""))[:200]
        except ValueError:
            reason = ""
        if reply.status_code == 429:
            raise HTTPException(status_code=429, detail="Quota gratuit atteint, réessayez plus tard.")
        raise HTTPException(status_code=503 if reply.status_code in (400, 401, 403, 404) else 502, detail=f"Service de lecture ({reply.status_code}) : {reason or 'erreur'}")
    try:
        parts = reply.json()["candidates"][0]["content"]["parts"]
        data = json.loads("".join(part.get("text", "") for part in parts))
    except (KeyError, IndexError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Réponse illisible du service de lecture.") from exc
    return {"fields": {key: (data.get(key) or None) for key in FICHE_FIELDS}}


def _ocr_space(image_base64: str, media_type: str, language: str, engine: int) -> str:
    response = httpx.post(
        "https://api.ocr.space/parse/image",
        headers={"apikey": settings.ocr_space_api_key},
        data={
            "base64Image": f"data:{media_type};base64,{image_base64}",
            "language": language,
            "OCREngine": str(engine),
            "scale": "true",
            "detectOrientation": "true",
            "isTable": "true",
            **({"filetype": "PDF"} if media_type == "application/pdf" else {}),
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("IsErroredOnProcessing"):
        message = payload.get("ErrorMessage")
        raise ValueError(" ".join(message) if isinstance(message, list) else str(message or "erreur"))
    return "\n".join(item.get("ParsedText", "") for item in payload.get("ParsedResults") or [])


@router.post("/ocr-text")
def ocr_text(request: FicheRequest) -> dict[str, Any]:
    """Free OCR (OCR.space, no Google account): French and Arabic readings returned as raw text."""
    texts: list[str] = []
    problems: list[str] = []
    for language, engine in (("fre", 2), ("ara", 1)):
        try:
            text_value = _ocr_space(request.image_base64, request.media_type, language, engine).strip()
        except (httpx.HTTPError, ValueError) as exc:
            problems.append(f"{language}: {str(exc)[:150]}")
            continue
        if text_value:
            texts.append(text_value)
    if not texts:
        raise HTTPException(status_code=503, detail=f"Service OCR indisponible ({'; '.join(problems) or 'aucun texte lu'}).")
    return {"texts": texts}


@router.post("/extract-fiche")
def extract_fiche(request: FicheRequest) -> dict[str, Any]:
    if not settings.anthropic_api_key:
        if settings.gemini_api_key:
            return _extract_fiche_gemini(request)
        raise HTTPException(
            status_code=503,
            detail="Lecture IA non configurée : définissez GEMINI_API_KEY (gratuit) ou ANTHROPIC_API_KEY sur le serveur.",
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.assistant_model,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": FICHE_SCHEMA}, "effort": "medium"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document" if request.media_type == "application/pdf" else "image",
                        "source": {"type": "base64", "media_type": request.media_type, "data": request.image_base64},
                    },
                    {"type": "text", "text": FICHE_PROMPT},
                ],
            }],
        )
        if response.stop_reason == "refusal":
            raise HTTPException(status_code=422, detail="Le document n'a pas pu être analysé.")
        raw = next((block.text for block in response.content if block.type == "text"), "{}")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Réponse illisible du service IA.") from exc
    except anthropic.AuthenticationError as exc:
        raise HTTPException(status_code=503, detail="Clé API de l'assistant invalide.") from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(status_code=429, detail="Service IA surchargé, réessayez dans un instant.") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(status_code=503, detail="Impossible de joindre le service IA.") from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Erreur du service IA ({exc.status_code}).") from exc

    return {"fields": {key: (data.get(key) or None) for key in FICHE_FIELDS}}
