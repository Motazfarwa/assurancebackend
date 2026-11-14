"""Administration → WhatsApp: links the agency's WhatsApp number to the robot that sends the mémoires de règlement."""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import whatsapp_robot
from app.auth import require_admin

router = APIRouter(prefix="/api/admin/whatsapp", tags=["WhatsApp (robot)"], dependencies=[Depends(require_admin)])

QR_LIFETIME = 20  # seconds a QR code can be scanned before WhatsApp shows the next one


def _view() -> dict[str, Any]:
    state = whatsapp_robot.status()
    qr, image = state.pop("qr"), None
    if qr and state["statut"] == "qr":
        try:
            import segno
            image = segno.make_qr(qr).svg_data_uri(scale=6, border=2)
        except Exception:  # noqa: BLE001 - the text is still returned
            image = None
    state["qr_image"] = image
    shown = state.pop("qr_le", None)
    # WhatsApp stops renewing the QR code after about a minute: a new one is then asked for ("Nouveau QR code").
    state["qr_expire"] = bool(image and shown and time.time() - shown > QR_LIFETIME * 3)
    return state


@router.get("")
def robot_status() -> dict[str, Any]:
    return _view()


@router.post("/connecter")
async def robot_connect() -> dict[str, Any]:
    await run_in_threadpool(whatsapp_robot.start)
    return _view()


@router.post("/nouveau-qr")
async def robot_new_qr() -> dict[str, Any]:
    await run_in_threadpool(whatsapp_robot.restart)
    return _view()


class Pairing(BaseModel):
    telephone: str = Field(min_length=8, max_length=20)


@router.post("/code")
async def robot_pair_code(payload: Pairing) -> dict[str, Any]:
    digits = re.sub(r"\D", "", payload.telephone)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 8:
        digits = "216" + digits
    try:
        code = await run_in_threadpool(whatsapp_robot.pair_code, digits)
    except Exception as exc:  # noqa: BLE001 - shown to the administrator
        raise HTTPException(status_code=502, detail=f"Code impossible à obtenir : {exc}") from exc
    return {**_view(), "code": code}


@router.post("/deconnecter")
async def robot_logout() -> dict[str, Any]:
    await run_in_threadpool(whatsapp_robot.logout)
    return _view()
