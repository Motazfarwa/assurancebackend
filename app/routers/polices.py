
import os
import re
from datetime import date, datetime, timedelta
from secrets import randbelow

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.police_schemas import PoliceWithQuittanceCreate
from app.routers.records_search import STATUT_SQL

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


# The code branche of a company has its Nbr_Chiffre_Code characters (GAT 3, BIAT 4, MAGHREBIA 2).
def branch_length(conn, company) -> int | None:
    if company is None:
        return None
    return conn.execute(
        text('SELECT "Nbr_Chiffre_Code" FROM dbo."TCompagnie" WHERE code_compagnie = :c'), {"c": company}
    ).scalar()


# TPolice, TQuittance, TPoliceFlotte and Sinistre keep the code branche in 3 characters, TBranche in 4 (BIAT):
# widened to 4 the first time a 4-character code is stored. Growing a varchar loses nothing and rewrites no data.
BRANCHE_WIDTH_DDL = """
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['TPolice', 'TQuittance', 'TPoliceFlotte', 'Sinistre'] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'dbo' AND table_name = t
                   AND column_name = 'code_branche' AND character_maximum_length < 4) THEN
            EXECUTE format('ALTER TABLE dbo.%I ALTER COLUMN code_branche TYPE varchar(4)', t);
        END IF;
    END LOOP;
END $$;
"""


# N° police: the year (4 digits), the code branche, then as many digits as wanted; 15 characters at most (the column).
def validate_policy_number(number, branch, length=None):
    if length and len(branch) != length:
        raise HTTPException(
            status_code=422,
            detail=f"Le code branche de cette compagnie contient {length} caractères."
        )
    if not re.fullmatch(r"\d{4}" + re.escape(branch) + r"\d+", number):
        raise HTTPException(
            status_code=422,
            detail="Format attendu : année (4 chiffres) + code branche + chiffres"
        )
    if len(number) > 15:
        raise HTTPException(status_code=422, detail="Le numéro de police contient 15 caractères au maximum.")


@router.post("", status_code=201)
def create_police_with_quittance(
    payload: PoliceWithQuittanceCreate
):
    p = payload.police
    q = payload.quittance
    if q is None and not p.flotte:
        raise HTTPException(status_code=422, detail="La quittance est obligatoire pour une police qui n'est pas une flotte.")

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
                        'SELECT COALESCE(MAX(CAST(RIGHT(TRIM(num_police), 5) AS integer)), 0) '
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
                policy_number, p.code_branche, branch_length(conn, p.code_compagnie)
            )
            if len(p.code_branche) > 3:
                conn.execute(text(BRANCHE_WIDTH_DDL))

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
                        prime_total, flotte
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
                        :prime_total, :flotte
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
                    "flotte": "O" if p.flotte else "N",
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
                    "prime_net": q.prime_net if q else None,
                    "commission": q.commission if q else None,
                    "prime_total": q.prime_total if q else None,
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

            if q is not None:
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
            "num_quittance": receipt_id if q is not None else None,
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
                    nom_compagnie,
                    "Nbr_Chiffre_Code" AS nbr_chiffre_code
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
    code_branche = code_branche.strip().upper()

    with engine.connect() as conn:
        length = branch_length(conn, code_compagnie)
        if not code_branche.isalnum() or (length and len(code_branche) != length):
            raise HTTPException(
                status_code=422,
                detail=f"Le code branche de cette compagnie contient {length or 3} caractères"
            )
        # taux_comm: the commission rate (0.1 = 10 %), commission = prime nette × taux.
        result = conn.execute(
            text("""
                SELECT
                    TRIM(code_branche) AS code_branche,
                    code_compagnie,
                    TRIM(nom_branche) AS nom_branche,
                    taux_comm
                FROM dbo."TBranche"
                WHERE code_compagnie = :compagnie
                  AND TRIM(code_branche) = :branche
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

    if statut in STATUT_SQL:
        conditions.append(STATUT_SQL[statut])

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
