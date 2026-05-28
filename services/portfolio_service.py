"""
services/portfolio_service.py

Virtual portfolio ledger for SignalBoard Auto-Trader v2.
All positions, balances, and trades are stored in Firestore — Alpaca is
NOT used for order execution.  Alpaca remains the data source for prices only.

Firestore schema (per user):
  users/{uid}/trader            — wallet summary + strategy settings
  users/{uid}/positions/{sym}   — open positions (one doc per symbol)
  users/{uid}/trades/{auto_id}  — immutable trade log (every BUY/SELL)
  users/{uid}/transactions/{id} — wallet events (deposit/withdraw/reset/strategy_change)

Strategy universes, position sizing, and stop-loss ranges are from
System Design Document v4.2, Section 11.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from firebase_admin import firestore as fs

logger = logging.getLogger(__name__)

# ── Strategy definitions ──────────────────────────────────────────────────────

STRATEGIES: dict[str, dict] = {
    "aggressive": {
        "label":             "Aggressive Growth",
        "description":       "100% equities, HIGH confidence only, max 25% per position.",
        "risk_level":        "HIGH",
        "position_pct":      0.25,        # max % of wallet per position
        "min_confidence":    "HIGH",
        "stop_loss_default": 8.0,         # %
        "stop_loss_min":     6.0,
        "stop_loss_max":     10.0,
        "universe":          ["NVDA", "AAPL", "META", "AMZN", "GOOGL", "MSFT", "HOOD"],
        "cash_reserve_pct":  0.0,
    },
    "balanced": {
        "label":             "Balanced / Hybrid",
        "description":       "60% equities, 30% ETFs, 10% defensive. HIGH + MEDIUM signals. Max 15% per position.",
        "risk_level":        "MEDIUM",
        "position_pct":      0.15,
        "min_confidence":    "MEDIUM",
        "stop_loss_default": 5.0,
        "stop_loss_min":     3.0,
        "stop_loss_max":     7.0,
        "universe":          ["SPY", "VOO", "SCHD", "MSFT", "AAPL", "NVDA", "JEPI"],
        "cash_reserve_pct":  0.10,
    },
    "tech_heavy": {
        "label":             "Tech Heavy",
        "description":       "80% technology sector, 20% ETFs. HIGH confidence only. Max 20% per position.",
        "risk_level":        "HIGH",
        "position_pct":      0.20,
        "min_confidence":    "HIGH",
        "stop_loss_default": 7.0,
        "stop_loss_min":     5.0,
        "stop_loss_max":     9.0,
        "universe":          ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMZN"],
        "cash_reserve_pct":  0.0,
    },
    "income": {
        "label":             "Income / Dividend",
        "description":       "70% dividend ETFs, 30% defensive equities. Max 20% per position.",
        "risk_level":        "LOW",
        "position_pct":      0.20,
        "min_confidence":    "MEDIUM",
        "stop_loss_default": 4.0,
        "stop_loss_min":     2.0,
        "stop_loss_max":     6.0,
        "universe":          ["JEPI", "JEPQ", "SCHD", "VOO", "SGOV"],
        "cash_reserve_pct":  0.10,
    },
    "conservative": {
        "label":             "Conservative",
        "description":       "50% defensive ETFs, 30% blue-chip, 20% cash. HIGH confidence BUY only. Tight stop-loss.",
        "risk_level":        "LOW",
        "position_pct":      0.10,
        "min_confidence":    "HIGH",
        "stop_loss_default": 3.0,
        "stop_loss_min":     1.0,
        "stop_loss_max":     5.0,
        "universe":          ["SGOV", "SCHD", "VOO", "AAPL", "MSFT"],
        "cash_reserve_pct":  0.20,
    },
}

STARTING_BALANCE = 10_000.00
CONFIDENCE_RANK  = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


class PortfolioService:
    """
    Firestore-backed virtual portfolio ledger.

    Usage:
        portfolio_svc = PortfolioService()
        portfolio_svc.set_db(db)           # called at startup after Firebase init
    """

    def __init__(self):
        self._db = None

    def set_db(self, db) -> None:
        self._db = db
        logger.info("PortfolioService: Firestore connected ✓")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _user_ref(self, uid: str):
        return self._db.collection("users").document(uid)

    def _trader_ref(self, uid: str):
        return self._user_ref(uid).collection("trader").document("wallet")

    def _position_ref(self, uid: str, symbol: str):
        return self._user_ref(uid).collection("positions").document(symbol.upper())

    def _trades_ref(self, uid: str):
        return self._user_ref(uid).collection("trades")

    def _transactions_ref(self, uid: str):
        return self._user_ref(uid).collection("transactions")

    async def _run(self, fn):
        """Run a synchronous Firestore call in a thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _serialize(self, data: dict) -> dict:
        """Convert Firestore Timestamps to ISO strings for JSON responses."""
        out = {}
        for k, v in data.items():
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            elif hasattr(v, "seconds"):          # Firestore Timestamp
                out[k] = datetime.fromtimestamp(v.seconds, tz=timezone.utc).isoformat()
            else:
                out[k] = v
        return out

    # ── Wallet init & fetch ───────────────────────────────────────────────────

    async def get_or_create_wallet(self, uid: str) -> dict:
        """
        Return the user's trader wallet.
        Creates it with STARTING_BALANCE on first access.
        """
        ref = self._trader_ref(uid)
        doc = await self._run(ref.get)

        if doc.exists:
            return self._serialize(doc.to_dict() or {})

        # First time — initialise wallet
        now = self._now_iso()
        wallet = {
            "balance":           STARTING_BALANCE,
            "invested":          0.0,
            "total_value":       STARTING_BALANCE,
            "total_deposited":   STARTING_BALANCE,
            "total_withdrawn":   0.0,
            "realized_pnl":      0.0,
            "strategy":          "balanced",
            "stop_loss_pct":     STRATEGIES["balanced"]["stop_loss_default"],
            "is_active":         False,
            "agreement_accepted": False,
            "created_at":        now,
            "last_updated":      now,
        }
        await self._run(lambda: ref.set(wallet))

        # Log initial deposit as a transaction
        await self._log_transaction(uid, {
            "type":           "deposit",
            "amount":         STARTING_BALANCE,
            "balance_before": 0.0,
            "balance_after":  STARTING_BALANCE,
            "notes":          "Initial virtual balance",
        })

        logger.info(f"PortfolioService: created wallet for {uid} (${STARTING_BALANCE:,.2f})")
        return wallet

    async def get_wallet(self, uid: str) -> Optional[dict]:
        """Return wallet if it exists, None otherwise."""
        ref = self._trader_ref(uid)
        doc = await self._run(ref.get)
        if not doc.exists:
            return None
        return self._serialize(doc.to_dict() or {})

    # ── Wallet updates ────────────────────────────────────────────────────────

    async def deposit(self, uid: str, amount: float) -> dict:
        """Add virtual funds to the wallet."""
        if amount <= 0:
            raise ValueError("Deposit amount must be positive")

        wallet = await self.get_or_create_wallet(uid)
        old_balance = wallet["balance"]
        new_balance = round(old_balance + amount, 2)

        now = self._now_iso()
        await self._run(lambda: self._trader_ref(uid).update({
            "balance":         new_balance,
            "total_deposited": round(wallet.get("total_deposited", 0) + amount, 2),
            "last_updated":    now,
        }))

        await self._log_transaction(uid, {
            "type":           "deposit",
            "amount":         amount,
            "balance_before": old_balance,
            "balance_after":  new_balance,
            "notes":          f"Virtual deposit of ${amount:,.2f}",
        })

        logger.info(f"PortfolioService: deposit ${amount:,.2f} for {uid} → balance ${new_balance:,.2f}")
        return {"balance": new_balance, "deposited": amount}

    async def withdraw(self, uid: str, amount: float) -> dict:
        """
        Withdraw virtual cash from the wallet.
        Only available balance (not invested funds) can be withdrawn.
        """
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive")

        wallet = await self.get_or_create_wallet(uid)
        available = wallet["balance"]

        if amount > available:
            raise ValueError(
                f"Cannot withdraw ${amount:,.2f} — only ${available:,.2f} available "
                f"(${wallet.get('invested', 0):,.2f} is currently invested)"
            )

        new_balance = round(available - amount, 2)
        now = self._now_iso()

        await self._run(lambda: self._trader_ref(uid).update({
            "balance":        new_balance,
            "total_withdrawn": round(wallet.get("total_withdrawn", 0) + amount, 2),
            "last_updated":   now,
        }))

        await self._log_transaction(uid, {
            "type":           "withdraw",
            "amount":         amount,
            "balance_before": available,
            "balance_after":  new_balance,
            "notes":          f"Virtual withdrawal of ${amount:,.2f}",
        })

        logger.info(f"PortfolioService: withdraw ${amount:,.2f} for {uid} → balance ${new_balance:,.2f}")
        return {"balance": new_balance, "withdrawn": amount}

    async def reset_portfolio(self, uid: str) -> dict:
        """
        Reset portfolio: close all positions, return to STARTING_BALANCE.
        Trade log and transaction history are preserved for audit.
        """
        wallet = await self.get_or_create_wallet(uid)

        # Close all positions — log each as a reset-sell
        positions = await self.get_positions(uid)
        for pos in positions:
            await self._run(lambda sym=pos["symbol"]: self._position_ref(uid, sym).delete())

        now = self._now_iso()
        await self._run(lambda: self._trader_ref(uid).update({
            "balance":        STARTING_BALANCE,
            "invested":       0.0,
            "total_value":    STARTING_BALANCE,
            "realized_pnl":   0.0,
            "last_updated":   now,
        }))

        await self._log_transaction(uid, {
            "type":           "reset",
            "amount":         STARTING_BALANCE,
            "balance_before": wallet.get("balance", 0),
            "balance_after":  STARTING_BALANCE,
            "notes":          f"Portfolio reset. {len(positions)} positions closed.",
        })

        logger.info(f"PortfolioService: reset portfolio for {uid} — {len(positions)} positions cleared")
        return {"balance": STARTING_BALANCE, "positions_closed": len(positions)}

    # ── Strategy management ───────────────────────────────────────────────────

    async def set_strategy(self, uid: str, strategy: str, stop_loss_pct: Optional[float] = None) -> dict:
        """
        Change investment strategy. Existing positions are held under old strategy rules
        until closed; new positions follow the new strategy.
        stop_loss_pct must be within the strategy's allowed range if provided.
        """
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy '{strategy}'. Valid: {list(STRATEGIES.keys())}")

        cfg = STRATEGIES[strategy]

        # Validate custom stop-loss
        if stop_loss_pct is not None:
            if not (cfg["stop_loss_min"] <= stop_loss_pct <= cfg["stop_loss_max"]):
                raise ValueError(
                    f"stop_loss_pct {stop_loss_pct}% out of range for '{strategy}' "
                    f"({cfg['stop_loss_min']}–{cfg['stop_loss_max']}%)"
                )
            sl = stop_loss_pct
        else:
            sl = cfg["stop_loss_default"]

        wallet = await self.get_or_create_wallet(uid)
        old_strategy = wallet.get("strategy", "balanced")

        now = self._now_iso()
        await self._run(lambda: self._trader_ref(uid).update({
            "strategy":      strategy,
            "stop_loss_pct": sl,
            "last_updated":  now,
        }))

        await self._log_transaction(uid, {
            "type":           "strategy_change",
            "amount":         0.0,
            "balance_before": wallet.get("balance", 0),
            "balance_after":  wallet.get("balance", 0),
            "notes":          f"Strategy changed from '{old_strategy}' to '{strategy}'. Stop-loss: {sl}%",
        })

        logger.info(f"PortfolioService: strategy change for {uid}: {old_strategy} → {strategy} (SL {sl}%)")
        return {"strategy": strategy, "stop_loss_pct": sl, "config": cfg}

    async def set_active(self, uid: str, is_active: bool) -> dict:
        """Toggle auto-trader on/off for a user."""
        now = self._now_iso()
        await self._run(lambda: self._trader_ref(uid).update({
            "is_active":    is_active,
            "last_updated": now,
        }))
        state = "started" if is_active else "paused"
        logger.info(f"PortfolioService: auto-trader {state} for {uid}")
        return {"is_active": is_active}

    async def accept_agreement(self, uid: str) -> dict:
        """Record that the user accepted the paper trading disclaimer."""
        now = self._now_iso()
        await self._run(lambda: self._trader_ref(uid).update({
            "agreement_accepted":    True,
            "agreement_accepted_at": now,
            "last_updated":          now,
        }))
        return {"agreement_accepted": True}

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self, uid: str) -> list[dict]:
        """Return all open positions for a user."""
        def _fetch():
            docs = self._user_ref(uid).collection("positions").stream()
            return [self._serialize(d.to_dict() or {}) for d in docs]

        return await self._run(_fetch)

    async def get_position(self, uid: str, symbol: str) -> Optional[dict]:
        """Return a single open position, or None if not held."""
        doc = await self._run(lambda: self._position_ref(uid, symbol.upper()).get())
        if not doc.exists:
            return None
        return self._serialize(doc.to_dict() or {})

    # ── Trade execution ───────────────────────────────────────────────────────

    async def execute_buy(
        self,
        uid:         str,
        symbol:      str,
        price:       float,
        signal:      dict,
        trigger:     str = "auto",
    ) -> dict:
        """
        Open a virtual BUY position.

        Pre-checks (all must pass):
          1. Wallet is_active=True  (or trigger=="manual" to allow manual buys)
          2. Signal confidence meets strategy minimum
          3. Symbol is in strategy universe  (skipped for manual buys)
          4. Sufficient cash available (respecting cash_reserve_pct)
          5. Not already holding this symbol

        Position size = strategy.position_pct × wallet.balance
        """
        symbol = symbol.upper()
        wallet = await self.get_or_create_wallet(uid)
        strategy_key = wallet.get("strategy", "balanced")
        cfg = STRATEGIES[strategy_key]

        # 1. Active check (manual buys bypass is_active requirement)
        if not wallet.get("is_active") and trigger != "manual":
            return {"status": "skipped", "reason": "Auto-trader is paused"}

        # 2. Agreement check
        if not wallet.get("agreement_accepted"):
            return {"status": "skipped", "reason": "Paper trading agreement not accepted"}

        # 3. Confidence check (skip for manual)
        if trigger != "manual":
            signal_conf = signal.get("confidence", "LOW")
            min_conf = cfg["min_confidence"]
            if CONFIDENCE_RANK.get(signal_conf, 0) < CONFIDENCE_RANK.get(min_conf, 0):
                return {
                    "status": "skipped",
                    "reason": f"Signal confidence {signal_conf} below {strategy_key} minimum ({min_conf})",
                }

        # 4. Universe check (skip for manual)
        if trigger != "manual" and symbol not in cfg["universe"]:
            return {
                "status": "skipped",
                "reason": f"{symbol} not in {strategy_key} universe: {cfg['universe']}",
            }

        # 5. Already holding?
        existing = await self.get_position(uid, symbol)
        if existing:
            return {"status": "skipped", "reason": f"Already holding {symbol}"}

        # 6. Calculate investable cash
        balance = wallet["balance"]
        reserve = balance * cfg["cash_reserve_pct"]
        investable = balance - reserve
        position_size = round(investable * cfg["position_pct"], 2)

        if position_size < 1.0:
            return {"status": "skipped", "reason": f"Insufficient cash (${balance:.2f} available, reserve ${reserve:.2f})"}

        shares = round(position_size / price, 6)
        stop_loss_price = round(price * (1 - wallet.get("stop_loss_pct", cfg["stop_loss_default"]) / 100), 2)

        now = self._now_iso()

        # Write position
        position = {
            "symbol":           symbol,
            "shares":           shares,
            "buy_price":        price,
            "buy_date":         now,
            "current_price":    price,
            "current_value":    position_size,
            "unrealized_pnl":   0.0,
            "unrealized_pnl_pct": 0.0,
            "stop_loss_price":  stop_loss_price,
            "strategy_at_buy":  strategy_key,
            "signal_ref":       signal.get("generated_at", now),
            "signal_confidence": signal.get("confidence", ""),
            "signal_conviction": signal.get("conviction_score", 0),
        }
        await self._run(lambda: self._position_ref(uid, symbol).set(position))

        # Deduct from balance
        new_balance = round(balance - position_size, 2)
        new_invested = round(wallet.get("invested", 0.0) + position_size, 2)

        await self._run(lambda: self._trader_ref(uid).update({
            "balance":      new_balance,
            "invested":     new_invested,
            "total_value":  round(new_balance + new_invested, 2),
            "last_updated": now,
        }))

        # Log trade
        trade = {
            "symbol":           symbol,
            "action":           "BUY",
            "shares":           shares,
            "price":            price,
            "total":            position_size,
            "pnl":              0.0,
            "reason":           signal.get("summary", ""),
            "signal_ref":       signal.get("generated_at", now),
            "signal_confidence": signal.get("confidence", ""),
            "trigger":          trigger,
            "balance_after":    new_balance,
            "timestamp":        now,
        }
        await self._run(lambda: self._trades_ref(uid).add(trade))

        logger.info(
            f"PortfolioService: BUY {symbol} {shares:.4f}sh @ ${price:.2f} "
            f"(${position_size:.2f}) for {uid} — balance ${new_balance:.2f}"
        )
        return {"status": "executed", "action": "BUY", **trade}

    async def _execute_buy_shares(
        self,
        uid:     str,
        symbol:  str,
        price:   float,
        shares:  float,
        signal:  dict,
        trigger: str = "manual",
    ) -> dict:
        """
        Execute a BUY with an exact share count (for manual fractional trades).
        Skips strategy position sizing — caller is responsible for validation.
        """
        symbol = symbol.upper()
        wallet = await self.get_or_create_wallet(uid)

        strategy_key   = wallet.get("strategy", "balanced")
        cfg            = STRATEGIES.get(strategy_key, STRATEGIES["balanced"])
        position_size  = round(shares * price, 2)
        stop_loss_price = round(price * (1 - wallet.get("stop_loss_pct", cfg["stop_loss_default"]) / 100), 2)

        now = self._now_iso()

        position = {
            "symbol":             symbol,
            "shares":             round(shares, 6),
            "buy_price":          price,
            "buy_date":           now,
            "current_price":      price,
            "current_value":      position_size,
            "unrealized_pnl":     0.0,
            "unrealized_pnl_pct": 0.0,
            "stop_loss_price":    stop_loss_price,
            "strategy_at_buy":    strategy_key,
            "signal_ref":         signal.get("generated_at", now),
            "signal_confidence":  signal.get("confidence", "HIGH"),
            "signal_conviction":  signal.get("conviction_score", 0),
        }
        await self._run(lambda: self._position_ref(uid, symbol).set(position))

        new_balance  = round(wallet["balance"] - position_size, 2)
        new_invested = round(wallet.get("invested", 0.0) + position_size, 2)

        await self._run(lambda: self._trader_ref(uid).update({
            "balance":      new_balance,
            "invested":     new_invested,
            "total_value":  round(new_balance + new_invested, 2),
            "last_updated": now,
        }))

        trade = {
            "symbol":            symbol,
            "action":            "BUY",
            "shares":            round(shares, 6),
            "price":             price,
            "total":             position_size,
            "pnl":               0.0,
            "reason":            signal.get("summary", "Manual buy"),
            "signal_ref":        signal.get("generated_at", now),
            "signal_confidence": signal.get("confidence", "HIGH"),
            "trigger":           trigger,
            "balance_after":     new_balance,
            "timestamp":         now,
        }
        await self._run(lambda: self._trades_ref(uid).add(trade))

        logger.info(
            f"PortfolioService: BUY {symbol} {shares:.6f}sh @ ${price:.2f} "
            f"(${position_size:.2f}) trigger={trigger} for {uid}"
        )
        return {"status": "executed", "action": "BUY", **trade}

    async def execute_sell(
        self,
        uid:     str,
        symbol:  str,
        price:   float,
        signal:  dict,
        trigger: str = "auto",
        reason:  str = "signal",
    ) -> dict:
        """
        Close a virtual position (full exit only).
        trigger: "auto" | "manual" | "stop_loss" | "rebalance"
        """
        symbol = symbol.upper()
        position = await self.get_position(uid, symbol)

        if not position:
            return {"status": "skipped", "reason": f"No open position for {symbol}"}

        wallet = await self.get_or_create_wallet(uid)

        shares      = position["shares"]
        buy_price   = position["buy_price"]
        proceeds    = round(shares * price, 2)
        cost_basis  = round(shares * buy_price, 2)
        pnl         = round(proceeds - cost_basis, 2)
        pnl_pct     = round((pnl / cost_basis) * 100, 2) if cost_basis else 0.0

        now = self._now_iso()

        # Delete position
        await self._run(lambda: self._position_ref(uid, symbol).delete())

        # Update wallet
        new_balance  = round(wallet["balance"] + proceeds, 2)
        new_invested = round(max(0.0, wallet.get("invested", 0.0) - cost_basis), 2)
        new_realized = round(wallet.get("realized_pnl", 0.0) + pnl, 2)

        await self._run(lambda: self._trader_ref(uid).update({
            "balance":      new_balance,
            "invested":     new_invested,
            "total_value":  round(new_balance + new_invested, 2),
            "realized_pnl": new_realized,
            "last_updated": now,
        }))

        # Log trade
        trade = {
            "symbol":           symbol,
            "action":           "SELL",
            "shares":           shares,
            "price":            price,
            "total":            proceeds,
            "pnl":              pnl,
            "pnl_pct":          pnl_pct,
            "reason":           reason or signal.get("summary", ""),
            "signal_ref":       signal.get("generated_at", now),
            "signal_confidence": signal.get("confidence", ""),
            "trigger":          trigger,
            "balance_after":    new_balance,
            "timestamp":        now,
        }
        await self._run(lambda: self._trades_ref(uid).add(trade))

        logger.info(
            f"PortfolioService: SELL {symbol} {shares:.4f}sh @ ${price:.2f} "
            f"P&L ${pnl:+.2f} ({pnl_pct:+.2f}%) trigger={trigger} for {uid}"
        )
        return {"status": "executed", "action": "SELL", **trade}

    # ── Position valuation ────────────────────────────────────────────────────

    async def update_position_prices(self, uid: str, prices: dict[str, float]) -> dict:
        """
        Refresh current_price, current_value, unrealized P&L for all positions.
        Called by the stop-loss monitor and the portfolio summary endpoint.
        Returns summary of updated positions.
        """
        positions = await self.get_positions(uid)
        if not positions:
            return {"updated": 0}

        total_invested = 0.0
        updated = 0

        for pos in positions:
            symbol    = pos["symbol"]
            new_price = prices.get(symbol)
            if new_price is None:
                continue

            shares     = pos["shares"]
            buy_price  = pos["buy_price"]
            new_value  = round(shares * new_price, 2)
            pnl        = round(new_value - shares * buy_price, 2)
            pnl_pct    = round((pnl / (shares * buy_price)) * 100, 2) if buy_price else 0.0

            await self._run(lambda s=symbol, nv=new_value, np_=new_price, p=pnl, pp=pnl_pct: (
                self._position_ref(uid, s).update({
                    "current_price":    np_,
                    "current_value":    nv,
                    "unrealized_pnl":   p,
                    "unrealized_pnl_pct": pp,
                })
            ))
            total_invested += new_value
            updated += 1

        # Sync invested total on wallet
        wallet = await self.get_wallet(uid)
        if wallet and updated:
            now = self._now_iso()
            await self._run(lambda ti=total_invested: self._trader_ref(uid).update({
                "invested":     round(ti, 2),
                "total_value":  round(wallet["balance"] + ti, 2),
                "last_updated": now,
            }))

        return {"updated": updated, "total_invested": round(total_invested, 2)}

    # ── Stop-loss monitor ─────────────────────────────────────────────────────

    async def check_stop_losses(self, uid: str, prices: dict[str, float]) -> list[dict]:
        """
        Check all open positions against their stop_loss_price.
        Executes sell for any position breaching the threshold.
        Called every 60 seconds by the scheduler.
        Returns list of triggered stop-losses.
        """
        positions = await self.get_positions(uid)
        triggered = []

        for pos in positions:
            symbol       = pos["symbol"]
            current      = prices.get(symbol)
            stop_price   = pos.get("stop_loss_price")

            if current is None or stop_price is None:
                continue

            if current <= stop_price:
                reason = (
                    f"Stop-loss triggered: ${current:.2f} ≤ ${stop_price:.2f} "
                    f"({pos.get('unrealized_pnl_pct', 0):+.2f}%)"
                )
                logger.warning(f"PortfolioService: STOP-LOSS {symbol} for {uid} — {reason}")

                result = await self.execute_sell(
                    uid, symbol, current,
                    signal={},
                    trigger="stop_loss",
                    reason=reason,
                )
                triggered.append(result)

        return triggered

    # ── Portfolio summary ─────────────────────────────────────────────────────

    async def get_summary(self, uid: str, prices: Optional[dict[str, float]] = None) -> dict:
        """
        Return full portfolio snapshot for the Auto-Trader dashboard.
        If prices dict provided, positions are valued at current market prices.
        """
        wallet    = await self.get_or_create_wallet(uid)
        positions = await self.get_positions(uid)

        if prices:
            for pos in positions:
                sym   = pos["symbol"]
                price = prices.get(sym)
                if price:
                    shares   = pos["shares"]
                    bp       = pos["buy_price"]
                    cv       = round(shares * price, 2)
                    pnl      = round(cv - shares * bp, 2)
                    pnl_pct  = round((pnl / (shares * bp)) * 100, 2) if bp else 0.0
                    pos.update({
                        "current_price":      price,
                        "current_value":      cv,
                        "unrealized_pnl":     pnl,
                        "unrealized_pnl_pct": pnl_pct,
                    })

        invested_live = sum(p.get("current_value", 0) for p in positions)
        total_value   = round(wallet["balance"] + invested_live, 2)
        cost_basis    = sum(p["shares"] * p["buy_price"] for p in positions)
        unrealized    = round(invested_live - cost_basis, 2)
        realized      = wallet.get("realized_pnl", 0.0)
        total_pnl     = round(unrealized + realized, 2)
        total_pnl_pct = round(
            (total_pnl / wallet.get("total_deposited", STARTING_BALANCE)) * 100, 2
        ) if wallet.get("total_deposited") else 0.0

        strategy_key = wallet.get("strategy", "balanced")
        cfg          = STRATEGIES.get(strategy_key, STRATEGIES["balanced"])

        return {
            "wallet": {
                **wallet,
                "total_value":    total_value,
                "invested":       round(invested_live, 2),
                "unrealized_pnl": unrealized,
                "realized_pnl":   realized,
                "total_pnl":      total_pnl,
                "total_pnl_pct":  total_pnl_pct,
            },
            "strategy":       cfg,
            "strategy_key":   strategy_key,
            "positions":      positions,
            "position_count": len(positions),
        }

    # ── Trade & transaction history ───────────────────────────────────────────

    async def get_trade_history(self, uid: str, limit: int = 50) -> list[dict]:
        """Return most recent trades, newest first."""
        def _fetch():
            docs = (
                self._trades_ref(uid)
                .order_by("timestamp", direction=fs.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            return [self._serialize(d.to_dict() or {}) for d in docs]

        return await self._run(_fetch)

    async def get_transaction_history(self, uid: str, limit: int = 50) -> list[dict]:
        """Return wallet transaction history (deposits, withdrawals, resets, strategy changes)."""
        def _fetch():
            docs = (
                self._transactions_ref(uid)
                .order_by("timestamp", direction=fs.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            return [self._serialize(d.to_dict() or {}) for d in docs]

        return await self._run(_fetch)

    # ── P&L calculation ───────────────────────────────────────────────────────

    async def get_pnl(self, uid: str, prices: Optional[dict[str, float]] = None) -> dict:
        """
        Compute realized + unrealized P&L and daily breakdown from trade history.
        Used by backend #43 (P&L + performance endpoint).
        """
        wallet    = await self.get_or_create_wallet(uid)
        positions = await self.get_positions(uid)

        # Unrealized
        unrealized = 0.0
        if prices:
            for pos in positions:
                price = prices.get(pos["symbol"])
                if price:
                    unrealized += (price - pos["buy_price"]) * pos["shares"]
        else:
            unrealized = sum(p.get("unrealized_pnl", 0.0) for p in positions)

        realized = wallet.get("realized_pnl", 0.0)
        deposited = wallet.get("total_deposited", STARTING_BALANCE)

        total_pnl = round(unrealized + realized, 2)
        total_return_pct = round((total_pnl / deposited) * 100, 2) if deposited else 0.0

        # Daily P&L from trade log
        trades = await self.get_trade_history(uid, limit=200)
        daily: dict[str, float] = {}
        for t in trades:
            if t.get("action") == "SELL":
                day = (t.get("timestamp") or "")[:10]
                if day:
                    daily[day] = round(daily.get(day, 0.0) + t.get("pnl", 0.0), 2)

        return {
            "realized_pnl":      round(realized, 2),
            "unrealized_pnl":    round(unrealized, 2),
            "total_pnl":         total_pnl,
            "total_return_pct":  total_return_pct,
            "total_deposited":   deposited,
            "total_withdrawn":   wallet.get("total_withdrawn", 0.0),
            "daily_pnl":         dict(sorted(daily.items())),
            "open_positions":    len(positions),
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _log_transaction(self, uid: str, data: dict) -> None:
        """Append an entry to the user's transaction log."""
        entry = {
            **data,
            "timestamp": self._now_iso(),
        }
        await self._run(lambda: self._transactions_ref(uid).add(entry))

    # ── Strategy catalogue (public, no auth needed) ───────────────────────────

    @staticmethod
    def get_strategy_catalogue() -> dict:
        """Return all strategies for the frontend strategy selector."""
        return {
            k: {
                "key":           k,
                "label":         v["label"],
                "description":   v["description"],
                "risk_level":    v["risk_level"],
                "position_pct":  v["position_pct"],
                "min_confidence": v["min_confidence"],
                "stop_loss_default": v["stop_loss_default"],
                "stop_loss_min": v["stop_loss_min"],
                "stop_loss_max": v["stop_loss_max"],
                "universe":      v["universe"],
                "cash_reserve_pct": v["cash_reserve_pct"],
            }
            for k, v in STRATEGIES.items()
        }