"""Phase 7: centralized security audit logging.

An HTTP middleware records every state-changing request (POST/PATCH/PUT/DELETE)
to the audit_logs table — actor, method, path, status, client IP. Centralizing
it in middleware means new mutating endpoints are covered automatically without
per-route wiring, and login attempts (POST /auth/login) are captured including
failures (non-2xx status).

Writing the audit row must never break the request it's auditing, so all of it
is wrapped in a best-effort try/except.
"""

import logging

from starlette.requests import Request

from app import database
from app.models import AuditLog
from app.security import decode_access_token

logger = logging.getLogger(__name__)

_AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def _actor_id_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return decode_access_token(auth[7:])


async def audit_middleware(request: Request, call_next):
    response = await call_next(request)

    if request.method in _AUDITED_METHODS:
        try:
            db = database.SessionLocal()
            try:
                db.add(
                    AuditLog(
                        actor_id=_actor_id_from_request(request),
                        method=request.method,
                        path=request.url.path,
                        status_code=response.status_code,
                        client_ip=request.client.host if request.client else None,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception:  # audit must never break the actual request
            logger.warning("Failed to write audit log", exc_info=True)

    return response
