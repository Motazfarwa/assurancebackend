"""Polices flotte: one contract (dbo."TPolice", flotte = 'O') and its vehicles (dbo."TPoliceFlotte", one row per vehicle,
keyed by num_int + ordre), as in the legacy software.

The contract's client is the souscripteur (who signs and pays); each vehicle has its own client (driver, subsidiary…),
the souscripteur by default. A vehicle is never deleted: it leaves the fleet with an exit date (date_re, etat 'RE'),
so its history and its claims stay right. A registration is in force on one policy only.

Changes during the contract are avenants (dbo."TAvenantFlotte", numbered per contract; each quittance d'avenant is also
written to the legacy dbo."TAvenant"): incorporation (a vehicle enters),
retrait (it leaves), modification (its premium changes) and remplacement (one vehicle for another). Each one is billed
prorata temporis on the days left until the contract's expiration: a premium owed gives a quittance d'avenant
(dbo."TQuittance", type P, comptant, to the souscripteur), a premium returned gives a ristourne (type R, as entered in
"Gestion des ristournes").
"""
from datetime import date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_current_user
from app.database import get_db
from app.police_schemas import CarteVerteCreate, QuittanceCreate


router = APIRouter(prefix="/api/flotte", tags=["Polices flotte"], dependencies=[Depends(require_current_user)])

# A vehicle is in the fleet while it is 'AN' and has no exit date in the past.
ACTIVE_SQL = "UPPER(COALESCE(TRIM(f.etat), 'AN')) = 'AN' AND (f.date_re IS NULL OR f.date_re > CURRENT_DATE)"
# A vehicle covered on the day :jour (entered on or before it, not gone by then).
COVERED_SQL = (
    "(f.effet IS NULL OR f.effet::date <= :jour) AND (f.date_re IS NULL OR f.date_re::date > :jour) "
    "AND (UPPER(COALESCE(TRIM(f.etat), 'AN')) <> 'RE' OR f.date_re IS NOT NULL)"
)
PLATE_SQL = "UPPER(REGEXP_REPLACE(COALESCE({col}, ''), '\\s', '', 'g'))"
CLAIM_PLATE = PLATE_SQL.format(col='s."Num_Imat"')
VEHICLE_PLATE = PLATE_SQL.format(col="f.num_imat")
VEHICLE_SELECT = (
    "SELECT TRIM(f.num_int) AS num_int, TRIM(f.ordre) AS ordre, TRIM(f.num_imat) AS num_imat, TRIM(f.\"Marque\") AS marque, "
    "TRIM(f.\"Serie\") AS serie, f.\"Puissance\" AS puissance, TRIM(f.energie) AS energie, TRIM(f.genre) AS genre, f.nbpl, "
    "f.\"Circulation\" AS circulation, TRIM(f.risque) AS risque, TRIM(f.\"Garantie\") AS garantie, f.venale, f.neuf, f.capital, "
    "f.\"Franchise\" AS franchise, f.prime_net, f.commission, f.prime_total, TRIM(f.code_client) AS code_client, "
    "TRIM(c.nom_client) AS nom_client, f.code_benificiaire, f.effet, f.date_re, UPPER(TRIM(f.etat)) AS etat, "
    "COALESCE(f.epave, 'N') AS epave, TRIM(f.obser) AS obser, "
    f"({ACTIVE_SQL}) AS actif, "
    '(SELECT COUNT(*) FROM dbo."Sinistre" s WHERE TRIM(s."Num_Int") = TRIM(f.num_int) '
    f"AND {CLAIM_PLATE} = {VEHICLE_PLATE}) AS nb_sinistres "
    'FROM dbo."TPoliceFlotte" f LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(f.code_client) '
)

AVENANT_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TAvenantFlotte" (
        id bigserial PRIMARY KEY,
        num_int character varying(20) NOT NULL,
        num_avenant integer NOT NULL,
        type character varying(20) NOT NULL,
        date_effet date NOT NULL,
        ordre character varying(4),
        num_imat character varying(20),
        ordre_remplace character varying(4),
        jours integer,
        jours_periode integer,
        montant numeric(14, 3) NOT NULL DEFAULT 0,
        num_quittance character varying(15),
        observation character varying(120),
        annule character varying(1) NOT NULL DEFAULT 'N',
        create_user character varying(20),
        create_date timestamp NOT NULL DEFAULT now(),
        UNIQUE (num_int, num_avenant)
    )
"""
_avenants_ready = False
AVENANT_TYPES = {"incorporation": "Incorporation", "retrait": "Retrait", "modification": "Modification", "remplacement": "Remplacement"}
LEGACY_AVENANT_TYPES = {"incorporation": "I", "retrait": "R", "modification": "M", "remplacement": "C"}  # TAvenant.Type_Avenant (1 char)


def _ensure_avenants(db: Session) -> None:
    global _avenants_ready
    if not _avenants_ready:
        db.execute(text(AVENANT_DDL))
        db.commit()
        _avenants_ready = True


def _plain(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key] = value
    return out


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Base de données indisponible : {str(getattr(exc, 'orig', exc))[:200]}")


def _day(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _plate(value: Any) -> str:
    return "".join(str(value or "").split()).upper()


def _contract(db: Session, num_int: str) -> dict[str, Any]:
    row = db.execute(
        text(
            'SELECT TRIM(p.num_int) AS num_int, TRIM(p.num_police) AS num_police, TRIM(p.code_client) AS code_client, '
            'TRIM(c.nom_client) AS nom_client, p.code_compagnie, TRIM(co.nom_compagnie) AS nom_compagnie, TRIM(p.code_branche) AS code_branche, '
            'p.effet, p.expiration, TRIM(p.echeance) AS echeance, p.duree, UPPER(TRIM(p.etat)) AS etat, TRIM(p.risque) AS risque, '
            "COALESCE(p.flotte, 'N') = 'O' AS flotte "
            'FROM dbo."TPolice" p LEFT JOIN dbo."TClient" c ON TRIM(c.code_client) = TRIM(p.code_client) '
            'LEFT JOIN dbo."TCompagnie" co ON co.code_compagnie = p.code_compagnie '
            "WHERE TRIM(p.num_int) = :n ORDER BY p.effet DESC NULLS LAST LIMIT 1"
        ),
        {"n": num_int.strip()},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Police introuvable.")
    return _plain(row)


def _vehicles(db: Session, num_int: str) -> list[dict[str, Any]]:
    rows = db.execute(text(VEHICLE_SELECT + "WHERE TRIM(f.num_int) = :n ORDER BY TRIM(f.ordre)"), {"n": num_int.strip()}).mappings().all()
    return [_plain(row) for row in rows]


def _vehicle(db: Session, num_int: str, ordre: str) -> dict[str, Any]:
    vehicle = next((item for item in _vehicles(db, num_int) if item["ordre"] == ordre.strip()), None)
    if not vehicle:
        raise HTTPException(status_code=404, detail="Véhicule introuvable dans cette flotte.")
    return vehicle


def _avenants(db: Session, num_int: str) -> list[dict[str, Any]]:
    rows = db.execute(
        text('SELECT * FROM dbo."TAvenantFlotte" WHERE num_int = :n ORDER BY num_avenant DESC'), {"n": num_int.strip()}
    ).mappings().all()
    return [{**_plain(row), "libelle": AVENANT_TYPES.get(row["type"], row["type"])} for row in rows]


def _summary(db: Session, num_int: str) -> dict[str, Any]:
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    vehicles = _vehicles(db, num_int)
    active = [vehicle for vehicle in vehicles if vehicle["actif"]]
    return {
        "contrat": contract,
        "vehicules": vehicles,
        "avenants": _avenants(db, contract["num_int"]),
        "nb_actifs": len(active),
        "prime_nette": round(sum(float(vehicle["prime_net"] or 0) for vehicle in active), 3),
        "prime_totale": round(sum(float(vehicle["prime_total"] or 0) for vehicle in active), 3),
    }


# ---------------------------------------------------------------------------------------------
# Prorata, quittances d'avenant, ristournes
# ---------------------------------------------------------------------------------------------
def _prorata(contract: dict[str, Any], start: date) -> tuple[float, int, int]:
    """(share, days, period days) of the contract left from `start` (included) to the expiration (included)."""
    effet = _day(contract["effet"]) or start
    expiration = _day(contract["expiration"]) or (effet + timedelta(days=364))
    period = max((expiration - effet).days + 1, 1)
    days = max(min((expiration - start).days + 1, period), 0)
    return days / period, days, period


def _premiums(vehicle: dict[str, Any] | None) -> dict[str, float]:
    vehicle = vehicle or {}
    return {key: float(vehicle.get(key) or 0) for key in ("prime_net", "prime_total", "commission")}


def _next_number(db: Session) -> str:
    """Receipt number as for a new policy: today's date (YYYYMMDD) + a 6-digit counter (lock held until the commit)."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.numbering'))"))
    prefix = f"{datetime.now():%Y%m%d}"
    last = db.execute(
        text('SELECT COALESCE(MAX(CAST(RIGHT(TRIM(num_quittance), 6) AS integer)), 0) FROM dbo."TQuittance" WHERE TRIM(num_quittance) ~ :p'),
        {"p": f"^{prefix}[0-9]{{6}}$"},
    ).scalar_one()
    return f"{prefix}{last + 1:06d}"


def _issue(db: Session, contract: dict[str, Any], kind: str, start: date, amounts: dict[str, float], observation: str, user: str) -> str:
    """Quittance d'avenant (kind 'P', premium owed) or ristourne ('R', premium returned) of the souscripteur, amounts > 0."""
    number = _next_number(db)
    net, total, commission = (round(amounts[key], 3) for key in ("prime_net", "prime_total", "commission"))
    db.execute(
        text("""
            INSERT INTO dbo."TQuittance" (
                num_quittance, num_int, code_client, code_branche, date_saisie, date_emission, echeance, "Periode", tc,
                prime_net, prime_total, commission, "Taxe", montant_quittance, solde_quittance,
                timbre, cout_attestation, transport, frais_quit, observation, type_quittance,
                "Feuille_Caisse", "Comptabiliser", "Validation", "RE", "AV", "Date_M", "Create_User", "Create_Date"
            ) VALUES (
                :n, :num_int, :code_client, :code_branche, now(), :du, :au, :periode, 'C',
                :net, :total, :commission, :taxe, :total, :total,
                0, 0, 0, 0, :observation, :kind,
                'N', 'N', 'N', 'N', 'N', now(), :user, now()
            )
        """),
        {
            "n": number, "num_int": contract["num_int"], "code_client": contract["code_client"], "code_branche": contract["code_branche"],
            "du": start, "au": start if kind == "P" else _day(contract["expiration"]), "periode": f"{start:%m/%Y}",
            "net": net, "total": total, "commission": commission, "taxe": round(max(total - net, 0), 3),
            "observation": observation[:50], "kind": kind, "user": user,
        },
    )
    return number


def _avenant(
    db: Session, contract: dict[str, Any], kind: str, start: date, user: str, *, vehicle: dict[str, Any] | None = None,
    replaced: dict[str, Any] | None = None, delta: dict[str, float] | None = None, bill: bool = True, note: str = "",
) -> dict[str, Any]:
    """Records an avenant of the contract; with `bill`, its prorata of `delta` (annual premiums, + owed / - returned)
    becomes a quittance d'avenant or a ristourne."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.TAvenantFlotte'))"))
    number = db.execute(text('SELECT COALESCE(MAX(num_avenant), 0) + 1 FROM dbo."TAvenantFlotte" WHERE num_int = :n'), {"n": contract["num_int"]}).scalar_one()
    share, days, period = _prorata(contract, start)
    amounts = {key: value * share for key, value in (delta or {}).items()}
    total = round(amounts.get("prime_total", 0), 3)
    plate = (vehicle or {}).get("num_imat") or ""
    label = f"Avenant {number} {AVENANT_TYPES[kind].lower()} {plate}".strip()
    receipt = None
    if bill and abs(total) >= 0.001:
        sign = 1 if total > 0 else -1
        receipt = _issue(db, contract, "P" if sign > 0 else "R", start, {key: value * sign for key, value in amounts.items()}, label, user)
        # The legacy avenant record of the quittance (as the legacy software kept it).
        db.execute(
            text(
                'INSERT INTO dbo."TAvenant" ("Num_int", "Num_quittance", "Type_Quittance", "Type_Avenant", "Changement_Vehicule", '
                '"Changement_Garantie", "Observation", "Date_Saisie") VALUES (:n, :q, :tq, :ta, :cv, :cg, :obs, now())'
            ),
            {"n": contract["num_int"], "q": receipt, "tq": "P" if sign > 0 else "R", "ta": LEGACY_AVENANT_TYPES[kind],
             "cv": kind in ("incorporation", "retrait", "remplacement"), "cg": kind == "modification", "obs": (note or label)[:100]},
        )
    db.execute(
        text(
            'INSERT INTO dbo."TAvenantFlotte" (num_int, num_avenant, type, date_effet, ordre, num_imat, ordre_remplace, jours, jours_periode, '
            "montant, num_quittance, observation, create_user) VALUES (:n, :num, :type, :d, :ordre, :imat, :old, :days, :period, "
            ":montant, :q, :obs, :u)"
        ),
        {
            "n": contract["num_int"], "num": number, "type": kind, "d": start, "ordre": (vehicle or {}).get("ordre"), "imat": plate or None,
            "old": (replaced or {}).get("ordre"), "days": days, "period": period, "montant": total if receipt else 0, "q": receipt,
            "obs": (note or label)[:120], "u": user,
        },
    )
    return {"num_avenant": number, "num_quittance": receipt, "montant": total if receipt else 0, "jours": days}


# ---------------------------------------------------------------------------------------------
# Contract and vehicles
# ---------------------------------------------------------------------------------------------
@router.get("/immatriculation/verifier")
def check_plate(plate: str, exclude: str = "", db: Session = Depends(get_db)) -> dict[str, Any]:
    """Where a registration is in force, before its police flotte exists: refused in a fleet, to confirm on a policy."""
    key = _plate(plate)
    in_fleet = db.execute(
        text(f'SELECT TRIM(f.num_int) FROM dbo."TPoliceFlotte" f WHERE {ACTIVE_SQL} AND {PLATE_SQL.format(col="f.num_imat")} = :plate AND TRIM(f.num_int) <> :x LIMIT 1'),
        {"plate": key, "x": exclude.strip()},
    ).scalar()
    on_policy = db.execute(
        text(
            'SELECT TRIM(p.num_int) FROM dbo."TPolice" p WHERE UPPER(TRIM(p.etat)) = \'AN\' AND (p.expiration IS NULL OR p.expiration >= CURRENT_DATE) '
            f"AND {PLATE_SQL.format(col='p.num_imat')} = :plate AND TRIM(p.num_int) <> :x LIMIT 1"
        ),
        {"plate": key, "x": exclude.strip()},
    ).scalar()
    return {"flotte": in_fleet, "police": on_policy}


@router.get("/{num_int}")
def fleet(num_int: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The fleet contract, all its vehicles (in the fleet or gone) with their claims count, its avenants, and the
    totals of the vehicles in the fleet."""
    try:
        return _summary(db, num_int)
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc


class FlotteIn(BaseModel):
    flotte: bool


@router.put("/{num_int}")
def set_fleet(num_int: str, payload: FlotteIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Marks the policy as a fleet contract (or back to a single-vehicle policy, once it has no vehicle in the fleet)."""
    contract = _contract(db, num_int)
    if not payload.flotte and any(vehicle["actif"] for vehicle in _vehicles(db, num_int)):
        raise HTTPException(status_code=409, detail="Des véhicules sont encore dans la flotte : retirez-les d'abord.")
    db.execute(
        text('UPDATE dbo."TPolice" SET flotte = :f, "Update_User" = :u, "Update_Date" = now() WHERE TRIM(num_int) = :n'),
        {"f": "O" if payload.flotte else "N", "u": str(user.get("sub", ""))[:20], "n": contract["num_int"]},
    )
    db.commit()
    return fleet(num_int, db)


class VehiculeIn(BaseModel):
    num_imat: str = Field(min_length=2, max_length=20)
    marque: str | None = Field(default=None, max_length=50)
    serie: str | None = Field(default=None, max_length=50)
    puissance: int | None = Field(default=None, ge=0, le=9999)
    energie: str | None = Field(default=None, max_length=50)
    genre: str | None = Field(default=None, max_length=50)
    nbpl: int | None = Field(default=None, ge=0, le=999)
    circulation: date | None = None
    risque: str | None = Field(default=None, max_length=100)
    garantie: str | None = Field(default=None, max_length=100)
    venale: float | None = Field(default=None, ge=0)
    neuf: float | None = Field(default=None, ge=0)
    capital: float | None = Field(default=None, ge=0)
    franchise: int | None = Field(default=None, ge=0)
    prime_net: float | None = Field(default=None, ge=0)  # annual premiums of the vehicle (for the contract's period)
    commission: float | None = Field(default=None, ge=0)
    prime_total: float | None = Field(default=None, ge=0)
    code_client: str | None = Field(default=None, max_length=8)  # empty: the souscripteur
    code_benificiaire: int | None = None
    effet: date | None = None  # date the vehicle enters the fleet; empty: the contract's effective date
    obser: str | None = Field(default=None, max_length=50)
    forcer: bool = False  # the registration is on another policy in force: add it anyway (the user confirmed)
    # Avenant: None = automatic (a vehicle entering after the contract's start is an incorporation, billed prorata;
    # one entered with the contract is part of its first quittance). On a change: True bills the premium difference.
    avenant: bool | None = None
    date_avenant: date | None = None  # date of a modification avenant (default today)
    # Entered with its police flotte: the vehicle's own quittance, in the name of its client (then no avenant).
    quittance: QuittanceCreate | None = None
    carte_verte: CarteVerteCreate | None = None


VEHICLE_COLUMNS = {
    "num_imat": "num_imat", "marque": '"Marque"', "serie": '"Serie"', "puissance": '"Puissance"', "energie": "energie",
    "genre": "genre", "nbpl": "nbpl", "circulation": '"Circulation"', "risque": "risque", "garantie": '"Garantie"',
    "venale": "venale", "neuf": "neuf", "capital": "capital", "franchise": '"Franchise"', "prime_net": "prime_net",
    "commission": "commission", "prime_total": "prime_total", "code_client": "code_client",
    "code_benificiaire": "code_benificiaire", "effet": "effet", "obser": "obser",
}


def _check_vehicle(db: Session, contract: dict[str, Any], payload: VehiculeIn, ordre: str | None, *, ignore: str | None = None) -> dict[str, Any]:
    """Payload -> column values, after the checks: client known, entry inside the contract, registration not in force
    elsewhere (`ignore`: the ordre of a vehicle being replaced, still in the fleet until the replacement)."""
    values = payload.model_dump(exclude={"forcer", "avenant", "date_avenant", "quittance", "carte_verte"})
    values["num_imat"] = " ".join(payload.num_imat.upper().split())
    values["code_client"] = (payload.code_client or "").strip() or contract["code_client"]
    if not db.execute(text('SELECT 1 FROM dbo."TClient" WHERE TRIM(code_client) = :c'), {"c": values["code_client"]}).first():
        raise HTTPException(status_code=422, detail=f"Client {values['code_client']} introuvable.")
    values["effet"] = payload.effet or _day(contract["effet"]) or date.today()
    if _day(contract["effet"]) and values["effet"] < _day(contract["effet"]):
        raise HTTPException(status_code=422, detail="La date d'entrée précède la date d'effet du contrat.")
    if _day(contract["expiration"]) and values["effet"] > _day(contract["expiration"]):
        raise HTTPException(status_code=422, detail="La date d'entrée est après l'expiration du contrat.")

    plate = _plate(values["num_imat"])
    fleet_twin = db.execute(
        text(
            f'SELECT TRIM(f.num_int) AS num_int, TRIM(f.ordre) AS ordre FROM dbo."TPoliceFlotte" f WHERE {ACTIVE_SQL} '
            f"AND {PLATE_SQL.format(col='f.num_imat')} = :plate AND NOT (TRIM(f.num_int) = :n AND TRIM(f.ordre) IN (:o, :ignore)) LIMIT 1"
        ),
        {"plate": plate, "n": contract["num_int"], "o": ordre or "", "ignore": ignore or ""},
    ).mappings().first()
    if fleet_twin:
        raise HTTPException(status_code=409, detail=f"Le véhicule {values['num_imat']} est déjà dans la flotte de la police {fleet_twin['num_int']}.")
    if not payload.forcer:
        policy_twin = db.execute(
            text(
                'SELECT TRIM(p.num_int) AS num_int FROM dbo."TPolice" p '
                "WHERE UPPER(TRIM(p.etat)) = 'AN' AND (p.expiration IS NULL OR p.expiration >= CURRENT_DATE) "
                f"AND {PLATE_SQL.format(col='p.num_imat')} = :plate AND TRIM(p.num_int) <> :n LIMIT 1"
            ),
            {"plate": plate, "n": contract["num_int"]},
        ).mappings().first()
        if policy_twin:
            raise HTTPException(
                status_code=409,
                detail=f"CONFIRMER:Le véhicule {values['num_imat']} est assuré par la police {policy_twin['num_int']} en vigueur. L'ajouter quand même ?",
            )
    return values


def _insert_vehicle(db: Session, contract: dict[str, Any], values: dict[str, Any], user: str) -> dict[str, Any]:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.flotte'))"))
    number = db.execute(
        text('SELECT COALESCE(MAX(CAST(NULLIF(REGEXP_REPLACE(ordre, \'\\D\', \'\', \'g\'), \'\') AS integer)), 0) + 1 FROM dbo."TPoliceFlotte" WHERE TRIM(num_int) = :n'),
        {"n": contract["num_int"]},
    ).scalar_one()
    ordre = f"{number:04d}"
    db.execute(
        text(
            f'INSERT INTO dbo."TPoliceFlotte" (num_int, ordre, "Numordre", num_police, code_compagnie, code_branche, echeance, expiration, duree, '
            f'etat, flotte, "Souscripteur", date_creation, "Date_Mvt", "Create_User", "Create_Date", {", ".join(VEHICLE_COLUMNS.values())}) '
            "VALUES (:num_int, :ordre, :ordre, :num_police, :code_compagnie, :code_branche, :echeance, :expiration, :duree, "
            f"'AN', 'O', :souscripteur, now(), now(), :user, now(), {', '.join(':' + key for key in VEHICLE_COLUMNS)})"
        ),
        {
            **values, "num_int": contract["num_int"], "ordre": ordre, "num_police": contract["num_police"],
            "code_compagnie": contract["code_compagnie"], "code_branche": contract["code_branche"], "echeance": contract["echeance"],
            "expiration": contract["expiration"], "duree": contract["duree"], "souscripteur": contract["code_client"], "user": user,
        },
    )
    db.execute(text('UPDATE dbo."TPolice" SET flotte = \'O\' WHERE TRIM(num_int) = :n'), {"n": contract["num_int"]})
    return {**values, "ordre": ordre}


def _vehicle_receipt(db: Session, contract: dict[str, Any], vehicle: dict[str, Any], payload: VehiculeIn, user: str) -> str:
    """The vehicle's own quittance, paid by its client; the contract's premiums are the sum of those of its vehicles."""
    from app.routers.polices import CARTE_VERTE_DDL  # here: polices imports records_search, which imports this module

    q = payload.quittance
    number = _next_number(db)
    db.execute(
        text("""
            INSERT INTO dbo."TQuittance" (
                num_quittance, num_int, code_client, code_branche, date_saisie, date_emission, "Periode",
                prime_net, prime_total, commission, montant_quittance, solde_quittance, "Taxe", timbre, cout_attestation,
                "Num_Attestation", observation, type_quittance, "Comptabiliser", "Create_User", "Create_Date"
            ) VALUES (
                :n, :num_int, :code_client, :code_branche, :date_saisie, :date_emission, :periode,
                :prime_net, :prime_total, :commission, :montant, :solde, :taxe, :timbre, :cout,
                :attestation, :observation, 'P', 'N', :user, now()
            )
        """),
        {
            "n": number, "num_int": contract["num_int"], "code_client": vehicle["code_client"], "code_branche": contract["code_branche"],
            "date_saisie": q.date_saisie or datetime.now(), "date_emission": q.date_emission, "periode": q.periode,
            "prime_net": q.prime_net, "prime_total": q.prime_total, "commission": q.commission, "montant": q.montant_quittance, "solde": q.solde_quittance,
            "taxe": q.taxe, "timbre": q.timbre, "cout": q.cout_attestation, "attestation": q.num_attestation,
            "observation": q.observation or f"Flotte {vehicle['num_imat']}"[:50], "user": user,
        },
    )
    if payload.carte_verte and (payload.carte_verte.du or payload.carte_verte.au):
        db.execute(text(CARTE_VERTE_DDL))
        db.execute(
            text('INSERT INTO dbo."TQuittanceCarteVerte" (num_quittance, date_debut, date_fin) VALUES (:n, :du, :au)'),
            {"n": number, "du": payload.carte_verte.du, "au": payload.carte_verte.au},
        )
    db.execute(
        text(
            'UPDATE dbo."TPolice" SET prime_net = COALESCE(prime_net, 0) + :net, prime_total = COALESCE(prime_total, 0) + :total, '
            'commission = COALESCE(commission, 0) + :commission WHERE TRIM(num_int) = :i'
        ),
        {"net": q.prime_net or 0, "total": q.prime_total or 0, "commission": q.commission or 0, "i": contract["num_int"]},
    )
    return number


def _add(db: Session, contract: dict[str, Any], payload: VehiculeIn, user: str) -> dict[str, Any]:
    """One vehicle added, with its incorporation avenant when it enters after the contract's start (or when asked)."""
    values = _check_vehicle(db, contract, payload, None)
    vehicle = _insert_vehicle(db, contract, values, user)
    if payload.quittance:
        return {**vehicle, "num_quittance": _vehicle_receipt(db, contract, vehicle, payload, user)}
    later = bool(_day(contract["effet"]) and values["effet"] > _day(contract["effet"]))
    if payload.avenant if payload.avenant is not None else later:
        return {**vehicle, "avenant": _avenant(db, contract, "incorporation", values["effet"], user, vehicle=vehicle, delta=_premiums(values))}
    return vehicle


def _message(vehicle: dict[str, Any], what: str) -> str:
    avenant = vehicle.get("avenant")
    if not avenant:
        return f"Véhicule {vehicle['num_imat']} {what}."
    document = f"quittance {avenant['num_quittance']} de {abs(avenant['montant']):.3f}" if avenant["montant"] > 0 else (
        f"ristourne {avenant['num_quittance']} de {abs(avenant['montant']):.3f}" if avenant["num_quittance"] else "sans prime à régler")
    return f"Véhicule {vehicle['num_imat']} {what} · avenant {avenant['num_avenant']} ({avenant['jours']} jours) : {document}."


@router.post("/{num_int}/vehicules", status_code=201)
def add_vehicle(num_int: str, payload: VehiculeIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Adds a vehicle to the fleet (the contract becomes a fleet contract); it takes the contract's company, numbers and dates."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    try:
        vehicle = _add(db, contract, payload, str(user.get("sub", ""))[:20])
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {**fleet(num_int, db), "message": _message(vehicle, "ajouté à la flotte")}


class ImportIn(BaseModel):
    vehicules: list[VehiculeIn] = Field(min_length=1, max_length=500)


@router.post("/{num_int}/vehicules/import")
def import_vehicles(num_int: str, payload: ImportIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Adds a list of vehicles (read from the company's Excel / PDF list); each one is checked on its own, those refused
    are listed with the reason and the others are added."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    username = str(user.get("sub", ""))[:20]
    results = []
    for line in payload.vehicules:
        savepoint = db.begin_nested()
        try:
            vehicle = _add(db, contract, line, username)
            savepoint.commit()
            results.append({"num_imat": vehicle["num_imat"], "ok": True, "message": _message(vehicle, "ajouté")})
        except HTTPException as exc:
            savepoint.rollback()
            results.append({"num_imat": line.num_imat, "ok": False, "message": str(exc.detail).removeprefix("CONFIRMER:")})
        except SQLAlchemyError as exc:
            savepoint.rollback()
            results.append({"num_imat": line.num_imat, "ok": False, "message": _fail(exc).detail})
    db.commit()
    return {**fleet(num_int, db), "resultats": results}


@router.put("/{num_int}/vehicules/{ordre}")
def update_vehicle(num_int: str, ordre: str, payload: VehiculeIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Corrects a vehicle; with avenant, a premium change is billed (or returned) prorata from date_avenant."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    before = _vehicle(db, num_int, ordre)
    values = _check_vehicle(db, contract, payload, ordre.strip())
    username = str(user.get("sub", ""))[:20]
    assignments = ", ".join(f"{column} = :{key}" for key, column in VEHICLE_COLUMNS.items())
    try:
        db.execute(
            text(f'UPDATE dbo."TPoliceFlotte" SET {assignments}, "Update_User" = :user, "Update_Date" = now(), "Date_Mvt" = now() '
                 "WHERE TRIM(num_int) = :n AND TRIM(ordre) = :o"),
            {**values, "user": username, "n": contract["num_int"], "o": ordre.strip()},
        )
        delta = {key: value - _premiums(before)[key] for key, value in _premiums(values).items()}
        message = f"Véhicule {values['num_imat']} modifié."
        if payload.avenant and any(abs(value) >= 0.001 for value in delta.values()):
            start = payload.date_avenant or date.today()
            if not before["actif"]:
                raise HTTPException(status_code=422, detail="Ce véhicule n'est plus dans la flotte : pas d'avenant de modification.")
            message = _message({**values, "avenant": _avenant(db, contract, "modification", start, username, vehicle={**values, "ordre": ordre.strip()}, delta=delta)}, "modifié")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {**fleet(num_int, db), "message": message}


class SortieIn(BaseModel):
    date_sortie: date
    motif: Literal["vendu", "epave", "transfert", "autre"] = "autre"
    observation: str | None = Field(default=None, max_length=50)
    ristourne: bool = True  # return the premium of the days left (avenant de retrait)


MOTIFS = {"vendu": "Vendu", "epave": "Épave", "transfert": "Transféré", "autre": "Retiré", "remplace": "Remplacé"}


def _exit(db: Session, contract: dict[str, Any], vehicle: dict[str, Any], day: date, motif: str, note: str | None, user: str) -> None:
    if not vehicle["actif"]:
        raise HTTPException(status_code=409, detail=f"Le véhicule {vehicle['num_imat']} n'est plus dans la flotte.")
    if vehicle["effet"] and day < _day(vehicle["effet"]):
        raise HTTPException(status_code=422, detail="La date de sortie précède la date d'entrée du véhicule.")
    last_claim = db.execute(
        text(f'SELECT MAX("Date_Sinistre") FROM dbo."Sinistre" s WHERE TRIM("Num_Int") = :n AND {CLAIM_PLATE} = :plate'),
        {"n": contract["num_int"], "plate": _plate(vehicle["num_imat"])},
    ).scalar()
    if last_claim and day <= _day(last_claim):
        raise HTTPException(status_code=422, detail=f"Ce véhicule a un sinistre le {_day(last_claim):%d/%m/%Y} : la sortie doit être après.")
    text_note = " · ".join(filter(None, [f"{MOTIFS[motif]} le {day:%d/%m/%Y}", note]))[:50]
    db.execute(
        text(
            'UPDATE dbo."TPoliceFlotte" SET etat = \'RE\', date_re = :d, epave = :e, date_epave = :de, obser = :o, '
            '"Update_User" = :u, "Update_Date" = now(), "Date_Mvt" = now() WHERE TRIM(num_int) = :n AND TRIM(ordre) = :ordre'
        ),
        {"d": day, "e": "O" if motif == "epave" else "N", "de": day if motif == "epave" else None, "o": text_note, "u": user,
         "n": contract["num_int"], "ordre": vehicle["ordre"]},
    )


@router.post("/{num_int}/vehicules/{ordre}/sortie")
def remove_vehicle(num_int: str, ordre: str, payload: SortieIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Takes a vehicle out of the fleet from a date (avenant de retrait): it stays in the history with its claims, and
    the premium of the days left is returned as a ristourne."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    vehicle = _vehicle(db, num_int, ordre)
    username = str(user.get("sub", ""))[:20]
    try:
        _exit(db, contract, vehicle, payload.date_sortie, payload.motif, payload.observation, username)
        returned = {key: -value for key, value in _premiums(vehicle).items()}
        avenant = _avenant(db, contract, "retrait", payload.date_sortie, username, vehicle=vehicle, delta=returned, bill=payload.ristourne)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {**fleet(num_int, db), "message": _message({**vehicle, "avenant": avenant}, f"retiré de la flotte le {payload.date_sortie:%d/%m/%Y}")}


@router.delete("/{num_int}/vehicules/{ordre}/sortie")
def cancel_removal(num_int: str, ordre: str, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Puts a vehicle taken out by mistake back in the fleet: its retrait avenant is cancelled and its ristourne deleted
    (refused once that ristourne is paid, or when the registration is in force elsewhere since)."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    vehicle = _vehicle(db, num_int, ordre)
    if vehicle["actif"]:
        raise HTTPException(status_code=409, detail="Ce véhicule est déjà dans la flotte.")
    twin = db.execute(
        text(f'SELECT TRIM(f.num_int) FROM dbo."TPoliceFlotte" f WHERE {ACTIVE_SQL} AND {PLATE_SQL.format(col="f.num_imat")} = :plate '
             "AND NOT (TRIM(f.num_int) = :n AND TRIM(f.ordre) = :o) LIMIT 1"),
        {"plate": _plate(vehicle["num_imat"]), "n": contract["num_int"], "o": vehicle["ordre"]},
    ).scalar()
    if twin:
        raise HTTPException(status_code=409, detail=f"Ce véhicule est maintenant dans la flotte de la police {twin}.")
    retrait = db.execute(
        text('SELECT id, num_quittance FROM dbo."TAvenantFlotte" WHERE num_int = :n AND ordre = :o AND type IN (\'retrait\', \'remplacement\') '
             "AND annule = 'N' ORDER BY num_avenant DESC LIMIT 1"),
        {"n": contract["num_int"], "o": vehicle["ordre"]},
    ).mappings().first()
    try:
        if retrait and retrait["num_quittance"]:
            paid = db.execute(
                text("SELECT COALESCE(SUM(mont_dep), 0) FROM dbo.\"TDepense\" WHERE TRIM(\"Typ_dep\") = 'T' AND TRIM(num_quittance) = :q"),
                {"q": retrait["num_quittance"]},
            ).scalar_one()
            if float(paid or 0) > 0.0005:
                raise HTTPException(status_code=409, detail=f"La ristourne {retrait['num_quittance']} de ce retrait est déjà payée : la sortie ne peut plus être annulée.")
            db.execute(text('DELETE FROM dbo."TQuittance" WHERE TRIM(num_quittance) = :q AND TRIM(type_quittance) = \'R\''), {"q": retrait["num_quittance"]})
            db.execute(text('DELETE FROM dbo."TAvenant" WHERE TRIM("Num_quittance") = :q'), {"q": retrait["num_quittance"]})
        if retrait:
            db.execute(text('UPDATE dbo."TAvenantFlotte" SET annule = \'O\' WHERE id = :i'), {"i": retrait["id"]})
        db.execute(
            text('UPDATE dbo."TPoliceFlotte" SET etat = \'AN\', date_re = NULL, epave = \'N\', date_epave = NULL, obser = NULL, '
                 '"Update_User" = :u, "Update_Date" = now(), "Date_Mvt" = now() WHERE TRIM(num_int) = :n AND TRIM(ordre) = :o'),
            {"u": str(user.get("sub", ""))[:20], "n": contract["num_int"], "o": vehicle["ordre"]},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    cancelled = f" Avenant de retrait annulé{' et ristourne ' + retrait['num_quittance'] + ' supprimée' if retrait and retrait['num_quittance'] else ''}." if retrait else ""
    return {**fleet(num_int, db), "message": f"{vehicle['num_imat']} est de nouveau dans la flotte.{cancelled}"}


class RemplacementIn(BaseModel):
    date_remplacement: date
    vehicule: VehiculeIn
    avenant: bool = True  # bill (or return) the premium difference between the two vehicles, prorata


@router.post("/{num_int}/vehicules/{ordre}/remplacement")
def replace_vehicle(num_int: str, ordre: str, payload: RemplacementIn, user: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """One vehicle for another on a date (avenant de remplacement): the old one leaves, the new one enters, and only the
    premium difference is billed (quittance) or returned (ristourne)."""
    _ensure_avenants(db)
    contract = _contract(db, num_int)
    old = _vehicle(db, num_int, ordre)
    username = str(user.get("sub", ""))[:20]
    new_payload = payload.vehicule.model_copy(update={"effet": payload.date_remplacement, "avenant": False})
    values = _check_vehicle(db, contract, new_payload, None, ignore=old["ordre"])
    try:
        _exit(db, contract, old, payload.date_remplacement, "autre", f"Remplacé par {values['num_imat']}", username)
        new = _insert_vehicle(db, contract, values, username)
        delta = {key: value - _premiums(old)[key] for key, value in _premiums(values).items()}
        avenant = _avenant(db, contract, "remplacement", payload.date_remplacement, username, vehicle=new, replaced=old, delta=delta,
                           bill=payload.avenant, note=f"Remplacement de {old['num_imat']} par {new['num_imat']}")
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {**fleet(num_int, db), "message": _message({**new, "avenant": avenant}, f"remplace {old['num_imat']}")}


@router.get("/{num_int}/vehicules/{ordre}/sinistres")
def vehicle_claims(num_int: str, ordre: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The claims of one vehicle of the fleet (same policy and registration)."""
    vehicle = _vehicle(db, num_int, ordre)
    rows = db.execute(
        text(
            'SELECT TRIM("Num_Sinistre_Agence") AS numero, "Date_Sinistre" AS date_sinistre, TRIM("Type_Sinistre") AS type, '
            'TRIM("Lieu_Sinistre") AS lieu, "Montant_Reglement" AS reglement, "Regle" AS regle, "Cloture" AS cloture '
            f'FROM dbo."Sinistre" s WHERE TRIM("Num_Int") = :n AND {CLAIM_PLATE} = :plate ORDER BY "Date_Sinistre" DESC'
        ),
        {"n": vehicle["num_int"], "plate": _plate(vehicle["num_imat"])},
    ).mappings().all()
    return {"vehicule": vehicle, "sinistres": [_plain(row) for row in rows]}


def covered_vehicles(db: Session, num_int: str, day: date | None) -> list[dict[str, Any]]:
    """The vehicles of a fleet covered on a day (all those ever in the fleet when the day is unknown), for a claim."""
    where = f"TRIM(f.num_int) = :n AND ({COVERED_SQL})" if day else "TRIM(f.num_int) = :n"
    rows = db.execute(text(VEHICLE_SELECT + f"WHERE {where} ORDER BY TRIM(f.ordre)"), {"n": num_int.strip(), "jour": day}).mappings().all()
    return [_plain(row) for row in rows]


def check_claim_vehicle(db: Session, num_int: str | None, plate: str | None, day: date | None) -> None:
    """A claim on a fleet contract must name a vehicle of the fleet covered on the claim's date."""
    number = (num_int or "").strip()
    if not number or not db.execute(text('SELECT 1 FROM dbo."TPoliceFlotte" WHERE TRIM(num_int) = :n LIMIT 1'), {"n": number}).first():
        return  # not a fleet contract
    if not _plate(plate):
        raise HTTPException(status_code=422, detail="Police flotte : choisissez le véhicule sinistré (immatriculation).")
    if not day:
        return
    covered = db.execute(
        text(f'SELECT 1 FROM dbo."TPoliceFlotte" f WHERE TRIM(f.num_int) = :n AND {PLATE_SQL.format(col="f.num_imat")} = :plate AND {COVERED_SQL} LIMIT 1'),
        {"n": number, "plate": _plate(plate), "jour": day},
    ).first()
    if not covered:
        raise HTTPException(status_code=422, detail=f"Le véhicule {plate} n'était pas dans la flotte de la police {number} le {day:%d/%m/%Y}.")
