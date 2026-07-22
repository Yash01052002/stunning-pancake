import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import bcrypt

from app.config import settings

_HEADER = {"alg": "HS256", "typ": "JWT"}

# bcrypt only examines the first 72 bytes of the input; truncate explicitly
# so behavior is well-defined instead of relying on the library to error/silently drop.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    truncated = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(truncated, hashed_password.encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(message: bytes) -> str:
    signature = hmac.new(
        settings.jwt_secret_key.encode("utf-8"), message, hashlib.sha256
    ).digest()
    return _b64url_encode(signature)


def create_access_token(subject: str) -> str:
    """Minimal, dependency-free HS256 JWT encoder (header.payload.signature)."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": subject, "exp": int(expire.timestamp())}

    header_segment = _b64url_encode(json.dumps(_HEADER, separators=(",", ":")).encode("utf-8"))
    payload_segment = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _sign(signing_input)
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def decode_access_token(token: str) -> str | None:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        expected_signature = _sign(signing_input)
        if not hmac.compare_digest(expected_signature, signature_segment):
            return None

        payload = json.loads(_b64url_decode(payload_segment))
        exp = payload.get("exp")
        if exp is None or datetime.now(timezone.utc).timestamp() > exp:
            return None
        return payload.get("sub")
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
