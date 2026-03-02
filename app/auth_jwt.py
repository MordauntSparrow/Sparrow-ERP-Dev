"""
Session JWT for API clients (e.g. Lovable, mobile) that prefer Bearer token over cookies.
Uses app SECRET_KEY; tokens are short-lived (default 24h).
"""
import os
from datetime import datetime, timedelta, timezone

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None

# Default lifetime when not specified
DEFAULT_EXPIRY_HOURS = 24


def _get_secret():
    return os.environ.get("SECRET_KEY", "defaultsecretkey")


def _get_expiry_hours():
    try:
        return int(os.environ.get("SESSION_TOKEN_EXPIRY_HOURS", DEFAULT_EXPIRY_HOURS))
    except ValueError:
        return DEFAULT_EXPIRY_HOURS


def encode_session_token(user_id: int, username: str, role: str, expiry_hours: int = None) -> str:
    """
    Encode a short-lived JWT for API use. Payload: sub=user_id, username, role, exp, iat.
    Returns the token string, or empty string if JWT not available.
    """
    if not pyjwt:
        return ""
    expiry = expiry_hours if expiry_hours is not None else _get_expiry_hours()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=expiry),
    }
    out = pyjwt.encode(
        payload,
        _get_secret(),
        algorithm="HS256",
    )
    return out if isinstance(out, str) else out.decode("utf-8")


def decode_session_token(token: str):
    """
    Decode and validate a session JWT. Returns payload dict (with sub, username, role)
    or None if invalid/expired/missing JWT lib.
    """
    if not pyjwt or not token or not token.strip():
        return None
    try:
        payload = pyjwt.decode(
            token.strip(),
            _get_secret(),
            algorithms=["HS256"],
        )
        if isinstance(payload.get("sub"), int) and payload.get("username") and payload.get("role"):
            return payload
    except Exception:
        pass
    return None
