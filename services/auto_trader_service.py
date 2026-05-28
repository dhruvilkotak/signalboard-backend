"""
services/auto_trader_service.py

Autonomous execution engine for SignalBoard Auto-Trader v2.
Wired into the existing APScheduler jobs in main.py.

Responsibilities:
  1. run_for_all_users()     — called by market-hours signal jobs after signals fire
  2. stop_loss_monitor()     — called every 60s by price_refresh_job
  3. rebalance_user()        — sell weakest position to fund a better signal
  4. execute_manual_trade()  — user-triggered BUY/SELL from Live Prices page

Kill switch:
  Reads config/autotrader.enabled from Firestore before every run.
  Set to false to halt all autonomous activity instantly without a redeploy.

Rebalancing rules (from design doc v4.2 §11.3):
  - New signal expected_return must exceed current weakest position's
    expected_return by MORE than 2%
  - Never sell a position that is up >10% (let winners run)
  - Only rebalance if cash is below strategy's minimum investable threshold
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Rebalancing thresholds
REBALANCE_RETURN_EDGE  = 2.0   # new signal must beat weakest by this many % points
REBALANCE_WINNER_GUARD = 10.0  # never sell a position up more than this %


class AutoTraderService:
    """
    Autonomous trading engine.  All state lives in Firestore via PortfolioService.

    Usage (main.py):
        auto_trader_svc = AutoTraderService(portfolio_svc, price_svc, signal_svc)
        auto_trader_svc.set_db(db)
        # then in market_hours_signal_job:
        await auto_trader_svc.run_for_all_users(signals)
        # and in price_refresh_job:
        await auto_trader_svc.stop_loss_monitor()
    """

    def __init__(self, portfolio_service, price_service, signal_service):
        self.portfolio_svc = portfolio_service
        self.price_svc     = price_service
        self.signal_svc    = signal_service
        self._db           = None

    def set_db(self, db) -> None:
        self._db = db
        logger.info("AutoTraderService: Firestore connected ✓")

    # ── Kill switch ───────────────────────────────────────────────────────────

    async def _is_enabled(self) -> bool:
        """
        Check config/autotrader.enabled in Firestore.
        Returns True if the field is absent (safe default = enabled).
        Returns False to halt all autonomous trading immediately.
        """
        if not self._db:
            return True
        try:
            loop = asyncio.get_running_loop()
            doc  = await loop.run_in_executor(
                None,
                lambda: self._db.collection("config").document("autotrader").get(),
            )
            if not doc.exists:
                return True
            return doc.to_dict().get("enabled", True)
        except Exception as e:
            logger.warning(f"AutoTraderService: kill-switch check failed ({e}) — defaulting to enabled")
            return True

    # ── Active user list ──────────────────────────────────────────────────────

    async def _get_active_users(self) -> list[str]:
        """
        Return UIDs of users who have is_active=True in their trader wallet.
        Queries Firestore collection-group 'trader' for active wallets.
        """
        if not self._db:
            return []
        try:
            loop = asyncio.get_running_loop()

            def _query():
                # Collection group query across all users/{uid}/trader
                docs = (
                    self._db.collection_group("trader")
                    .where("is_active", "==", True)
                    .where("agreement_accepted", "==", True)
                    .stream()
                )
                uids = []
                for doc in docs:
                    # Path: users/{uid}/trader/wallet
                    parts = doc.reference.path.split("/")
                    if len(parts) >= 2:
                        uids.append(parts[1])   # users/{uid}/...
                return uids

            return await loop.run_in_executor(None, _query)
        except Exception as e:
            logger.error(f"AutoTraderService: failed to fetch active users: {e}")
            return []

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run_for_all_users(self, signals: dict[str, dict]) -> dict:
        """
        Called by market_hours_signal_job after signals are generated.

        For each active user:
          1. Filter signals to those eligible for their strategy
          2. Execute BUY for qualifying BUY signals
          3. Execute SELL for qualifying SELL signals
          4. Attempt rebalance if cash is low but a strong signal exists

        signals: { symbol -> signal_dict } from run_signals_for_admin_tickers()
        Returns summary dict for logging.
        """
        if not await self._is_enabled():
            logger.info("AutoTraderService: kill switch active — skipping run")
            return {"skipped": True, "reason": "kill_switch"}

        active_uids = await self._get_active_users()
        if not active_uids:
            logger.info("AutoTraderService: no active users — nothing to do")
            return {"active_users": 0}

        # Fetch current prices once for all users
        try:
            prices = await self.price_svc.get_all()
        except Exception as e:
            logger.error(f"AutoTraderService: price fetch failed: {e}")
            prices = {}

        summary = {
            "active_users": len(active_uids),
            "buys":         0,
            "sells":        0,
            "skipped":      0,
            "rebalances":   0,
            "errors":       0,
        }

        # Run each user concurrently (capped at 10 to avoid Firestore hammering)
        sem = asyncio.Semaphore(10)

        async def _process_user(uid: str):
            async with sem:
                try:
                    result = await self._process_user_signals(uid, signals, prices)
                    summary["buys"]       += result.get("buys", 0)
                    summary["sells"]      += result.get("sells", 0)
                    summary["skipped"]    += result.get("skipped", 0)
                    summary["rebalances"] += result.get("rebalances", 0)
                except Exception as e:
                    logger.error(f"AutoTraderService: error processing {uid}: {e}")
                    summary["errors"] += 1

        await asyncio.gather(*[_process_user(uid) for uid in active_uids])

        logger.info(
            f"AutoTraderService: run complete — "
            f"{summary['buys']} buys / {summary['sells']} sells / "
            f"{summary['rebalances']} rebalances across {len(active_uids)} users"
        )
        return summary

    async def _process_user_signals(
        self,
        uid:     str,
        signals: dict[str, dict],
        prices:  dict[str, dict],
    ) -> dict:
        """
        Execute signal-driven trades for a single user.
        Returns per-user trade counts.
        """
        wallet = await self.portfolio_svc.get_wallet(uid)
        if not wallet:
            return {"buys": 0, "sells": 0, "skipped": 0, "rebalances": 0}

        from services.portfolio_service import STRATEGIES
        strategy_key = wallet.get("strategy", "balanced")
        cfg          = STRATEGIES.get(strategy_key, STRATEGIES["balanced"])
        universe     = set(cfg["universe"])

        counts = {"buys": 0, "sells": 0, "skipped": 0, "rebalances": 0}

        # 1. Process SELL signals first (free up cash before buying)
        for symbol, signal in signals.items():
            if symbol not in universe:
                continue
            if signal.get("signal") != "SELL":
                continue

            price_data = prices.get(symbol, {})
            price      = price_data.get("price", 0) if isinstance(price_data, dict) else float(price_data or 0)
            if price <= 0:
                continue

            result = await self.portfolio_svc.execute_sell(
                uid, symbol, price, signal, trigger="auto", reason=signal.get("summary", "")
            )
            if result.get("status") == "executed":
                counts["sells"] += 1
                logger.info(f"AutoTrader SELL: {uid[:8]}… {symbol} @ ${price:.2f}")
            else:
                counts["skipped"] += 1

        # 2. Process BUY signals
        for symbol, signal in signals.items():
            if symbol not in universe:
                continue
            if signal.get("signal") != "BUY":
                continue
            if not signal.get("feed_eligible"):
                counts["skipped"] += 1
                continue

            price_data = prices.get(symbol, {})
            price      = price_data.get("price", 0) if isinstance(price_data, dict) else float(price_data or 0)
            if price <= 0:
                continue

            result = await self.portfolio_svc.execute_buy(
                uid, symbol, price, signal, trigger="auto"
            )
            if result.get("status") == "executed":
                counts["buys"] += 1
                logger.info(f"AutoTrader BUY: {uid[:8]}… {symbol} @ ${price:.2f}")
            elif result.get("reason", "").startswith("Insufficient cash"):
                # Try rebalancing to free up cash for this signal
                rebalanced = await self.rebalance_user(uid, symbol, signal, prices)
                if rebalanced:
                    counts["rebalances"] += 1
                    # Retry the buy after rebalancing
                    wallet_fresh = await self.portfolio_svc.get_wallet(uid)
                    retry = await self.portfolio_svc.execute_buy(
                        uid, symbol, price, signal, trigger="auto"
                    )
                    if retry.get("status") == "executed":
                        counts["buys"] += 1
                else:
                    counts["skipped"] += 1
            else:
                counts["skipped"] += 1

        return counts

    # ── Stop-loss monitor ─────────────────────────────────────────────────────

    async def stop_loss_monitor(self) -> dict:
        """
        Called every 60 seconds by price_refresh_job.
        Checks all active users' positions against their stop-loss prices.
        """
        if not await self._is_enabled():
            return {"skipped": True, "reason": "kill_switch"}

        active_uids = await self._get_active_users()
        if not active_uids:
            return {"triggered": 0}

        try:
            prices_raw = await self.price_svc.get_all()
            # Flatten to {symbol: price} for portfolio_service
            prices = {}
            for sym, data in prices_raw.items():
                if isinstance(data, dict):
                    prices[sym] = data.get("price", 0)
                else:
                    prices[sym] = float(data or 0)
        except Exception as e:
            logger.error(f"AutoTraderService: stop-loss price fetch failed: {e}")
            return {"error": str(e)}

        total_triggered = 0
        sem = asyncio.Semaphore(10)

        async def _check(uid: str):
            nonlocal total_triggered
            async with sem:
                try:
                    triggered = await self.portfolio_svc.check_stop_losses(uid, prices)
                    total_triggered += len(triggered)
                    if triggered:
                        syms = [t.get("symbol") for t in triggered]
                        logger.warning(f"AutoTrader stop-loss: {uid[:8]}… triggered for {syms}")
                except Exception as e:
                    logger.error(f"AutoTraderService: stop-loss check failed for {uid}: {e}")

        await asyncio.gather(*[_check(uid) for uid in active_uids])

        if total_triggered:
            logger.info(f"AutoTraderService: stop-loss monitor — {total_triggered} positions closed")

        return {"triggered": total_triggered, "users_checked": len(active_uids)}

    # ── Rebalancing ───────────────────────────────────────────────────────────

    async def rebalance_user(
        self,
        uid:        str,
        new_symbol: str,
        new_signal: dict,
        prices:     dict[str, dict],
    ) -> bool:
        """
        Sell the weakest eligible position to free up cash for a better signal.

        Rules:
          - New signal's expected_return must beat weakest position's by > REBALANCE_RETURN_EDGE (2%)
          - Never sell a position that is up > REBALANCE_WINNER_GUARD (10%)
          - If no eligible position found, returns False (no action taken)

        Returns True if a position was sold, False otherwise.
        """
        positions = await self.portfolio_svc.get_positions(uid)
        if not positions:
            return False

        new_return = float(new_signal.get("expected_return_pct", 0) or 0)

        # Build list of sell candidates: not up >10%, sorted by expected_return asc
        candidates = []
        for pos in positions:
            sym = pos["symbol"]

            # Refresh current price
            price_data = prices.get(sym, {})
            current    = price_data.get("price", 0) if isinstance(price_data, dict) else float(price_data or 0)
            if current <= 0:
                current = pos.get("current_price", pos["buy_price"])

            pnl_pct = ((current - pos["buy_price"]) / pos["buy_price"]) * 100 if pos["buy_price"] else 0

            # Rule: never sell a winner up >10%
            if pnl_pct > REBALANCE_WINNER_GUARD:
                logger.debug(f"Rebalance: skipping {sym} — up {pnl_pct:.1f}% (winner guard)")
                continue

            # Get cached signal for this position to compare expected returns
            cached_signals = self.signal_svc.get_all_cached()
            pos_signal     = cached_signals.get(sym, {})
            pos_return     = float(pos_signal.get("expected_return_pct", 0) or 0)

            candidates.append({
                "symbol":       sym,
                "pnl_pct":      pnl_pct,
                "current":      current,
                "pos_return":   pos_return,
            })

        if not candidates:
            logger.info(f"Rebalance: no eligible candidates for {uid[:8]}… (all winners or no positions)")
            return False

        # Sort by position expected_return ascending — weakest first
        candidates.sort(key=lambda x: x["pos_return"])
        weakest = candidates[0]

        # Only rebalance if new signal's return beats weakest by > 2%
        edge = new_return - weakest["pos_return"]
        if edge <= REBALANCE_RETURN_EDGE:
            logger.info(
                f"Rebalance: skipping — {new_symbol} return ({new_return:.1f}%) "
                f"beats {weakest['symbol']} ({weakest['pos_return']:.1f}%) "
                f"by only {edge:.1f}% (need >{REBALANCE_RETURN_EDGE}%)"
            )
            return False

        logger.info(
            f"Rebalance: selling {weakest['symbol']} (return {weakest['pos_return']:.1f}%, "
            f"P&L {weakest['pnl_pct']:+.1f}%) to fund {new_symbol} (return {new_return:.1f}%)"
        )

        result = await self.portfolio_svc.execute_sell(
            uid,
            weakest["symbol"],
            weakest["current"],
            signal={},
            trigger="rebalance",
            reason=(
                f"Rebalanced: {new_symbol} expected return ({new_return:.1f}%) "
                f"exceeds {weakest['symbol']} ({weakest['pos_return']:.1f}%) by {edge:.1f}%"
            ),
        )
        return result.get("status") == "executed"

    # ── Manual trade (user-triggered from Live Prices page) ───────────────────

    async def execute_manual_trade(
        self,
        uid:        str,
        symbol:     str,
        action:     str,            # "BUY" | "SELL"
        amount_usd: float = None,   # e.g. 150.00 → buy $150 worth (fractional)
        shares:     float = None,   # e.g. 0.25 → buy exactly 0.25 shares
    ) -> dict:
        """
        User-triggered trade from the Live Prices page.
        Bypasses is_active and universe checks; still requires agreement_accepted.

        Amount priority: shares > amount_usd > strategy position sizing
        Fractional shares supported (stored to 6 decimal places).
        """
        symbol = symbol.upper()

        if not await self._is_enabled():
            return {"status": "error", "reason": "Auto-trader is currently disabled by admin"}

        try:
            price_data = await self.price_svc.get_one(symbol)
            price      = price_data.get("price", 0) if price_data else 0
        except Exception as e:
            return {"status": "error", "reason": f"Could not fetch price for {symbol}: {e}"}

        if not price or price <= 0:
            return {"status": "error", "reason": f"No valid price available for {symbol}"}

        # Build custom signal with override position size for manual trades
        signal = {"confidence": "HIGH", "summary": "Manual trade"}

        if action == "BUY":
            # Calculate shares from input
            if shares and shares > 0:
                computed_shares = round(shares, 6)
                cost            = round(computed_shares * price, 2)
            elif amount_usd and amount_usd > 0:
                computed_shares = round(amount_usd / price, 6)
                cost            = round(amount_usd, 2)
            else:
                # Fall back to strategy position sizing (existing behaviour)
                return await self.portfolio_svc.execute_buy(
                    uid, symbol, price, signal=signal, trigger="manual",
                )

            # Validate wallet has enough cash
            wallet = await self.portfolio_svc.get_wallet(uid)
            if not wallet:
                return {"status": "error", "reason": "Wallet not found"}
            if not wallet.get("agreement_accepted"):
                return {"status": "error", "reason": "Paper trading agreement not accepted"}
            if cost > wallet.get("balance", 0):
                return {
                    "status": "error",
                    "reason": f"Insufficient cash — need ${cost:.2f}, have ${wallet['balance']:.2f}",
                }

            # Check not already holding
            existing = await self.portfolio_svc.get_position(uid, symbol)
            if existing:
                return {"status": "skipped", "reason": f"Already holding {symbol}"}

            # Execute with custom shares directly (bypass strategy sizing)
            return await self.portfolio_svc._execute_buy_shares(
                uid, symbol, price, computed_shares, signal, trigger="manual",
            )

        elif action == "SELL":
            return await self.portfolio_svc.execute_sell(
                uid, symbol, price,
                signal={"summary": "Manual sell"},
                trigger="manual",
                reason="Manual sell from Live Prices",
            )
        else:
            return {"status": "error", "reason": f"Unknown action '{action}'"}
        
    # ── Admin kill switch control ─────────────────────────────────────────────

    async def set_kill_switch(self, enabled: bool) -> dict:
        """
        Set config/autotrader.enabled in Firestore.
        Called by admin endpoint to halt/resume all autonomous trading.
        """
        if not self._db:
            return {"error": "Firestore not available"}

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._db.collection("config").document("autotrader").set(
                    {"enabled": enabled, "updated_at": datetime.now(timezone.utc).isoformat()},
                    merge=True,
                ),
            )
            state = "enabled" if enabled else "disabled"
            logger.info(f"AutoTraderService: kill switch set to {state}")
            return {"enabled": enabled, "status": state}
        except Exception as e:
            logger.error(f"AutoTraderService: kill switch update failed: {e}")
            return {"error": str(e)}

    async def get_status(self) -> dict:
        """Summary status for admin dashboard and /health endpoint."""
        enabled     = await self._is_enabled()
        active_uids = await self._get_active_users()
        return {
            "enabled":      enabled,
            "active_users": len(active_uids),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }