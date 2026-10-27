"""Sync worker of the agency server: replays the changes of each database on the other one.

    py -m sync.worker                      run forever (every SYNC_INTERVAL_SECONDS)
    py -m sync.worker --once               one pass, then exit
    py -m sync.worker --install local      install / update the sync schema (local, remote or both)
    py -m sync.worker --start-after-restore   right after restoring a dump of the online database on this PC (see deploy/agency)

The local database is DATABASE_URL, the online one (any PostgreSQL host: Render, OVH...) REMOTE_DATABASE_URL. Each side keeps, in its own sync.state,
"cursor:<other side>" = the last change of the other side applied there, updated in the same transaction as the
change itself: a pass interrupted by a lost connection resumes exactly where it stopped.
"""

import argparse
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError

from app.config import settings

LOCAL, REMOTE = "agency", "online"
BATCH_SIZE = 500
RETENTION_DAYS = 30
LOCK_ID = 726541001  # advisory lock: a single worker per agency database
INSTALL_SQL = Path(__file__).with_name("install.sql")

log = logging.getLogger("sync")


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


def get_state(conn: Connection, key: str, default: str | None = None) -> str | None:
    value = conn.execute(text("SELECT value FROM sync.state WHERE key = :k"), {"k": key}).scalar()
    return default if value is None else value


def set_state(conn: Connection, key: str, value: Any) -> None:
    conn.execute(
        text(
            "INSERT INTO sync.state (key, value, updated_at) VALUES (:k, :v, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        ),
        {"k": key, "v": None if value is None else str(value)},
    )


def _is_connection_problem(exc: DBAPIError) -> bool:
    return isinstance(exc, OperationalError) or exc.connection_invalidated


def record_conflict(local: Engine, direction: str, change: dict[str, Any], error: str | None = None) -> None:
    with local.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sync.conflicts (direction, source_change_id, tbl, op, old_row, new_row, error) "
                "VALUES (:d, :id, :tbl, :op, CAST(:old AS jsonb), CAST(:new AS jsonb), :error)"
            ),
            {
                "d": direction, "id": change["id"], "tbl": change["tbl"], "op": change["op"],
                "old": json.dumps(change["old_row"]) if change["old_row"] is not None else None,
                "new": json.dumps(change["new_row"]) if change["new_row"] is not None else None,
                "error": error,
            },
        )


def transfer(source: Engine, target: Engine, local: Engine, *, source_name: str, direction: str) -> int:
    """Replays on `target` the changes of `source` it has not applied yet; returns how many were replayed."""
    cursor_key = f"cursor:{source_name}"
    replayed = 0
    while True:
        with target.connect() as conn:
            cursor = int(get_state(conn, cursor_key, "0"))
        with source.connect() as conn:
            changes = conn.execute(
                text("SELECT id, tbl, op, old_row, new_row FROM sync.changes WHERE id > :c ORDER BY id LIMIT :n"),
                {"c": cursor, "n": BATCH_SIZE},
            ).mappings().all()
        if not changes:
            return replayed
        for change in changes:
            params = {
                "tbl": change["tbl"], "op": change["op"],
                "old": json.dumps(change["old_row"]) if change["old_row"] is not None else None,
                "new": json.dumps(change["new_row"]) if change["new_row"] is not None else None,
            }
            try:
                with target.begin() as conn:
                    applied = conn.execute(
                        text("SELECT sync.apply_change(:tbl, :op, CAST(:old AS jsonb), CAST(:new AS jsonb))"), params
                    ).scalar()
                    set_state(conn, cursor_key, change["id"])
                if not applied:
                    record_conflict(local, direction, change)
                    log.warning("%s: conflict on %s (%s) change %s", direction, change["tbl"], change["op"], change["id"])
            except DBAPIError as exc:
                if _is_connection_problem(exc):
                    raise
                # A change that cannot be replayed at all (table missing, bad data...) is set aside, not retried forever.
                record_conflict(local, direction, change, str(exc.orig or exc)[:500])
                with target.begin() as conn:
                    set_state(conn, cursor_key, change["id"])
                log.error("%s: change %s on %s set aside: %s", direction, change["id"], change["tbl"], exc.orig or exc)
            replayed += 1
        with target.begin() as conn:
            conn.execute(text("SELECT sync.bump_sequences()"))


def purge(source: Engine, target: Engine, source_name: str) -> None:
    """Old changes already replayed on the other side are no longer needed."""
    with target.connect() as conn:
        cursor = int(get_state(conn, f"cursor:{source_name}", "0"))
    with source.begin() as conn:
        conn.execute(
            text("DELETE FROM sync.changes WHERE id <= :c AND at < now() - make_interval(days => :d)"),
            {"c": cursor, "d": RETENTION_DAYS},
        )


def sync_once(local: Engine, remote: Engine) -> tuple[int, int]:
    with local.begin() as conn:
        set_state(conn, "last_attempt_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        pushed = transfer(local, remote, local, source_name=LOCAL, direction="push")
        pulled = transfer(remote, local, local, source_name=REMOTE, direction="pull")
        with remote.connect() as conn:
            pushed_upto = get_state(conn, f"cursor:{LOCAL}", "0")
            remote_last = conn.execute(text("SELECT COALESCE(max(id), 0) FROM sync.changes")).scalar()
        purge(local, remote, LOCAL)
        purge(remote, local, REMOTE)
    except DBAPIError as exc:
        with local.begin() as conn:
            set_state(conn, "online", "0")
            set_state(conn, "last_error", str(exc.orig or exc)[:500])
        raise
    with local.begin() as conn:
        set_state(conn, "online", "1")
        set_state(conn, "pushed_upto", pushed_upto)  # lets the status count pending changes while offline
        set_state(conn, "remote_last_change", remote_last)
        set_state(conn, "last_success_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        set_state(conn, "last_error", None)
    return pushed, pulled


def install(engine: Engine) -> int:
    with engine.begin() as conn:
        # Straight to psycopg, without parameters: the script's "%" (LIKE patterns, format()) must stay as written.
        conn.connection.driver_connection.execute(INSTALL_SQL.read_text(encoding="utf-8"))
        return conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgname = 'sync_capture_row'")).scalar()


def start_after_restore(local: Engine, remote: Engine) -> None:
    """The local database was just restored from a dump of the online database, which carried its change log as of the dump:
    its last id is exactly where pulling must start. The local log is then emptied, and the online side reads it
    from the beginning."""
    with local.begin() as conn:
        restored_upto = conn.execute(text("SELECT COALESCE(max(id), 0) FROM sync.changes")).scalar()
        conn.execute(text("TRUNCATE sync.changes, sync.conflicts RESTART IDENTITY"))
        conn.execute(text("DELETE FROM sync.state"))
        set_state(conn, f"cursor:{REMOTE}", restored_upto)
        set_state(conn, "pushed_upto", 0)
    with remote.begin() as conn:
        set_state(conn, f"cursor:{LOCAL}", 0)
    log.info("Sync initialised: pulling online changes after %s.", restored_upto)


@contextmanager
def single_worker(local: Engine):
    """Two workers replaying at once would apply changes twice: the second one stops."""
    with local.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}).scalar():
            raise SystemExit("Another sync worker is already running on this database.")
        lock_conn.commit()
        with local.begin() as conn:
            # Installs from before the online side was renamed kept its cursor under "cursor:render".
            legacy = get_state(conn, "cursor:render")
            if legacy is not None:
                if get_state(conn, f"cursor:{REMOTE}") is None:
                    set_state(conn, f"cursor:{REMOTE}", legacy)
                conn.execute(text("DELETE FROM sync.state WHERE key = 'cursor:render'"))
        yield


def run_forever(local: Engine, remote: Engine) -> None:
    with single_worker(local):
        log.info("Sync worker started (every %s s).", settings.sync_interval_seconds)
        while True:
            try:
                pushed, pulled = sync_once(local, remote)
                if pushed or pulled:
                    log.info("Synced: %s sent online, %s received.", pushed, pulled)
            except DBAPIError as exc:
                log.info("Online database unreachable, retrying later (%s).", str(exc.orig or exc).splitlines()[0])
            time.sleep(settings.sync_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--install", choices=["local", "remote", "both"])
    parser.add_argument("--start-after-restore", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not settings.database_url or not settings.remote_database_url:
        raise SystemExit("Set DATABASE_URL (agency database) and REMOTE_DATABASE_URL (online database) in .env.")
    local, remote = make_engine(settings.database_url), make_engine(settings.remote_database_url)

    if args.install:
        for name, engine in (("local", local), ("remote", remote)):
            if args.install in (name, "both"):
                log.info("%s: sync installed on %s tables.", name, install(engine))
    elif args.start_after_restore:
        start_after_restore(local, remote)
    elif args.once:
        with single_worker(local):
            pushed, pulled = sync_once(local, remote)
        log.info("Synced: %s sent online, %s received.", pushed, pulled)
    else:
        run_forever(local, remote)


if __name__ == "__main__":
    main()
