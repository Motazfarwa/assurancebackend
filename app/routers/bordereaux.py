"""Bordereaux received from the insurers' head offices (Excel files) and their confrontation with the agency data.

Reception stores the lines of the file; confrontation compares them with what the agency already holds
(policies and receipts) and lists what does not match. Nothing in the policy / receipt tables is modified.
"""
import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db


router = APIRouter(prefix="/api/bordereaux", tags=["Bordereaux"], dependencies=[Depends(require_current_user)])

DDL = (
    """
    CREATE TABLE IF NOT EXISTS dbo."TBordereauCie" (
        id_bordereau bigserial PRIMARY KEY,
        code_compagnie integer NOT NULL,
        tc character varying(1) NOT NULL,
        periode character varying(7) NOT NULL,
        date_reception date,
        fichier character varying(200),
        nb_lignes integer NOT NULL DEFAULT 0,
        total_prime numeric(16, 3) NOT NULL DEFAULT 0,
        created_by character varying(60),
        created_at timestamp NOT NULL DEFAULT now(),
        UNIQUE (code_compagnie, tc, periode)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dbo."TBordereauLigne" (
        id_ligne bigserial PRIMARY KEY,
        id_bordereau bigint NOT NULL REFERENCES dbo."TBordereauCie" (id_bordereau) ON DELETE CASCADE,
        ligne integer NOT NULL DEFAULT 0,
        num_police character varying(30) NOT NULL,
        code_client character varying(12),
        nom_client character varying(120),
        echeance date,
        num_quittance character varying(30),
        prime_net numeric(14, 3),
        commission numeric(14, 3),
        prime_total numeric(14, 3)
    )
    """,
    'CREATE INDEX IF NOT EXISTS ix_bordereau_ligne_bordereau ON dbo."TBordereauLigne" (id_bordereau)',
)
_tables_ready = False

TOLERANCE = 0.005
LINE_COLUMNS = "id_ligne, ligne, num_police, code_client, nom_client, echeance, num_quittance, prime_net, commission, prime_total"


class LigneIn(BaseModel):
    num_police: str = Field(min_length=1, max_length=30)
    code_client: str | None = Field(default=None, max_length=12)
    nom_client: str | None = Field(default=None, max_length=120)
    echeance: date | None = None
    num_quittance: str | None = Field(default=None, max_length=30)
    prime_net: float | None = None
    commission: float | None = None
    prime_total: float | None = None


class BordereauIn(BaseModel):
    code_compagnie: int
    tc: Literal["T", "C"] = "T"
    periode: str = Field(pattern=r"^(0[1-9]|1[0-2])/[0-9]{4}$")
    date_reception: date | None = None
    fichier: str | None = Field(default=None, max_length=200)
    lignes: list[LigneIn] = Field(min_length=1, max_length=20000)


def _ensure_tables(db: Session) -> None:
    global _tables_ready
    if _tables_ready:
        return
    try:
        for statement in DDL:
            db.execute(text(statement))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail=f"Tables des bordereaux indisponibles : {str(getattr(exc, 'orig', exc))[:200]} (exécutez migrations/004_bordereaux.sql).",
        ) from exc
    _tables_ready = True


def _clean_number(value: Any) -> str:
    """'2320140843.0' / ' 2320140843 ' -> '2320140843'."""
    return re.sub(r"\.0+$", "", str(value or "").strip())


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key] = value
    return out


def _refresh_totals(db: Session, bordereau_id: int) -> None:
    db.execute(
        text(
            'UPDATE dbo."TBordereauCie" SET '
            'nb_lignes = (SELECT COUNT(*) FROM dbo."TBordereauLigne" WHERE id_bordereau = :i), '
            'total_prime = COALESCE((SELECT SUM(prime_total) FROM dbo."TBordereauLigne" WHERE id_bordereau = :i), 0) '
            "WHERE id_bordereau = :i"
        ),
        {"i": bordereau_id},
    )


def _line_params(line: LigneIn, number: int) -> dict[str, Any]:
    return {
        "ligne": number,
        "num_police": _clean_number(line.num_police),
        "code_client": (line.code_client or "").strip() or None,
        "nom_client": (line.nom_client or "").strip() or None,
        "echeance": line.echeance,
        "num_quittance": _clean_number(line.num_quittance) or None,
        "prime_net": line.prime_net,
        "commission": line.commission,
        "prime_total": line.prime_total,
    }


INSERT_LINE = (
    'INSERT INTO dbo."TBordereauLigne" (id_bordereau, ligne, num_police, code_client, nom_client, echeance, num_quittance, prime_net, commission, prime_total) '
    "VALUES (:id_bordereau, :ligne, :num_police, :code_client, :nom_client, :echeance, :num_quittance, :prime_net, :commission, :prime_total)"
)


@router.post("", status_code=201)
def save_bordereau(payload: BordereauIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Stores a received bordereau; a bordereau already stored for the same company / type / period is replaced."""
    _ensure_tables(db)
    try:
        db.execute(
            text('DELETE FROM dbo."TBordereauCie" WHERE code_compagnie = :c AND tc = :t AND periode = :p'),
            {"c": payload.code_compagnie, "t": payload.tc, "p": payload.periode},
        )
        bordereau_id = db.execute(
            text(
                'INSERT INTO dbo."TBordereauCie" (code_compagnie, tc, periode, date_reception, fichier, created_by) '
                "VALUES (:c, :t, :p, :d, :f, :u) RETURNING id_bordereau"
            ),
            {
                "c": payload.code_compagnie, "t": payload.tc, "p": payload.periode,
                "d": payload.date_reception or date.today(), "f": payload.fichier, "u": str(user.get("sub", ""))[:60],
            },
        ).scalar_one()
        rows = [{"id_bordereau": bordereau_id, **_line_params(line, number)} for number, line in enumerate(payload.lignes, start=1)]
        db.execute(text(INSERT_LINE), rows)
        _refresh_totals(db, bordereau_id)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Enregistrement impossible : {str(getattr(exc, 'orig', exc))[:250]}") from exc
    return {"id_bordereau": bordereau_id, "nb_lignes": len(payload.lignes)}


@router.get("")
def list_bordereaux(
    code_compagnie: int | None = None,
    tc: str | None = Query(default=None, pattern="^[TC]$"),
    periode: str | None = Query(default=None, pattern=r"^[0-9]{2}/[0-9]{4}$"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _ensure_tables(db)
    conditions = ["TRUE"]
    params: dict[str, Any] = {}
    for column, value in (("code_compagnie", code_compagnie), ("tc", tc), ("periode", periode)):
        if value is not None:
            conditions.append(f"b.{column} = :{column}")
            params[column] = value
    rows = db.execute(
        text(
            'SELECT b.*, c.nom_compagnie FROM dbo."TBordereauCie" b LEFT JOIN dbo."TCompagnie" c ON c.code_compagnie = b.code_compagnie '
            f"WHERE {' AND '.join(conditions)} ORDER BY b.created_at DESC LIMIT 200"
        ),
        params,
    ).mappings().all()
    return [_plain(row) for row in rows]


@router.get("/confrontation")
def confrontation(
    code_compagnie: int,
    tc: str = Query(default="T", pattern="^[TC]$"),
    periode: str = Query(pattern=r"^[0-9]{2}/[0-9]{4}$"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compares the stored bordereau with the agency's policies and receipts for the same company and period."""
    _ensure_tables(db)
    header = db.execute(
        text(
            'SELECT b.*, c.nom_compagnie FROM dbo."TBordereauCie" b LEFT JOIN dbo."TCompagnie" c ON c.code_compagnie = b.code_compagnie '
            "WHERE b.code_compagnie = :c AND b.tc = :t AND b.periode = :p"
        ),
        {"c": code_compagnie, "t": tc, "p": periode},
    ).mappings().first()
    if not header:
        raise HTTPException(status_code=404, detail="Aucun bordereau reçu pour cette compagnie et cette période. Importez-le d'abord (Réception bordereau).")

    month, year = int(periode[:2]), int(periode[3:])
    first_day = date(year, month, 1)
    next_first = date(year + (month == 12), month % 12 + 1, 1)

    lines = [dict(row) for row in db.execute(
        text(f'SELECT {LINE_COLUMNS} FROM dbo."TBordereauLigne" WHERE id_bordereau = :i ORDER BY ligne'),
        {"i": header["id_bordereau"]},
    ).mappings().all()]

    policy_select = (
        'SELECT p.num_int, TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client, c.nom_client, '
        'TRIM(p.echeance) AS echeance, p."Unite" AS unite, p.duree, UPPER(TRIM(p.etat)) AS etat, TRIM(p.code_branche) AS code_branche, '
        "p.code_compagnie, p.effet, p.expiration "
        'FROM dbo."TPolice" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
    )

    # What the agency expects for the period: policies falling due (terme) or receipts issued in the month (comptant).
    if tc == "T":
        expected = db.execute(
            text(
                policy_select
                + "WHERE p.code_compagnie = :c AND UPPER(TRIM(p.etat)) = 'AN' AND split_part(TRIM(p.echeance), '/', 2) = :mm "
                "AND p.effet < :next_first AND (p.expiration IS NULL OR p.expiration >= :first_day) "
                "ORDER BY TRIM(p.num_police)"
            ),
            {"c": code_compagnie, "mm": f"{month:02d}", "next_first": next_first, "first_day": first_day},
        ).mappings().all()
    else:
        expected = db.execute(
            text(
                policy_select
                + 'WHERE p.code_compagnie = :c AND EXISTS (SELECT 1 FROM dbo."TQuittance" q WHERE TRIM(q.num_int) = TRIM(p.num_int) '
                "AND q.tc = 'C' AND q.date_emission >= :first_day AND q.date_emission < :next_first) ORDER BY TRIM(p.num_police)"
            ),
            {"c": code_compagnie, "next_first": next_first, "first_day": first_day},
        ).mappings().all()
    expected_numbers = {row["num_police"] for row in expected}

    # The policies named by the bordereau (whatever their company), in chunks to keep the IN list small.
    numbers = sorted({_clean_number(line["num_police"]) for line in lines})
    found: dict[str, Any] = {}
    for start in range(0, len(numbers), 1000):
        chunk = numbers[start:start + 1000]
        statement = text(policy_select + "WHERE TRIM(p.num_police) IN :numbers").bindparams(bindparam("numbers", expanding=True))
        for row in db.execute(statement, {"numbers": chunk}).mappings().all():
            found.setdefault(row["num_police"], row)

    # Receipts the agency already holds for these policies, to compare amounts.
    quittance_numbers = sorted({_clean_number(line["num_quittance"]) for line in lines if line["num_quittance"]})
    receipts_by_number: dict[str, Any] = {}
    for start in range(0, len(quittance_numbers), 1000):
        chunk = quittance_numbers[start:start + 1000]
        statement = text(
            'SELECT TRIM(num_quittance) AS num_quittance, TRIM(num_int) AS num_int, prime_net, commission, prime_total FROM dbo."TQuittance" '
            "WHERE TRIM(num_quittance) IN :numbers"
        ).bindparams(bindparam("numbers", expanding=True))
        for row in db.execute(statement, {"numbers": chunk}).mappings().all():
            receipts_by_number[row["num_quittance"]] = row
    receipts_by_policy: dict[str, Any] = {}
    internal_ids = sorted({str(found[number]["num_int"]).strip() for number in numbers if number in found})
    for start in range(0, len(internal_ids), 1000):
        chunk = internal_ids[start:start + 1000]
        statement = text(
            'SELECT TRIM(num_quittance) AS num_quittance, TRIM(num_int) AS num_int, prime_net, commission, prime_total FROM dbo."TQuittance" '
            "WHERE TRIM(num_int) IN :ids AND echeance >= :first_day AND echeance < :next_first ORDER BY echeance DESC"
        ).bindparams(bindparam("ids", expanding=True))
        for row in db.execute(statement, {"ids": chunk, "first_day": first_day, "next_first": next_first}).mappings().all():
            receipts_by_policy.setdefault(row["num_int"], row)

    branch_names = {
        str(row["code_branche"]).strip(): row["nom_branche"]
        for row in db.execute(
            text('SELECT code_branche, nom_branche FROM dbo."TBranche" WHERE code_compagnie = :c'), {"c": code_compagnie}
        ).mappings().all()
    }

    def differs(theirs: Any, ours: Any) -> bool:
        return theirs is not None and ours is not None and abs(float(theirs) - float(ours)) > TOLERANCE

    results = []
    seen: set[str] = set()
    for line in lines:
        number = _clean_number(line["num_police"])
        policy = found.get(number)
        reasons: list[str] = []
        status = "ok"
        if policy is None:
            status = "inconnue"
            reasons.append("Police inconnue chez nous")
        else:
            if policy["code_compagnie"] != code_compagnie:
                status = "ecart"
                reasons.append("Police d'une autre compagnie chez nous")
            if line["code_client"] and str(line["code_client"]).strip() != (policy["code_client"] or ""):
                reasons.append(f"Code client différent (chez nous : {policy['code_client']})")
            if tc == "T" and line["echeance"] and policy["echeance"]:
                theirs = f"{line['echeance'].day:02d}/{line['echeance'].month:02d}"
                if theirs != policy["echeance"]:
                    reasons.append(f"Échéance différente (chez nous : {policy['echeance']})")
            if policy["etat"] != "AN":
                reasons.append(f"Police {policy['etat'] or 'sans état'} chez nous")
            if tc == "T" and number not in expected_numbers and policy["etat"] == "AN":
                reasons.append("Hors terme pour cette période chez nous")
            receipt = receipts_by_number.get(_clean_number(line["num_quittance"])) if line["num_quittance"] else None
            receipt = receipt or receipts_by_policy.get(str(policy["num_int"]).strip())
            if receipt:
                for label, theirs, ours in (
                    ("Prime nette", line["prime_net"], receipt["prime_net"]),
                    ("Commission", line["commission"], receipt["commission"]),
                    ("Prime totale", line["prime_total"], receipt["prime_total"]),
                ):
                    if differs(theirs, ours):
                        reasons.append(f"{label} différente (bordereau {float(theirs):.3f} / chez nous {float(ours):.3f})")
        if number in seen:
            reasons.append("Police en double dans le bordereau")
        seen.add(number)
        if reasons and status == "ok":
            status = "ecart"
        results.append(_plain({
            **line,
            "statut": status,
            "motifs": reasons,
            "nom_client_notre": policy["nom_client"] if policy else None,
            "code_client_notre": policy["code_client"] if policy else None,
            "code_branche": policy["code_branche"] if policy else None,
        }))

    received = set(numbers)
    not_received = [_plain({**row, "observation": "Terme non parvenu"}) for row in expected if row["num_police"] not in received]

    # Totals per branch code: bordereau side and what the agency expected.
    branches: dict[str, dict[str, Any]] = {}

    def branch_entry(code: str | None) -> dict[str, Any]:
        key = code or "—"
        return branches.setdefault(key, {
            "code_branche": key, "nom_branche": branch_names.get(key, ""),
            "nb_bordereau": 0, "prime_net": 0.0, "commission": 0.0, "prime_total": 0.0,
            "nb_attendues": 0, "nb_non_parvenues": 0,
        })

    for row in expected:
        branch_entry(row["code_branche"])["nb_attendues"] += 1
    for row in not_received:
        branch_entry(row["code_branche"])["nb_non_parvenues"] += 1
    for row in results:
        entry = branch_entry(row["code_branche"])
        entry["nb_bordereau"] += 1
        for key in ("prime_net", "commission", "prime_total"):
            entry[key] += float(row[key] or 0)

    counts = {name: sum(1 for row in results if row["statut"] == name) for name in ("ok", "ecart", "inconnue")}
    summary = {
        "nb_lignes": len(results),
        "nb_ok": counts["ok"],
        "nb_ecarts": counts["ecart"],
        "nb_inconnues": counts["inconnue"],
        "nb_non_parvenus": len(not_received),
        "nb_attendues": len(expected),
        "total_prime_net": sum(float(row["prime_net"] or 0) for row in results),
        "total_commission": sum(float(row["commission"] or 0) for row in results),
        "total_prime_total": sum(float(row["prime_total"] or 0) for row in results),
        "fusion": counts["ecart"] == 0 and counts["inconnue"] == 0 and not not_received,
    }
    return {
        "bordereau": _plain(header),
        "resume": summary,
        "lignes": results,
        "non_parvenus": not_received,
        "branches": sorted(branches.values(), key=lambda entry: entry["code_branche"]),
    }


@router.get("/{bordereau_id}")
def get_bordereau(bordereau_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _ensure_tables(db)
    header = db.execute(
        text('SELECT b.*, c.nom_compagnie FROM dbo."TBordereauCie" b LEFT JOIN dbo."TCompagnie" c ON c.code_compagnie = b.code_compagnie WHERE b.id_bordereau = :i'),
        {"i": bordereau_id},
    ).mappings().first()
    if not header:
        raise HTTPException(status_code=404, detail="Bordereau introuvable.")
    lines = db.execute(
        text(f'SELECT {LINE_COLUMNS} FROM dbo."TBordereauLigne" WHERE id_bordereau = :i ORDER BY ligne'), {"i": bordereau_id}
    ).mappings().all()
    return {"bordereau": _plain(header), "lignes": [_plain(line) for line in lines]}


@router.delete("/{bordereau_id}", status_code=204)
def delete_bordereau(bordereau_id: int, db: Session = Depends(get_db)) -> None:
    _ensure_tables(db)
    db.execute(text('DELETE FROM dbo."TBordereauCie" WHERE id_bordereau = :i'), {"i": bordereau_id})
    db.commit()
