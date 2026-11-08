"""Notifications (the bell of the workspace): the latest actions of dbo."TMouchard".

The administrator is notified of every user's actions; any other user only of their own. What a user has
already seen is kept in dbo."TNotifLu" (the last "N" seen), so the unread count follows them on any computer.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.routers.access import is_admin

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])

SEEN_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TNotifLu" (
        "Nom_Utilisateur" varchar(50) PRIMARY KEY,
        "Dernier_N" integer NOT NULL DEFAULT 0
    )
"""
_ready = False


def _ensure(db: Session) -> None:
    global _ready
    if not _ready:
        db.execute(text(SEEN_DDL))
        db.commit()
        _ready = True


def _scope(user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """SQL condition limiting the history to what this user is notified of."""
    if is_admin(user):
        return "TRUE", {}
    return "LOWER(TRIM(utilisateur)) = LOWER(TRIM(:me))", {"me": str(user["sub"])}


def _last_seen(db: Session, name: str, scope: str, params: dict[str, Any]) -> int:
    row = db.execute(text('SELECT "Dernier_N" FROM dbo."TNotifLu" WHERE "Nom_Utilisateur" = :u'), {"u": name}).first()
    if row:
        return int(row[0])
    # First time: the past history is not "new"; only what happens from now on is.
    latest = db.execute(text(f'SELECT COALESCE(MAX("N"), 0) FROM dbo."TMouchard" WHERE {scope}'), params).scalar() or 0
    db.execute(text('INSERT INTO dbo."TNotifLu" ("Nom_Utilisateur", "Dernier_N") VALUES (:u, :n) ON CONFLICT DO NOTHING'), {"u": name, "n": latest})
    db.commit()
    return int(latest)


@router.get("")
def my_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    user: dict[str, Any] = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _ensure(db)
    name = str(user["sub"])
    scope, params = _scope(user)
    seen = _last_seen(db, name, scope, params)
    unread = db.execute(text(f'SELECT COUNT(*) FROM dbo."TMouchard" WHERE {scope} AND "N" > :seen'), {**params, "seen": seen}).scalar()
    rows = db.execute(
        text(f'SELECT "N" AS n, utilisateur, "Mvt" AS mvt, dmvt, "Hmvt" AS hmvt, design, code FROM dbo."TMouchard" WHERE {scope} ORDER BY "N" DESC LIMIT :limit'),
        {**params, "limit": limit},
    ).mappings().all()
    items = [{
        "n": row["n"], "utilisateur": row["utilisateur"], "mvt": row["mvt"], "design": row["design"], "code": row["code"], "unread": row["n"] > seen,
        "date": row["dmvt"].date().isoformat() if row["dmvt"] else None,
        "heure": row["hmvt"].strftime("%H:%M") if row["hmvt"] else None,
    } for row in rows]
    return {"unread": unread, "items": items}


# Clients to complete (administrator), what each one lacks:
#  - coordonnées: an address, city and postal code (at home or at the office), and at least one phone and one email on
#    the address chosen for correspondence (Correspondant 'B': the office, otherwise home);
#  - polices: no police at all, or a police in force (etat AN) without company, code branche, effect date, risk, or its
#    term (échéance if renewable, expiration if not);
#  - quittances of a police in force without date d'émission, échéance, prime nette, prime totale, montant or solde;
#  - sinistres not closed without date, police, company or the company's claim number.
_PHONE = "(CONCAT({cols}) ~ '[0-9]{{4}}' OR COALESCE(to_jsonb(k)->>'{json}', '') ~ '\"value\": \"[^\"]*[0-9]{{4}}')"
_EMAIL = "(CONCAT({cols}) ~ '@' OR COALESCE(to_jsonb(k)->>'{json}', '') ~ '\"value\": \"[^\"]*@')"
PHONE_HOME = _PHONE.format(cols="k.tel, k.tel1, k.tel2, k.tel3", json="telephones_domicile")
PHONE_OFFICE = _PHONE.format(cols="k.tel_b, k.tel_b2, k.tel_b3, k.tel_b4", json="telephones_bureau")
EMAIL_HOME = _EMAIL.format(cols="k.email1, k.email2, k.email3", json="emails_domicile")
EMAIL_OFFICE = _EMAIL.format(cols="k.adresse_email, k.emailb2, k.emailb3", json="emails_bureau")

INCOMPLETE_SQL = f"""
    WITH pol AS (
        SELECT code, COALESCE(ARRAY_AGG(numero || ' : ' || ARRAY_TO_STRING(manque, ', ') ORDER BY numero) FILTER (WHERE CARDINALITY(manque) > 0), ARRAY[]::text[]) AS polices
        FROM (
            SELECT TRIM(p.code_client) AS code, TRIM(p.num_police) AS numero,
                   CASE WHEN UPPER(TRIM(p.etat)) = 'AN' THEN ARRAY_REMOVE(ARRAY[
                       CASE WHEN p.code_compagnie IS NULL THEN 'compagnie' END,
                       CASE WHEN TRIM(COALESCE(p.code_branche, '')) = '' THEN 'code branche' END,
                       CASE WHEN p.effet IS NULL THEN 'date effet' END,
                       CASE WHEN TRIM(COALESCE(p.risque, '')) = '' THEN 'risque' END,
                       CASE WHEN TRIM(COALESCE(p."Renouv_An", '')) = 'O' AND TRIM(COALESCE(p.echeance, '')) = '' THEN 'échéance' END,
                       CASE WHEN TRIM(COALESCE(p."Renouv_An", '')) <> 'O' AND p.expiration IS NULL THEN 'expiration' END
                   ], NULL) ELSE ARRAY[]::text[] END AS manque
            FROM dbo."TPolice" p
        ) x
        GROUP BY 1
    ), quit AS (
        SELECT TRIM(q.code_client) AS code, ARRAY_AGG(TRIM(q.num_quittance) || ' : ' || ARRAY_TO_STRING(ARRAY_REMOVE(ARRAY[
                   CASE WHEN q.date_emission IS NULL THEN 'date émission' END, CASE WHEN q.echeance IS NULL THEN 'échéance' END,
                   CASE WHEN q.prime_net IS NULL THEN 'prime nette' END, CASE WHEN q.prime_total IS NULL THEN 'prime totale' END,
                   CASE WHEN q.montant_quittance IS NULL THEN 'montant' END, CASE WHEN q.solde_quittance IS NULL THEN 'solde' END
               ], NULL), ', ') ORDER BY TRIM(q.num_quittance)) AS quittances
        FROM dbo."TQuittance" q
        JOIN dbo."TPolice" p ON TRIM(p.num_int) = TRIM(q.num_int) AND UPPER(TRIM(p.etat)) = 'AN'
        WHERE q.date_emission IS NULL OR q.echeance IS NULL OR q.prime_net IS NULL OR q.prime_total IS NULL
           OR q.montant_quittance IS NULL OR q.solde_quittance IS NULL
        GROUP BY 1
    ), sin AS (
        SELECT TRIM(s."Code_Client") AS code, ARRAY_AGG(TRIM(s."Num_Sinistre_Agence"::text) || ' : ' || ARRAY_TO_STRING(ARRAY_REMOVE(ARRAY[
                   CASE WHEN s."Date_Sinistre" IS NULL THEN 'date' END, CASE WHEN TRIM(COALESCE(s."Num_Int", '')) = '' THEN 'police' END,
                   CASE WHEN s."Code_Compagnie_Assure" IS NULL THEN 'compagnie' END,
                   CASE WHEN TRIM(COALESCE(s."Num_Sinistre_Compagnie_Assure", '')) = '' THEN 'N° sinistre compagnie' END
               ], NULL), ', ') ORDER BY s."Num_Sinistre_Agence") AS sinistres
        FROM dbo."Sinistre" s
        WHERE COALESCE(s."Cloture", FALSE) = FALSE AND (
            s."Date_Sinistre" IS NULL OR TRIM(COALESCE(s."Num_Int", '')) = '' OR s."Code_Compagnie_Assure" IS NULL
            OR TRIM(COALESCE(s."Num_Sinistre_Compagnie_Assure", '')) = '')
        GROUP BY 1
    ), c AS (
        SELECT TRIM(k.code_client) AS code_client, TRIM(k.nom_client) AS nom_client,
               ARRAY_REMOVE(ARRAY[
                   CASE WHEN TRIM(COALESCE(k.adresse, '')) = '' AND TRIM(COALESCE(k.adresse_b, '')) = '' THEN 'adresse' END,
                   CASE WHEN TRIM(COALESCE(k.ville, '')) = '' AND TRIM(COALESCE(k.ville_b, '')) = '' THEN 'ville' END,
                   CASE WHEN COALESCE(k.cp, 0) = 0 AND COALESCE(k.cp_b, 0) = 0 THEN 'code postal' END,
                   CASE WHEN TRIM(COALESCE(k."Correspondant", '')) = 'B'
                        THEN CASE WHEN NOT {PHONE_OFFICE} THEN 'téléphone (bureau)' END
                        ELSE CASE WHEN NOT {PHONE_HOME} THEN 'téléphone (domicile)' END END,
                   CASE WHEN TRIM(COALESCE(k."Correspondant", '')) = 'B'
                        THEN CASE WHEN NOT {EMAIL_OFFICE} THEN 'email (bureau)' END
                        ELSE CASE WHEN NOT {EMAIL_HOME} THEN 'email (domicile)' END END
               ], NULL) AS coordonnees,
               pol.code IS NULL AS sans_police,
               COALESCE(pol.polices, ARRAY[]::text[]) AS polices,
               COALESCE(quit.quittances, ARRAY[]::text[]) AS quittances,
               COALESCE(sin.sinistres, ARRAY[]::text[]) AS sinistres
        FROM dbo."TClient" k
        LEFT JOIN pol ON pol.code = TRIM(k.code_client)
        LEFT JOIN quit ON quit.code = TRIM(k.code_client)
        LEFT JOIN sin ON sin.code = TRIM(k.code_client)
    )
    SELECT * FROM c
    WHERE CARDINALITY(coordonnees) > 0 OR sans_police OR CARDINALITY(polices) > 0 OR CARDINALITY(quittances) > 0 OR CARDINALITY(sinistres) > 0
    ORDER BY CASE WHEN code_client ~ '^[0-9]+$' THEN CAST(code_client AS bigint) END DESC NULLS LAST, code_client
"""


@router.get("/clients-incomplets")
def incomplete_clients(
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Accès réservé à l'administrateur.")
    rows = db.execute(text(INCOMPLETE_SQL)).mappings().all()
    items = []
    for row in rows[:limit]:
        manques = list(row["coordonnees"])
        if row["sans_police"]:
            manques.append("aucune police")
        # "police 074202646653 : code branche, risque"
        manques.extend(f"police {entry}" for entry in row["polices"][:3])
        manques.extend(f"quittance {entry}" for entry in row["quittances"][:3])
        manques.extend(f"sinistre {entry}" for entry in row["sinistres"][:3])
        items.append({"code_client": row["code_client"], "nom_client": row["nom_client"], "manques": manques})
    return {
        "total": len(rows),
        "coordonnees": sum(1 for row in rows if row["coordonnees"]),
        "sans_police": sum(1 for row in rows if row["sans_police"]),
        "polices": sum(1 for row in rows if row["polices"]),
        "quittances": sum(1 for row in rows if row["quittances"]),
        "sinistres": sum(1 for row in rows if row["sinistres"]),
        "items": items,
    }


@router.put("/lu")
def mark_read(user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything up to now has been seen."""
    _ensure(db)
    scope, params = _scope(user)
    latest = db.execute(text(f'SELECT COALESCE(MAX("N"), 0) FROM dbo."TMouchard" WHERE {scope}'), params).scalar() or 0
    db.execute(
        text(
            'INSERT INTO dbo."TNotifLu" ("Nom_Utilisateur", "Dernier_N") VALUES (:u, :n) '
            'ON CONFLICT ("Nom_Utilisateur") DO UPDATE SET "Dernier_N" = GREATEST(dbo."TNotifLu"."Dernier_N", :n)'
        ),
        {"u": str(user["sub"]), "n": latest},
    )
    db.commit()
    return {"unread": 0}
