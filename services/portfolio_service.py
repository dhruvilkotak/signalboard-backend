"""
services/portfolio_service.py — v4

Two completely separate systems:

1. MANUAL TRADES — user's personal portfolio
   - Uses available_cash directly
   - No strategy needed
   - Auto-trader never touches these
   - Firestore: users/{uid}/manual_positions/{symbol}
                users/{uid}/manual_trades/{auto_id}

2. STRATEGIES — auto-trader managed funds
   - User allocates $ to a strategy (like a fund)
   - Auto-trader manages buys/sells/stop-losses inside
   - User never manually touches positions inside
   - Firestore: users/{uid}/strategies/{key}
                users/{uid}/strategy_positions/{symbol}_{key}
                users/{uid}/strategy_trades/{auto_id}

Shared:
   users/{uid}/portfolio/summary — available_cash, total_value
   users/{uid}/transactions/{auto_id} — all wallet events
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STARTING_CASH   = 10_000.00
CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

STRATEGIES: dict[str, dict] = {
    "aggressive": {
        "label":             "Aggressive Growth",
        "description":       "100% equities, HIGH confidence signals only. Max 25% per position. High risk, high reward.",
        "risk_level":        "HIGH",
        "position_pct":      0.25,
        "min_confidence":    "HIGH",
        "stop_loss_default": 8.0,
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
        "description":       "70% dividend ETFs, 30% defensive equities. Max 20% per position. Steady income.",
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
        "description":       "50% defensive ETFs, 30% blue-chip, 20% cash reserve. HIGH confidence BUY only. Max 20% per position. Capital preservation first.",
        "risk_level":        "LOW",
        "position_pct":      0.20,
        "min_confidence":    "HIGH",
        "stop_loss_default": 3.0,
        "stop_loss_min":     1.0,
        "stop_loss_max":     5.0,
        "universe":          ["SGOV", "SCHD", "VOO", "AAPL", "MSFT"],
        "cash_reserve_pct":  0.20,
    },
}


class PortfolioService:

    def __init__(self):
        self._db = None

    def set_db(self, db) -> None:
        self._db = db
        logger.info("PortfolioService v4: Firestore connected ✓")

    # ── Refs ──────────────────────────────────────────────────────────────────

    def _user(self, uid):
        return self._db.collection("users").document(uid)

    def _summary_ref(self, uid):
        return self._user(uid).collection("portfolio").document("summary")

    # Manual trade refs
    def _manual_pos_ref(self, uid, symbol):
        return self._user(uid).collection("manual_positions").document(symbol.upper())

    def _manual_trades_ref(self, uid):
        return self._user(uid).collection("manual_trades")

    # Strategy refs
    def _strategy_ref(self, uid, sk):
        return self._user(uid).collection("strategies").document(sk)

    def _strat_pos_ref(self, uid, symbol, sk):
        return self._user(uid).collection("strategy_positions").document(f"{symbol.upper()}_{sk}")

    def _strat_trades_ref(self, uid):
        return self._user(uid).collection("strategy_trades")

    # Shared transaction log
    def _tx_ref(self, uid):
        return self._user(uid).collection("transactions")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _run(self, fn):
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _r2(self, n) -> float:
        return round(float(n or 0), 2)

    def _ser(self, data: dict) -> dict:
        out = {}
        for k, v in (data or {}).items():
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            elif hasattr(v, "seconds"):
                out[k] = datetime.fromtimestamp(v.seconds, tz=timezone.utc).isoformat()
            else:
                out[k] = v
        return out

    async def _log_tx(self, uid: str, data: dict):
        entry = {**data, "timestamp": self._now()}
        await self._run(lambda: self._tx_ref(uid).add(entry))

    # ── Portfolio summary ─────────────────────────────────────────────────────

    async def get_or_create_summary(self, uid: str) -> dict:
        ref = self._summary_ref(uid)
        doc = await self._run(ref.get)
        if doc.exists:
            return self._ser(doc.to_dict() or {})
        now = self._now()
        summary = {
            "available_cash":     STARTING_CASH,
            "total_value":        STARTING_CASH,
            "total_deposited":    STARTING_CASH,
            "agreement_accepted": False,
            "created_at":         now,
            "updated_at":         now,
        }
        await self._run(lambda: ref.set(summary))
        await self._log_tx(uid, {
            "type": "initial", "amount": STARTING_CASH,
            "available_cash_before": 0.0, "available_cash_after": STARTING_CASH,
            "notes": "Starting virtual portfolio — $10,000 available cash",
        })
        logger.info(f"PortfolioService: created portfolio for {uid}")
        return summary

    async def accept_agreement(self, uid: str) -> dict:
        await self.get_or_create_summary(uid)
        now = self._now()
        await self._run(lambda: self._summary_ref(uid).update({
            "agreement_accepted":    True,
            "agreement_accepted_at": now,
            "updated_at":            now,
        }))
        return {"agreement_accepted": True}

    async def _sync_total_value(self, uid: str):
        """Recalculate total_value = available_cash + manual positions value + strategy values."""
        summary      = await self.get_or_create_summary(uid)
        manual_pos   = await self.get_manual_positions(uid)
        strats       = await self.get_all_strategies(uid)
        manual_val   = self._r2(sum(p.get("current_value", 0) for p in manual_pos))
        strategy_val = self._r2(sum(s.get("total_value", 0) for s in strats.values()))
        total        = self._r2(summary["available_cash"] + manual_val + strategy_val)
        await self._run(lambda: self._summary_ref(uid).update({
            "total_value": total, "updated_at": self._now(),
        }))
        return total

    # ── Agreement ─────────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════════
    # PART 1 — MANUAL TRADES
    # User's personal portfolio. Uses available_cash. Auto-trader ignores these.
    # ══════════════════════════════════════════════════════════════════════════

    async def get_manual_positions(self, uid: str, prices: dict = None) -> list[dict]:
        def _f():
            return [self._ser(d.to_dict() or {})
                    for d in self._user(uid).collection("manual_positions").stream()]
        positions = await self._run(_f)
        if prices:
            for pos in positions:
                p = prices.get(pos["symbol"])
                if p and p > 0:
                    cv   = self._r2(pos["shares"] * p)
                    abp  = pos.get("avg_buy_price", pos.get("buy_price", 0))
                    pnl  = self._r2(cv - pos["shares"] * abp)
                    ppct = self._r2((pnl / (pos["shares"] * abp)) * 100) if abp else 0
                    pos.update({"current_price": p, "current_value": cv,
                                "unrealized_pnl": pnl, "unrealized_pnl_pct": ppct})
        return positions

    async def get_manual_position(self, uid: str, symbol: str) -> Optional[dict]:
        doc = await self._run(lambda: self._manual_pos_ref(uid, symbol).get())
        return self._ser(doc.to_dict() or {}) if doc.exists else None

    async def manual_buy(
        self,
        uid:        str,
        symbol:     str,
        price:      float,
        shares:     float = None,
        amount_usd: float = None,
    ) -> dict:
        """
        Buy stock from available_cash. No strategy needed.
        shares or amount_usd must be provided.
        Supports adding to existing position (average down).
        """
        symbol  = symbol.upper()
        summary = await self.get_or_create_summary(uid)

        if not summary.get("agreement_accepted"):
            return {"status": "error", "reason": "Paper trading agreement not accepted"}
        if price <= 0:
            return {"status": "error", "reason": "Invalid price"}

        # Calculate shares and cost
        if shares and shares > 0:
            qty  = round(shares, 6)
            cost = self._r2(qty * price)
        elif amount_usd and amount_usd > 0:
            qty  = round(amount_usd / price, 6)
            cost = self._r2(amount_usd)
        else:
            return {"status": "error", "reason": "Provide shares or amount_usd"}

        if cost > summary["available_cash"]:
            return {
                "status": "error",
                "reason": f"Insufficient cash — need ${cost:.2f}, have ${summary['available_cash']:.2f}",
            }
        if qty <= 0:
            return {"status": "error", "reason": "Invalid quantity"}

        now      = self._now()
        existing = await self.get_manual_position(uid, symbol)

        if existing:
            # Average down — combine with existing position
            total_shares    = self._r2(existing["shares"] + qty)
            total_cost      = self._r2(existing["shares"] * existing.get("avg_buy_price", existing.get("buy_price", 0)) + cost)
            new_avg         = self._r2(total_cost / total_shares)
            await self._run(lambda: self._manual_pos_ref(uid, symbol).update({
                "shares":        total_shares,
                "avg_buy_price": new_avg,
                "current_price": price,
                "current_value": self._r2(total_shares * price),
                "unrealized_pnl": self._r2(total_shares * price - total_cost),
                "updated_at":    now,
            }))
        else:
            position = {
                "symbol":          symbol,
                "shares":          qty,
                "avg_buy_price":   price,
                "buy_date":        now,
                "current_price":   price,
                "current_value":   cost,
                "unrealized_pnl":  0.0,
                "unrealized_pnl_pct": 0.0,
                "updated_at":      now,
            }
            await self._run(lambda: self._manual_pos_ref(uid, symbol).set(position))

        # Deduct from available cash
        new_cash = self._r2(summary["available_cash"] - cost)
        await self._run(lambda: self._summary_ref(uid).update({
            "available_cash": new_cash, "updated_at": now,
        }))

        # Log trade
        trade = {
            "symbol":    symbol, "action": "BUY",
            "shares":    qty,    "price":  price,
            "total":     cost,   "pnl":    0.0,
            "trigger":   "manual",
            "balance_after": new_cash,
            "timestamp": now,
        }
        await self._run(lambda: self._manual_trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: MANUAL BUY {symbol} {qty:.4f}sh @ ${price:.2f} for {uid}")
        return {"status": "executed", "action": "BUY", **trade}

    async def manual_sell(
        self,
        uid:    str,
        symbol: str,
        price:  float,
        shares: float = None,   # None = sell all
    ) -> dict:
        """
        Sell manual position. Returns proceeds to available_cash.
        Partial sell supported — leave shares=None to sell entire position.
        """
        symbol   = symbol.upper()
        position = await self.get_manual_position(uid, symbol)

        if not position:
            return {"status": "error", "reason": f"No manual position for {symbol}"}
        if price <= 0:
            return {"status": "error", "reason": "Invalid price"}

        held = position["shares"]
        qty  = round(shares, 6) if shares and shares > 0 else held

        if qty > held:
            return {"status": "error", "reason": f"Cannot sell {qty:.4f} shares — only holding {held:.4f}"}

        avg_bp    = position.get("avg_buy_price", position.get("buy_price", 0))
        proceeds  = self._r2(qty * price)
        cost_basis = self._r2(qty * avg_bp)
        pnl       = self._r2(proceeds - cost_basis)
        pnl_pct   = self._r2((pnl / cost_basis) * 100) if cost_basis else 0
        now       = self._now()
        remaining = self._r2(held - qty)

        if remaining <= 0.000001:
            # Full sell — delete position
            await self._run(lambda: self._manual_pos_ref(uid, symbol).delete())
        else:
            # Partial sell — update position
            await self._run(lambda: self._manual_pos_ref(uid, symbol).update({
                "shares":        remaining,
                "current_price": price,
                "current_value": self._r2(remaining * price),
                "unrealized_pnl": self._r2(remaining * price - remaining * avg_bp),
                "unrealized_pnl_pct": self._r2(((price - avg_bp) / avg_bp) * 100) if avg_bp else 0,
                "updated_at":    now,
            }))

        # Return proceeds to available cash
        summary  = await self.get_or_create_summary(uid)
        new_cash = self._r2(summary["available_cash"] + proceeds)
        await self._run(lambda: self._summary_ref(uid).update({
            "available_cash": new_cash, "updated_at": now,
        }))

        trade = {
            "symbol":   symbol,   "action": "SELL",
            "shares":   qty,      "price":  price,
            "total":    proceeds, "pnl":    pnl,
            "pnl_pct":  pnl_pct,  "trigger": "manual",
            "balance_after": new_cash,
            "timestamp": now,
        }
        await self._run(lambda: self._manual_trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: MANUAL SELL {symbol} {qty:.4f}sh @ ${price:.2f} P&L ${pnl:+.2f} for {uid}")
        return {"status": "executed", "action": "SELL", **trade}

    async def get_manual_trades(self, uid: str, limit: int = 50) -> list[dict]:
        from firebase_admin import firestore as fs
        def _f():
            return [self._ser(d.to_dict() or {}) for d in
                    self._manual_trades_ref(uid)
                    .order_by("timestamp", direction=fs.Query.DESCENDING)
                    .limit(limit).stream()]
        return await self._run(_f)

    # ══════════════════════════════════════════════════════════════════════════
    # PART 2 — STRATEGY FUNDS (auto-trader managed)
    # User allocates $ to a strategy. Auto-trader manages everything inside.
    # ══════════════════════════════════════════════════════════════════════════

    async def get_strategy(self, uid: str, sk: str) -> Optional[dict]:
        doc = await self._run(self._strategy_ref(uid, sk).get)
        return self._ser(doc.to_dict() or {}) if doc.exists else None

    async def get_all_strategies(self, uid: str) -> dict:
        def _f():
            return {d.id: self._ser(d.to_dict() or {})
                    for d in self._user(uid).collection("strategies").stream()}
        return await self._run(_f)

    async def allocate(self, uid: str, sk: str, amount: float,
                       stop_loss_pct: float = None) -> dict:
        """Move $ from available_cash → strategy fund."""
        if sk not in STRATEGIES:  raise ValueError(f"Unknown strategy: {sk}")
        if amount <= 0:            raise ValueError("Amount must be positive")

        cfg     = STRATEGIES[sk]
        summary = await self.get_or_create_summary(uid)

        if not summary.get("agreement_accepted"):
            raise ValueError("Paper trading agreement must be accepted first")
        if amount > summary["available_cash"]:
            raise ValueError(f"Insufficient cash — need ${amount:.2f}, have ${summary['available_cash']:.2f}")

        sl  = stop_loss_pct if stop_loss_pct is not None else cfg["stop_loss_default"]
        if not (cfg["stop_loss_min"] <= sl <= cfg["stop_loss_max"]):
            raise ValueError(f"Stop-loss {sl}% out of range ({cfg['stop_loss_min']}–{cfg['stop_loss_max']}%)")

        existing = await self.get_strategy(uid, sk)
        now      = self._now()
        old_cash = summary["available_cash"]
        new_cash = self._r2(old_cash - amount)

        if existing:
            await self._run(lambda: self._strategy_ref(uid, sk).update({
                "allocated":        self._r2(existing["allocated"] + amount),
                "cash_in_strategy": self._r2(existing["cash_in_strategy"] + amount),
                "total_value":      self._r2(existing.get("total_value", 0) + amount),
                "is_active": True, "is_paused": False, "updated_at": now,
            }))
            notes = f"Added ${amount:.2f} to {cfg['label']}"
            tx_type = "add_more"
        else:
            await self._run(lambda: self._strategy_ref(uid, sk).set({
                "allocated":        amount,
                "cash_in_strategy": amount,
                "invested":         0.0,
                "total_value":      amount,
                "is_active":        True,
                "is_paused":        False,
                "stop_loss_pct":    sl,
                "realized_pnl":     0.0,
                "created_at":       now,
                "updated_at":       now,
            }))
            notes   = f"Allocated ${amount:.2f} to {cfg['label']}"
            tx_type = "allocate"

        await self._run(lambda: self._summary_ref(uid).update({
            "available_cash": new_cash, "updated_at": now,
        }))
        await self._log_tx(uid, {
            "type": tx_type, "strategy_key": sk, "amount": amount,
            "available_cash_before": old_cash, "available_cash_after": new_cash,
            "notes": notes,
        })
        await self._sync_total_value(uid)
        logger.info(f"PortfolioService: {tx_type} ${amount:.2f} → {sk} for {uid}")
        return {"strategy_key": sk, "amount": amount, "available_cash": new_cash}

    async def reduce(self, uid: str, sk: str, amount: float) -> dict:
        """Return idle cash from strategy → available_cash."""
        strat = await self.get_strategy(uid, sk)
        if not strat: raise ValueError(f"Strategy {sk} not allocated")
        if amount <= 0: raise ValueError("Amount must be positive")

        idle = strat["cash_in_strategy"]
        if amount > idle:
            raise ValueError(f"Only ${idle:.2f} idle cash available (${strat.get('invested',0):.2f} is invested)")

        summary  = await self.get_or_create_summary(uid)
        now      = self._now()
        old_cash = summary["available_cash"]
        new_cash = self._r2(old_cash + amount)

        await self._run(lambda: self._strategy_ref(uid, sk).update({
            "allocated":        self._r2(strat["allocated"] - amount),
            "cash_in_strategy": self._r2(idle - amount),
            "total_value":      self._r2(strat["total_value"] - amount),
            "updated_at":       now,
        }))
        await self._run(lambda: self._summary_ref(uid).update({
            "available_cash": new_cash, "updated_at": now,
        }))
        await self._log_tx(uid, {
            "type": "reduce", "strategy_key": sk, "amount": amount,
            "available_cash_before": old_cash, "available_cash_after": new_cash,
            "notes": f"Withdrew ${amount:.2f} idle cash from {STRATEGIES[sk]['label']}",
        })
        await self._sync_total_value(uid)
        return {"strategy_key": sk, "returned": amount, "available_cash": new_cash}

    async def pause_strategy(self, uid: str, sk: str, paused: bool) -> dict:
        strat = await self.get_strategy(uid, sk)
        if not strat: raise ValueError(f"Strategy {sk} not allocated")
        now = self._now()
        await self._run(lambda: self._strategy_ref(uid, sk).update({
            "is_paused": paused, "is_active": not paused, "updated_at": now,
        }))
        action = "paused" if paused else "resumed"
        await self._log_tx(uid, {
            "type": f"strategy_{action}", "strategy_key": sk, "amount": 0.0,
            "available_cash_before": 0.0, "available_cash_after": 0.0,
            "notes": f"{STRATEGIES[sk]['label']} {action}",
        })
        return {"strategy_key": sk, "is_paused": paused}

    async def stop_strategy(self, uid: str, sk: str, prices: dict) -> dict:
        """Close all strategy positions, return all cash to available_cash."""
        strat = await self.get_strategy(uid, sk)
        if not strat: raise ValueError(f"Strategy {sk} not allocated")

        positions = await self.get_strategy_positions(uid, sk)
        for pos in positions:
            price = prices.get(pos["symbol"], pos.get("current_price", pos["buy_price"]))
            await self.strategy_sell(uid, pos["symbol"], sk, price, {}, trigger="stop_strategy")

        strat_fresh  = await self.get_strategy(uid, sk)
        total_return = self._r2((strat_fresh or strat).get("cash_in_strategy", 0))
        summary      = await self.get_or_create_summary(uid)
        old_cash     = summary["available_cash"]
        new_cash     = self._r2(old_cash + total_return)
        now          = self._now()

        await self._run(lambda: self._summary_ref(uid).update({
            "available_cash": new_cash, "updated_at": now,
        }))
        await self._run(lambda: self._strategy_ref(uid, sk).delete())
        await self._log_tx(uid, {
            "type": "stop_withdraw", "strategy_key": sk, "amount": total_return,
            "available_cash_before": old_cash, "available_cash_after": new_cash,
            "notes": f"Stopped {STRATEGIES[sk]['label']} — {len(positions)} positions closed, ${total_return:.2f} returned",
        })
        await self._sync_total_value(uid)
        return {"strategy_key": sk, "positions_closed": len(positions),
                "returned": total_return, "available_cash": new_cash}

    # ── Strategy positions ────────────────────────────────────────────────────

    async def get_strategy_positions(self, uid: str, sk: str,
                                     prices: dict = None) -> list[dict]:
        def _f():
            return [self._ser(d.to_dict() or {}) for d in
                    self._user(uid).collection("strategy_positions")
                    .where("strategy_key", "==", sk).stream()]
        positions = await self._run(_f)
        if prices:
            for pos in positions:
                p = prices.get(pos["symbol"])
                if p and p > 0:
                    cv   = self._r2(pos["shares"] * p)
                    abp  = pos.get("avg_buy_price", pos.get("buy_price", 0))
                    pnl  = self._r2(cv - pos["shares"] * abp)
                    ppct = self._r2((pnl / (pos["shares"] * abp)) * 100) if abp else 0
                    pos.update({"current_price": p, "current_value": cv,
                                "unrealized_pnl": pnl, "unrealized_pnl_pct": ppct})
        return positions

    async def get_all_strategy_positions(self, uid: str,
                                         prices: dict = None) -> list[dict]:
        def _f():
            return [self._ser(d.to_dict() or {}) for d in
                    self._user(uid).collection("strategy_positions").stream()]
        positions = await self._run(_f)
        if prices:
            for pos in positions:
                p = prices.get(pos["symbol"])
                if p and p > 0:
                    cv   = self._r2(pos["shares"] * p)
                    abp  = pos.get("avg_buy_price", pos.get("buy_price", 0))
                    pnl  = self._r2(cv - pos["shares"] * abp)
                    ppct = self._r2((pnl / (pos["shares"] * abp)) * 100) if abp else 0
                    pos.update({"current_price": p, "current_value": cv,
                                "unrealized_pnl": pnl, "unrealized_pnl_pct": ppct})
        return positions

    async def get_strategy_position(self, uid: str, symbol: str,
                                    sk: str) -> Optional[dict]:
        doc = await self._run(lambda: self._strat_pos_ref(uid, symbol, sk).get())
        return self._ser(doc.to_dict() or {}) if doc.exists else None

    async def strategy_buy(self, uid: str, symbol: str, sk: str, price: float,
                           signal: dict, trigger: str = "auto") -> dict:
        """Auto-trader BUY inside a strategy fund."""
        symbol = symbol.upper()
        strat  = await self.get_strategy(uid, sk)
        if not strat:
            return {"status": "skipped", "reason": f"Strategy {sk} not allocated"}

        cfg = STRATEGIES[sk]

        if trigger != "manual":
            if not strat.get("is_active") or strat.get("is_paused"):
                return {"status": "skipped", "reason": f"Strategy {sk} not active"}
            sig_conf = signal.get("confidence", "LOW")
            if CONFIDENCE_RANK.get(sig_conf, 0) < CONFIDENCE_RANK.get(cfg["min_confidence"], 0):
                return {"status": "skipped", "reason": f"Signal {sig_conf} below minimum {cfg['min_confidence']}"}
            if symbol not in cfg["universe"]:
                return {"status": "skipped", "reason": f"{symbol} not in {sk} universe"}

        existing = await self.get_strategy_position(uid, symbol, sk)
        if existing:
            return {"status": "skipped", "reason": f"Already holding {symbol} in {sk}"}

        cash_avail = strat["cash_in_strategy"]
        reserve    = self._r2(cash_avail * cfg["cash_reserve_pct"])
        investable = self._r2(cash_avail - reserve)

        # Use ORIGINAL allocation for position sizing — keeps positions equal size
        # even as cash depletes. Cap at investable so we never overspend.
        target_cost = self._r2(strat["allocated"] * cfg["position_pct"])
        cost        = self._r2(min(target_cost, investable))
        shares      = round(cost / price, 6) if price > 0 else 0

        if cost < 1.0 or shares <= 0:
            return {"status": "skipped", "reason": f"Insufficient strategy cash (${cash_avail:.2f})"}

        sl_price = self._r2(price * (1 - strat.get("stop_loss_pct", cfg["stop_loss_default"]) / 100))
        now      = self._now()

        position = {
            "symbol":             symbol, "strategy_key": sk,
            "shares":             shares, "buy_price":    price,
            "buy_date":           now,    "current_price": price,
            "current_value":      cost,   "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,   "stop_loss_price": sl_price,
            "stop_loss_pct":      strat.get("stop_loss_pct", cfg["stop_loss_default"]),
            "trailing_high":      price,  # tracks peak price for trailing stop
            "signal_ref":         signal.get("generated_at", now),
            "signal_confidence":  signal.get("confidence", ""),
            "target_price":       signal.get("target_price", 0),
        }
        await self._run(lambda: self._strat_pos_ref(uid, symbol, sk).set(position))
        new_cash_in = self._r2(cash_avail - cost)
        await self._run(lambda: self._strategy_ref(uid, sk).update({
            "cash_in_strategy": new_cash_in,
            "invested":         self._r2(strat.get("invested", 0) + cost),
            "updated_at":       now,
        }))

        trade = {
            "symbol": symbol, "strategy_key": sk, "action": "BUY",
            "shares": shares, "price": price, "total": cost, "pnl": 0.0,
            "reason": signal.get("summary", ""), "trigger": trigger,
            "signal_confidence": signal.get("confidence", ""),
            "balance_after": new_cash_in, "timestamp": now,
        }
        await self._run(lambda: self._strat_trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: STRATEGY BUY {symbol}/{sk} {shares:.4f}sh @ ${price:.2f} for {uid}")
        return {"status": "executed", "action": "BUY", **trade}

    async def strategy_sell(self, uid: str, symbol: str, sk: str, price: float,
                            signal: dict, trigger: str = "auto",
                            reason: str = "") -> dict:
        symbol   = symbol.upper()
        position = await self.get_strategy_position(uid, symbol, sk)
        if not position:
            return {"status": "skipped", "reason": f"No position for {symbol} in {sk}"}

        strat      = await self.get_strategy(uid, sk)
        shares     = position["shares"]
        bp         = position["buy_price"]
        proceeds   = self._r2(shares * price)
        cost_basis = self._r2(shares * bp)
        pnl        = self._r2(proceeds - cost_basis)
        pnl_pct    = self._r2((pnl / cost_basis) * 100) if cost_basis else 0.0
        now        = self._now()

        await self._run(lambda: self._strat_pos_ref(uid, symbol, sk).delete())

        if strat:
            new_cash = self._r2(strat.get("cash_in_strategy", 0) + proceeds)
            new_inv  = self._r2(max(0.0, strat.get("invested", 0) - cost_basis))
            await self._run(lambda: self._strategy_ref(uid, sk).update({
                "cash_in_strategy": new_cash,
                "invested":         new_inv,
                "realized_pnl":     self._r2(strat.get("realized_pnl", 0) + pnl),
                "total_value":      self._r2(new_cash + new_inv),
                "updated_at":       now,
            }))
            bal_after = new_cash
        else:
            bal_after = proceeds

        trade = {
            "symbol": symbol, "strategy_key": sk, "action": "SELL",
            "shares": shares, "price": price,
            "total": proceeds, "pnl": pnl, "pnl_pct": pnl_pct,
            "reason": reason or signal.get("summary", ""),
            "signal_confidence": signal.get("confidence", ""),
            "trigger": trigger, "balance_after": bal_after, "timestamp": now,
        }
        await self._run(lambda: self._strat_trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: STRATEGY SELL {symbol}/{sk} @ ${price:.2f} P&L ${pnl:+.2f} for {uid}")
        return {"status": "executed", "action": "SELL", **trade}

    # ── Stop-loss (strategies only) ───────────────────────────────────────────

    async def check_stop_losses(self, uid: str, prices: dict,
                                signals: dict = None) -> list[dict]:
        """
        3-priority sell logic — runs every 60 seconds:

        Priority 1 — SELL signal (HIGH confidence)
            AI says exit → sell immediately regardless of price

        Priority 2 — Trailing stop breached
            Price fell X% from peak → sell to protect gains
            trailing_high tracks peak price since entry
            stop_loss_price updates upward as price rises (never down)

        Priority 3 — Hard stop-loss
            Price fell X% from buy_price → last resort, limits losses
        """
        triggered = []
        signals   = signals or {}

        for pos in await self.get_all_strategy_positions(uid):
            symbol = pos["symbol"]
            sk     = pos.get("strategy_key", "")
            if not symbol or not sk:
                continue

            # Get current price
            price_data = prices.get(symbol)
            curr = (price_data.get("price") if isinstance(price_data, dict)
                    else price_data) if price_data else None
            if not curr or curr <= 0:
                continue

            # Skip paused strategies
            strat = await self.get_strategy(uid, sk)
            if not strat or strat.get("is_paused"):
                continue

            cfg          = STRATEGIES.get(sk, {})
            sl_pct       = pos.get("stop_loss_pct", cfg.get("stop_loss_default", 5.0))
            buy_price    = pos.get("buy_price", 0)
            hard_stop    = pos.get("stop_loss_price", buy_price * (1 - sl_pct / 100))
            trailing_high= pos.get("trailing_high", buy_price)
            sell_reason  = None
            sell_trigger = None

            # ── Priority 1: SELL signal ───────────────────────────────────────
            sig = signals.get(symbol, {})
            eff_signal = sig.get("current_signal") or sig.get("signal", "")
            eff_conf   = sig.get("current_confidence") or sig.get("confidence", "")
            if eff_signal == "SELL" and eff_conf == "HIGH":
                sell_reason  = f"SELL signal (HIGH): AI recommends exit at ${curr:.2f}"
                sell_trigger = "sell_signal"

            # ── Priority 2: Trailing stop ────────────────────────────────────
            elif curr > 0:
                new_trailing_high = max(curr, trailing_high)
                trailing_stop     = self._r2(new_trailing_high * (1 - sl_pct / 100))

                if curr <= trailing_stop and curr > hard_stop:
                    pnl_pct = ((curr - buy_price) / buy_price * 100) if buy_price else 0
                    sell_reason  = (
                        f"Trailing stop: ${curr:.2f} ≤ ${trailing_stop:.2f} "
                        f"(peak ${new_trailing_high:.2f}, {pnl_pct:+.2f}%)"
                    )
                    sell_trigger = "trailing_stop"

                # ── Priority 3: Hard stop-loss ───────────────────────────────
                elif curr <= hard_stop:
                    pnl_pct = ((curr - buy_price) / buy_price * 100) if buy_price else 0
                    sell_reason  = (
                        f"Hard stop-loss: ${curr:.2f} ≤ ${hard_stop:.2f} "
                        f"(bought ${buy_price:.2f}, {pnl_pct:+.2f}%)"
                    )
                    sell_trigger = "hard_stop"

                # ── No sell: update trailing high + stop if price rose ────────
                elif new_trailing_high > trailing_high:
                    new_stop = self._r2(new_trailing_high * (1 - sl_pct / 100))
                    try:
                        await self._run(lambda: self._strat_pos_ref(uid, symbol, sk).update({
                            "trailing_high":  new_trailing_high,
                            "stop_loss_price": new_stop,
                            "current_price":  curr,
                        }))
                        logger.debug(
                            f"Trailing high updated {symbol}/{sk}: "
                            f"${trailing_high:.2f}→${new_trailing_high:.2f} "
                            f"stop ${new_stop:.2f}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to update trailing high for {symbol}: {e}")

            # ── Execute sell if any priority triggered ────────────────────────
            if sell_reason and sell_trigger:
                result = await self.strategy_sell(
                    uid, symbol, sk, curr, {},
                    trigger=sell_trigger, reason=sell_reason
                )
                if result.get("status") == "executed":
                    triggered.append({**result, "sell_trigger": sell_trigger})
                    logger.info(
                        f"PortfolioService: SELL {sell_trigger.upper()} "
                        f"{symbol}/{sk} @ ${curr:.2f} — {sell_reason}"
                    )

        return triggered

    # ── Active users (for scheduler) ─────────────────────────────────────────

    async def get_active_strategy_users(self) -> list[tuple[str, str]]:
        if not self._db:
            return []
        try:
            def _q():
                docs = (self._db.collection_group("strategies")
                        .where("is_active", "==", True)
                        .where("is_paused", "==", False)
                        .stream())
                result = []
                for doc in docs:
                    parts = doc.reference.path.split("/")
                    if len(parts) >= 4:
                        result.append((parts[1], parts[3]))
                return result
            return await self._run(_q)
        except Exception as e:
            logger.error(f"get_active_strategy_users failed: {e}")
            return []

    # ── Strategy trades history ───────────────────────────────────────────────

    async def get_strategy_trades(self, uid: str, sk: str = None,
                                  limit: int = 50) -> list[dict]:
        from firebase_admin import firestore as fs
        def _f():
            q = self._strat_trades_ref(uid).order_by(
                "timestamp", direction=fs.Query.DESCENDING)
            if sk:
                q = q.where("strategy_key", "==", sk)
            return [self._ser(d.to_dict() or {}) for d in q.limit(limit).stream()]
        return await self._run(_f)

    async def get_transactions(self, uid: str, limit: int = 50) -> list[dict]:
        from firebase_admin import firestore as fs
        def _f():
            return [self._ser(d.to_dict() or {}) for d in
                    self._tx_ref(uid)
                    .order_by("timestamp", direction=fs.Query.DESCENDING)
                    .limit(limit).stream()]
        return await self._run(_f)

    # ── Full dashboard overview ───────────────────────────────────────────────

    async def get_overview(self, uid: str, prices: dict = None) -> dict:
        summary      = await self.get_or_create_summary(uid)
        manual_pos   = await self.get_manual_positions(uid, prices)
        all_strats   = await self.get_all_strategies(uid)

        # Enrich strategies with positions
        strategies_out = {}
        for key, cfg in STRATEGIES.items():
            alloc     = all_strats.get(key)
            positions = []
            if alloc:
                positions = await self.get_strategy_positions(uid, key, prices)
                invested  = self._r2(sum(p.get("current_value", 0) for p in positions))
                cash_in   = alloc.get("cash_in_strategy", 0)
                total_val = self._r2(invested + cash_in)
                pnl       = self._r2(total_val - alloc.get("allocated", 0))
                pnl_pct   = self._r2((pnl / alloc["allocated"]) * 100) if alloc.get("allocated") else 0
                alloc.update({
                    "invested":       invested,
                    "total_value":    total_val,
                    "unrealized_pnl": self._r2(sum(p.get("unrealized_pnl", 0) for p in positions)),
                    "pnl":            pnl,
                    "pnl_pct":        pnl_pct,
                })
            strategies_out[key] = {
                "config":       {**cfg, "key": key},
                "allocation":   alloc,
                "positions":    positions,
                "is_allocated": alloc is not None,
            }

        # Manual portfolio value
        manual_value = self._r2(sum(p.get("current_value", 0) for p in manual_pos))
        manual_pnl   = self._r2(sum(p.get("unrealized_pnl", 0) for p in manual_pos))

        # Strategy total value
        strat_value  = self._r2(sum(
            s["allocation"].get("total_value", 0)
            for s in strategies_out.values() if s["allocation"]
        ))

        total_value = self._r2(summary["available_cash"] + manual_value + strat_value)

        return {
            "summary": {
                **summary,
                "total_value":    total_value,
                "manual_value":   manual_value,
                "strategy_value": strat_value,
            },
            "manual": {
                "positions":  manual_pos,
                "total_value": manual_value,
                "total_pnl":   manual_pnl,
            },
            "strategies": strategies_out,
        }

    # ── Static ────────────────────────────────────────────────────────────────

    @staticmethod
    def get_strategy_catalogue() -> dict:
        return {k: {**v, "key": k} for k, v in STRATEGIES.items()}