
import os
import re
from datetime import date, datetime, timedelta
from secrets import randbelow

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.police_schemas import PoliceWithQuittanceCreate

load_dotenv()

router = APIRouter(prefix="/api/polices", tags=["Polices"])

DATABASE_URL = os.environ["DATABASE_URL"]

# Reuse the same DB credentials as the application.
# Later this can be replaced with the existing app.database
# session dependency.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


GARANTIES_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TPoliceGarantie" (
        num_int character varying(20) PRIMARY KEY,
        vol numeric(14, 3),
        incendie numeric(14, 3),
        bdg numeric(14, 3),
        personnes_transportees numeric(14, 3),
        ind_conducteur numeric(14, 3),
        remarquage numeric(14, 3)
    )
"""


CARTE_VERTE_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TQuittanceCarteVerte" (
        num_quittance character varying(15) PRIMARY KEY,
        date_debut date,
        date_fin date
    )
"""


REVALORISATION_DDL = """
    CREATE TABLE IF NOT EXISTS dbo."TPoliceRevalorisation" (
        num_int character varying(20) PRIMARY KEY,
        date_revalorisation date,
        date_envoi date
    )
"""
_revalorisation_ready = False


def ensure_revalorisation_table():
    """Revalorisation dates of a policy (the policy table has no column for them); created on first use."""
    global _revalorisation_ready
    if not _revalorisation_ready:
        with engine.begin() as conn:
            conn.execute(text(REVALORISATION_DDL))
        _revalorisation_ready = True


def flag(value):
    if value is None:
        return None
    return "O" if value else "N"


def validate_policy_number(number, branch):
    if not re.fullmatch(r"\d{4}\d{3}\d{5}", number):
        raise HTTPException(
            status_code=422,
            detail="Format attendu : AAAA + branche(3) + 5 chiffres"
        )

    if number[4:7] != branch:
        raise HTTPException(
            status_code=422,
            detail="La branche du numéro ne correspond pas au code branche"
        )


@router.post("", status_code=201)
def create_police_with_quittance(
    payload: PoliceWithQuittanceCreate
):
    p = payload.police
    q = payload.quittance

    try:
        with engine.begin() as conn:
            # This database has no numbering sequences: numbers are derived from the existing data, under a
            # transaction lock so two simultaneous entries cannot take the same number.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('dbo.numbering'))"))

            if p.num_police:
                policy_number = p.num_police
            else:
                prefix = f"{datetime.now().year}{p.code_branche}"
                last_policy = conn.execute(
                    text(
                        'SELECT COALESCE(MAX(CAST(SUBSTRING(TRIM(num_police) FROM 8 FOR 5) AS integer)), 0) '
                        'FROM dbo."TPolice" WHERE TRIM(num_police) ~ :pattern'
                    ),
                    {"pattern": f"^{prefix}[0-9]{{5}}$"},
                ).scalar_one()

                if last_policy >= 99999:
                    raise HTTPException(
                        status_code=409,
                        detail="Séquence des polices épuisée"
                    )

                policy_number = f"{prefix}{last_policy + 1:05d}"

            validate_policy_number(
                policy_number, p.code_branche
            )

            already_used = conn.execute(
                text('SELECT 1 FROM dbo."TPolice" WHERE TRIM(num_police) = :n OR TRIM(num_int) = :n LIMIT 1'),
                {"n": policy_number},
            ).first()
            if already_used:
                raise HTTPException(
                    status_code=409,
                    detail="Ce numéro de police existe déjà."
                )

            # In this database the internal id of a policy is its policy number (column limited to 15 characters).
            internal_id = policy_number

            # Receipt number: today's date (YYYYMMDD) + a 6-digit counter that continues the day's last number.
            day_prefix = f"{datetime.now():%Y%m%d}"
            last_receipt = conn.execute(
                text(
                    'SELECT COALESCE(MAX(CAST(RIGHT(TRIM(num_quittance), 6) AS integer)), 0) '
                    'FROM dbo."TQuittance" WHERE TRIM(num_quittance) ~ :pattern'
                ),
                {"pattern": f"^{day_prefix}[0-9]{{6}}$"},
            ).scalar_one()
            receipt_id = f"{day_prefix}{last_receipt + 1:06d}"

            conn.execute(
                text("""
                    INSERT INTO dbo."TPolice" (
                        num_int, num_police, code_client,
                        code_branche, code_compagnie,
                        effet, "Heure_Effet",
                        expiration, echeance, duree,
                        etat, "Date_Mvt", date_creation,
                        "Renouv_An", "Participation",
                        "TousRisque", "HorsTaxe",
                        risque, num_imat, marque,
                        "Serie", "Puissance", "Circulation",
                        "Garantie", "Franchise", capital, neuf, venale,
                        obser, prime_net, commission,
                        prime_total
                    )
                    VALUES (
                        :num_int, :num_police, :code_client,
                        :code_branche, :code_compagnie,
                        :effet, :heure_effet,
                        :expiration, :echeance, :duree,
                        :etat, :date_mvt, :date_creation,
                        :renouv_an, :participation,
                        :tous_risque, :hors_taxe,
                        :risque, :num_imat, :marque,
                        :serie, :puissance, :circulation,
                        :garantie, :franchise, :capital, :neuf, :venale,
                        :obser, :prime_net, :commission,
                        :prime_total
                    )
                """),
                {
                    "num_int": internal_id,
                    "num_police": policy_number,
                    "code_client": p.code_client,
                    "code_branche": p.code_branche,
                    "code_compagnie": p.code_compagnie,
                    "effet": p.effet,
                    "heure_effet": p.heure_effet,
                    "expiration": p.expiration,
                    "echeance": p.echeance,
                    "duree": p.duree,
                    "etat": p.etat,
                    "date_mvt": p.date_mvt,
                    "date_creation": p.date_creation
                        or datetime.now(),
                    "renouv_an": flag(p.renouvelable),
                    "participation": flag(p.participation),
                    "tous_risque": flag(p.tous_risque),
                    "hors_taxe": flag(p.hors_taxe),
                    "risque": p.risque,
                    "num_imat": p.num_imat,
                    "marque": p.marque,
                    "serie": p.serie,
                    "puissance": p.puissance,
                    "circulation": p.circulation,
                    "garantie": p.garantie,
                    "franchise": p.franchise,
                    "capital": p.capital,
                    "neuf": p.neuf,
                    "venale": p.venale,
                    "obser": p.obser,
                    "prime_net": q.prime_net,
                    "commission": q.commission,
                    "prime_total": q.prime_total,
                }
            )

            garanties = payload.garanties.model_dump() if payload.garanties else {}
            if any(value is not None for value in garanties.values()):
                conn.execute(text(GARANTIES_DDL))
                conn.execute(
                    text("""
                        INSERT INTO dbo."TPoliceGarantie"
                            (num_int, vol, incendie, bdg, personnes_transportees, ind_conducteur, remarquage)
                        VALUES
                            (:num_int, :vol, :incendie, :bdg, :personnes_transportees, :ind_conducteur, :remarquage)
                    """),
                    {"num_int": internal_id, **garanties},
                )

            if payload.revalorisation and payload.revalorisation.date_revalorisation:
                conn.execute(text(REVALORISATION_DDL))
                conn.execute(
                    text('INSERT INTO dbo."TPoliceRevalorisation" (num_int, date_revalorisation, date_envoi) VALUES (:num_int, :d, :e)'),
                    {"num_int": internal_id, "d": payload.revalorisation.date_revalorisation, "e": payload.revalorisation.date_envoi},
                )

            conn.execute(
                text("""
                    INSERT INTO dbo."TQuittance" (
                        num_quittance, num_int,
                        code_client, code_branche,
                        date_saisie, date_emission,
                        echeance, "Periode",
                        prime_net, prime_total,
                        commission, montant_quittance,
                        solde_quittance,
                        "Taxe", timbre,
                        cout_attestation, transport,
                        frais_quit, "Num_Attestation",
                        observation, type_quittance
                    )
                    VALUES (
                        :num_quittance, :num_int,
                        :code_client, :code_branche,
                        :date_saisie, :date_emission,
                        :echeance, :periode,
                        :prime_net, :prime_total,
                        :commission, :montant_quittance,
                        :solde_quittance,
                        :taxe, :timbre,
                        :cout_attestation, :transport,
                        :frais_quit, :num_attestation,
                        :observation, :type_quittance
                    )
                """),
                {
                    "num_quittance": receipt_id,
                    "num_int": internal_id,
                    "code_client": p.code_client,
                    "code_branche": p.code_branche,
                    "date_saisie": q.date_saisie
                        or datetime.now(),
                    "date_emission": q.date_emission,
                    "echeance": q.echeance,
                    "periode": q.periode,
                    "prime_net": q.prime_net,
                    "prime_total": q.prime_total,
                    "commission": q.commission,
                    "montant_quittance": q.montant_quittance,
                    "solde_quittance": q.solde_quittance,
                    "taxe": q.taxe,
                    "timbre": q.timbre,
                    "cout_attestation": q.cout_attestation,
                    "transport": q.transport,
                    "frais_quit": q.frais_quit,
                    "num_attestation": q.num_attestation,
                    "observation": q.observation,
                    "type_quittance": q.type_quittance,
                }
            )

            # Green card validity ("Carte verte"): from / to dates, kept with the receipt.
            if payload.carte_verte and (payload.carte_verte.du or payload.carte_verte.au):
                conn.execute(text(CARTE_VERTE_DDL))
                conn.execute(
                    text(
                        'INSERT INTO dbo."TQuittanceCarteVerte" (num_quittance, date_debut, date_fin) '
                        "VALUES (:num_quittance, :du, :au)"
                    ),
                    {"num_quittance": receipt_id, "du": payload.carte_verte.du, "au": payload.carte_verte.au},
                )

        return {
            "message": "Police et quittance créées",
            "num_int": internal_id,
            "num_police": policy_number,
            "num_quittance": receipt_id,
        }

    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Identifiant ou numéro de police déjà utilisé"
        ) from exc
    except SQLAlchemyError as exc:
        # Return JSON (not a bare 500) so the browser can show the real database error.
        raise HTTPException(
            status_code=503,
            detail=f"Enregistrement impossible : {str(getattr(exc, 'orig', exc))[:300]}"
        ) from exc

@router.get("/compagnies")
def list_compagnies():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    code_compagnie,
                    nom_compagnie
                FROM dbo."TCompagnie"
                ORDER BY nom_compagnie
            """)
        ).mappings().all()

    return [dict(row) for row in rows]

@router.get("/branche")
def get_branche(
    code_compagnie: int = Query(...),
    code_branche: str = Query(...)
):
    code_branche = code_branche.strip()

    if not code_branche.isdigit() or len(code_branche) != 3:
        raise HTTPException(
            status_code=422,
            detail="Le code branche doit contenir 3 chiffres"
        )

    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT
                    code_branche,
                    code_compagnie,
                    nom_branche
                FROM dbo."TBranche"
                WHERE code_compagnie = :compagnie
                  AND code_branche = :branche
                LIMIT 1
            """),
            {
                "compagnie": code_compagnie,
                "branche": code_branche
            }
        ).mappings().first()

    if not result:
        raise HTTPException(
            status_code=404,
            detail="Aucune branche trouvée pour cette compagnie"
        )

    return dict(result)

def _plain(row) -> dict:
    """Row -> JSON-friendly dict with lower-case keys."""
    out = {}
    for key, value in dict(row).items():
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value.__class__.__name__ == "Decimal":
            value = float(value)
        out[key.lower()] = value
    return out


@router.get("/detail")
def get_police_detail(
    num_police: str | None = Query(default=None, max_length=12),
    code_client: str | None = Query(default=None, max_length=8),
):
    """Full policy (and its latest receipt) by policy number or, failing that, the client's latest policy."""
    number = (num_police or "").strip()
    client = (code_client or "").strip()
    if not number and not client:
        raise HTTPException(status_code=422, detail="Renseignez un numéro de police ou un code client")

    with engine.connect() as conn:
        if number:
            rows = conn.execute(
                text('SELECT * FROM dbo."TPolice" p WHERE TRIM(p.num_police) = :n ORDER BY p.date_creation DESC NULLS LAST'),
                {"n": number},
            ).mappings().all()
        else:
            rows = conn.execute(
                text('SELECT * FROM dbo."TPolice" p WHERE TRIM(p.code_client) = :c ORDER BY p.date_creation DESC NULLS LAST, p.num_police DESC'),
                {"c": client},
            ).mappings().all()
        if not rows:
            raise HTTPException(status_code=404, detail="Aucune police trouvée")

        police = _plain(rows[0])
        quittance_rows = conn.execute(
            text('SELECT * FROM dbo."TQuittance" q WHERE q.num_int = :i ORDER BY q.date_emission DESC NULLS LAST, q.num_quittance'),
            {"i": police.get("num_int")},
        ).mappings().all()
        quittance = quittance_rows[0] if quittance_rows else None
        branche = conn.execute(
            text('SELECT nom_branche FROM dbo."TBranche" WHERE code_compagnie = :cc AND code_branche = :cb LIMIT 1'),
            {"cc": police.get("code_compagnie"), "cb": police.get("code_branche")},
        ).scalar()

        nom_client = conn.execute(
            text('SELECT nom_client FROM dbo."TClient" WHERE TRIM(code_client) = :c LIMIT 1'),
            {"c": str(police.get("code_client") or "").strip()},
        ).scalar()

        try:
            garanties_row = conn.execute(
                text('SELECT vol, incendie, bdg, personnes_transportees, ind_conducteur, remarquage FROM dbo."TPoliceGarantie" WHERE num_int = :i'),
                {"i": police.get("num_int")},
            ).mappings().first()
        except SQLAlchemyError:
            conn.rollback()  # table not created yet: no guarantees saved so far
            garanties_row = None

        try:
            revalorisation_row = conn.execute(
                text('SELECT date_revalorisation, date_envoi FROM dbo."TPoliceRevalorisation" WHERE num_int = :i'),
                {"i": str(police.get("num_int") or "").strip()},
            ).mappings().first()
        except SQLAlchemyError:
            conn.rollback()  # table not created yet: no revalorisation saved so far
            revalorisation_row = None

    return {
        "garanties": _plain(garanties_row) if garanties_row else None,
        "revalorisation": _plain(revalorisation_row) if revalorisation_row else None,
        "nom_client": nom_client,
        "police": police,
        "quittance": _plain(quittance) if quittance else None,
        "quittances": [_plain(row) for row in quittance_rows],
        "nom_branche": branche,
        "matches": len(rows),
    }


@router.get("/client/{code_client}")
def get_client_polices(code_client: str):
    code_client = code_client.strip()

    if not code_client or len(code_client) > 8:
        raise HTTPException(
            status_code=422,
            detail="Code client invalide"
        )

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    p.num_int,
                    p.num_police,
                    p.code_client,
                    p.code_compagnie,
                    p.code_branche,
                    b.nom_branche,
                    p.effet,
                    p.expiration,
                    p.etat,
                    p.prime_net,
                    p.prime_total,
                    p.num_imat
                FROM dbo."TPolice" p

                LEFT JOIN LATERAL (
                    SELECT tb.nom_branche
                    FROM dbo."TBranche" tb
                    WHERE tb.code_compagnie = p.code_compagnie
                      AND tb.code_branche = p.code_branche
                    LIMIT 1
                ) b ON TRUE

                WHERE p.code_client = :code_client

                ORDER BY p.date_creation DESC NULLS LAST,
                         p.num_police
            """),
            {"code_client": code_client}
        ).mappings().all()

    return [dict(row) for row in rows]  

@router.get("")
def list_all_polices():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    p.num_int,
                    p.num_police,
                    p.code_client,
                    p.code_compagnie,
                    p.code_branche,
                    b.nom_branche,
                    p.effet,
                    p.expiration,
                    p.etat,
                    p.prime_net,
                    p.num_imat
                FROM dbo."TPolice" p
                LEFT JOIN LATERAL (
                    SELECT nom_branche
                    FROM dbo."TBranche" b
                    WHERE b.code_compagnie = p.code_compagnie
                      AND b.code_branche = p.code_branche
                    LIMIT 1
                ) b ON TRUE
                ORDER BY p.date_creation DESC NULLS LAST
                LIMIT 100
            """)
        ).mappings().all()

    return [dict(row) for row in rows]



@router.get("/search")
def search_polices(
    numero: str | None = Query(default=None, min_length=4, max_length=50),
    code_client: str | None = Query(default=None, max_length=8),
    code_branche: str | None = Query(default=None, pattern=r"^[0-9]{1,3}$"),
    etat: str | None = Query(default=None, max_length=10),
    num_imat: str | None = Query(default=None, max_length=12),
    effet_du: date | None = None,
    effet_au: date | None = None,
    statut: str | None = Query(default=None, pattern="^(tous|vigueur|susp|expirees)$"),
    revalorisation: str | None = Query(default=None, pattern="^(oui|non)$"),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    conditions = []
    params: dict = {}

    if numero and numero.strip():
        pattern = numero.strip()

        # Accept digits and % only
        if not re.fullmatch(r"[0-9%]+", pattern):
            raise HTTPException(
                status_code=422,
                detail="Utilisez uniquement des chiffres et %"
            )

        # First 4 digits must represent a year
        if not pattern[:4].isdigit():
            raise HTTPException(
                status_code=422,
                detail="Le numéro doit commencer par une année AAAA"
            )

        conditions.append("TRIM(p.num_police) LIKE :pattern")
        params["pattern"] = pattern

    if code_client and code_client.strip():
        conditions.append("TRIM(p.code_client) = :code_client")
        params["code_client"] = code_client.strip()

    if code_branche:
        conditions.append("TRIM(p.code_branche) = :code_branche")
        params["code_branche"] = code_branche.zfill(3)

    if etat and etat.strip():
        conditions.append("UPPER(TRIM(p.etat)) = :etat")
        params["etat"] = etat.strip().upper()

    if num_imat and num_imat.strip():
        conditions.append("UPPER(REPLACE(p.num_imat, ' ', '')) LIKE :num_imat")
        params["num_imat"] = "%" + num_imat.strip().upper().replace(" ", "") + "%"

    if effet_du:
        conditions.append("p.effet >= :effet_du")
        params["effet_du"] = effet_du

    if effet_au:
        conditions.append("p.effet < :effet_au_next")
        params["effet_au_next"] = effet_au + timedelta(days=1)

    # Policies with (oui) or without (non) a revalorisation date.
    ensure_revalorisation_table()
    if revalorisation == "oui":
        conditions.append('EXISTS (SELECT 1 FROM dbo."TPoliceRevalorisation" r WHERE r.num_int = TRIM(p.num_int) AND r.date_revalorisation IS NOT NULL)')
    elif revalorisation == "non":
        conditions.append('NOT EXISTS (SELECT 1 FROM dbo."TPoliceRevalorisation" r WHERE r.num_int = TRIM(p.num_int) AND r.date_revalorisation IS NOT NULL)')

    if not conditions and not statut:
        raise HTTPException(
            status_code=422,
            detail="Renseignez au moins un critère de recherche"
        )

    # Status as in the legacy "Liste des polices": AN = in force, SS / RE / RV = suspended or terminated.
    if statut == "vigueur":
        conditions.append("UPPER(TRIM(p.etat)) = 'AN' AND (p.expiration IS NULL OR p.expiration >= CURRENT_DATE)")
    elif statut == "expirees":
        conditions.append("UPPER(TRIM(p.etat)) = 'AN' AND p.expiration < CURRENT_DATE")
    elif statut == "susp":
        conditions.append("UPPER(TRIM(p.etat)) IN ('SS', 'RE', 'RV')")

    where_sql = " AND ".join("(" + condition + ")" for condition in conditions) or "TRUE"
    # Without `page` the answer is the plain list (max 100); with it, a page of results plus the total.
    limit_sql = "LIMIT 100" if page is None else f"LIMIT {int(page_size)} OFFSET {(int(page) - 1) * int(page_size)}"

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT
                    p.num_int,
                    p.echeance,
                    p.commission,
                    p.prime_total,
                    p.obser AS observation,
                    p.risque,
                    p.num_police,
                    p.code_client,
                    p.code_compagnie,
                    p.code_branche,
                    b.nom_branche,
                    p.effet,
                    p.expiration,
                    p.etat,
                    p.prime_net,
                    p.num_imat,
                    rv.date_revalorisation,
                    rv.date_envoi
                FROM dbo."TPolice" p
                LEFT JOIN LATERAL (
                    SELECT tb.nom_branche
                    FROM dbo."TBranche" tb
                    WHERE tb.code_compagnie = p.code_compagnie
                      AND tb.code_branche = p.code_branche
                    LIMIT 1
                ) b ON TRUE
                LEFT JOIN dbo."TPoliceRevalorisation" rv ON rv.num_int = TRIM(p.num_int)
                WHERE {where_sql}
                ORDER BY p.effet DESC NULLS LAST, p.num_police, p.num_int
                {limit_sql}
            """),
            params
        ).mappings().all()

        if page is None:
            return [dict(row) for row in rows]

        total = conn.execute(
            text(f'SELECT COUNT(*) FROM dbo."TPolice" p WHERE {where_sql}'),
            params
        ).scalar_one()

    return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}
