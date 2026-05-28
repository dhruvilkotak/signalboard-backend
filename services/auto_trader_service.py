"""
services/auto_trader_service.py  — v3

Works with the new multi-strategy PortfolioService.
Loops over (uid, strategy_key) pairs instead of single wallet per user.

Responsibilities:
  1. run_for_all_users(signals)  — called by market-hours signal jobs
  2. stop_loss_monitor()         — called every 60s by price_refresh_job
  3. rebalance_strategy()        — sell weakest position to fund better signal
  4. execute_manual_trade()      — user-triggered from Live Prices / Portfolio tab

Kill switch: config/autotrader.enabled in Firestore
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REBALANCE_RETURN_EDGE  = 2.0   # new signal must beat weakest by this % points
REBALANCE_WINNER_GUARD = 10.0  # never sell a position up more than this %


class AutoTraderService:
    def __init__(self, portfolio_service, price_service, signal_service):
        self.portfolio_svc = portfolio_service
        self.price_svc     = price_service
        self.signal_svc    = signal_service
        self._db           = None

    def set_db(self, db) -> None:
        self._db = db
        logger.info("AutoTraderService v3: Firestore connected ✓")

    # ── Kill switch ───────────────────────────────────────────────────────────

    async def _is_enabled(self) -> bool:
        if not self._db:
            return True
        try:
            loop = asyncio.get_running_loop()
            doc  = await loop.run_in_executor(
                None,
                lambda: self._db.collection("config").document("autotrader").get(),
            )
            return doc.to_dict().get("enabled", True) if doc.exists else True
        except Exception as e:
            logger.warning(f"AutoTraderService: kill-switch check failed ({e}) — defaulting enabled")
            return True

    # ── Main entry ────────────────────────────────────────────────────────────

    async def run_for_all_users(self, signals: dict[str, dict]) -> dict:
        """
        Called by market_hours_signal_job after signals fire.
        Loops over all active (uid, strategy_key) pairs and executes qualifying trades.
        """
        if not await self._is_enabled():
            logger.info("AutoTraderService: kill switch active — skipping")
            return {"skipped": True, "reason": "kill_switch"}

        pairs = await self.portfolio_svc.get_active_strategy_users()
        if not pairs:
            return {"active_pairs": 0}

        # Fetch prices once
        try:
            raw    = await self.price_svc.get_all()
            prices = {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                      for s, d in raw.items()}
        except Exception as e:
            logger.error(f"AutoTraderService: price fetch failed: {e}")
            return {"error": str(e)}

        summary = {"active_pairs": len(pairs), "buys": 0, "sells": 0,
                   "skipped": 0, "rebalances": 0, "errors": 0}

        sem = asyncio.Semaphore(10)

        async def _process(uid: str, sk: str):
            async with sem:
                try:
                    r = await self._process_strategy(uid, sk, signals, prices)
                    summary["buys"]       += r.get("buys", 0)
                    summary["sells"]      += r.get("sells", 0)
                    summary["skipped"]    += r.get("skipped", 0)
                    summary["rebalances"] += r.get("rebalances", 0)
                except Exception as e:
                    logger.error(f"AutoTraderService: error {uid}/{sk}: {e}")
                    summary["errors"] += 1

        await asyncio.gather(*[_process(uid, sk) for uid, sk in pairs])

        logger.info(
            f"AutoTraderService: run complete — "
            f"{summary['buys']} buys / {summary['sells']} sells / "
            f"{summary['rebalances']} rebalances across {len(pairs)} strategy accounts"
        )
        return summary

    async def _process_strategy(
        self, uid: str, sk: str,
        signals: dict[str, dict],
        prices: dict[str, float],
    ) -> dict:
        from services.portfolio_service import STRATEGIES
        cfg      = STRATEGIES.get(sk, STRATEGIES["balanced"])
        universe = set(cfg["universe"])
        counts   = {"buys": 0, "sells": 0, "skipped": 0, "rebalances": 0}

        # SELLs first — free cash before buying
        for symbol, signal in signals.items():
            if symbol not in universe or signal.get("signal") != "SELL":
                continue
            price = prices.get(symbol, 0)
            if price <= 0:
                continue
            result = await self.portfolio_svc.execute_sell(
                uid, symbol, sk, price, signal, trigger="auto",
                reason=signal.get("summary", ""),
            )
            if result.get("status") == "executed":
                counts["sells"] += 1
            else:
                counts["skipped"] += 1

        # BUYs
        for symbol, signal in signals.items():
            if symbol not in universe or signal.get("signal") != "BUY":
                continue
            if not signal.get("feed_eligible"):
                counts["skipped"] += 1
                continue
            price = prices.get(symbol, 0)
            if price <= 0:
                continue

            result = await self.portfolio_svc.execute_buy(
                uid, symbol, sk, price, signal, trigger="auto",
            )
            if result.get("status") == "executed":
                counts["buys"] += 1
            elif "Insufficient cash" in result.get("reason", ""):
                rebalanced = await self.rebalance_strategy(uid, sk, symbol, signal, prices)
                if rebalanced:
                    counts["rebalances"] += 1
                    retry = await self.portfolio_svc.execute_buy(
                        uid, symbol, sk, price, signal, trigger="auto",
                    )
                    if retry.get("status") == "executed":
                        counts["buys"] += 1
                    else:
                        counts["skipped"] += 1
                else:
                    counts["skipped"] += 1
            else:
                counts["skipped"] += 1

        return counts

    # ── Stop-loss monitor ─────────────────────────────────────────────────────

    async def stop_loss_monitor(self) -> dict:
        if not await self._is_enabled():
            return {"skipped": True, "reason": "kill_switch"}

        pairs = await self.portfolio_svc.get_active_strategy_users()
        if not pairs:
            return {"triggered": 0}

        # Get unique UIDs to check
        uids = list(set(uid for uid, _ in pairs))

        try:
            raw    = await self.price_svc.get_all()
            prices = {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0))
                      for s, d in raw.items()}
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
                        logger.warning(f"AutoTrader stop-loss: {uid[:8]}… {syms}")
                except Exception as e:
                    logger.error(f"AutoTraderService: stop-loss check failed for {uid}: {e}")

        await asyncio.gather(*[_check(uid) for uid in uids])

        if total_triggered:
            logger.info(f"AutoTraderService: stop-loss monitor — {total_triggered} positions closed")
        return {"triggered": total_triggered, "users_checked": len(uids)}

    # ── Rebalancing ───────────────────────────────────────────────────────────

    async def rebalance_strategy(
        self,
        uid:        str,
        sk:         str,
        new_symbol: str,
        new_signal: dict,
        prices:     dict[str, float],
    ) -> bool:
        """
        Sell the weakest position in a strategy to fund a better signal.
        Rules:
          - New signal expected_return must beat weakest by > REBALANCE_RETURN_EDGE (2%)
          - Never sell a position up > REBALANCE_WINNER_GUARD (10%)
        """
        positions  = await self.portfolio_svc.get_positions(uid, sk)
        if not positions:
            return False

        new_return = float(new_signal.get("expected_return_pct", 0) or 0)
        candidates = []

        for pos in positions:
            symbol  = pos["symbol"]
            price   = prices.get(symbol, pos.get("current_price", pos["buy_price"]))
            pnl_pct = ((price - pos["buy_price"]) / pos["buy_price"]) * 100 if pos["buy_price"] else 0

            if pnl_pct > REBALANCE_WINNER_GUARD:
                continue

            cached  = self.signal_svc.get_all_cached()
            pos_ret = float((cached.get(symbol) or {}).get("expected_return_pct", 0) or 0)
            candidates.append({"symbol": symbol, "pnl_pct": pnl_pct, "price": price, "pos_return": pos_ret})

        if not candidates:
            return False

        candidates.sort(key=lambda x: x["pos_return"])
        weakest = candidates[0]
        edge    = new_return - weakest["pos_return"]

        if edge <= REBALANCE_RETURN_EDGE:
            logger.info(f"Rebalance: skipping — edge {edge:.1f}% ≤ {REBALANCE_RETURN_EDGE}%")
            return False

        logger.info(
            f"Rebalance: selling {weakest['symbol']}/{sk} "
            f"(return {weakest['pos_return']:.1f}%) to fund {new_symbol} (return {new_return:.1f}%)"
        )
        result = await self.portfolio_svc.execute_sell(
            uid, weakest["symbol"], sk, weakest["price"],
            signal={}, trigger="rebalance",
            reason=(
                f"Rebalanced: {new_symbol} expected return ({new_return:.1f}%) "
                f"exceeds {weakest['symbol']} ({weakest['pos_return']:.1f}%) by {edge:.1f}%"
            ),
        )
        return result.get("status") == "executed"

    # ── Manual trade ──────────────────────────────────────────────────────────

    async def execute_manual_trade(
        self,
        uid:          str,
        symbol:       str,
        action:       str,         # "BUY" | "SELL"
        strategy_key: str,
        amount_usd:   float = None,
        shares:       float = None,
    ) -> dict:
        """
        User-triggered trade from Live Prices or Portfolio tab.
        Bypasses is_active/universe checks. Requires agreement + allocated strategy.
        """
        symbol = symbol.upper()

        if not await self._is_enabled():
            return {"status": "error", "reason": "Auto-trader is currently disabled by admin"}

        # Validate strategy is allocated
        strat = await self.portfolio_svc.get_strategy(uid, strategy_key)
        if not strat:
            return {"status": "error", "reason": f"Strategy '{strategy_key}' not allocated. Allocate funds to it first."}

        # Get live price
        try:
            price_data = await self.price_svc.get_one(symbol)
            price      = price_data.get("price", 0) if price_data else 0
        except Exception as e:
            return {"status": "error", "reason": f"Could not fetch price for {symbol}: {e}"}

        if not price or price <= 0:
            return {"status": "error", "reason": f"No valid price for {symbol}"}

        if action == "BUY":
            return await self.portfolio_svc.execute_buy(
                uid, symbol, strategy_key, price,
                signal={"confidence": "HIGH", "summary": f"Manual buy via {strategy_key}"},
                trigger="manual",
                shares_override=shares,
                amount_usd_override=amount_usd,
            )
        elif action == "SELL":
            return await self.portfolio_svc.execute_sell(
                uid, symbol, strategy_key, price,
                signal={"summary": "Manual sell"},
                trigger="manual",
                reason="Manual sell from Live Prices",
            )
        else:
            return {"status": "error", "reason": f"Unknown action '{action}'"}

    # ── Kill switch control ───────────────────────────────────────────────────

    async def set_kill_switch(self, enabled: bool) -> dict:
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
            logger.info(f"AutoTraderService: kill switch → {state}")
            return {"enabled": enabled, "status": state}
        except Exception as e:
            return {"error": str(e)}

    async def get_status(self) -> dict:
        enabled = await self._is_enabled()
        pairs   = await self.portfolio_svc.get_active_strategy_users()
        uids    = list(set(uid for uid, _ in pairs))
        return {
            "enabled":           enabled,
            "active_users":      len(uids),
            "active_strategies": len(pairs),
            "pairs":             [{"uid": uid[:8] + "…", "strategy": sk} for uid, sk in pairs[:10]],
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }