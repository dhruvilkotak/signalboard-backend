"""
middleware/error_logging.py

FastAPI middleware that emits a structured log_event for every 4xx and 5xx
response so GCP log-based metrics can count API errors without any app-level
boilerplate in individual routers.

GCP metric filters:
  api_error_5xx : jsonPayload.event = "api_error" AND jsonPayload.status >= 500
  api_error_4xx : jsonPayload.event = "api_error" AND jsonPayload.status >= 400
                  AND jsonPayload.status < 500

Registration in main.py (after CORSMiddleware):
  from middleware.error_logging import ErrorLoggingMiddleware
  app.add_middleware(ErrorLoggingMiddleware)
"""

import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from utils.logging_setup import log_event

logger = logging.getLogger(__name__)

# Don't log errors for these paths (noisy / expected)
_SKIP_PATHS = {
    "/health",
    "/api/market/status",
    "/.env",
    "/.env.orig",
    "/.env.save",
    "/.gitconfig",
    "/www/.env",
    "/app/.env",
    "/core/.env",
    "/",
}


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start    = time.monotonic()
        response = await call_next(request)
        duration = int((time.monotonic() - start) * 1000)

        status = response.status_code
        path   = request.url.path

        if status >= 400 and path not in _SKIP_PATHS:
            # Extract uid from request state if auth middleware set it
            uid = getattr(request.state, "uid", None)
            log_event(
                logger,
                "error" if status >= 500 else "warning",
                f"API error {status} {request.method} {path}",
                event      = "api_error",
                status     = status,
                method     = request.method,
                endpoint   = path,
                duration_ms= duration,
                uid        = uid,
            )

        return response