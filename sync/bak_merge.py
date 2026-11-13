"""Brings the tables the app reads (dbo."TQuittance", dbo."TRegCli"...) in line with the old software's database (.BAK).

The .BAK, converted to PostgreSQL, is loaded in the lowercase twins of these tables (dbo.tquittance, dbo.tregcli...).
This compares each app table with its twin and, with --apply, makes the app table equal to it:

    py -m sync.bak_merge                       dry run on the online database (REMOTE_DATABASE_URL), changes nothing
    py -m sync.bak_merge --apply               pg_dump backup first, then the sync in one transaction
    py -m sync.bak_merge --load FILE.sql       first loads the converted .BAK (FILE.sql) in the lowercase tables
    py -m sync.bak_merge --target local        on DATABASE_URL instead

Kept from the app: its own tables (TEncaissementValide, TJournalValide, ...), its own columns (client phones and emails,
sinistre fields), the encrypted passwords and lieu of RUtilisateur_Depot, and its activity log rows (TMouchard: the
.BAK rows are added). The app's journal ticks (TEncaissementValide, TJournalChoix) are cleared: the comptabilisations
are then the old software's. The rows only in the app table (before the sync) are saved to a CSV next to the backup.
"""

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "deploy" / "agency" / "backups"
PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
TICKS = ["TEncaissementValide", "TJournalChoix"]
APPEND_ONLY = {"TMouchard"}
# big log tables: only their recent rows are compared (the online server drops a connection hashing all of them)
RECENT = {"TMouchard": ("dmvt", 120)}


def recent(app, prefix=""):
    """WHERE clause limiting the rows compared of `app` or of its .BAK twin (the column has the same lowercase name)."""
    if app not in RECENT:
        return ""
    col, days = RECENT[app]
    return f" WHERE {prefix}{q(col)} >= CURRENT_DATE - {days}"


USERS = "RUtilisateur_Depot"
# the comparisons hash whole tables: in memory, not in temporary files (the online disk is small, its work_mem tiny)
WORK_MEM = " -c work_mem=128MB"


def libpq(url: str) -> str:
    """The URL for psql / pg_dump: without the SQLAlchemy driver (postgresql+psycopg://)."""
    scheme, rest = url.split("://", 1)
    return "postgresql://" + rest if scheme.startswith("postgres") else url


def rowhash(names, prefix=""):
    """md5 of the columns as trimmed text, NULL marked: compares rows in little memory (no 100-argument limit)."""
    parts = [f"COALESCE(TRIM(CAST({prefix}{q(n)} AS text)), chr(1))" for n in names]
    return "md5(" + " || '|' || ".join(parts) + ")"

def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def columns(c, table):
    return c.execute(text("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attidentity <> '' AS ident
        FROM pg_attribute a WHERE a.attrelid = to_regclass(:t) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"""),
        {"t": f"dbo.{q(table)}"}).all()


def primary_key(c, table):
    return c.execute(text("""
        SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = to_regclass(:t) AND i.indisprimary"""), {"t": f"dbo.{q(table)}"}).scalars().all()


def pairs(c):
    tables = set(c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='dbo' AND table_type='BASE TABLE'")).scalars())
    return sorted((t, t.lower()) for t in tables if t != t.lower() and t.lower() in tables)


def plan(c):
    """Per table pair: common columns, app-only columns, rows only in the app table / only in the .BAK copy."""
    out = []
    for app, bak in pairs(c):
        ca, cb = columns(c, app), {n.lower(): n for n, _, _ in columns(c, bak)}
        common = [(n, cb[n.lower()], typ, ident) for n, typ, ident in ca if n.lower() in cb]
        extra = [n for n, _, _ in ca if n.lower() not in cb]
        ea = rowhash([a for a, _, _, _ in common])
        eb = rowhash([b for _, b, _, _ in common])
        w_ = recent(app)
        only_app = c.execute(text(f"SELECT COUNT(*) FROM (SELECT {ea} FROM dbo.{q(app)}{w_} EXCEPT ALL SELECT {eb} FROM dbo.{q(bak)}{w_}) x")).scalar()
        only_bak = c.execute(text(f"SELECT COUNT(*) FROM (SELECT {eb} FROM dbo.{q(bak)}{w_} EXCEPT ALL SELECT {ea} FROM dbo.{q(app)}{w_}) x")).scalar()
        if only_app or only_bak:
            out.append({"app": app, "bak": bak, "common": common, "extra": extra, "only_app": only_app, "only_bak": only_bak,
                        "pk": primary_key(c, app)})
    return out


def save_app_only(c, items, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["table", "row"])
        for it in items:
            ea = ", ".join(f"TRIM(CAST({q(a)} AS text)) AS {q(a)}" for a, _, _, _ in it["common"])
            eb = ", ".join(f"TRIM(CAST({q(b)} AS text))" for _, b, _, _ in it["common"])
            w_ = recent(it["app"])
            for row in c.execute(text(f"SELECT {ea} FROM dbo.{q(it['app'])}{w_} EXCEPT ALL SELECT {eb} FROM dbo.{q(it['bak'])}{w_}")).mappings():
                w.writerow([it["app"], {k: v for k, v in row.items() if v not in (None, "")}])


def apply(c, it):
    app, bak, common, extra, pk = it["app"], it["bak"], it["common"], it["extra"], it["pk"]
    cols = ", ".join(q(a) for a, _, _, _ in common)
    sel = ", ".join(f"CAST(b.{q(b_)} AS {typ})" for _, b_, typ, _ in common)
    overriding = " OVERRIDING SYSTEM VALUE" if any(ident for *_, ident in common) else ""
    # Only the rows that differ are touched (a row = the md5 of its common columns as trimmed text): the online disk is
    # small, rewriting whole tables does not fit.
    ha = rowhash([a for a, _, _, _ in common])
    hb = rowhash([b_ for _, b_, _, _ in common], "b.")
    c.execute(text("DROP TABLE IF EXISTS pg_temp.diff_app, pg_temp.diff_bak"))
    wa, wb = recent(app), recent(app, "b.")
    c.execute(text(f"CREATE TEMP TABLE diff_app ON COMMIT DROP AS SELECT DISTINCT h FROM (SELECT {ha} AS h FROM dbo.{q(app)}{wa} EXCEPT ALL SELECT {hb} FROM dbo.{q(bak)} b{wb}) x"))
    c.execute(text(f"CREATE TEMP TABLE diff_bak ON COMMIT DROP AS SELECT DISTINCT h FROM (SELECT {hb} AS h FROM dbo.{q(bak)} b{wb} EXCEPT ALL SELECT {ha} FROM dbo.{q(app)}{wa}) x"))
    if app in APPEND_ONLY:
        n = c.execute(text(f"INSERT INTO dbo.{q(app)} ({cols}){overriding} SELECT {sel} FROM dbo.{q(bak)} b{wb or ' WHERE TRUE'} AND {hb} IN (SELECT h FROM diff_bak)")).rowcount
        return f"+{n} lignes de l'ancien logiciel"
    # a row whose copies differ in number only is replaced as a whole: every copy deleted, the .BAK's copies inserted
    c.execute(text("INSERT INTO diff_app SELECT h FROM diff_bak EXCEPT SELECT h FROM diff_app"))
    keep = None
    if app == USERS:
        keep = c.execute(text(f'SELECT "Nom_Utilisateur", "ModP", {", ".join(q(e) for e in extra) or "NULL"} FROM dbo.{q(app)}')).all()
    elif extra and pk:
        c.execute(text(f"CREATE TEMP TABLE keep_extra ON COMMIT DROP AS SELECT {', '.join(q(k) for k in pk + extra)} FROM dbo.{q(app)} WHERE {ha} IN (SELECT h FROM diff_app)"))
    d = c.execute(text(f"DELETE FROM dbo.{q(app)} WHERE {ha} IN (SELECT h FROM diff_app)")).rowcount
    n = c.execute(text(f"INSERT INTO dbo.{q(app)} ({cols}){overriding} SELECT {sel} FROM dbo.{q(bak)} b WHERE {hb} IN (SELECT h FROM diff_app)")).rowcount
    if app == USERS:
        for name, pwd, *rest in keep:
            sets = {"p": pwd, "n": name}
            assign = ['"ModP" = :p'] if pwd and str(pwd).startswith("pbkdf2_sha256$") else []
            for i, e in enumerate(extra):
                assign.append(f"{q(e)} = :e{i}")
                sets[f"e{i}"] = rest[i]
            if assign:
                c.execute(text(f'UPDATE dbo.{q(app)} SET {", ".join(assign)} WHERE "Nom_Utilisateur" = :n'), sets)
    elif extra and pk:
        on = " AND ".join(f"t.{q(k)} = k.{q(k)}" for k in pk)
        c.execute(text(f"UPDATE dbo.{q(app)} t SET {', '.join(f'{q(e)} = k.{q(e)}' for e in extra)} FROM keep_extra k WHERE {on}"))
        c.execute(text("DROP TABLE keep_extra"))
    for a, _, _, ident in common:
        if ident:
            c.execute(text(f"SELECT setval(pg_get_serial_sequence(:t, :c), GREATEST(COALESCE((SELECT MAX({q(a)}) FROM dbo.{q(app)}), 0), 1))"),
                      {"t": f"dbo.{q(app)}", "c": a})
    return f"-{d} lignes de l'app, +{n} lignes de l'ancien logiciel"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--target", choices=["remote", "local"], default="remote")
    ap.add_argument("--load", help="converted .BAK (.sql) to load first in the lowercase tables")
    ap.add_argument("--url", help="another database (a rehearsal copy); overrides --target")
    args = ap.parse_args()
    env = dotenv_values(ROOT / ".env")
    url = args.url or (env["REMOTE_DATABASE_URL"] if args.target == "remote" else env.get("DATABASE_URL"))
    if args.url:
        args.target = "test"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    BACKUPS.mkdir(parents=True, exist_ok=True)

    if args.load:
        if not args.apply:
            print("--load écrit dans la base : ajoutez --apply.")
            return 2
        print("Chargement de", args.load)
        subprocess.run([str(PG_BIN / "psql.exe"), "-v", "ON_ERROR_STOP=1", "-q", "-d", libpq(url), "-f", args.load], check=True,
                       env={**os.environ, "PGOPTIONS": "-c search_path=dbo"})

    ro = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on" + WORK_MEM})
    with ro.connect() as c:
        c.execute(text("SET statement_timeout = 0"))
        items = plan(c)
        for it in items:
            print(f"{it['app']:28} seulement app {it['only_app']:>5} | seulement .BAK {it['only_bak']:>5} | colonnes app gardées {it['extra']}")
        report = BACKUPS / f"app-only-rows-{args.target}-{stamp}.csv"
        save_app_only(c, items, report)
        print("Lignes présentes seulement dans l'app :", report)
    if not items:
        print("Déjà identique à la .BAK.")
        return 0
    if not args.apply:
        print("Simulation : rien n'a été modifié (ajoutez --apply).")
        return 0

    dump = BACKUPS / f"{args.target}-before-bak-{stamp}.dump"
    print("Sauvegarde", dump)
    subprocess.run([str(PG_BIN / "pg_dump.exe"), "-Fc", "-n", "dbo", "-f", str(dump), libpq(url)], check=True)

    rw = create_engine(url, connect_args={"options": WORK_MEM.strip()})
    with rw.begin() as c:
        c.execute(text("SET statement_timeout = 0"))
        c.execute(text("SET lock_timeout = '30s'"))
        for it in items:
            print(f"{it['app']:28} {apply(c, it)}")
        for t in TICKS:
            if c.execute(text("SELECT to_regclass(:t)"), {"t": f"dbo.{q(t)}"}).scalar():
                print(f"{t:28} {c.execute(text(f'DELETE FROM dbo.{q(t)}')).rowcount} coches de l'app effacées")
    with ro.connect() as c:
        left = plan(c)
        print("Vérification :", "identique à la .BAK" if all(i["app"] in APPEND_ONLY | {USERS} or not i["only_bak"] and not i["only_app"] for i in left) else [(i["app"], i["only_app"], i["only_bak"]) for i in left])
    return 0


if __name__ == "__main__":
    sys.exit(main())
