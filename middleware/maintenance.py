"""
middleware/maintenance.py

Maintenance mode middleware — Level 1 kill switch.

When MAINTENANCE_MODE=true in .env (+ restart), all API endpoints
return HTTP 503 "Under maintenance" except /health and /api/metrics.

To activate:  echo "MAINTENANCE_MODE=true" >> .env && sudo systemctl restart signalboard
To deactivate: remove line from .env + sudo systemctl restart signalboard

Design doc §10.3 Level 1.
"""

import os
import logging
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths that bypass maintenance mode
ALWAYS_ALLOWED = {"/health", "/api/metrics", "/docs", "/openapi.json"}


class MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """
    Checks MAINTENANCE_MODE env var on every request.
    Reads from env at request time — no restart needed if using
    a dynamic config, but for simplicity we use env + restart.
    """

    async def dispatch(self, request: Request, call_next):
        is_maintenance = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

        if is_maintenance and request.url.path not in ALWAYS_ALLOWED:
            logger.info(f"Maintenance mode: blocked {request.method} {request.url.path}")
            return JSONResponse(
                status_code=503,
                content={
                    "error":   "maintenance",
                    "message": "SignalBoard is under maintenance. Please try again shortly.",
                    "status":  503,
                },
                headers={"Retry-After": "300"},
            )

        return await call_next(request)