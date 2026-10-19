import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings


bearer = HTTPBearer(auto_error=False)
ACCESS_TOKEN_LIFETIME_SECONDS = 15 * 60
REFRESH_TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60
HASH_ITERATIONS = 600_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return f"pbkdf2_sha256${HASH_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Return (matches, needs_upgrade); legacy values are plain text."""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt, expected = stored.split("$", 3)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64decode(salt), int(rounds))
            return hmac.compare_digest(actual, _b64decode(expected)), False
        except (ValueError, TypeError):
            return False, False
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8")), True


def _sign_token(username: str, lieu: str | None, token_type: str, lifetime: int) -> str:
    now = int(time.time())
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64encode(json.dumps({"sub": username, "lieu": lieu, "typ": token_type, "iat": now, "exp": now + lifetime}, separators=(",", ":")).encode())
    message = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(settings.auth_secret_key.encode("utf-8"), message, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64encode(signature)}"


def create_access_token(username: str, lieu: str | None) -> str:
    return _sign_token(username, lieu, "access", ACCESS_TOKEN_LIFETIME_SECONDS)


def create_refresh_token(username: str, lieu: str | None) -> str:
    return _sign_token(username, lieu, "refresh", REFRESH_TOKEN_LIFETIME_SECONDS)


def _decode_token(token: str, expected_type: str) -> dict[str, Any]:
    try:
        header, payload, signature = token.split(".")
        message = f"{header}.{payload}".encode("ascii")
        expected = hmac.new(settings.auth_secret_key.encode("utf-8"), message, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            raise ValueError("Invalid signature")
        claims = json.loads(_b64decode(payload))
        if claims.get("exp", 0) < int(time.time()) or not claims.get("sub"):
            raise ValueError("Expired or incomplete token")
        if claims.get("typ", "access") != expected_type:
            raise ValueError("Wrong token type")
        return claims
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée.", headers={"WWW-Authenticate": "Bearer"}) from exc


def decode_access_token(token: str) -> dict[str, Any]:
    return _decode_token(token, "access")


def decode_refresh_token(token: str) -> dict[str, Any]:
    return _decode_token(token, "refresh")


def require_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Connexion requise.", headers={"WWW-Authenticate": "Bearer"})
    return decode_access_token(credentials.credentials)


def require_admin(user: dict[str, Any] = Depends(require_current_user)) -> dict[str, Any]:
    if str(user["sub"]).casefold() != "admin":
        raise HTTPException(status_code=403, detail="Accès réservé à l'administrateur.")
    return user
