
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
            internal_id = (
                "P" + str(conn.execute(
                    text("SELECT nextval('dbo.police_internal_seq')")
                ).scalar_one()).zfill(14)
            )

            receipt_id = (
                "Q" + str(conn.execute(
                    text("SELECT nextval('dbo.quittance_internal_seq')")
                ).scalar_one()).zfill(14)
            )

            if p.num_police:
                policy_number = p.num_police
            else:
                seq = conn.execute(
                    text("SELECT nextval('dbo.police_number_seq')")
                ).scalar_one()

                if seq > 99999:
                    raise HTTPException(
                        status_code=409,
                        detail="Séquence des polices épuisée"
                    )

                year = datetime.now().year
                policy_number = (
                    f"{year}{p.code_branche}{seq:05d}"
                )

            validate_policy_number(
                policy_number, p.code_branche
            )

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
                        "Garantie", "Franchise", capital,
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
                        :garantie, :franchise, :capital,
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
                    "obser": p.obser,
                    "prime_net": q.prime_net,
                    "commission": q.commission,
                    "prime_total": q.prime_total,
                }
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

    return {
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

    if not conditions:
        raise HTTPException(
            status_code=422,
            detail="Renseignez au moins un critère de recherche"
        )

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
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
                    SELECT tb.nom_branche
                    FROM dbo."TBranche" tb
                    WHERE tb.code_compagnie = p.code_compagnie
                      AND tb.code_branche = p.code_branche
                    LIMIT 1
                ) b ON TRUE
                WHERE {" AND ".join(conditions)}
                ORDER BY p.effet DESC NULLS LAST, p.num_police
                LIMIT 100
            """),
            params
        ).mappings().all()

    return [dict(row) for row in rows]
