"""
services/auto_trader_service.py — v4
Only manages STRATEGY trades. Manual trades are completely separate.
"""
import asyncio, logging
from datetime import datetime, timezone
logger = logging.getLogger(__name__)
REBALANCE_RETURN_EDGE  = 2.0
REBALANCE_WINNER_GUARD = 10.0

class AutoTraderService:
    def __init__(self, portfolio_service, price_service, signal_service):
        self.portfolio_svc = portfolio_service
        self.price_svc     = price_service
        self.signal_svc    = signal_service
        self._db           = None

    def set_db(self, db):
        self._db = db
        logger.info("AutoTraderService v4: Firestore connected ✓")

    async def _is_enabled(self) -> bool:
        if not self._db: return True
        try:
            loop = asyncio.get_running_loop()
            doc  = await loop.run_in_executor(None, lambda: self._db.collection("config").document("autotrader").get())
            return doc.to_dict().get("enabled", True) if doc.exists else True
        except Exception as e:
            logger.warning(f"Kill-switch check failed ({e}) — defaulting enabled")
            return True

    async def run_for_all_users(self, signals: dict) -> dict:
        """Called by market_hours_signal_job. Manages strategy positions only."""
        if not await self._is_enabled():
            return {"skipped": True, "reason": "kill_switch"}

        pairs = await self.portfolio_svc.get_active_strategy_users()
        if not pairs:
            return {"active_pairs": 0}

        try:
            raw    = await self.price_svc.get_all()
            prices = {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0)) for s, d in raw.items()}
        except Exception as e:
            logger.error(f"Price fetch failed: {e}")
            return {"error": str(e)}

        summary = {"active_pairs": len(pairs), "buys": 0, "sells": 0, "skipped": 0, "rebalances": 0, "errors": 0}
        sem     = asyncio.Semaphore(10)

        async def _process(uid, sk):
            async with sem:
                try:
                    r = await self._process_strategy(uid, sk, signals, prices)
                    for k in ["buys", "sells", "skipped", "rebalances"]:
                        summary[k] += r.get(k, 0)
                except Exception as e:
                    logger.error(f"Error {uid}/{sk}: {e}")
                    summary["errors"] += 1

        await asyncio.gather(*[_process(uid, sk) for uid, sk in pairs])
        logger.info(f"AutoTrader: {summary['buys']} buys / {summary['sells']} sells across {len(pairs)} strategy accounts")
        return summary

    async def _process_strategy(self, uid, sk, signals, prices):
        from services.portfolio_service import STRATEGIES
        cfg      = STRATEGIES.get(sk, STRATEGIES["balanced"])
        universe = set(cfg["universe"])
        counts   = {"buys": 0, "sells": 0, "skipped": 0, "rebalances": 0}

        for symbol, signal in signals.items():
            if symbol not in universe or signal.get("signal") != "SELL": continue
            price = prices.get(symbol, 0)
            if price <= 0: continue
            r = await self.portfolio_svc.strategy_sell(uid, symbol, sk, price, signal, trigger="auto", reason=signal.get("summary",""))
            counts["sells" if r.get("status") == "executed" else "skipped"] += 1

        for symbol, signal in signals.items():
            if symbol not in universe or signal.get("signal") != "BUY": continue
            if not signal.get("feed_eligible"): counts["skipped"] += 1; continue
            price = prices.get(symbol, 0)
            if price <= 0: continue
            r = await self.portfolio_svc.strategy_buy(uid, symbol, sk, price, signal, trigger="auto")
            if r.get("status") == "executed":
                counts["buys"] += 1
            elif "Insufficient" in r.get("reason", ""):
                if await self.rebalance_strategy(uid, sk, symbol, signal, prices):
                    counts["rebalances"] += 1
                    retry = await self.portfolio_svc.strategy_buy(uid, symbol, sk, price, signal, trigger="auto")
                    counts["buys" if retry.get("status") == "executed" else "skipped"] += 1
                else:
                    counts["skipped"] += 1
            else:
                counts["skipped"] += 1
        return counts

    async def stop_loss_monitor(self) -> dict:
        if not await self._is_enabled(): return {"skipped": True}
        pairs = await self.portfolio_svc.get_active_strategy_users()
        if not pairs: return {"triggered": 0}
        uids = list(set(uid for uid, _ in pairs))
        try:
            raw    = await self.price_svc.get_all()
            prices = {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0)) for s, d in raw.items()}
        except Exception as e:
            return {"error": str(e)}
        total = 0
        sem   = asyncio.Semaphore(10)
        async def _check(uid):
            nonlocal total
            async with sem:
                try:
                    t = await self.portfolio_svc.check_stop_losses(uid, prices)
                    total += len(t)
                except Exception as e:
                    logger.error(f"Stop-loss check failed for {uid}: {e}")
        await asyncio.gather(*[_check(uid) for uid in uids])
        return {"triggered": total, "users_checked": len(uids)}

    async def rebalance_strategy(self, uid, sk, new_symbol, new_signal, prices) -> bool:
        positions  = await self.portfolio_svc.get_strategy_positions(uid, sk)
        if not positions: return False
        new_return = float(new_signal.get("expected_return_pct", 0) or 0)
        candidates = []
        for pos in positions:
            symbol  = pos["symbol"]
            price   = prices.get(symbol, pos.get("current_price", pos["buy_price"]))
            pnl_pct = ((price - pos["buy_price"]) / pos["buy_price"]) * 100 if pos["buy_price"] else 0
            if pnl_pct > REBALANCE_WINNER_GUARD: continue
            cached  = self.signal_svc.get_all_cached()
            pos_ret = float((cached.get(symbol) or {}).get("expected_return_pct", 0) or 0)
            candidates.append({"symbol": symbol, "pnl_pct": pnl_pct, "price": price, "pos_return": pos_ret})
        if not candidates: return False
        candidates.sort(key=lambda x: x["pos_return"])
        weakest = candidates[0]
        edge    = new_return - weakest["pos_return"]
        if edge <= REBALANCE_RETURN_EDGE: return False
        result = await self.portfolio_svc.strategy_sell(
            uid, weakest["symbol"], sk, weakest["price"], signal={}, trigger="rebalance",
            reason=f"Rebalanced: {new_symbol} return ({new_return:.1f}%) beats {weakest['symbol']} ({weakest['pos_return']:.1f}%) by {edge:.1f}%",
        )
        return result.get("status") == "executed"

    async def execute_manual_trade(self, uid, symbol, action, amount_usd=None, shares=None) -> dict:
        """
        User-triggered trade from Live Prices. Uses available_cash directly.
        No strategy needed. Auto-trader never touches these positions.
        """
        if not await self._is_enabled():
            return {"status": "error", "reason": "Auto-trader is currently disabled by admin"}
        try:
            price_data = await self.price_svc.get_one(symbol)
            price      = price_data.get("price", 0) if price_data else 0
        except Exception as e:
            return {"status": "error", "reason": f"Could not fetch price for {symbol}: {e}"}
        if not price or price <= 0:
            return {"status": "error", "reason": f"No valid price for {symbol}"}

        if action == "BUY":
            return await self.portfolio_svc.manual_buy(
                uid, symbol, price, shares=shares, amount_usd=amount_usd,
            )
        elif action == "SELL":
            return await self.portfolio_svc.manual_sell(uid, symbol, price, shares=shares)
        else:
            return {"status": "error", "reason": f"Unknown action '{action}'"}

    async def set_kill_switch(self, enabled: bool) -> dict:
        if not self._db: return {"error": "Firestore not available"}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._db.collection("config").document("autotrader").set(
                {"enabled": enabled, "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True))
            logger.info(f"AutoTraderService: kill switch → {'enabled' if enabled else 'disabled'}")
            return {"enabled": enabled, "status": "enabled" if enabled else "disabled"}
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
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }