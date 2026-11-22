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

MAX_GROUPS = 200  # groups detailed per kind; the counts are always complete
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


def _classify(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    """A group of rows with the same key: which row only repeats another (incomplete), and the columns that differ."""
    values = [{column: _value(row.get(column)) for column in columns} for row in rows]
    differing = [column for column in columns if len({repr(item[column]) for item in values}) > 1]
    filled = [sum(item[column] is not None for column in columns) for item in values]

    def covered(small: dict[str, Any], large: dict[str, Any]) -> bool:
        return all(value is None or value == large[column] for column, value in small.items())

    states: list[str] = []
    for index, item in enumerate(values):
        others = [other for at, other in enumerate(values) if at != index]
        if any(other == item for other in others):
            states.append("identique")
        elif any(covered(item, other) and filled[at] > filled[index] for at, other in enumerate(values) if at != index):
            states.append("incomplete")
        else:
            states.append("complete")
    if all(state == "identique" for state in states):
        verdict = "identiques"
    elif "incomplete" in states and sum(state != "incomplete" for state in states) == 1:
        verdict = "incomplete"  # one full row, the others only repeat part of it
    else:
        verdict = "differentes"
    return {
        "verdict": verdict,
        "colonnes_differentes": differing,
        "lignes": [{"ctid": row["ctid"], "etat": state, "remplies": count, "valeurs": item}
                   for row, state, count, item in zip(rows, states, filled, values)],
    }


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

        if resume:
            return result

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
