"""
routers/portfolio.py

Auto-Trader portfolio endpoints — protected by Firebase Auth.
All state is stored in Firestore via PortfolioService.
AutoTraderService handles autonomous execution and the kill switch.

Endpoints:
  GET  /api/portfolio/                     — wallet + positions summary
  GET  /api/portfolio/wallet               — wallet only
  GET  /api/portfolio/positions            — open positions
  GET  /api/portfolio/pnl                  — P&L + daily breakdown
  GET  /api/portfolio/trades               — trade history (newest first)
  GET  /api/portfolio/transactions         — deposit/withdraw/reset log
  GET  /api/portfolio/strategies           — strategy catalogue (no auth)

  POST /api/portfolio/deposit              — add virtual funds
  POST /api/portfolio/withdraw             — withdraw virtual cash
  POST /api/portfolio/reset                — reset to starting balance
  POST /api/portfolio/strategy             — change strategy
  POST /api/portfolio/toggle               — start / pause auto-trader
  POST /api/portfolio/agreement            — accept paper trading disclaimer
  POST /api/portfolio/trade                — manual BUY/SELL from Live Prices

  GET  /api/portfolio/admin/status         — kill switch + active users (admin)
  POST /api/portfolio/admin/kill-switch    — enable/disable all auto-trading (admin)

Services injected by main.py:
    portfolio.portfolio_svc  = portfolio_svc
    portfolio.auto_trader_svc = auto_trader_svc
    portfolio.price_svc      = price_svc
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected by main.py
portfolio_svc    = None
auto_trader_svc  = None
price_svc        = None


# ── Request models ────────────────────────────────────────────────────────────

class DepositRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Virtual amount to deposit (must be > 0)")

class WithdrawRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Virtual amount to withdraw (must be > 0)")

class StrategyRequest(BaseModel):
    strategy:       str            # aggressive | balanced | tech_heavy | income | conservative
    stop_loss_pct:  Optional[float] = None   # override within ±2% of strategy default

class ToggleRequest(BaseModel):
    is_active: bool

class TradeRequest(BaseModel):
    symbol: str
    action: str   # "BUY" | "SELL"

class KillSwitchRequest(BaseModel):
    enabled: bool


# ── Guards ────────────────────────────────────────────────────────────────────

def _require_portfolio_svc():
    if not portfolio_svc:
        raise HTTPException(503, "Portfolio service not initialised")

def _require_auto_trader_svc():
    if not auto_trader_svc:
        raise HTTPException(503, "Auto-trader service not initialised")


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get("/")
async def get_portfolio_summary(user=Depends(get_current_user)):
    """
    Full dashboard snapshot: wallet totals, positions valued at current prices,
    P&L summary, strategy config.
    """
    _require_portfolio_svc()
    uid = user["uid"]

    # Fetch live prices to value positions
    prices = {}
    if price_svc:
        try:
            raw = await price_svc.get_all()
            prices = {sym: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                      for sym, d in raw.items()}
        except Exception as e:
            logger.warning(f"portfolio summary: price fetch failed for {uid}: {e}")

    try:
        return await portfolio_svc.get_summary(uid, prices=prices)
    except Exception as e:
        logger.error(f"get_portfolio_summary failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch portfolio summary")


@router.get("/wallet")
async def get_wallet(user=Depends(get_current_user)):
    """Return wallet balances and settings only (no positions)."""
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        return await portfolio_svc.get_or_create_wallet(uid)
    except Exception as e:
        logger.error(f"get_wallet failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch wallet")


@router.get("/positions")
async def get_positions(user=Depends(get_current_user)):
    """Return open positions with live P&L if prices available."""
    _require_portfolio_svc()
    uid = user["uid"]

    prices = {}
    if price_svc:
        try:
            raw = await price_svc.get_all()
            prices = {sym: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                      for sym, d in raw.items()}
        except Exception:
            pass

    try:
        positions = await portfolio_svc.get_positions(uid)

        # Enrich with live price if available
        for pos in positions:
            sym = pos["symbol"]
            lp  = prices.get(sym)
            if lp and lp > 0:
                shares = pos["shares"]
                bp     = pos["buy_price"]
                cv     = round(shares * lp, 2)
                pnl    = round(cv - shares * bp, 2)
                pos.update({
                    "current_price":      lp,
                    "current_value":      cv,
                    "unrealized_pnl":     pnl,
                    "unrealized_pnl_pct": round((pnl / (shares * bp)) * 100, 2) if bp else 0.0,
                })

        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        logger.error(f"get_positions failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch positions")


@router.get("/pnl")
async def get_pnl(user=Depends(get_current_user)):
    """
    P&L breakdown: realized, unrealized, total return %, daily P&L chart data.
    Used by the portfolio value chart on the Auto-Trader page.
    """
    _require_portfolio_svc()
    uid = user["uid"]

    prices = {}
    if price_svc:
        try:
            raw = await price_svc.get_all()
            prices = {sym: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                      for sym, d in raw.items()}
        except Exception:
            pass

    try:
        return await portfolio_svc.get_pnl(uid, prices=prices)
    except Exception as e:
        logger.error(f"get_pnl failed for {uid}: {e}")
        raise HTTPException(500, "Failed to calculate P&L")


@router.get("/trades")
async def get_trade_history(user=Depends(get_current_user), limit: int = 50):
    """Trade history newest-first. limit capped at 200."""
    _require_portfolio_svc()
    uid   = user["uid"]
    limit = min(limit, 200)
    try:
        trades = await portfolio_svc.get_trade_history(uid, limit=limit)
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        logger.error(f"get_trade_history failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch trade history")


@router.get("/transactions")
async def get_transaction_history(user=Depends(get_current_user), limit: int = 50):
    """Wallet transaction log (deposits, withdrawals, resets, strategy changes)."""
    _require_portfolio_svc()
    uid   = user["uid"]
    limit = min(limit, 200)
    try:
        txns = await portfolio_svc.get_transaction_history(uid, limit=limit)
        return {"transactions": txns, "count": len(txns)}
    except Exception as e:
        logger.error(f"get_transaction_history failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch transactions")


@router.get("/strategies")
async def get_strategies():
    """
    Return all 5 strategy definitions for the frontend strategy selector.
    No auth required — public catalogue.
    """
    _require_portfolio_svc()
    try:
        from services.portfolio_service import PortfolioService
        return {"strategies": PortfolioService.get_strategy_catalogue()}
    except Exception as e:
        logger.error(f"get_strategies failed: {e}")
        raise HTTPException(500, "Failed to fetch strategies")


# ── Write endpoints ───────────────────────────────────────────────────────────

@router.post("/deposit")
async def deposit(req: DepositRequest, user=Depends(get_current_user)):
    """Add virtual funds to the wallet."""
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        return await portfolio_svc.deposit(uid, req.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"deposit failed for {uid}: {e}")
        raise HTTPException(500, "Deposit failed")


@router.post("/withdraw")
async def withdraw(req: WithdrawRequest, user=Depends(get_current_user)):
    """
    Withdraw virtual cash.
    Returns 400 if amount exceeds available (uninvested) balance.
    """
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        return await portfolio_svc.withdraw(uid, req.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"withdraw failed for {uid}: {e}")
        raise HTTPException(500, "Withdrawal failed")


@router.post("/reset")
async def reset_portfolio(user=Depends(get_current_user)):
    """
    Reset portfolio: close all positions, restore starting balance.
    Trade and transaction history is preserved.
    """
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        return await portfolio_svc.reset_portfolio(uid)
    except Exception as e:
        logger.error(f"reset_portfolio failed for {uid}: {e}")
        raise HTTPException(500, "Portfolio reset failed")


@router.post("/strategy")
async def set_strategy(req: StrategyRequest, user=Depends(get_current_user)):
    """
    Change investment strategy.
    stop_loss_pct must be within ±2% of the strategy default if provided.
    Existing positions remain open under old strategy rules until closed.
    """
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        return await portfolio_svc.set_strategy(uid, req.strategy, req.stop_loss_pct)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"set_strategy failed for {uid}: {e}")
        raise HTTPException(500, "Strategy change failed")


@router.post("/toggle")
async def toggle_auto_trader(req: ToggleRequest, user=Depends(get_current_user)):
    """Start or pause the auto-trader for this user."""
    _require_portfolio_svc()
    uid = user["uid"]

    # Must accept agreement before activating
    if req.is_active:
        wallet = await portfolio_svc.get_or_create_wallet(uid)
        if not wallet.get("agreement_accepted"):
            raise HTTPException(
                400,
                "Paper trading agreement must be accepted before activating auto-trader"
            )

    try:
        return await portfolio_svc.set_active(uid, req.is_active)
    except Exception as e:
        logger.error(f"toggle_auto_trader failed for {uid}: {e}")
        raise HTTPException(500, "Toggle failed")


@router.post("/agreement")
async def accept_agreement(user=Depends(get_current_user)):
    """
    Record that the user has accepted the paper trading disclaimer.
    Must be called before the auto-trader can be activated.
    Shown once on first visit to the Auto-Trader tab.
    """
    _require_portfolio_svc()
    uid = user["uid"]
    try:
        await portfolio_svc.get_or_create_wallet(uid)   # ensure wallet exists
        return await portfolio_svc.accept_agreement(uid)
    except Exception as e:
        logger.error(f"accept_agreement failed for {uid}: {e}")
        raise HTTPException(500, "Failed to record agreement")


@router.post("/trade")
async def manual_trade(req: TradeRequest, user=Depends(get_current_user)):
    """
    User-triggered BUY or SELL from the Live Prices page.
    Bypasses auto-trader is_active and universe restrictions.
    Still requires agreement_accepted.
    """
    _require_auto_trader_svc()
    uid    = user["uid"]
    action = req.action.upper()

    if action not in ("BUY", "SELL"):
        raise HTTPException(400, "action must be BUY or SELL")

    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol is required")

    try:
        result = await auto_trader_svc.execute_manual_trade(uid, symbol, action)
        if result.get("status") == "error":
            raise HTTPException(400, result.get("reason", "Trade failed"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"manual_trade failed for {uid} {action} {symbol}: {e}")
        raise HTTPException(500, "Trade execution failed")


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.get("/admin/status")
async def get_auto_trader_status(admin=Depends(require_admin)):
    """
    Admin: auto-trader kill switch state + number of active users.
    Shown on admin dashboard.
    """
    _require_auto_trader_svc()
    try:
        return await auto_trader_svc.get_status()
    except Exception as e:
        logger.error(f"get_auto_trader_status failed: {e}")
        raise HTTPException(500, "Failed to fetch auto-trader status")


@router.post("/admin/kill-switch")
async def set_kill_switch(req: KillSwitchRequest, admin=Depends(require_admin)):
    """
    Admin: enable or disable all autonomous trading globally.
    Writes config/autotrader.enabled to Firestore — takes effect within 60s
    (next scheduled run / stop-loss check). No redeploy required.
    """
    _require_auto_trader_svc()
    try:
        result = await auto_trader_svc.set_kill_switch(req.enabled)
        state  = "enabled" if req.enabled else "disabled"
        logger.info(f"Admin {admin['uid'][:8]}… set auto-trader kill switch → {state}")
        return result
    except Exception as e:
        logger.error(f"set_kill_switch failed: {e}")
        raise HTTPException(500, "Failed to update kill switch")