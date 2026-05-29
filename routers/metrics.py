"""
routers/metrics.py — GET /api/metrics

Admin-only application metrics endpoint.
Returns: signals_generated_today, claude_calls_today, errors_today,
         cache_size, memory_mb, uptime, rate_limit_stats.

Design doc §10.2.
"""

import os
import time
import logging
from datetime import datetime, timezone

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
from fastapi import APIRouter, Depends, HTTPException

from middleware.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# ── In-memory counters (reset on restart) ────────────────────────────────────
# These are incremented by signal_service, ondemand_service, etc.
# via metrics.increment("signals_generated") calls.

_counters: dict = {
    "signals_generated_today": 0,
    "claude_calls_today":      0,
    "errors_today":            0,
    "cache_hits_today":        0,
    "trades_today":            0,
    "stop_losses_today":       0,
}
_start_time = time.time()
_today_date = datetime.now(timezone.utc).date()

# References to services — injected by main.py
signal_svc      = None
portfolio_svc   = None
auto_trader_svc = None
limiter         = None   # RateLimiter instance


def increment(key: str, amount: int = 1):
    """Call from any service to increment a metric counter."""
    global _today_date, _counters

    # Reset counters at midnight UTC
    today = datetime.now(timezone.utc).date()
    if today != _today_date:
        _today_date = today
        for k in _counters:
            _counters[k] = 0
        logger.info("Metrics: counters reset for new day")

    if key in _counters:
        _counters[key] += amount


def get_memory_mb() -> float:
    if not _HAS_PSUTIL:
        return 0.0
    try:
        proc = psutil.Process(os.getpid())
        return round(proc.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        return 0.0


def get_uptime_seconds() -> int:
    return int(time.time() - _start_time)


def format_uptime(seconds: int) -> str:
    days    = seconds // 86400
    hours   = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@router.get("/")
async def get_metrics(admin=Depends(require_admin)):
    """
    Application metrics — admin only.
    Returns counters, cache size, memory usage, uptime.
    """
    try:
        uptime_s = get_uptime_seconds()

        # Signal cache size
        cache_size = 0
        if signal_svc:
            try:
                cache_size = len(signal_svc.get_all_cached())
            except Exception:
                pass

        # Auto-trader status
        trader_status = {}
        if auto_trader_svc:
            try:
                trader_status = await auto_trader_svc.get_status()
            except Exception:
                pass

        # Maintenance mode
        maintenance = os.getenv("MAINTENANCE_MODE", "false").lower() == "true"

        # Rate limiter stats (top buckets)
        rate_stats = {}
        if limiter:
            try:
                limiter.clear_expired()
                rate_stats = {
                    "active_windows": len(limiter._windows),
                }
            except Exception:
                pass

        return {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "uptime":     format_uptime(uptime_s),
            "uptime_s":   uptime_s,

            # Daily counters (reset midnight UTC)
            "today": {
                "date":                 _today_date.isoformat(),
                "signals_generated":    _counters["signals_generated_today"],
                "claude_calls":         _counters["claude_calls_today"],
                "cache_hits":           _counters["cache_hits_today"],
                "errors":               _counters["errors_today"],
                "trades":               _counters["trades_today"],
                "stop_losses":          _counters["stop_losses_today"],
            },

            # System
            "system": {
                "memory_mb":        get_memory_mb(),
                "cache_size":       cache_size,
                "maintenance_mode": maintenance,
                "environment":      os.getenv("ENVIRONMENT", "development"),
            },

            # Auto-trader
            "auto_trader": trader_status,

            # Rate limiting
            "rate_limiter": rate_stats,
        }

    except Exception as e:
        logger.error(f"Metrics endpoint error: {e}")
        raise HTTPException(500, f"Metrics unavailable: {e}")