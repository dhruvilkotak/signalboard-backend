"""
routers/ondemand.py

On-demand signal endpoint for the Live Prices → AI Signal tab.
Separate from scheduler signals (signals/{symbol}).

Auth:
  POST /api/ondemand/signal  — any authenticated user (get_current_user)
  GET  /api/ondemand/signal/{symbol} — any authenticated user

Cache: 24h shared in Firestore signals_ondemand/{symbol}
Service injected by main.py: ondemand.ondemand_svc = ondemand_svc
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin
from middleware.rate_limit import rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py
ondemand_svc = None


class SignalRequest(BaseModel):
    symbol: str


@router.post("/signal")
async def get_ondemand_signal(
    req: SignalRequest,
    user=Depends(get_current_user),   # any logged-in user
):
    """
    Generate or return cached on-demand signal for a symbol.
    24h shared cache — only calls Claude if no fresh signal exists for any user.
    Includes: SEC Form 4 insider trades, StockTwits sentiment, price targets,
    bull/bear case, stop loss.
    """
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")

    result = await ondemand_svc.get_signal(symbol)
    return result


@router.get("/signal/{symbol}")
async def get_ondemand_signal_get(
    symbol: str,
    user=Depends(get_current_user),
):
    """GET version — same behaviour, for easy testing."""
    symbol = symbol.strip().upper()
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")
    return await ondemand_svc.get_signal(symbol)


@router.post("/signal/force")
async def force_regenerate_signal(
    req: SignalRequest,
    admin=Depends(require_admin),   # admin only
):
    """
    Admin-only: force regenerate on-demand signal bypassing 24h cache.
    Invalidates existing cache then generates fresh signal.
    Use when signal shows 'data unavailable' or is stale.
    """
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")

    logger.info(f"[ondemand] Admin force-regenerate: {symbol} by uid={admin['uid'][:8]}…")

    # Invalidate cache so get_signal generates fresh
    ondemand_svc.invalidate(symbol)
    result = await ondemand_svc.get_signal(symbol)
    return {**result, "force_regenerated": True}

@router.post("/cache/reset")
async def reset_cache(admin=Depends(require_admin)):
    """
    Admin-only: clears ALL in-memory signal caches.
    - ondemand cache (signals_ondemand — 24h shared)
    - scheduled signal cache (signals — 30min TTL)
    Does NOT delete Firestore documents — only clears server RAM.
    Next request per symbol will re-fetch from Firestore or regenerate.
    """
    from main import signal_svc as _signal_svc
    ondemand_cleared = ondemand_svc.invalidate_all() if ondemand_svc else 0
    scheduled_cleared = _signal_svc.invalidate_all() if _signal_svc else 0
    logger.info(
        f"[ondemand] Admin cache reset by uid={admin['uid'][:8]}… "
        f"ondemand={ondemand_cleared} scheduled={scheduled_cleared}"
    )
    return {
        "status":            "cleared",
        "ondemand_cleared":  ondemand_cleared,
        "scheduled_cleared": scheduled_cleared,
        "message":           "All in-memory caches cleared. Next signal request will regenerate from scratch.",
    }