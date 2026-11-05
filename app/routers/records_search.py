"""Search of policies and receipts across clients (filters of the "Polices du client" / "Quittances de la police" windows)."""
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.routers.client_records import serialize
from app.routers.flotte import ACTIVE_SQL


router = APIRouter(prefix="/api/records", tags=["Recherche polices et quittances"], dependencies=[Depends(require_current_user)])

BRANCH_EXPRESSION = "COALESCE(NULLIF(TRIM(q.code_branche), ''), TRIM(p.code_branche))"

# Policy status as in the legacy "Liste des polices" (Tous / En vigueur / Susp. Rés. / Expirées):
# AN = in force (expired once its expiration date is past), SS / RE / RV = suspended or terminated.
STATUT_SQL = {
    "vigueur": "UPPER(TRIM(p.etat)) = 'AN' AND (p.expiration IS NULL OR p.expiration >= CURRENT_DATE)",
    "expirees": "UPPER(TRIM(p.etat)) = 'AN' AND p.expiration < CURRENT_DATE",
    "susp": "UPPER(TRIM(p.etat)) IN ('SS', 'RE', 'RV')",
}


def _like(value: str) -> str:
    return f"%{value.strip()}%"


@router.get("/policies")
def search_policies(
    code_client: str | None = Query(default=None, max_length=8),
    nom_client: str | None = Query(default=None, max_length=60),
    num_police: str | None = Query(default=None, max_length=20),
    statut: str | None = Query(default=None, pattern="^(tous|vigueur|susp|expirees)$"),
    # Filters of the legacy "Liste des polices": company, date d'effet between two dates, observation, order.
    code_compagnie: int | None = None,
    date_debut: date | None = None,
    date_fin: date | None = None,
    obs: str | None = Query(default=None, max_length=60),
    ordre: str = Query(default="effet", pattern="^(effet|client|police)$"),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if statut in STATUT_SQL:
        conditions.append(STATUT_SQL[statut])
    if code_compagnie is not None:
        conditions.append("p.code_compagnie = :code_compagnie")
        params["code_compagnie"] = code_compagnie
    if date_debut:
        conditions.append("p.effet >= :date_debut")
        params["date_debut"] = date_debut
    if date_fin:
        conditions.append("p.effet < :date_fin_next")
        params["date_fin_next"] = date_fin + timedelta(days=1)
    if obs and obs.strip():
        conditions.append("p.obser ILIKE :obs")
        params["obs"] = _like(obs)
    if code_client and code_client.strip():
        # The client's own policies and the fleet contracts where the client has a vehicle.
        conditions.append(
            "(TRIM(p.code_client) = :code_client OR EXISTS (SELECT 1 FROM dbo.\"TPoliceFlotte\" f "
            "WHERE TRIM(f.num_int) = TRIM(p.num_int) AND TRIM(f.code_client) = :code_client))"
        )
        params["code_client"] = code_client.strip()
    if nom_client and nom_client.strip():
        conditions.append("c.nom_client ILIKE :nom_client")
        params["nom_client"] = _like(nom_client)
    if num_police and num_police.strip():
        conditions.append("TRIM(p.num_police) ILIKE :num_police")
        params["num_police"] = _like(num_police)
    rows = db.execute(
        text(
            'SELECT p.*, TRIM(c.nom_client) AS nom_client, TRIM(co.nom_compagnie) AS nom_compagnie, '
            # Fleet: vehicles in the fleet, and (client filter) the client's vehicles when the contract is not theirs.
            f'(SELECT COUNT(*) FROM dbo."TPoliceFlotte" f WHERE TRIM(f.num_int) = TRIM(p.num_int) AND {ACTIVE_SQL}) AS nb_vehicules, '
            + ("(SELECT STRING_AGG(TRIM(f.num_imat), ', ' ORDER BY TRIM(f.ordre)) FROM dbo.\"TPoliceFlotte\" f WHERE TRIM(f.num_int) = TRIM(p.num_int) "
               "AND TRIM(f.code_client) = :code_client AND TRIM(p.code_client) <> :code_client) AS vehicules_client "
               if "code_client" in params else "NULL AS vehicules_client ")
            + 'FROM dbo."TPolice" p '
            'LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
            'LEFT JOIN dbo."TCompagnie" co ON co.code_compagnie = p.code_compagnie '
            f"WHERE {' AND '.join(conditions)} ORDER BY {POLICY_ORDERS[ordre]} LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return {"items": [serialize(row) for row in rows], "total": len(rows)}


POLICY_ORDERS = {
    "effet": "p.effet DESC NULLS LAST, p.num_police",
    "client": "c.nom_client, LENGTH(TRIM(p.code_client)), TRIM(p.code_client), p.effet DESC NULLS LAST",
    "police": "TRIM(p.num_police), p.effet DESC NULLS LAST",
}
# Gestion des quittances: the receipts still owed (solde > 0) or settled.
REGLEMENT_SQL = {
    "impayees": "COALESCE(q.solde_quittance, 0) > 0.0005",
    "reglees": "COALESCE(q.solde_quittance, 0) <= 0.0005",
}


@router.get("/receipts")
def search_receipts(
    code_client: str | None = Query(default=None, max_length=8),
    nom_client: str | None = Query(default=None, max_length=60),
    code_branche: str | None = Query(default=None, max_length=3),
    num_int: str | None = Query(default=None, max_length=15),
    num_quittance: str | None = Query(default=None, max_length=15),
    date_saisie: date | None = None,
    # Échéance between date_min and date_max (both days included).
    date_min: date | None = None,
    date_max: date | None = None,
    code_compagnie: int | None = None,
    num_police: str | None = Query(default=None, max_length=20),
    reglement: str | None = Query(default=None, pattern="^(tous|impayees|reglees)$"),
    limit: int = Query(default=200, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if reglement in REGLEMENT_SQL:
        conditions.append(REGLEMENT_SQL[reglement])
    if code_compagnie is not None:
        conditions.append("p.code_compagnie = :code_compagnie")
        params["code_compagnie"] = code_compagnie
    if num_police and num_police.strip():
        conditions.append("(TRIM(p.num_police) ILIKE :num_police OR TRIM(q.num_int) ILIKE :num_police)")
        params["num_police"] = _like(num_police)
    if date_min:
        conditions.append("q.echeance >= :date_min")
        params["date_min"] = date_min
    if date_max:
        conditions.append("q.echeance < :date_after")
        params["date_after"] = date_max + timedelta(days=1)
    if date_saisie:
        conditions.append("q.date_saisie >= :date_saisie AND q.date_saisie < :date_saisie_next")
        params["date_saisie"] = date_saisie
        params["date_saisie_next"] = date_saisie + timedelta(days=1)
    if num_quittance and num_quittance.strip():
        conditions.append("TRIM(q.num_quittance) ILIKE :num_quittance")
        params["num_quittance"] = _like(num_quittance)
    if num_int and num_int.strip():
        conditions.append("TRIM(q.num_int) = :num_int")
        params["num_int"] = num_int.strip()
    if code_client and code_client.strip():
        conditions.append("TRIM(q.code_client) = :code_client")
        params["code_client"] = code_client.strip()
    if nom_client and nom_client.strip():
        conditions.append("c.nom_client ILIKE :nom_client")
        params["nom_client"] = _like(nom_client)
    if code_branche and code_branche.strip():
        conditions.append(f"{BRANCH_EXPRESSION} = :code_branche")
        params["code_branche"] = code_branche.strip()
    rows = db.execute(
        text(
            f"SELECT q.*, TRIM(c.nom_client) AS nom_client, TRIM(p.num_police) AS num_police, {BRANCH_EXPRESSION} AS branche, "
            "TRIM(co.nom_compagnie) AS nom_compagnie "
            'FROM dbo."TQuittance" q '
            'LEFT JOIN dbo."TPolice" p ON TRIM(p.num_int) = TRIM(q.num_int) '
            'LEFT JOIN dbo."TCompagnie" co ON co.code_compagnie = p.code_compagnie '
            'LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(q.code_client) '
            f"WHERE {' AND '.join(conditions)} ORDER BY q.date_emission DESC NULLS LAST, q.num_quittance LIMIT :limit"
        ),
        params,
    ).mappings().all()
    return {"items": [serialize(row) for row in rows], "total": len(rows)}
