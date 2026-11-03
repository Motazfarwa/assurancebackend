"""OCR of the imported documents (no AI): the text of a PDF or an image, printed and handwritten.

Azure AI Vision "Read" recognises printed and handwritten text (French included) on images and PDFs; it only returns
the text, which each module's own parser then analyses. Without AZURE_VISION_ENDPOINT / AZURE_VISION_KEY the
frontend reads the document with its own OCR (printed text only).
"""
import base64
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_current_user
from app.config import settings
from app.routers.assistant import FicheRequest

router = APIRouter(prefix="/api/ocr", tags=["OCR"], dependencies=[Depends(require_current_user)])
logger = logging.getLogger(__name__)

READ_TIMEOUT = 120  # seconds for Azure to read the document (a few seconds for one page)


def _configured() -> bool:
    return bool(settings.azure_vision_endpoint and settings.azure_vision_key)


@router.get("/status")
def ocr_status() -> dict[str, Any]:
    """Whether the server OCR (printed + handwritten) is available; otherwise the browser OCR is used."""
    return {"handwriting": _configured(), "engine": "azure-read" if _configured() else None}


@router.post("/text")
def read_text(request: FicheRequest) -> dict[str, Any]:
    """The text of the document, page by page, line by line, as Azure Read recognises it."""
    if not _configured():
        raise HTTPException(status_code=501, detail="OCR serveur non configuré (AZURE_VISION_ENDPOINT, AZURE_VISION_KEY).")
    endpoint = settings.azure_vision_endpoint.rstrip("/")
    headers = {"Ocp-Apim-Subscription-Key": settings.azure_vision_key, "Content-Type": "application/octet-stream"}
    started = time.monotonic()
    try:
        submitted = httpx.post(f"{endpoint}/vision/v3.2/read/analyze", headers=headers,
                               content=base64.b64decode(request.image_base64), timeout=30)
        if submitted.status_code != 202:
            raise HTTPException(status_code=502, detail=f"OCR Azure ({submitted.status_code}) : {_reason(submitted)}")
        operation = submitted.headers["Operation-Location"]
        while True:  # Azure reads asynchronously: the result is fetched once it is ready
            time.sleep(1)
            answer = httpx.get(operation, headers={"Ocp-Apim-Subscription-Key": settings.azure_vision_key}, timeout=30)
            body = answer.json()
            status = body.get("status")
            if status == "succeeded":
                break
            if status == "failed" or answer.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"OCR Azure : lecture impossible ({_reason(answer)})")
            if time.monotonic() - started > READ_TIMEOUT:
                raise HTTPException(status_code=504, detail=f"OCR Azure : pas de résultat en {READ_TIMEOUT} secondes.")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"OCR Azure injoignable ({exc.__class__.__name__}).") from exc
    pages = body.get("analyzeResult", {}).get("readResults", [])
    text = "\n\n".join("\n".join(line.get("text", "") for line in page.get("lines", [])) for page in pages)
    logger.info("OCR Azure : %s page(s), %s caractères en %.1f s", len(pages), len(text), time.monotonic() - started)
    return {"text": text, "engine": "azure-read", "pages": len(pages)}


def _reason(answer: httpx.Response) -> str:
    try:
        error = answer.json().get("error", {})
        return str(error.get("message") or error.get("code") or answer.status_code)[:200]
    except ValueError:
        return str(answer.status_code)
