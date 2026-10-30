"""Gestion des chèques: cheques, bills of exchange and other payment pieces of a client, and what they are meant to settle.

A piece "en instance" is stored in TPiece (the same table the legacy software uses) and its split over the client's unpaid
receipts in RPiece: it only reserves amounts. A piece "à verser" is a client settlement: it is stored in TRegCli, the receipts
it pays in RDetRegCli, and their balance (TQuittance.solde_quittance) goes down, as the legacy software does.
"""
import json
import re
from datetime import date, datetime
from typing import Any, Literal

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.config import settings
from app.database import get_db
from app.routers.assistant import FicheRequest, _gemini_generate, _ocr_space


router = APIRouter(prefix="/api/cheques", tags=["Chèques"], dependencies=[Depends(require_current_user)])

TOLERANCE = 0.001
# Piece types stored in TPiece.typep: T = traite, C = chèque, V = virement, E = espèce, B = carte bancaire.
TYPE_NAMES = {"T": "Traite", "C": "Chèque", "V": "Virement", "E": "Espèce", "B": "Carte bancaire"}


class AllocationIn(BaseModel):
    num_quittance: str = Field(min_length=1, max_length=15)
    montant: float = Field(ge=0)


class ChequeIn(BaseModel):
    code_client: str = Field(min_length=1, max_length=8)
    nump: str = Field(min_length=1, max_length=20)
    typep: Literal["T", "C", "V", "E", "B"] = "C"
    datep: date | None = None
    montant: float = Field(gt=0)
    code_banque: str | None = Field(default=None, max_length=15)
    a_verser: bool = True
    en_instance: bool = True
    # Agency account (TBanque) the piece is paid into, required "à verser".
    compte_versement: str | None = Field(default=None, max_length=15)
    allocations: list[AllocationIn] = Field(default_factory=list, max_length=500)


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key] = value
    return out


def _open_quittances(db: Session, code_client: str, exclude_nump: str | None = None) -> list[dict[str, Any]]:
    """Unpaid premium receipts (type P, not cancelled) of the client, with what other pieces "en instance" already reserve on them."""
    rows = db.execute(
        text(
            """
            SELECT TRIM(q.num_quittance) AS num_quittance, TRIM(q.num_int) AS num_int, TRIM(p.num_police) AS num_police,
                   q.echeance, COALESCE(q.solde_quittance, 0) AS solde,
                   COALESCE((
                       SELECT SUM(r."Montant_Reglement")
                       FROM dbo."RPiece" r
                       JOIN dbo."TPiece" tp ON TRIM(tp.nump) = TRIM(r."NumP")
                       WHERE TRIM(r."Num_Quittance") = TRIM(q.num_quittance)
                         AND COALESCE(tp.regle, 'N') = 'N'
                         AND (CAST(:exclude AS text) IS NULL OR TRIM(tp.nump) <> CAST(:exclude AS text))
                   ), 0) AS instance
            FROM dbo."TQuittance" q
            LEFT JOIN dbo."TPolice" p ON TRIM(p.num_int) = TRIM(q.num_int)
            WHERE TRIM(q.code_client) = :client AND COALESCE(q.solde_quittance, 0) > 0.0005
              AND TRIM(q.type_quittance) = 'P' AND COALESCE(q.annule, 'N') <> 'O'
            ORDER BY q.echeance, q.num_quittance
            """
        ),
        {"client": code_client.strip(), "exclude": exclude_nump.strip() if exclude_nump else None},
    ).mappings().all()
    return [_plain(row) for row in rows]


def _check_allocations(db: Session, payload: ChequeIn, exclude_nump: str | None = None) -> list[AllocationIn]:
    wanted = [item for item in payload.allocations if item.montant > 0]
    if not wanted:
        return []
    open_by_number = {row["num_quittance"]: row for row in _open_quittances(db, payload.code_client, exclude_nump)}
    total = 0.0
    for item in wanted:
        row = open_by_number.get(item.num_quittance.strip())
        if row is None:
            raise HTTPException(status_code=422, detail=f"La quittance {item.num_quittance} n'est pas une quittance impayée de ce client.")
        available = float(row["solde"]) - float(row["instance"])
        if item.montant > available + TOLERANCE:
            raise HTTPException(status_code=422, detail=f"Montant trop élevé pour la quittance {item.num_quittance} (reste à régler : {available:.3f}).")
        total += item.montant
    if total > payload.montant + TOLERANCE:
        raise HTTPException(status_code=422, detail=f"Le total réparti ({total:.3f}) dépasse le montant de la pièce ({payload.montant:.3f}).")
    return wanted


def _store(db: Session, payload: ChequeIn, user: str, allocations: list[AllocationIn], creating: bool) -> None:
    values = {
        "nump": payload.nump.strip(), "client": payload.code_client.strip(), "datep": payload.datep or date.today(),
        "montant": payload.montant, "typep": payload.typep, "banque": (payload.code_banque or "").strip() or None,
        # As in the legacy software, a piece en instance is not paid into the bank yet: verser = 'N'.
        "verser": "N" if payload.en_instance else "O", "regle": "N" if payload.en_instance else "O", "user": user[:20],
    }
    if creating:
        db.execute(
            text(
                'INSERT INTO dbo."TPiece" (nump, datep, dates, montant, typep, verser, code_banque, code_client, dep_rec, regle, "Create_User", "Create_Date") '
                "VALUES (:nump, :datep, now(), :montant, :typep, :verser, :banque, :client, 'R', :regle, :user, now())"
            ),
            values,
        )
    else:
        db.execute(
            text(
                'UPDATE dbo."TPiece" SET datep = :datep, montant = :montant, typep = :typep, verser = :verser, code_banque = :banque, '
                'regle = :regle, "Update_User" = :user, "Update_Date" = now() WHERE TRIM(nump) = :nump AND TRIM(code_client) = :client'
            ),
            values,
        )
        db.execute(text('DELETE FROM dbo."RPiece" WHERE TRIM("NumP") = :nump AND TRIM("Code_Client") = :client'), values)
    for item in allocations:
        db.execute(
            text(
                'INSERT INTO dbo."RPiece" ("Code_Client", "NumP", "TypeP", "Num_Quittance", "Montant_Reglement", create_user, create_date) '
                "VALUES (:client, :nump, :typep, :quittance, :montant, :user, now())"
            ),
            {**values, "quittance": item.num_quittance.strip(), "montant": item.montant},
        )


def _store_reglement(db: Session, payload: ChequeIn, user: str, allocations: list[AllocationIn]) -> int:
    """Records the piece as a client settlement (TRegCli) paying the allocated receipts (RDetRegCli, TQuittance balance)."""
    # Settlements saved at the same time from several PCs get different numbers (locks held until the commit).
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.TRegCli.num_reg_cli'))"))
    numero = db.execute(text('SELECT COALESCE(MAX(num_reg_cli), 0) + 1 FROM dbo."TRegCli"')).scalar()
    values = {
        "numero": numero, "client": payload.code_client.strip(), "typep": payload.typep, "nump": payload.nump.strip(),
        "montant": payload.montant, "compte": (payload.compte_versement or "").strip(), "user": user[:20],
    }
    db.execute(
        text(
            'INSERT INTO dbo."TRegCli" (num_reg_cli, date_reg_cli, code_client, type_reg_cli, num_piece, mont_reg_cli, code_banque, "Fectif", "Create_User", "Create_Date") '
            "VALUES (:numero, CURRENT_DATE, :client, :typep, :nump, :montant, :compte, 'N', :user, now())"
        ),
        values,
    )
    if allocations:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.RDetRegCli.Num_Recu'))"))
        recu = db.execute(text('SELECT COALESCE(MAX("Num_Recu"), 0) FROM dbo."RDetRegCli"')).scalar()
    for item in allocations:
        solde = db.execute(
            text('SELECT COALESCE(solde_quittance, 0) FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :q FOR UPDATE'), {"q": item.num_quittance.strip()}
        ).scalar() or 0
        left = round(float(solde) - item.montant, 3)
        left = 0.0 if abs(left) < TOLERANCE else left
        recu += 1
        db.execute(
            text(
                'INSERT INTO dbo."RDetRegCli" ("Num_Recu", "Num_reg_cli", "Num_quitance", "Ncpt", "Montant", "Validation", "Comptabiliser", '
                '"Acompte_Solde", "Solde_Quittance", create_user, create_date) '
                "VALUES (:recu, :numero, :quittance, :client, :montant, 'N', 'N', :acompte, :left, :user, now())"
            ),
            {**values, "recu": recu, "quittance": item.num_quittance.strip(), "montant": item.montant, "acompte": "S" if left <= 0 else "A", "left": left},
        )
        db.execute(
            text('UPDATE dbo."TQuittance" SET solde_quittance = :left, "Date_Reglement" = CURRENT_DATE WHERE TRIM(num_quittance) = :quittance'),
            {"left": left, "quittance": item.num_quittance.strip()},
        )
    return numero


def _pay_in(db: Session, payload: ChequeIn, nump: str, user: str, allocations: list[AllocationIn]) -> int:
    """The piece en instance is paid into the agency account: it becomes a client settlement, its reservations are
    released and it stays in TPiece as paid in and settled (verser = regle = 'O')."""
    numero = _store_reglement(db, payload, user, allocations)
    keys = {"nump": nump.strip(), "client": payload.code_client.strip(), "user": user[:20]}
    db.execute(text('DELETE FROM dbo."RPiece" WHERE TRIM("NumP") = :nump AND TRIM("Code_Client") = :client'), keys)
    db.execute(
        text('UPDATE dbo."TPiece" SET verser = \'O\', regle = \'O\', "Update_User" = :user, "Update_Date" = now() WHERE TRIM(nump) = :nump AND TRIM(code_client) = :client'),
        keys,
    )
    return numero


def _check_reglement(db: Session, payload: ChequeIn) -> None:
    """A piece "à verser": one status only, an agency account, and not a piece already settled for this client."""
    if payload.en_instance:
        raise HTTPException(status_code=422, detail="Choisissez un seul statut : « À verser » ou « En instance ».")
    compte = (payload.compte_versement or "").strip()
    if not compte:
        raise HTTPException(status_code=422, detail="Choisissez le compte de versement de l'agence.")
    if db.execute(text('SELECT 1 FROM dbo."TBanque" WHERE TRIM(code_banque) = :c'), {"c": compte}).first() is None:
        raise HTTPException(status_code=422, detail=f"Compte de versement {compte} inconnu.")
    if db.execute(
        text('SELECT 1 FROM dbo."TRegCli" WHERE TRIM(num_piece) = :n AND TRIM(code_client) = :c LIMIT 1'),
        {"n": payload.nump.strip(), "c": payload.code_client.strip()},
    ).first():
        raise HTTPException(status_code=409, detail=f"La pièce {payload.nump.strip()} est déjà enregistrée comme règlement de ce client.")


@router.get("/comptes")
def list_accounts(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Agency bank accounts (TBanque, not archived) a piece "à verser" can be paid into."""
    rows = db.execute(
        text('SELECT TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque FROM dbo."TBanque" WHERE COALESCE("Archive", FALSE) = FALSE ORDER BY code_banque')
    ).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/banques")
def list_banks(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(text('SELECT TRIM(code_banque) AS code_banque, TRIM(nom_banque) AS nom_banque FROM dbo."TTBanque" ORDER BY code_banque')).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/client/{code_client}")
def client_overview(code_client: str, exclude_nump: str | None = Query(default=None, max_length=20), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Client name, unpaid receipts and the pieces already recorded for the client."""
    client = db.execute(
        text('SELECT TRIM(code_client) AS code_client, TRIM(nom_client) AS nom_client FROM dbo."TClient" WHERE TRIM(code_client) = :c LIMIT 1'),
        {"c": code_client.strip()},
    ).mappings().first()
    if not client:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    pieces = db.execute(
        text(
            'SELECT TRIM(p.nump) AS nump, p.datep, p.dates, p.montant, p.typep, p.verser, p.regle, TRIM(p.code_banque) AS code_banque '
            'FROM dbo."TPiece" p WHERE TRIM(p.code_client) = :c ORDER BY p.dates DESC NULLS LAST, p.nump LIMIT 300'
        ),
        {"c": code_client.strip()},
    ).mappings().all()
    return {"client": _plain(client), "quittances": _open_quittances(db, code_client, exclude_nump), "pieces": [_plain(row) for row in pieces]}


@router.get("/piece")
def get_piece(code_client: str = Query(max_length=8), nump: str = Query(max_length=20), db: Session = Depends(get_db)) -> dict[str, Any]:
    piece = db.execute(
        text(
            'SELECT TRIM(nump) AS nump, datep, dates, montant, typep, verser, regle, TRIM(code_banque) AS code_banque, TRIM(code_client) AS code_client '
            'FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :c LIMIT 1'
        ),
        {"n": nump.strip(), "c": code_client.strip()},
    ).mappings().first()
    if not piece:
        raise HTTPException(status_code=404, detail="Pièce introuvable.")
    allocations = db.execute(
        text(
            'SELECT TRIM("Num_Quittance") AS num_quittance, "Montant_Reglement" AS montant FROM dbo."RPiece" '
            'WHERE TRIM("NumP") = :n AND TRIM("Code_Client") = :c ORDER BY "Num_Quittance"'
        ),
        {"n": nump.strip(), "c": code_client.strip()},
    ).mappings().all()
    return {"piece": _plain(piece), "allocations": [_plain(row) for row in allocations]}


@router.get("")
def list_pieces(
    code_client: str | None = Query(default=None, max_length=8),
    instance: bool = True,
    limit: int = Query(default=300, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The pieces "en instance" (not settled yet), of one client or of all of them."""
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if instance:
        conditions.append("COALESCE(p.regle, 'N') = 'N'")
    if code_client and code_client.strip():
        conditions.append("TRIM(p.code_client) = :client")
        params["client"] = code_client.strip()
    rows = db.execute(
        text(
            'SELECT TRIM(p.nump) AS nump, p.datep, p.dates, p.montant, p.typep, p.verser, p.regle, TRIM(p.code_banque) AS code_banque, '
            'TRIM(p.code_client) AS code_client, TRIM(c.nom_client) AS nom_client, TRIM(b.nom_banque) AS nom_banque '
            'FROM dbo."TPiece" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
            'LEFT JOIN dbo."TTBanque" b ON TRIM(b.code_banque) = TRIM(p.code_banque) '
            f"WHERE {' AND '.join(conditions)} ORDER BY p.dates DESC NULLS LAST, p.nump LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/instance")
def list_pieces_to_pay_in(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Chèques et traites en instance: the pieces not paid into the bank yet (verser = 'N') nor settled, by échéance."""
    rows = db.execute(
        text(
            'SELECT TRIM(p.nump) AS nump, p.datep, p.dates, p.montant, p.typep, TRIM(p.code_client) AS code_client, '
            'TRIM(c.nom_client) AS nom_client, TRIM(b.nom_banque) AS nom_banque '
            'FROM dbo."TPiece" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
            'LEFT JOIN dbo."TTBanque" b ON TRIM(b.code_banque) = TRIM(p.code_banque) '
            "WHERE COALESCE(p.verser, 'N') = 'N' AND COALESCE(p.regle, 'N') = 'N' "
            "ORDER BY p.datep NULLS LAST, p.nump LIMIT 2000"
        )
    ).mappings().all()
    return [_plain(row) for row in rows]


class VersementIn(BaseModel):
    # In the body, not the path: piece numbers may hold a "/" (e.g. BT9168333/2).
    nump: str = Field(min_length=1, max_length=20)
    code_client: str = Field(min_length=1, max_length=8)
    compte_versement: str = Field(min_length=1, max_length=15)


@router.post("/verser")
def pay_in_piece(payload: VersementIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Pays a piece en instance into an agency account (TBanque), the quittances it was reserved for being settled."""
    nump = payload.nump
    piece = db.execute(
        text(
            'SELECT TRIM(nump) AS nump, typep, datep, montant, TRIM(code_banque) AS code_banque, verser, regle '
            'FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :c'
        ),
        {"n": nump.strip(), "c": payload.code_client.strip()},
    ).mappings().first()
    if piece is None:
        raise HTTPException(status_code=404, detail="Pièce introuvable.")
    if (piece["verser"] or "N") != "N" or (piece["regle"] or "N") != "N":
        raise HTTPException(status_code=409, detail=f"La pièce {piece['nump']} est déjà versée.")
    allocations = db.execute(
        text('SELECT TRIM("Num_Quittance") AS num_quittance, "Montant_Reglement" AS montant FROM dbo."RPiece" WHERE TRIM("NumP") = :n AND TRIM("Code_Client") = :c'),
        {"n": nump.strip(), "c": payload.code_client.strip()},
    ).mappings().all()
    cheque = ChequeIn(
        code_client=payload.code_client, nump=piece["nump"], typep=piece["typep"] if piece["typep"] in TYPE_NAMES else "C",
        datep=piece["datep"].date() if piece["datep"] else None, montant=piece["montant"], code_banque=piece["code_banque"],
        a_verser=True, en_instance=False, compte_versement=payload.compte_versement,
        allocations=[AllocationIn(num_quittance=row["num_quittance"], montant=max(float(row["montant"] or 0), 0)) for row in allocations],
    )
    _check_reglement(db, cheque)
    checked = _check_allocations(db, cheque, exclude_nump=nump)
    try:
        numero = _pay_in(db, cheque, nump, str(user.get("sub", "")), checked)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Versement impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"nump": piece["nump"], "num_reg_cli": numero, "compte_versement": payload.compte_versement.strip()}


@router.post("", status_code=201)
def create_piece(payload: ChequeIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.execute(text('SELECT 1 FROM dbo."TPiece" WHERE TRIM(nump) = :n LIMIT 1'), {"n": payload.nump.strip()}).first():
        raise HTTPException(status_code=409, detail=f"Le numéro de pièce {payload.nump} existe déjà.")
    if not payload.a_verser and not payload.en_instance:
        raise HTTPException(status_code=422, detail="Le statut est obligatoire : « À verser » ou « En instance ».")
    if payload.a_verser:
        _check_reglement(db, payload)
    allocations = _check_allocations(db, payload)
    numero = None
    try:
        if payload.a_verser:
            numero = _store_reglement(db, payload, str(user.get("sub", "")), allocations)
        else:
            _store(db, payload, str(user.get("sub", "")), allocations, creating=True)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Enregistrement impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"nump": payload.nump.strip(), "num_reg_cli": numero}


@router.put("/{nump}")
def update_piece(nump: str, payload: ChequeIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    payload.nump = nump
    current = db.execute(
        text('SELECT regle FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :c'), {"n": nump.strip(), "c": payload.code_client.strip()}
    ).first()
    if current is None:
        raise HTTPException(status_code=404, detail="Pièce introuvable.")
    if (current[0] or "N") != "N":
        raise HTTPException(status_code=409, detail="Cette pièce est déjà réglée : modification impossible.")
    if not payload.a_verser and not payload.en_instance:
        raise HTTPException(status_code=422, detail="Le statut est obligatoire : « À verser » ou « En instance ».")
    if payload.a_verser:
        _check_reglement(db, payload)
    allocations = _check_allocations(db, payload, exclude_nump=nump)
    numero = None
    try:
        if payload.a_verser:
            numero = _pay_in(db, payload, nump, str(user.get("sub", "")), allocations)
        else:
            _store(db, payload, str(user.get("sub", "")), allocations, creating=False)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Modification impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"nump": nump.strip(), "num_reg_cli": numero}


@router.delete("/{nump}", status_code=204)
def delete_piece(nump: str, code_client: str = Query(max_length=8), db: Session = Depends(get_db)) -> None:
    current = db.execute(
        text('SELECT regle FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :c'), {"n": nump.strip(), "c": code_client.strip()}
    ).first()
    if current is None:
        raise HTTPException(status_code=404, detail="Pièce introuvable.")
    if (current[0] or "N") != "N":
        raise HTTPException(status_code=409, detail="Cette pièce est déjà réglée : suppression impossible.")
    try:
        db.execute(text('DELETE FROM dbo."RPiece" WHERE TRIM("NumP") = :n AND TRIM("Code_Client") = :c'), {"n": nump.strip(), "c": code_client.strip()})
        db.execute(text('DELETE FROM dbo."TPiece" WHERE TRIM(nump) = :n AND TRIM(code_client) = :c'), {"n": nump.strip(), "c": code_client.strip()})
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Suppression impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc


# ---------------------------------------------------------------------------------------------
# Reading a photographed / scanned cheque (JPG, PNG or PDF)
# ---------------------------------------------------------------------------------------------
CHEQUE_FIELDS = {
    "numero": "Cheque / draft number printed on the piece (digits, letters if any), exactly as printed.",
    "montant": "Amount in dinars as a number with a dot for decimals (Tunisian amounts have 3 decimals: '1 074,768' -> '1074.768').",
    "date_echeance": "Date of the cheque / due date of the bill, dd/mm/yyyy.",
    "banque": "Name of the bank (e.g. BIAT, STB, ATTIJARI, BNA, BT, UIB, AMEN BANK, ZITOUNA).",
    "tireur": "Name of the drawer / account holder who signs the piece.",
    "type": "'traite' for a bill of exchange (lettre de change), otherwise 'cheque'.",
}
CHEQUE_PROMPT = (
    "This image is a photographed or scanned Tunisian cheque or bill of exchange (traite), in French and/or Arabic. "
    "Read it and fill the requested fields. Copy values exactly as printed; use null for anything not visible or not certain, "
    "never guess. Amounts: use the figures box when present. Dates as dd/mm/yyyy."
)


def _extract_cheque_ai(request: FicheRequest) -> dict[str, Any]:
    if settings.anthropic_api_key:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        schema = {
            "type": "object",
            "properties": {key: {"type": ["string", "null"], "description": hint} for key, hint in CHEQUE_FIELDS.items()},
            "required": list(CHEQUE_FIELDS),
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
                    {"type": "text", "text": CHEQUE_PROMPT},
                ],
            }],
        )
        raw = next((block.text for block in response.content if block.type == "text"), "{}")
        return json.loads(raw)
    schema = {
        "type": "OBJECT",
        "properties": {key: {"type": "STRING", "nullable": True, "description": hint} for key, hint in CHEQUE_FIELDS.items()},
        "required": list(CHEQUE_FIELDS),
    }
    data = _gemini_generate({
        "contents": [{"parts": [{"text": CHEQUE_PROMPT}, {"inline_data": {"mime_type": request.media_type, "data": request.image_base64}}]}],
        "generationConfig": {"response_mime_type": "application/json", "response_schema": schema, "temperature": 0},
    })
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return json.loads("".join(part.get("text", "") for part in parts))


def _parse_cheque_text(raw: str, bank_names: list[str]) -> dict[str, Any]:
    """Rule-based reading of OCR text when no AI service is available."""
    flat = re.sub(r"\s+", " ", raw)
    fields: dict[str, Any] = {key: None for key in CHEQUE_FIELDS}
    def to_float(value: str) -> float:
        cleaned = value.replace(" ", "")
        if re.search(r",\d{3}$", cleaned):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        return float(cleaned)

    amounts = [to_float(m) for m in re.findall(r"\d{1,3}(?:[ .]\d{3})*[.,]\d{3}", flat)]
    if amounts:
        fields["montant"] = f"{max(amounts):.3f}"
    match = re.search(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{2,4})\b", flat)
    if match:
        day, month, year = match.groups()
        fields["date_echeance"] = f"{int(day):02d}/{int(month):02d}/{year if len(year) == 4 else '20' + year}"
    number = re.search(r"(?:n[°o]|num[ée]ro|cheque|ch[èe]que)\s*[:.]?\s*([A-Z]{0,4}\s?\d{6,12})", flat, re.IGNORECASE) or re.search(r"\b(\d{7,12})\b", flat)
    if number:
        fields["numero"] = re.sub(r"\s", "", number.group(1))
    lowered = flat.lower()
    fields["banque"] = next((name for name in sorted(bank_names, key=len, reverse=True) if name and name.lower() in lowered), None)
    fields["type"] = "traite" if re.search(r"traite|lettre de change|حوالة", lowered) else "cheque"
    return fields


@router.post("/extract")
def extract_cheque(request: FicheRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Reads a cheque (image or PDF) and returns the values found; the user checks them before use."""
    source = "ai"
    fields: dict[str, Any] | None = None
    if settings.anthropic_api_key or settings.gemini_api_key:
        try:
            fields = _extract_cheque_ai(request)
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
            raise HTTPException(status_code=503, detail="Lecture du chèque impossible (service OCR indisponible ou texte illisible).")
        banks = [row[0] for row in db.execute(text('SELECT TRIM(nom_banque) FROM dbo."TTBanque"')).all() if row[0]]
        fields = _parse_cheque_text("\n".join(texts), banks)
    return {"source": source, "fields": {key: (fields.get(key) or None) for key in CHEQUE_FIELDS}}
