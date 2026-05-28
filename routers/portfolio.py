"""
routers/portfolio.py  — v3 (multi-strategy)

Endpoints:
  GET  /api/portfolio/overview              — full dashboard (summary + all strategies)
  GET  /api/portfolio/summary               — available cash + total value only
  GET  /api/portfolio/pnl                   — P&L breakdown across strategies
  GET  /api/portfolio/strategies            — catalogue (no auth)
  GET  /api/portfolio/positions/{sk}        — positions for one strategy
  GET  /api/portfolio/trades                — all trades (optional ?strategy=sk)
  GET  /api/portfolio/transactions          — wallet/allocation history

  POST /api/portfolio/agreement             — accept disclaimer
  POST /api/portfolio/allocate              — { strategy_key, amount, stop_loss_pct? }
  POST /api/portfolio/reduce                — { strategy_key, amount }
  POST /api/portfolio/pause                 — { strategy_key, paused: bool }
  POST /api/portfolio/stop                  — { strategy_key }
  POST /api/portfolio/trade                 — { symbol, action, strategy_key, amount_usd?, shares? }

  GET  /api/portfolio/admin/status          — kill switch + active users (admin)
  POST /api/portfolio/admin/kill-switch     — { enabled: bool } (admin)
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py
portfolio_svc    = None
auto_trader_svc  = None
price_svc        = None


# ── Request models ────────────────────────────────────────────────────────────

class AgreementRequest(BaseModel):
    pass

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

class TradeRequest(BaseModel):
    symbol:       str
    action:       str           # "BUY" | "SELL"
    strategy_key: str
    amount_usd:   Optional[float] = None
    shares:       Optional[float] = None

class KillSwitchRequest(BaseModel):
    enabled: bool


# ── Guards ────────────────────────────────────────────────────────────────────

def _need_portfolio():
    if not portfolio_svc:
        raise HTTPException(503, "Portfolio service not initialised")

def _need_auto_trader():
    if not auto_trader_svc:
        raise HTTPException(503, "Auto-trader service not initialised")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_prices() -> dict:
    if not price_svc:
        return {}
    try:
        raw = await price_svc.get_all()
        return {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                for s, d in raw.items()}
    except Exception:
        return {}


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get("/overview")
async def get_overview(user=Depends(get_current_user)):
    """Full dashboard — summary + all 5 strategy cards with allocations and positions."""
    _need_portfolio()
    uid    = user["uid"]
    prices = await _get_prices()
    try:
        return await portfolio_svc.get_portfolio_overview(uid, prices)
    except Exception as e:
        logger.error(f"get_overview failed for {uid}: {e}")
        raise HTTPException(500, "Failed to load portfolio")


@router.get("/summary")
async def get_summary(user=Depends(get_current_user)):
    """Available cash + total value — used by header portfolio bar."""
    _need_portfolio()
    uid = user["uid"]
    try:
        return await portfolio_svc.get_or_create_summary(uid)
    except Exception as e:
        logger.error(f"get_summary failed for {uid}: {e}")
        raise HTTPException(500, "Failed to load summary")


@router.get("/pnl")
async def get_pnl(user=Depends(get_current_user)):
    """P&L breakdown across all strategies."""
    _need_portfolio()
    uid    = user["uid"]
    prices = await _get_prices()
    try:
        return await portfolio_svc.get_pnl(uid, prices)
    except Exception as e:
        logger.error(f"get_pnl failed for {uid}: {e}")
        raise HTTPException(500, "Failed to calculate P&L")


@router.get("/strategies")
async def get_strategy_catalogue():
    """All 5 strategy definitions — public, no auth needed."""
    from services.portfolio_service import PortfolioService
    return {"strategies": PortfolioService.get_strategy_catalogue()}


@router.get("/positions/{strategy_key}")
async def get_positions(strategy_key: str, user=Depends(get_current_user)):
    """Open positions for one strategy, enriched with live prices."""
    _need_portfolio()
    uid    = user["uid"]
    prices = await _get_prices()
    try:
        positions = await portfolio_svc.get_positions(uid, strategy_key, prices)
        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        logger.error(f"get_positions failed for {uid}/{strategy_key}: {e}")
        raise HTTPException(500, "Failed to load positions")


@router.get("/trades")
async def get_trades(
    user=Depends(get_current_user),
    strategy: Optional[str] = Query(None),
    limit:    int           = Query(50, le=200),
):
    """Trade history. Optional ?strategy=sk filter."""
    _need_portfolio()
    uid = user["uid"]
    try:
        trades = await portfolio_svc.get_trades(uid, sk=strategy, limit=limit)
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        logger.error(f"get_trades failed for {uid}: {e}")
        raise HTTPException(500, "Failed to load trades")


@router.get("/transactions")
async def get_transactions(
    user=Depends(get_current_user),
    limit: int = Query(50, le=200),
):
    """Allocation/deallocation history."""
    _need_portfolio()
    uid = user["uid"]
    try:
        txns = await portfolio_svc.get_transactions(uid, limit=limit)
        return {"transactions": txns, "count": len(txns)}
    except Exception as e:
        logger.error(f"get_transactions failed for {uid}: {e}")
        raise HTTPException(500, "Failed to load transactions")


# ── Write endpoints ───────────────────────────────────────────────────────────

@router.post("/agreement")
async def accept_agreement(user=Depends(get_current_user)):
    """Accept paper trading disclaimer — required before any action."""
    _need_portfolio()
    uid = user["uid"]
    try:
        await portfolio_svc.get_or_create_summary(uid)
        return await portfolio_svc.accept_agreement(uid)
    except Exception as e:
        logger.error(f"accept_agreement failed for {uid}: {e}")
        raise HTTPException(500, "Failed to record agreement")


@router.post("/allocate")
async def allocate(req: AllocateRequest, user=Depends(get_current_user)):
    """
    Move cash from available_cash → strategy sub-account.
    Creates strategy if first allocation, adds more if already exists.
    """
    _need_portfolio()
    uid = user["uid"]
    try:
        return await portfolio_svc.allocate(uid, req.strategy_key, req.amount, req.stop_loss_pct)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"allocate failed for {uid}: {e}")
        raise HTTPException(500, "Allocation failed")


@router.post("/reduce")
async def reduce(req: ReduceRequest, user=Depends(get_current_user)):
    """
    Return idle (uninvested) cash from a strategy back to available_cash.
    Cannot withdraw invested funds — only idle cash in strategy.
    """
    _need_portfolio()
    uid = user["uid"]
    try:
        return await portfolio_svc.reduce(uid, req.strategy_key, req.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"reduce failed for {uid}: {e}")
        raise HTTPException(500, "Reduce failed")


@router.post("/pause")
async def pause_strategy(req: PauseRequest, user=Depends(get_current_user)):
    """
    Pause: freeze new auto-trades, keep positions open, cash stays in strategy.
    Resume: auto-trading picks back up on next signal.
    """
    _need_portfolio()
    uid = user["uid"]
    try:
        return await portfolio_svc.pause_strategy(uid, req.strategy_key, req.paused)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"pause failed for {uid}: {e}")
        raise HTTPException(500, "Pause failed")


@router.post("/stop")
async def stop_strategy(req: StopRequest, user=Depends(get_current_user)):
    """
    Stop a strategy:
    - Closes ALL open positions at current market price
    - Returns all cash + proceeds to available_cash
    - Removes strategy sub-account
    This action cannot be undone.
    """
    _need_portfolio()
    uid    = user["uid"]
    prices = await _get_prices()
    try:
        return await portfolio_svc.stop_strategy(uid, req.strategy_key, prices)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"stop failed for {uid}/{req.strategy_key}: {e}")
        raise HTTPException(500, "Stop strategy failed")


@router.post("/trade")
async def manual_trade(req: TradeRequest, user=Depends(get_current_user)):
    """
    Manual BUY or SELL from Live Prices / Portfolio tab.
    Bypasses auto-trader is_active and universe checks.
    Strategy must be allocated. Agreement must be accepted.
    """
    _need_auto_trader()
    uid    = user["uid"]
    action = req.action.upper()

    if action not in ("BUY", "SELL"):
        raise HTTPException(400, "action must be BUY or SELL")
    if not req.symbol.strip():
        raise HTTPException(400, "symbol is required")
    if not req.strategy_key.strip():
        raise HTTPException(400, "strategy_key is required")
    if action == "BUY" and req.amount_usd is not None and req.amount_usd <= 0:
        raise HTTPException(400, "amount_usd must be positive")
    if action == "BUY" and req.shares is not None and req.shares <= 0:
        raise HTTPException(400, "shares must be positive")

    try:
        result = await auto_trader_svc.execute_manual_trade(
            uid,
            req.symbol.strip().upper(),
            action,
            req.strategy_key,
            amount_usd=req.amount_usd,
            shares=req.shares,
        )
        if result.get("status") == "error":
            raise HTTPException(400, result.get("reason", "Trade failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"manual_trade failed for {uid}: {e}")
        raise HTTPException(500, "Trade execution failed")


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.get("/admin/status")
async def get_status(admin=Depends(require_admin)):
    """Kill switch state + active users/strategies count."""
    _need_auto_trader()
    try:
        return await auto_trader_svc.get_status()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/admin/kill-switch")
async def set_kill_switch(req: KillSwitchRequest, admin=Depends(require_admin)):
    """Enable or disable all autonomous trading globally. Takes effect within 60s."""
    _need_auto_trader()
    try:
        result = await auto_trader_svc.set_kill_switch(req.enabled)
        logger.info(f"Admin {admin['uid'][:8]}… set kill switch → {req.enabled}")
        return result
    except Exception as e:
        raise HTTPException(500, str(e))