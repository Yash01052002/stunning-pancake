"""Phase 7: lightweight abuse protection on public-facing endpoints.

A fixed-window in-memory counter, keyed by client IP + a bucket name. Applied
to the unauthenticated / cheap-to-spam endpoints (ticket creation, register,
login) so a single source can't flood them.

IMPORTANT — this state lives in the worker process, so with multiple API
workers each enforces the limit independently (effective limit = N_workers x
configured limit), and it resets on restart. That's fine as a first line of
defense but is NOT a substitute for a shared limiter (Redis / an API gateway /
a WAF) in production — see docs/runbook.md. It's deliberately dependency-free
so the app runs with zero extra infra out of the box.

Disabled entirely when settings.rate_limit_enabled is False (the default in
tests, so the suite isn't coupled to these counters).
"""

import threading
import time
from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from app.config import settings

_lock = threading.Lock()
# key -> (window_start_epoch, count)
_windows: dict[str, tuple[float, int]] = {}


def reset() -> None:
    """Clear all counters — used by tests between cases."""
    with _lock:
        _windows.clear()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check(key: str, limit: int, window_seconds: int) -> None:
    now = time.time()
    with _lock:
        window_start, count = _windows.get(key, (now, 0))
        if now - window_start >= window_seconds:
            # window elapsed — start a fresh one
            window_start, count = now, 0
        count += 1
        _windows[key] = (window_start, count)
        if count > limit:
            retry_after = int(window_seconds - (now - window_start)) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please slow down.",
                headers={"Retry-After": str(retry_after)},
            )


def rate_limit(bucket: str, limit_fn: Callable[[], int], window_seconds: int = 60):
    """Build a FastAPI dependency enforcing per-IP requests per window for the
    named bucket. `limit_fn` is read at request time (not captured) so the
    limit tracks live config. No-op when rate limiting is disabled in config."""

    def dependency(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        _check(f"{bucket}:{_client_ip(request)}", limit_fn(), window_seconds)

    return Depends(dependency)
