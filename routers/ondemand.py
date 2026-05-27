"""
routers/ondemand.py

On-demand signal endpoint for the Live Prices → Signal tab.
Completely separate from scheduler signals (signals/{symbol}).

Endpoint:
    POST /api/ondemand/signal   { "symbol": "TSLA" }
    GET  /api/ondemand/signal/{symbol}

Cache: 24h in Firestore signals_ondemand/{symbol}
Service injected by main.py: ondemand.ondemand_svc = ondemand_svc
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py
ondemand_svc = None


class SignalRequest(BaseModel):
    symbol: str


@router.post("/signal")
async def get_ondemand_signal(req: SignalRequest):
    """
    Generate or return cached on-demand signal for a symbol.
    24h cache — only calls Claude if no fresh signal exists.
    Includes: insider trades (SEC Form 4), retail sentiment (StockTwits),
    price targets, bull/bear case, stop loss.
    """
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")

    result = await ondemand_svc.get_signal(symbol)
    return result


@router.get("/signal/{symbol}")
async def get_ondemand_signal_get(symbol: str):
    """GET version — same behaviour, for easy browser testing."""
    symbol = symbol.strip().upper()
    if not ondemand_svc:
        raise HTTPException(status_code=503, detail="OnDemand signal service not initialised")
    return await ondemand_svc.get_signal(symbol)