"""Every "label : value" read on a document by OCR, so that nothing read is lost.

Each module's parser takes the values it knows (policy, amounts, dates…); this lists, in addition, all the labelled
values of the text - printed forms ("N° CONTRAT 2520065437", "Agence : 144") and the boxes filled in by hand - to be
shown in the import window.
"""
import re
from typing import Any

# Printed labels of the agency's documents (contracts, quittances, constats, RIB…), most specific first. A line that
# starts with one of them is read as that label followed by its value, even without a colon.
KNOWN_LABELS = [
    "N° CONTRAT", "N° POLICE", "N° QUITTANCE", "N° SINISTRE", "N° ATTESTATION", "N° D'IMMATRICULATION", "N° IMMAT",
    "N° CHASSIS", "N° SERIE", "N° CIN", "N° C.I.N", "N° PERMIS", "PERMIS DE CONDUIRE N°", "N° DE COMPTE", "RIB",
    "SOUSCRIPTEUR", "ASSURE", "ASSURÉ", "ADRESSE", "VILLE", "AGENCE", "TELEPHONE", "TÉLÉPHONE", "TEL", "TÉL", "GSM",
    "E-MAIL", "EMAIL", "PROFESSION", "NOM", "PRENOM", "PRÉNOM", "DATE DE NAISSANCE", "LIEU DE NAISSANCE",
    "NATIONALITE", "NATIONALITÉ", "FRACTIONNEMENT", "A EFFET DU", "EFFET", "JUSQU'AU", "ECHEANCE", "ÉCHÉANCE",
    "EXPIRATION", "DATE D'EFFET", "DATE", "HEURE", "LIEU", "MARQUE", "TYPE", "GENRE", "ENERGIE", "PUISSANCE",
    "IMMATRICULATION", "USAGE", "CATEGORIE DU RISQUE", "CATÉGORIE DU RISQUE", "VALEUR A NEUF", "VALEUR VENALE",
    "CAPITAL", "FRANCHISE", "PRIME NETTE", "PRIME TOTALE", "PRIME TTC", "TAXES", "TIMBRE", "COMMISSION", "TOTAL",
    "MONTANT", "BANQUE", "TITULAIRE", "VEHICULE ASSURE PAR", "VÉHICULE ASSURÉ PAR", "POLICE D'ASSURANCE N°",
    "ATTESTATION VALABLE", "DELIVRE LE", "DÉLIVRÉ LE", "SENS SUIVI", "VENANT DE", "ALLANT A", "ALLANT À",
    "DEGATS APPARENTS", "DÉGÂTS APPARENTS", "OBSERVATIONS", "TEMOINS", "TÉMOINS", "BLESSES", "BLESSÉS",
]
_KNOWN = sorted(KNOWN_LABELS, key=len, reverse=True)
_LABEL_RE = re.compile(r"^\s*([A-Za-zÀ-ÿ°'’.\- ]{2,40}?)\s*[:：]\s*(.+?)\s*$")
_GAP = re.compile(r"\s{3,}")  # the OCR keeps wide gaps between the cells of a row


def _clean(value: str) -> str:
    return re.sub(r"\s{2,}", " ", value.replace("…", "").strip(" .:;-_|"))


def label_values(text: str, limit: int = 120) -> list[dict[str, Any]]:
    """[{libelle, valeur}] for every labelled value of the text, in reading order, without duplicates."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, value: str) -> None:
        label, value = _clean(label), _clean(value)
        if len(label) < 2 or not value or len(value) > 120 or not re.search(r"[A-Za-z0-9À-ÿ]", value):
            return
        key = (label.upper(), value.upper())
        if key not in seen:
            seen.add(key)
            found.append({"libelle": label[:1].upper() + label[1:].lower() if label.isupper() else label, "valeur": value})

    def read_cell(line: str) -> None:
        line = line.strip()
        if len(line) < 3:
            return
        upper = line.upper()
        known = next((label for label in _KNOWN if upper.startswith(label + " ") or upper.startswith(label + ":") or upper == label), None)
        if known:
            value = line[len(known):].lstrip(" :：")
            # "A EFFET DU 07/05/2026 JUSQU'AU 31/07/2026": a second printed label inside the value starts a new cell
            following = _next_label(value)
            if following is not None:
                add(line[:len(known)], value[:following])
                read_cell(value[following:])
            else:
                add(line[:len(known)], value)
            return
        match = _LABEL_RE.match(line)
        if match:
            add(match.group(1), match.group(2))

    for raw_line in text.splitlines():
        # a table row of several "label value" cells is read cell by cell
        for cell in _GAP.split(raw_line):
            read_cell(cell)
        if len(found) >= limit:
            break
    return found[:limit]


def _next_label(value: str) -> int | None:
    """Where another known label starts inside a value: printed in capitals as on the form, or followed by ':'."""
    best = None
    for label in _KNOWN:
        if len(label) < 4:
            continue  # short labels (NOM, TEL, RIB…) are too often part of a value
        for match in re.finditer(r"(?<=\s)" + re.escape(label) + r"(?=\s|:|$)", value, re.IGNORECASE):
            printed = value[match.start():match.end()]
            if printed.isupper() or value[match.end():match.end() + 2].lstrip().startswith(":"):
                if best is None or match.start() < best:
                    best = match.start()
                break
    return best
