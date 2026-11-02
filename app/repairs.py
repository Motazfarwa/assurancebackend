"""Data repairs run when the API starts; each one is idempotent (it changes nothing once the data is clean)."""
import logging

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

# A former version of the client screen read TClient.code (a legacy value, 788 on ~123 clients) as the contact's
# code and filled the contact's name from client 788 (SAID OUESLATI); saving the client then wrote that pair into
# code_locuteur / nom_locuteur, giving clients such as 4623 a Lien they never had. A contact typed in the new
# screens never matches this: the screen does not write TClient.code from the contact.
STALE_CONTACT = """
    UPDATE dbo."TClient" SET code_locuteur = '', nom_locuteur = ''
    WHERE code = 788 AND TRIM(code_locuteur) = '788' AND TRIM(code_client) <> '788'
"""


def run_repairs() -> None:
    db = SessionLocal()
    try:
        fixed = db.execute(text(STALE_CONTACT)).rowcount
        db.commit()
        if fixed:
            logger.warning("Lien retiré de %s client(s) : contact 788 recopié par erreur depuis TClient.code.", fixed)
    except Exception:  # noqa: BLE001 - a repair must never stop the API from starting
        db.rollback()
        logger.exception("Réparation des contacts (Lien) non effectuée.")
    finally:
        db.close()
