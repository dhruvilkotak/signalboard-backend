"""
routers/ondemand.py

On-demand signal endpoint for the Live Prices → AI Signal tab.
Separate from scheduler signals (signals/{symbol}).

Auth:
  POST /api/ondemand/signal  — any authenticated user (get_current_user)
  GET  /api/ondemand/signal/{symbol} — any authenticated user
  GET  /api/ondemand/peek/{symbol}   — any authenticated user; read-only, says whether
                                       opening this symbol is free / uses 1 / is blocked

Quota (flag `paywall`, see services/usage_service.py):
  admin: unlimited
  free/trial: 5 new signals in total, never reset   → 402 PAYWALL when used up
  pro/paid/coupon: 5 new signals per week (Mon ET)  → 429 WEEKLY_LIMIT when used up
  Re-viewing a still-current signal is free. Failed generations are not counted.
  Successful responses include "quota" so the UI can show signals left.

Cache: shared in Firestore signals_ondemand/{symbol}; every signal expires 11:59 PM ET.
A user's first view of a symbol each day counts even if someone else generated it.
Services injected by main.py:
  ondemand.ondemand_svc = ondemand_svc
  ondemand.usage_svc    = usage_svc
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin
from middleware.rate_limit import rate_limiter
from services.usage_service import QuotaExceeded

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py
ondemand_svc = None
signal_svc   = None   # injected for cache reset
usage_svc    = None   # injected for quota; None = no quota


class SignalRequest(BaseModel):
    symbol: str


async def _serve(uid: str, symbol: str) -> dict:
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")
    if usage_svc is None:
        return await ondemand_svc.get_signal(symbol)

    try:
        res = await run_in_threadpool(usage_svc.reserve, uid, symbol)
    except QuotaExceeded as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    try:
        signal = await ondemand_svc.get_signal(symbol)
    except Exception:
        await run_in_threadpool(usage_svc.release, uid, res)
        raise

    if signal.get("trigger") == "fallback":
        await run_in_threadpool(usage_svc.release, uid, res)
        return signal

    await run_in_threadpool(usage_svc.confirm, uid, res, signal.get("expires_at"))
    return {**signal, "quota": res.quota or None}


@router.post("/signal")
async def get_ondemand_signal(
    req: SignalRequest,
    user=Depends(get_current_user),
):
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    return await _serve(user["uid"], symbol)


@router.get("/signal/{symbol}")
async def get_ondemand_signal_get(
    symbol: str,
    user=Depends(get_current_user),
):
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    return await _serve(user["uid"], symbol)


@router.get("/peek/{symbol}")
async def peek_ondemand_signal(
    symbol: str,
    user=Depends(get_current_user),
):
    """Read-only: lets the UI ask before spending a signal. Never counts anything."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if usage_svc is None:
        return {"symbol": symbol, "status": "unlimited", "quota": None, "code": None}
    return await run_in_threadpool(usage_svc.peek, user["uid"], symbol)


@router.post("/signal/force")
async def force_regenerate_signal(
    req: SignalRequest,
    admin=Depends(require_admin),
):
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")
    logger.info(f"[ondemand] Admin force-regenerate: {symbol} by uid={admin['uid'][:8]}…")

    ondemand_svc.invalidate(symbol)                        # clear memory cache
    await ondemand_svc.invalidate_firestore(symbol)         # clear Firestore doc

    result = await ondemand_svc.get_signal(symbol)          # now guaranteed cache+Firestore miss → _generate()
    return {**result, "force_regenerated": True}


@router.post("/cache/reset")
async def reset_cache(admin=Depends(require_admin)):
    ondemand_cleared  = ondemand_svc.invalidate_all() if ondemand_svc else 0
    scheduled_cleared = signal_svc.invalidate_all() if signal_svc else 0
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
