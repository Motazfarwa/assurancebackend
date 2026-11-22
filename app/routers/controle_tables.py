"""Contrôle des tables (administrateur): for each table of the database, the anomalies found in it, read only.

- doublons exacts: rows identical in every column (the same line entered or copied twice);
- doublons de clé: several rows with the same key (the N° that should be unique: code client, num_int, N° quittance...).
  Within such a group a row is « incomplète » when every value it has is also in another row of the group, which has more
  (the same information, one of them missing some of it): that one is the copy to remove. Otherwise the rows « diffèrent »
  (the columns that differ are given) and a person has to choose;
- clés vides: rows whose key is empty.

The legacy tables have no primary key: each row is shown with its physical address (ctid) to find it again.
Nothing is changed by this module.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db

router = APIRouter(prefix="/api/controle-tables", tags=["Contrôle des tables"], dependencies=[Depends(require_admin)])

# The key of the main tables (what should be unique in them). Any other table: no key unless one is chosen.
KNOWN_KEYS: dict[str, list[str]] = {
    "TClient": ["code_client"],
    "TPolice": ["num_int"],
    "TQuittance": ["num_quittance"],
    "TRegCli": ["num_reg_cli"],
    "RDetRegCli": ["Num_reg_cli", "Num_quitance"],
    "TDepense": ["Typ_dep", "num_dep"],  # each type (T technique, D frais généraux, E, S) has its own numbering
    "TPiece": ["nump", "typep", "code_client"],
    "RPiece": ["NumP", "TypeP", "Num_Quittance"],
    "TBordereau": ["Code_Compagnie", "tc", "Periode", "num_quittance"],
    "Sinistre": ["Num_Sinistre_Agence"],
    "TCompagnie": ["code_compagnie"],
    "TBranche": ["code_compagnie", "code_branche"],
    "TBanque": ["code_banque"],
    "TEncaissementValide": ["Num_reg_cli", "Num_quitance"],
    "RQuittance_Attestation": ["Code_Compagnie", "Num_Quittance"],  # an attestation is not always numbered
    "TJEQValider": ["NUM_JEQ", "code_compagnie"],
}

# Probable duplicates: rows describing the same operation although their N° differ (the same payment entered twice,
# one copy without its client: TDepense 10611 / 10614, 2 264,589 by chèque ATT0000039 on 21/08/2026). For the main
# tables, what makes the operation; every listed column must be filled. Any other table with a key: the same content
# once its key and the columns of who / when entered it are left aside.
SIGNATURES: dict[str, list[tuple[str, list[str]]]] = {
    "TDepense": [("Même dépense (type, date, montant, pièce, banque, objet)", ["Typ_dep", "date_dep", "mont_dep", "num_cheq", "code_banque", "objet_dep"])],
    "TQuittance": [("Même quittance (police, échéance, TC, prime totale)", ["num_int", "echeance", "tc", "prime_total"])],
    "TPolice": [("Même police (N° police, compagnie)", ["num_police", "code_compagnie"])],
    "TClient": [("Même nom de client", ["nom_client"]), ("Même N° CIN", ["NumeroCIN"])],
    "TRegCli": [("Même règlement (client, date, type, pièce, montant)", ["code_client", "date_reg_cli", "type_reg_cli", "num_piece", "mont_reg_cli"])],
    "TPiece": [("Même pièce (N°, type, montant, banque)", ["nump", "typep", "montant", "code_banque"])],
    "Sinistre": [("Même sinistre compagnie", ["Code_Compagnie_Assure", "Num_Sinistre_Compagnie_Assure"]),
                 ("Même police et même date de sinistre", ["Num_Int", "Date_Sinistre"])],
}
AUDIT = re.compile(r"^(create|update)_(user|date)$|^code_agence$|^date_m$|^date_maj$", re.IGNORECASE)

MAX_GROUPS = 500  # groups detailed per kind; the counts are always complete
MAX_SCANNED = 20000  # probable groups classified per check
TIMEOUT = "120s"


def _fail(exc: SQLAlchemyError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Analyse impossible : {str(getattr(exc, 'orig', exc))[:250]}")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables(db: Session) -> dict[str, int]:
    rows = db.execute(text(
        "SELECT c.relname AS name, GREATEST(c.reltuples, 0)::bigint AS estimate FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'dbo' AND c.relkind = 'r' ORDER BY c.relname"
    )).mappings().all()
    return {row["name"]: int(row["estimate"]) for row in rows}


def _columns(db: Session, table: str) -> list[str]:
    return [row[0] for row in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'dbo' AND table_name = :t ORDER BY ordinal_position"
    ), {"t": table})]


def _value(value: Any) -> Any:
    """A value as compared and shown: strings trimmed, '' as nothing."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return "<binaire>"
    return value


def _checked(db: Session, table: str, cles: str | None) -> tuple[list[str], list[str]]:
    if table not in _tables(db):
        raise HTTPException(status_code=404, detail=f"Table inconnue : {table}")
    columns = _columns(db, table)
    if cles is None:
        keys = [key for key in KNOWN_KEYS.get(table, []) if key in columns]
    else:
        keys = [key for key in (part.strip() for part in cles.split(",")) if key]
        unknown = [key for key in keys if key not in columns]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Colonne inconnue dans {table} : {', '.join(unknown)}")
    return columns, keys


def _key_sql(keys: list[str]) -> list[str]:
    return [f"NULLIF(TRIM(CAST(t.{_quote(key)} AS text)), '')" for key in keys]


def _classify(rows: list[dict[str, Any]], columns: list[str], keys: list[str] | None = None) -> dict[str, Any]:
    """A group of rows with the same key: which row only repeats another (incomplete), and the columns that differ."""
    values = [{column: _value(row.get(column)) for column in columns} for row in rows]
    differing = [column for column in columns if len({repr(item[column]) for item in values}) > 1]
    filled = [sum(item[column] is not None for column in columns) for item in values]
    # what each row lacks: the columns empty in it and filled in another row of the group
    missing = [[column for column in columns if item[column] is None and any(other[column] is not None for other in values)] for item in values]

    def covered(small: dict[str, Any], large: dict[str, Any]) -> bool:
        return all(value is None or value == large[column] for column, value in small.items())

    ignored = set(keys or [])

    def conflicts(item: dict[str, Any], other: dict[str, Any]) -> int:
        """Columns filled in both rows with different values (the N° and the who / when columns aside)."""
        return sum(1 for column in columns if column not in ignored and not AUDIT.search(column)
                   and item[column] is not None and other[column] is not None and item[column] != other[column])

    states: list[str] = []
    for index, item in enumerate(values):
        others = [other for at, other in enumerate(values) if at != index]
        if any(other == item for other in others):
            states.append("identique")
        elif any(covered(item, other) and filled[at] > filled[index] for at, other in enumerate(values) if at != index):
            states.append("incomplete")
        elif missing[index] and any(filled[at] > filled[index] and conflicts(item, other) <= 2 for at, other in enumerate(values) if at != index):
            # lacks information a fuller copy has (10611 without its code client), the rest the same but for 1 or 2
            # values (its sinistre letter, its comptabilisation)
            states.append("incomplete")
        else:
            states.append("complete")
    if all(state == "identique" for state in states):
        verdict = "identiques"
    elif "incomplete" in states:
        verdict = "incomplete"  # a copy lacks information another has: the one to check first
    elif keys is not None and all(column in keys or AUDIT.search(column) for column in differing):
        verdict = "meme_contenu"  # only the N° differs: entered several times, or several real identical lines
    else:
        verdict = "differentes"
    return {
        "verdict": verdict,
        "colonnes_differentes": differing,
        "lignes": [{"ctid": row["ctid"], "etat": state, "remplies": count, "manque": lacks, "valeurs": item}
                   for row, state, count, lacks, item in zip(rows, states, filled, missing, values)],
    }


def _norm(column: str) -> str:
    return f"NULLIF(UPPER(TRIM(CAST(t.{_quote(column)} AS text))), '')"


def _probable_checks(table: str, columns: list[str], keys: list[str]) -> list[dict[str, Any]]:
    """The ways rows of the table can be the same operation under different N°: (label, SQL expression(s), params)."""
    checks = []
    for label, wanted in SIGNATURES.get(table, []):
        if all(column in columns for column in wanted):
            checks.append({"label": label, "colonnes": wanted, "exprs": [_norm(column) for column in wanted], "params": {}})
    # the generic rule only for a table numbered by one column (with a key of several columns, the rest is too little)
    if not checks and len(keys) == 1:
        ignored = [column for column in columns if column in keys or AUDIT.search(column)]
        if len(columns) - len(ignored) >= 2:
            checks.append({"label": "Même contenu, N° différent", "colonnes": [column for column in columns if column not in ignored],
                           "exprs": ["(to_jsonb(t) - CAST(:ignored AS text[]))"], "params": {"ignored": ignored}})
    return checks


def _probables(db: Session, source: str, table: str, columns: list[str], keys: list[str], detail: bool) -> dict[str, Any]:
    """The groups of rows that are the same operation (see SIGNATURES), each read with its rows in one query and classified:
    a copy lacking information (incomplete), different information, identical, or only the N° differing."""
    groups_count = rows_count = 0
    details: list[dict[str, Any]] = []
    for check in _probable_checks(table, columns, keys):
        exprs = check["exprs"]
        # every column filled with a real value: not a placeholder (".", "-", "0", "0.000") that unrelated rows share
        where = " AND ".join(f"{expr} IS NOT NULL" + ("" if expr.startswith("(to_jsonb") else f" AND {expr} ~ '[0-9A-Z]' AND {expr} !~ '^[0.,[:space:]-]*$'")
                             for expr in exprs)
        found = db.execute(text(
            f"SELECT {', '.join(f'{expr} AS s{at}' for at, expr in enumerate(exprs))}, COUNT(*) AS n, "
            f"jsonb_agg(to_jsonb(t) || jsonb_build_object('__ctid', t.ctid::text)) AS lignes FROM {source} WHERE {where} "
            f"GROUP BY {', '.join(exprs)} HAVING COUNT(*) > 1 AND COUNT(DISTINCT to_jsonb(t)) > 1 "  # not only identical copies
            f"ORDER BY COUNT(*) DESC LIMIT {MAX_SCANNED}"
        ), check["params"]).mappings().all()
        groups_count += len(found)
        for group in found:
            rows_count += int(group["n"])
            rows = [{"ctid": row.pop("__ctid"), **row} for row in group["lignes"]]
            info = _classify(rows, columns, keys)
            signature = {column: group[f"s{at}"] for at, column in enumerate(check["colonnes"])} if len(exprs) == len(check["colonnes"]) else {}
            details.append({"type": check["label"], "signature": signature, "nombre": int(group["n"]), **info})
    # a copy lacking information first (the most likely to remove), then the largest groups
    order = {"incomplete": 0, "differentes": 1, "identiques": 2, "meme_contenu": 3}
    details.sort(key=lambda item: (order.get(item["verdict"], 9), -item["nombre"]))
    verdicts = {name: sum(item["verdict"] == name for item in details) for name in order}
    return {"groupes": groups_count, "lignes": rows_count, "verdicts": verdicts, "details": details[:MAX_GROUPS] if detail else []}


@router.get("")
def list_tables(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The tables of the database (estimated number of rows) and the key known for each."""
    try:
        tables = _tables(db)
        columns = {
            row["t"]: row["c"] for row in db.execute(text(
                "SELECT table_name AS t, array_agg(column_name::text ORDER BY ordinal_position) AS c "
                "FROM information_schema.columns WHERE table_schema = 'dbo' GROUP BY table_name"
            )).mappings().all()
        }
    except SQLAlchemyError as exc:
        raise _fail(exc) from exc
    return {"items": [
        {"table": name, "lignes_estimees": estimate, "colonnes": columns.get(name, []),
         "cle": [key for key in KNOWN_KEYS.get(name, []) if key in columns.get(name, [])]}
        for name, estimate in tables.items()
    ]}


@router.get("/scan")
def scan_table(
    table: str = Query(..., min_length=1, max_length=128),
    cles: str | None = Query(default=None, max_length=1000, description="Key columns, comma separated (default: the known key)"),
    resume: bool = Query(default=False, description="Only the counts (scan of every table)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The anomalies of a table: exact duplicates, duplicates of its key, empty keys."""
    columns, keys = _checked(db, table, cles)
    source = f"dbo.{_quote(table)} t"
    try:
        db.execute(text(f"SET LOCAL statement_timeout = '{TIMEOUT}'"))
        total = int(db.execute(text(f"SELECT COUNT(*) FROM {source}")).scalar() or 0)

        exact = db.execute(text(
            f"SELECT COUNT(*) AS groupes, COALESCE(SUM(n - 1), 0) AS en_trop FROM "
            f"(SELECT COUNT(*) AS n FROM {source} GROUP BY to_jsonb(t) HAVING COUNT(*) > 1) g"
        )).mappings().one()
        result: dict[str, Any] = {
            "table": table, "colonnes": columns, "cle": keys, "lignes": total,
            "doublons_exacts": {"groupes": int(exact["groupes"]), "lignes_en_trop": int(exact["en_trop"])},
        }

        if keys:
            key_sql = _key_sql(keys)
            not_empty = " AND ".join(f"{expr} IS NOT NULL" for expr in key_sql)
            dup = db.execute(text(
                f"SELECT COUNT(*) AS groupes, COALESCE(SUM(n), 0) AS lignes FROM "
                f"(SELECT COUNT(*) AS n FROM {source} WHERE {not_empty} GROUP BY {', '.join(key_sql)} HAVING COUNT(*) > 1) g"
            )).mappings().one()
            empty = int(db.execute(text(
                f"SELECT COUNT(*) FROM {source} WHERE " + " OR ".join(f"{expr} IS NULL" for expr in key_sql)
            )).scalar() or 0)
            result["doublons_cle"] = {"groupes": int(dup["groupes"]), "lignes": int(dup["lignes"])}
            result["cles_vides"] = empty
        else:
            result["doublons_cle"] = None
            result["cles_vides"] = None

        probable = _probables(db, source, table, columns, keys, detail=not resume)
        result["probables"] = {"groupes": probable["groupes"], "lignes": probable["lignes"], "verdicts": probable["verdicts"]}
        if resume:
            return result
        result["probables_details"] = probable["details"]

        # The exact duplicates: one row of each group, and how many times it is there.
        exact_rows = db.execute(text(
            f"SELECT to_jsonb(t) AS ligne, COUNT(*) AS n, array_agg(t.ctid::text) AS ctids FROM {source} "
            f"GROUP BY to_jsonb(t) HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC LIMIT {MAX_GROUPS}"
        )).mappings().all()
        result["exacts"] = [
            {"fois": int(row["n"]), "ctids": list(row["ctids"]), "valeurs": {column: _value(row["ligne"].get(column)) for column in columns}}
            for row in exact_rows
        ]

        result["groupes"] = []
        result["vides"] = []
        if keys:
            key_sql = _key_sql(keys)
            not_empty = " AND ".join(f"{expr} IS NOT NULL" for expr in key_sql)
            groups = db.execute(text(
                f"SELECT {', '.join(f'{expr} AS k{at}' for at, expr in enumerate(key_sql))}, COUNT(*) AS n FROM {source} "
                f"WHERE {not_empty} GROUP BY {', '.join(key_sql)} HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, 1 LIMIT {MAX_GROUPS}"
            )).mappings().all()
            for group in groups:
                params = {f"k{at}": group[f"k{at}"] for at in range(len(keys))}
                rows = db.execute(text(
                    f"SELECT t.ctid::text AS ctid, to_jsonb(t) AS ligne FROM {source} WHERE "
                    + " AND ".join(f"{expr} = :k{at}" for at, expr in enumerate(key_sql))
                ), params).mappings().all()
                detail = _classify([{"ctid": row["ctid"], **row["ligne"]} for row in rows], columns)
                result["groupes"].append({"cle": {key: group[f"k{at}"] for at, key in enumerate(keys)}, "nombre": int(group["n"]), **detail})
            empty_rows = db.execute(text(
                f"SELECT t.ctid::text AS ctid, to_jsonb(t) AS ligne FROM {source} WHERE "
                + " OR ".join(f"{expr} IS NULL" for expr in key_sql) + f" LIMIT {MAX_GROUPS}"
            )).mappings().all()
            result["vides"] = [{"ctid": row["ctid"], "valeurs": {column: _value(row["ligne"].get(column)) for column in columns}} for row in empty_rows]
        result["limite"] = MAX_GROUPS
        return result
    except SQLAlchemyError as exc:
        db.rollback()
        raise _fail(exc) from exc
    finally:
        db.rollback()  # read only: nothing to keep (and the statement_timeout ends with it)
