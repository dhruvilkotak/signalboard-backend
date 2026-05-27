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
    user=Depends(get_current_user),   # any logged-in user
):
    """GET version — same behaviour, for easy testing."""
    symbol = symbol.strip().upper()
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")
    return await ondemand_svc.get_signal(symbol)