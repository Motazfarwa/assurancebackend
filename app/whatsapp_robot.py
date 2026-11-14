"""The WhatsApp robot: the agency's WhatsApp account, linked to the server like WhatsApp Web (library neonize), sends the
mémoires de règlement directly, free of charge.

This is not the official WhatsApp API: WhatsApp may block the number that is linked (use a number kept for this), and the
link may stop working when WhatsApp changes. The administrator links the number once (QR code or code by phone number);
the session is kept in the database, so the robot reconnects alone when the server restarts.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings

logger = logging.getLogger("app.whatsapp_robot")

DEVICE_NAME = "agence-memoire"
_lock = threading.RLock()
_state: dict[str, Any] = {"statut": "arrete", "qr": None, "qr_le": None, "code": None, "numero": None, "erreur": None}
_client: Any = None
_thread: threading.Thread | None = None


def _set(**values: Any) -> None:
    with _lock:
        _state.update(values)


def status() -> dict[str, Any]:
    with _lock:
        state = dict(_state)
    state["actif"] = bool(_thread and _thread.is_alive())
    return state


def session_store() -> str:
    """Where whatsmeow keeps the session: a postgres:// URL (the app's database by default), or a SQLite file."""
    if settings.whatsapp_robot_database_url:
        return settings.whatsapp_robot_database_url
    if settings.database_url and settings.database_url.startswith("postgres"):
        return make_url(settings.database_url).set(drivername="postgres").render_as_string(hide_password=False)
    return str(Path(__file__).resolve().parent.parent / "whatsapp_robot.sqlite3")


def has_session() -> bool:
    """A number has been linked before (the robot can reconnect without a QR code)."""
    store = session_store()
    if not store.startswith("postgres"):
        return Path(store).exists()
    from app.database import engine
    if engine is None:
        return False
    try:
        with engine.connect() as connection:
            if connection.execute(text("SELECT to_regclass('public.whatsmeow_device')")).scalar() is None:
                return False
            return bool(connection.execute(text("SELECT EXISTS (SELECT 1 FROM public.whatsmeow_device)")).scalar())
    except Exception as exc:  # noqa: BLE001 - no session readable: the robot is simply not started
        logger.warning("WhatsApp robot: session not readable: %s", exc)
        return False


def _own_number(client: Any) -> str | None:
    try:
        return client.get_me().JID.User or None
    except Exception:  # noqa: BLE001 - only shown to the administrator
        return None


def _run(client: Any) -> None:
    try:
        client.connect()  # blocking until stop() / logout
    except Exception as exc:  # noqa: BLE001
        logger.warning("WhatsApp robot stopped: %s", exc)
        _set(statut="erreur", erreur=str(exc)[:300], qr=None)
        return
    with _lock:
        if _state["statut"] not in ("deconnecte", "erreur"):
            _state.update(statut="arrete", qr=None, code=None)


def start() -> dict[str, Any]:
    """Starts the robot (nothing if it runs): it reconnects with the saved session, or shows a QR code to link a number."""
    global _client, _thread
    with _lock:
        if _thread and _thread.is_alive():
            return status()
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, DisconnectedEv, LoggedOutEv, PairStatusEv
        except Exception as exc:  # noqa: BLE001 - library or system library (libmagic) missing on the server
            _set(statut="indisponible", erreur=f"La bibliothèque WhatsApp n’est pas installée sur le serveur : {exc}"[:300])
            return status()

        client = NewClient(session_store(), uuid=DEVICE_NAME)

        def on_qr(_client_: Any, data: bytes) -> None:
            _set(statut="qr", qr=data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data), qr_le=time.time(), erreur=None)

        def on_connected(connected: Any, _event: Any) -> None:
            _set(statut="connecte", qr=None, code=None, erreur=None, numero=_own_number(connected) or _state["numero"])
            logger.info("WhatsApp robot connected (%s)", _state["numero"])

        def on_paired(_client_: Any, event: Any) -> None:
            try:
                _set(numero=event.ID.User or _state["numero"])
            except Exception:  # noqa: BLE001
                pass

        def on_logged_out(_client_: Any, _event: Any) -> None:
            _set(statut="deconnecte", numero=None, qr=None, code=None)
            logger.warning("WhatsApp robot: the number was unlinked")

        def on_disconnected(_client_: Any, _event: Any) -> None:
            with _lock:
                if _state["statut"] == "connecte":
                    _state["statut"] = "reconnexion"

        client.qr(on_qr)
        client.event(ConnectedEv)(on_connected)
        client.event(PairStatusEv)(on_paired)
        client.event(LoggedOutEv)(on_logged_out)
        client.event(DisconnectedEv)(on_disconnected)
        _client = client
        _set(statut="demarrage", qr=None, code=None, erreur=None)
        _thread = threading.Thread(target=_run, args=(client,), daemon=True, name="whatsapp-robot")
        _thread.start()
    return status()


def stop(timeout: float = 8) -> None:
    global _client, _thread
    with _lock:
        client, thread = _client, _thread
        _client, _thread = None, None
    if client is not None:
        try:
            client.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WhatsApp robot: stop failed: %s", exc)
    if thread is not None:
        thread.join(timeout)
    _set(statut="arrete", qr=None, code=None)


def restart() -> dict[str, Any]:
    stop()
    return start()


def _wait(statuses: tuple[str, ...], seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _state["statut"] in statuses:
            return True
        if _state["statut"] in ("erreur", "indisponible"):
            return False
        time.sleep(0.3)
    return _state["statut"] in statuses


def pair_code(phone: str) -> str:
    """Links by phone number instead of the QR code: the code to type in WhatsApp → Appareils connectés."""
    start()
    if _state["statut"] == "connecte":
        raise RuntimeError("Un numéro est déjà connecté : déconnectez-le d’abord.")
    if not _wait(("qr",), 25):
        raise RuntimeError(_state["erreur"] or "Le robot n’a pas pu démarrer : réessayez.")
    code = _client.PairPhone(phone, True)
    _set(code=code)
    return code


def logout() -> None:
    client = _client
    if client is not None:
        try:
            client.logout()
        except Exception as exc:  # noqa: BLE001 - the session is dropped anyway
            logger.warning("WhatsApp robot: logout failed: %s", exc)
    stop()
    _set(statut="deconnecte", numero=None)


def configured() -> bool:
    """The robot is used for the mémoires: it runs, or a number was linked before."""
    return bool(_thread and _thread.is_alive()) or has_session()


def send_document(number: str, pdf: bytes, filename: str, caption: str) -> None:
    """The PDF with the message as caption, to the client's WhatsApp (raises when the robot is not connected)."""
    if _state["statut"] != "connecte":
        start()
        if not _wait(("connecte",), 30):
            raise RuntimeError("le robot WhatsApp n’est pas connecté (Administration → WhatsApp : scannez le QR code).")
    from neonize.utils import build_jid
    _client.send_document(build_jid(number), pdf, caption=caption[:1000] or None, filename=filename, mimetype="application/pdf")


def start_if_linked() -> None:
    """At the server start: the robot reconnects when a number was linked."""
    try:
        if has_session():
            start()
    except Exception as exc:  # noqa: BLE001 - never stops the server
        logger.warning("WhatsApp robot not started: %s", exc)
