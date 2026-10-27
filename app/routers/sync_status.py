"""Offline mode status for the frontend badge: is Render reachable, what is waiting, what conflicted.

On Render itself (no REMOTE_DATABASE_URL) the app is simply online and no sync runs there.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_admin, require_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/sync", tags=["Synchronisation"])

WORKER_SILENT_AFTER = timedelta(minutes=2)  # no attempt for that long: the worker is not running


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z") if value else None
    except ValueError:
        return None


@router.get("/status")
def sync_status(_: dict[str, Any] = Depends(require_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not settings.remote_database_url:
        return {"mode": "online"}
    try:
        state = dict(db.execute(text("SELECT key, value FROM sync.state")).all())
        pending = db.execute(text("SELECT count(*) FROM sync.changes WHERE id > :c"), {"c": int(state.get("pushed_upto") or 0)}).scalar()
        conflicts = db.execute(
            text(
                "SELECT id, at, direction, tbl, op, error FROM sync.conflicts WHERE resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 50"
            )
        ).mappings().all()
        open_conflicts = db.execute(text("SELECT count(*) FROM sync.conflicts WHERE resolved_at IS NULL")).scalar()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Synchronisation non installée sur ce serveur (sync/install.sql).") from exc
    last_attempt = _when(state.get("last_attempt_at"))
    return {
        "mode": "agency",
        "worker_running": bool(last_attempt and datetime.now(timezone.utc) - last_attempt < WORKER_SILENT_AFTER),
        "online": state.get("online") == "1",
        "pending_push": pending,
        "last_success_at": state.get("last_success_at"),
        "last_error": state.get("last_error"),
        "open_conflicts": open_conflicts,
        "conflicts": [{**row, "at": row["at"].isoformat()} for row in conflicts],
    }


@router.post("/conflicts/{conflict_id}/resolve", status_code=204)
def resolve_conflict(conflict_id: int, user: dict[str, Any] = Depends(require_admin), db: Session = Depends(get_db)) -> None:
    """The administrator checked both versions and corrected the record by hand where needed."""
    result = db.execute(
        text("UPDATE sync.conflicts SET resolved_at = now(), resolved_by = :u WHERE id = :id AND resolved_at IS NULL"),
        {"u": user["sub"], "id": conflict_id},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Conflit introuvable ou déjà traité.")
