"""
routers/portfolio.py — v4

Manual trade endpoints (no strategy needed):
  GET  /api/portfolio/overview              — full dashboard
  GET  /api/portfolio/summary               — available cash + total value
  GET  /api/portfolio/manual/positions      — user's personal positions
  GET  /api/portfolio/manual/trades         — user's personal trade history
  POST /api/portfolio/manual/buy            — buy from available cash
  POST /api/portfolio/manual/sell           — sell manual position

Strategy endpoints (auto-trader funds):
  GET  /api/portfolio/strategies            — catalogue (no auth)
  GET  /api/portfolio/strategy/{sk}/positions  — positions inside strategy
  GET  /api/portfolio/strategy/{sk}/trades     — trades inside strategy
  POST /api/portfolio/allocate              — move cash → strategy fund
  POST /api/portfolio/reduce                — return idle cash ← strategy
  POST /api/portfolio/pause                 — pause/resume strategy
  POST /api/portfolio/stop                  — close all + return cash

Shared:
  POST /api/portfolio/agreement             — accept disclaimer
  GET  /api/portfolio/transactions          — wallet event log
  GET  /api/portfolio/admin/status          — kill switch + active users
  POST /api/portfolio/admin/kill-switch     — enable/disable all trading
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from middleware.auth import get_current_user
from middleware.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

portfolio_svc   = None
auto_trader_svc = None
price_svc       = None


# ── Request models ────────────────────────────────────────────────────────────

class ManualBuyRequest(BaseModel):
    symbol:     str
    amount_usd: Optional[float] = None
    shares:     Optional[float] = None

class ManualSellRequest(BaseModel):
    symbol: str
    shares: Optional[float] = None   # None = sell all

class AllocateRequest(BaseModel):
    strategy_key:  str
    amount:        float = Field(..., gt=0)
    stop_loss_pct: Optional[float] = None

class ReduceRequest(BaseModel):
    strategy_key: str
    amount:       float = Field(..., gt=0)

class PauseRequest(BaseModel):
    strategy_key: str
    paused:       bool

class StopRequest(BaseModel):
    strategy_key: str

class KillSwitchRequest(BaseModel):
    enabled: bool


# ── Guards + helpers ──────────────────────────────────────────────────────────

def _need_portfolio():
    if not portfolio_svc: raise HTTPException(503, "Portfolio service not initialised")

def _need_auto_trader():
    if not auto_trader_svc: raise HTTPException(503, "Auto-trader service not initialised")

async def _prices() -> dict:
    if not price_svc: return {}
    try:
        raw = await price_svc.get_all()
        return {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0)) for s, d in raw.items()}
    except Exception:
        return {}


# ── Shared ────────────────────────────────────────────────────────────────────

@router.get("/overview")
async def get_overview(user=Depends(get_current_user)):
    """Full dashboard — summary + manual portfolio + all strategy cards."""
    _need_portfolio()
    try:
        return await portfolio_svc.get_overview(user["uid"], await _prices())
    except Exception as e:
        logger.error(f"get_overview {user['uid']}: {e}")
        raise HTTPException(500, "Failed to load portfolio")

@router.get("/summary")
async def get_summary(user=Depends(get_current_user)):
    """Available cash + total value — used by header bar."""
    _need_portfolio()
    try:
        return await portfolio_svc.get_or_create_summary(user["uid"])
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/agreement")
async def accept_agreement(user=Depends(get_current_user)):
    _need_portfolio()
    try:
        await portfolio_svc.get_or_create_summary(user["uid"])
        return await portfolio_svc.accept_agreement(user["uid"])
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/transactions")
async def get_transactions(user=Depends(get_current_user), limit: int = Query(50, le=200)):
    _need_portfolio()
    try:
        txns = await portfolio_svc.get_transactions(user["uid"], limit=limit)
        return {"transactions": txns, "count": len(txns)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/strategies")
async def get_strategy_catalogue():
    """Public — no auth needed."""
    from services.portfolio_service import PortfolioService
    return {"strategies": PortfolioService.get_strategy_catalogue()}


# ── Manual trade endpoints ────────────────────────────────────────────────────

@router.get("/manual/positions")
async def get_manual_positions(user=Depends(get_current_user)):
    _need_portfolio()
    try:
        positions = await portfolio_svc.get_manual_positions(user["uid"], await _prices())
        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/manual/trades")
async def get_manual_trades(user=Depends(get_current_user), limit: int = Query(50, le=200)):
    _need_portfolio()
    try:
        trades = await portfolio_svc.get_manual_trades(user["uid"], limit=limit)
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/manual/buy")
async def manual_buy(req: ManualBuyRequest, user=Depends(get_current_user)):
    """Buy stock from available cash. No strategy needed."""
    _need_auto_trader()
    uid = user["uid"]
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if req.amount_usd is None and req.shares is None:
        raise HTTPException(400, "Provide amount_usd or shares")
    if req.amount_usd is not None and req.amount_usd <= 0:
        raise HTTPException(400, "amount_usd must be positive")
    if req.shares is not None and req.shares <= 0:
        raise HTTPException(400, "shares must be positive")
    try:
        result = await auto_trader_svc.execute_manual_trade(
            uid, req.symbol.strip().upper(), "BUY",
            amount_usd=req.amount_usd, shares=req.shares,
        )
        if result.get("status") == "error":
            raise HTTPException(400, result.get("reason", "Buy failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"manual_buy {uid}: {e}")
        raise HTTPException(500, "Buy failed")

@router.post("/manual/sell")
async def manual_sell(req: ManualSellRequest, user=Depends(get_current_user)):
    """Sell manual position. shares=None sells entire position."""
    _need_auto_trader()
    uid = user["uid"]
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    try:
        result = await auto_trader_svc.execute_manual_trade(
            uid, req.symbol.strip().upper(), "SELL", shares=req.shares,
        )
        if result.get("status") == "error":
            raise HTTPException(400, result.get("reason", "Sell failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"manual_sell {uid}: {e}")
        raise HTTPException(500, "Sell failed")


# ── Strategy endpoints ────────────────────────────────────────────────────────

@router.get("/strategy/{strategy_key}/positions")
async def get_strategy_positions(strategy_key: str, user=Depends(get_current_user)):
    _need_portfolio()
    try:
        positions = await portfolio_svc.get_strategy_positions(
            user["uid"], strategy_key, await _prices())
        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.get("/strategy/{strategy_key}/trades")
async def get_strategy_trades(strategy_key: str, user=Depends(get_current_user),
                               limit: int = Query(50, le=200)):
    _need_portfolio()
    try:
        trades = await portfolio_svc.get_strategy_trades(user["uid"], strategy_key, limit)
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/allocate")
async def allocate(req: AllocateRequest, user=Depends(get_current_user)):
    _need_portfolio()
    try:
        return await portfolio_svc.allocate(user["uid"], req.strategy_key,
                                            req.amount, req.stop_loss_pct)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/reduce")
async def reduce(req: ReduceRequest, user=Depends(get_current_user)):
    _need_portfolio()
    try:
        return await portfolio_svc.reduce(user["uid"], req.strategy_key, req.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/pause")
async def pause_strategy(req: PauseRequest, user=Depends(get_current_user)):
    _need_portfolio()
    try:
        return await portfolio_svc.pause_strategy(user["uid"], req.strategy_key, req.paused)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/stop")
async def stop_strategy(req: StopRequest, user=Depends(get_current_user)):
    _need_portfolio()
    try:
        return await portfolio_svc.stop_strategy(user["uid"], req.strategy_key, await _prices())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"stop_strategy {user['uid']}/{req.strategy_key}: {e}")
        raise HTTPException(500, "Stop strategy failed")


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.get("/admin/status")
async def get_status(admin=Depends(require_admin)):
    _need_auto_trader()
    try:
        return await auto_trader_svc.get_status()
    except Exception as e:
        raise HTTPException(500, str(e))

@router.post("/admin/kill-switch")
async def set_kill_switch(req: KillSwitchRequest, admin=Depends(require_admin)):
    _need_auto_trader()
    try:
        result = await auto_trader_svc.set_kill_switch(req.enabled)
        logger.info(f"Admin {admin['uid'][:8]}… kill switch → {req.enabled}")
        return result
    except Exception as e:
        raise HTTPException(500, str(e))