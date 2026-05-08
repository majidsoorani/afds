"""
AFDS Middleware — Rate limiting, request audit logging, request ID injection.
"""

import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a unique request ID into every request/response."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding window rate limiter.

    Production: use Redis-backed rate limiting.
    """

    def __init__(self, app, requests_per_minute: int = 600):
        super().__init__(app)
        self.rpm = requests_per_minute
        self._windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        # Skip health checks and docs
        if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window = self._windows[client_ip]

        # Clean old entries (> 60s)
        window[:] = [t for t in window if now - t < 60]

        if len(window) >= self.rpm:
            return Response(
                content='{"detail":"Rate limit exceeded. Max {} requests/minute."}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        window.append(now)
        return await call_next(request)


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Log all API calls for compliance audit trail."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        elapsed = round((time.time() - start) * 1000, 2)

        # Log all mutating requests and auth attempts
        if request.method in ("POST", "PUT", "PATCH", "DELETE") or "/auth/" in request.url.path:
            logger.info(
                "AUDIT | %s %s | status=%d | ip=%s | user_agent=%s | duration=%sms | request_id=%s",
                request.method,
                request.url.path,
                response.status_code,
                request.client.host if request.client else "unknown",
                request.headers.get("user-agent", "unknown")[:100],
                elapsed,
                getattr(request.state, "request_id", "n/a"),
            )

        return response
