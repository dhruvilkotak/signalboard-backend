"""
services/portfolio_service.py  — v3 (multi-strategy rewrite)

Architecture:
  - One portfolio summary per user (available_cash, total_value)
  - Up to 5 concurrent strategy sub-accounts (one Firestore doc per active strategy)
  - Positions keyed by {symbol}_{strategy_key} so same stock can be held across strategies
  - Every action has a transaction log entry

Firestore schema:
  users/{uid}/portfolio/summary
      available_cash, total_value, total_deposited, agreement_accepted

  users/{uid}/strategies/{strategy_key}
      allocated, cash_in_strategy, invested, total_value
      is_active, is_paused, stop_loss_pct, realized_pnl
      created_at, updated_at

  users/{uid}/positions/{symbol}_{strategy_key}
      symbol, strategy_key, shares, buy_price, stop_loss_price
      current_price, current_value, unrealized_pnl, unrealized_pnl_pct

  users/{uid}/trades/{auto_id}
      symbol, strategy_key, action, shares, price, total
      pnl, pnl_pct, trigger, reason, timestamp, balance_after

  users/{uid}/transactions/{auto_id}
      type, strategy_key, amount
      available_cash_before, available_cash_after
      timestamp, notes
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
        "description":       "100% equities, HIGH confidence signals only. Max 25% of strategy allocation per position. High risk, high reward.",
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
        "description":       "60% equities, 30% ETFs, 10% defensive. HIGH + MEDIUM signals. Max 15% per position. Good risk/reward balance.",
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
        "description":       "80% technology sector, 20% ETFs. HIGH confidence only. Max 20% per position. Focused tech exposure.",
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
        "description":       "70% dividend ETFs, 30% defensive equities. Max 20% per position. Steady income with low volatility.",
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
        "description":       "50% defensive ETFs, 30% blue-chip, 20% cash reserve. HIGH confidence BUY only. Tight stop-loss. Capital preservation first.",
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


class PortfolioService:
    def __init__(self):
        self._db = None

    def set_db(self, db) -> None:
        self._db = db
        logger.info("PortfolioService v3: Firestore connected ✓")

    def _user_ref(self, uid):
        return self._db.collection("users").document(uid)

    def _summary_ref(self, uid):
        return self._user_ref(uid).collection("portfolio").document("summary")

    def _strategy_ref(self, uid, sk):
        return self._user_ref(uid).collection("strategies").document(sk)

    def _position_ref(self, uid, symbol, sk):
        return self._user_ref(uid).collection("positions").document(f"{symbol.upper()}_{sk}")

    def _trades_ref(self, uid):
        return self._user_ref(uid).collection("trades")

    def _transactions_ref(self, uid):
        return self._user_ref(uid).collection("transactions")

    async def _run(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _r2(self, n: float) -> float:
        return round(n, 2)

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

    # ── Summary ───────────────────────────────────────────────────────────────

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
            "type": "initial_deposit", "strategy_key": None,
            "amount": STARTING_CASH, "available_cash_before": 0.0,
            "available_cash_after": STARTING_CASH,
            "notes": "Initial virtual portfolio — $10,000 available cash",
        })
        return summary

    async def accept_agreement(self, uid: str) -> dict:
        await self.get_or_create_summary(uid)
        now = self._now()
        await self._run(lambda: self._summary_ref(uid).update({
            "agreement_accepted": True,
            "agreement_accepted_at": now,
            "updated_at": now,
        }))
        return {"agreement_accepted": True}

    async def _sync_total_value(self, uid: str) -> float:
        summary   = await self.get_or_create_summary(uid)
        strats    = await self.get_all_strategies(uid)
        strat_sum = sum(s.get("total_value", 0) for s in strats.values())
        total     = self._r2(summary["available_cash"] + strat_sum)
        await self._run(lambda: self._summary_ref(uid).update({"total_value": total, "updated_at": self._now()}))
        return total

    # ── Strategies ────────────────────────────────────────────────────────────

    async def get_strategy(self, uid: str, sk: str) -> Optional[dict]:
        doc = await self._run(self._strategy_ref(uid, sk).get)
        return self._ser(doc.to_dict() or {}) if doc.exists else None

    async def get_all_strategies(self, uid: str) -> dict:
        def _f():
            return {d.id: self._ser(d.to_dict() or {}) for d in self._user_ref(uid).collection("strategies").stream()}
        return await self._run(_f)

    async def get_portfolio_overview(self, uid: str, prices: dict = None) -> dict:
        summary   = await self.get_or_create_summary(uid)
        allocated = await self.get_all_strategies(uid)

        strategies_out = {}
        for key, cfg in STRATEGIES.items():
            alloc     = allocated.get(key)
            positions = []
            if alloc:
                positions = await self.get_positions(uid, key, prices)
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

        strat_total = self._r2(sum(
            s["allocation"].get("total_value", 0) for s in strategies_out.values() if s["allocation"]
        ))
        total_value = self._r2(summary["available_cash"] + strat_total)

        return {"summary": {**summary, "total_value": total_value}, "strategies": strategies_out}

    # ── Allocate / Deallocate / Pause / Stop ──────────────────────────────────

    async def allocate(self, uid: str, sk: str, amount: float, stop_loss_pct: float = None) -> dict:
        if sk not in STRATEGIES:    raise ValueError(f"Unknown strategy: {sk}")
        if amount <= 0:             raise ValueError("Amount must be positive")

        cfg     = STRATEGIES[sk]
        summary = await self.get_or_create_summary(uid)

        if not summary.get("agreement_accepted"):
            raise ValueError("Paper trading agreement must be accepted first")
        if amount > summary["available_cash"]:
            raise ValueError(f"Insufficient cash — need ${amount:.2f}, have ${summary['available_cash']:.2f}")

        sl = stop_loss_pct if stop_loss_pct is not None else cfg["stop_loss_default"]
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
            tx_type  = "add_more"
            tx_notes = f"Added ${amount:.2f} to {cfg['label']}"
        else:
            await self._run(lambda: self._strategy_ref(uid, sk).set({
                "allocated": amount, "cash_in_strategy": amount,
                "invested": 0.0, "total_value": amount,
                "is_active": True, "is_paused": False,
                "stop_loss_pct": sl, "realized_pnl": 0.0,
                "created_at": now, "updated_at": now,
            }))
            tx_type  = "allocate"
            tx_notes = f"Allocated ${amount:.2f} to {cfg['label']}"

        await self._run(lambda: self._summary_ref(uid).update({"available_cash": new_cash, "updated_at": now}))
        await self._log_tx(uid, {"type": tx_type, "strategy_key": sk, "amount": amount,
                                  "available_cash_before": old_cash, "available_cash_after": new_cash, "notes": tx_notes})
        await self._sync_total_value(uid)
        logger.info(f"PortfolioService: {tx_type} ${amount:.2f} → {sk} for {uid}")
        return {"strategy_key": sk, "amount": amount, "available_cash": new_cash}

    async def reduce(self, uid: str, sk: str, amount: float) -> dict:
        strat = await self.get_strategy(uid, sk)
        if not strat: raise ValueError(f"Strategy {sk} not allocated")
        if amount <= 0: raise ValueError("Amount must be positive")

        avail = strat["cash_in_strategy"]
        if amount > avail:
            raise ValueError(f"Only ${avail:.2f} idle cash available (${strat.get('invested',0):.2f} is invested)")

        summary  = await self.get_or_create_summary(uid)
        now      = self._now()
        old_cash = summary["available_cash"]
        new_cash = self._r2(old_cash + amount)

        await self._run(lambda: self._strategy_ref(uid, sk).update({
            "allocated":        self._r2(strat["allocated"] - amount),
            "cash_in_strategy": self._r2(avail - amount),
            "total_value":      self._r2(strat["total_value"] - amount),
            "updated_at":       now,
        }))
        await self._run(lambda: self._summary_ref(uid).update({"available_cash": new_cash, "updated_at": now}))
        await self._log_tx(uid, {"type": "reduce", "strategy_key": sk, "amount": amount,
                                  "available_cash_before": old_cash, "available_cash_after": new_cash,
                                  "notes": f"Withdrew ${amount:.2f} idle cash from {STRATEGIES[sk]['label']}"})
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
        await self._log_tx(uid, {"type": f"strategy_{action}", "strategy_key": sk, "amount": 0.0,
                                  "available_cash_before": 0.0, "available_cash_after": 0.0,
                                  "notes": f"{STRATEGIES[sk]['label']} {action}"})
        return {"strategy_key": sk, "is_paused": paused}

    async def stop_strategy(self, uid: str, sk: str, prices: dict) -> dict:
        strat = await self.get_strategy(uid, sk)
        if not strat: raise ValueError(f"Strategy {sk} not allocated")

        positions = await self.get_positions(uid, sk)
        for pos in positions:
            price = prices.get(pos["symbol"], pos.get("current_price", pos["buy_price"]))
            await self.execute_sell(uid, pos["symbol"], sk, price, {}, trigger="stop_strategy")

        strat_fresh  = await self.get_strategy(uid, sk)
        total_return = self._r2((strat_fresh or strat).get("cash_in_strategy", 0))
        summary      = await self.get_or_create_summary(uid)
        old_cash     = summary["available_cash"]
        new_cash     = self._r2(old_cash + total_return)
        now          = self._now()

        await self._run(lambda: self._summary_ref(uid).update({"available_cash": new_cash, "updated_at": now}))
        await self._run(lambda: self._strategy_ref(uid, sk).delete())
        await self._log_tx(uid, {"type": "stop_withdraw", "strategy_key": sk, "amount": total_return,
                                  "available_cash_before": old_cash, "available_cash_after": new_cash,
                                  "notes": f"Stopped {STRATEGIES[sk]['label']} — {len(positions)} positions closed, ${total_return:.2f} returned"})
        await self._sync_total_value(uid)
        logger.info(f"PortfolioService: stopped {sk} for {uid} — returned ${total_return:.2f}")
        return {"strategy_key": sk, "positions_closed": len(positions), "returned": total_return, "available_cash": new_cash}

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(self, uid: str, sk: str, prices: dict = None) -> list[dict]:
        def _f():
            return [self._ser(d.to_dict() or {}) for d in
                    self._user_ref(uid).collection("positions").where("strategy_key", "==", sk).stream()]
        positions = await self._run(_f)
        if prices:
            for pos in positions:
                p = prices.get(pos["symbol"])
                if p and p > 0:
                    cv = self._r2(pos["shares"] * p)
                    pnl = self._r2(cv - pos["shares"] * pos["buy_price"])
                    ppct = self._r2((pnl / (pos["shares"] * pos["buy_price"])) * 100) if pos["buy_price"] else 0
                    pos.update({"current_price": p, "current_value": cv, "unrealized_pnl": pnl, "unrealized_pnl_pct": ppct})
        return positions

    async def get_all_positions(self, uid: str, prices: dict = None) -> list[dict]:
        def _f():
            return [self._ser(d.to_dict() or {}) for d in self._user_ref(uid).collection("positions").stream()]
        positions = await self._run(_f)
        if prices:
            for pos in positions:
                p = prices.get(pos["symbol"])
                if p and p > 0:
                    cv = self._r2(pos["shares"] * p)
                    pnl = self._r2(cv - pos["shares"] * pos["buy_price"])
                    ppct = self._r2((pnl / (pos["shares"] * pos["buy_price"])) * 100) if pos["buy_price"] else 0
                    pos.update({"current_price": p, "current_value": cv, "unrealized_pnl": pnl, "unrealized_pnl_pct": ppct})
        return positions

    async def get_position(self, uid: str, symbol: str, sk: str) -> Optional[dict]:
        doc = await self._run(lambda: self._position_ref(uid, symbol, sk).get())
        return self._ser(doc.to_dict() or {}) if doc.exists else None

    # ── Trade execution ───────────────────────────────────────────────────────

    async def execute_buy(self, uid: str, symbol: str, sk: str, price: float, signal: dict,
                          trigger: str = "auto", shares_override: float = None,
                          amount_usd_override: float = None) -> dict:
        symbol = symbol.upper()
        strat  = await self.get_strategy(uid, sk)
        if not strat:
            return {"status": "skipped", "reason": f"Strategy {sk} not allocated"}

        summary = await self.get_or_create_summary(uid)
        if not summary.get("agreement_accepted"):
            return {"status": "skipped", "reason": "Agreement not accepted"}

        cfg = STRATEGIES[sk]

        if trigger != "manual":
            if not strat.get("is_active") or strat.get("is_paused"):
                return {"status": "skipped", "reason": f"Strategy {sk} not active"}
            sig_conf = signal.get("confidence", "LOW")
            if CONFIDENCE_RANK.get(sig_conf, 0) < CONFIDENCE_RANK.get(cfg["min_confidence"], 0):
                return {"status": "skipped", "reason": f"Confidence {sig_conf} below {cfg['min_confidence']}"}
            if symbol not in cfg["universe"]:
                return {"status": "skipped", "reason": f"{symbol} not in {sk} universe"}

        existing = await self.get_position(uid, symbol, sk)
        if existing:
            return {"status": "skipped", "reason": f"Already holding {symbol} in {sk}"}

        cash_avail = strat["cash_in_strategy"]
        reserve    = self._r2(cash_avail * cfg["cash_reserve_pct"])
        investable = self._r2(cash_avail - reserve)

        if shares_override and shares_override > 0:
            shares = round(shares_override, 6)
            cost   = self._r2(shares * price)
        elif amount_usd_override and amount_usd_override > 0:
            shares = round(amount_usd_override / price, 6)
            cost   = self._r2(amount_usd_override)
        else:
            cost   = self._r2(investable * cfg["position_pct"])
            shares = round(cost / price, 6) if price > 0 else 0

        if cost < 1.0 or shares <= 0:
            return {"status": "skipped", "reason": f"Insufficient cash in strategy (${cash_avail:.2f})"}
        if cost > cash_avail:
            return {"status": "skipped", "reason": f"Cost ${cost:.2f} exceeds strategy cash ${cash_avail:.2f}"}

        sl_price = self._r2(price * (1 - strat.get("stop_loss_pct", cfg["stop_loss_default"]) / 100))
        now      = self._now()

        position = {
            "symbol": symbol, "strategy_key": sk, "shares": shares,
            "buy_price": price, "buy_date": now, "current_price": price,
            "current_value": cost, "unrealized_pnl": 0.0, "unrealized_pnl_pct": 0.0,
            "stop_loss_price": sl_price,
            "signal_ref": signal.get("generated_at", now),
            "signal_confidence": signal.get("confidence", ""),
            "signal_conviction": signal.get("conviction_score", 0),
        }
        await self._run(lambda: self._position_ref(uid, symbol, sk).set(position))
        await self._run(lambda: self._strategy_ref(uid, sk).update({
            "cash_in_strategy": self._r2(cash_avail - cost),
            "invested":         self._r2(strat.get("invested", 0) + cost),
            "updated_at":       now,
        }))

        trade = {"symbol": symbol, "strategy_key": sk, "action": "BUY",
                 "shares": shares, "price": price, "total": cost, "pnl": 0.0,
                 "reason": signal.get("summary", ""), "signal_confidence": signal.get("confidence", ""),
                 "trigger": trigger, "balance_after": self._r2(cash_avail - cost), "timestamp": now}
        await self._run(lambda: self._trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: BUY {symbol}/{sk} {shares:.4f}sh @ ${price:.2f} for {uid}")
        return {"status": "executed", "action": "BUY", **trade}

    async def execute_sell(self, uid: str, symbol: str, sk: str, price: float, signal: dict,
                           trigger: str = "auto", reason: str = "") -> dict:
        symbol   = symbol.upper()
        position = await self.get_position(uid, symbol, sk)
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

        await self._run(lambda: self._position_ref(uid, symbol, sk).delete())

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

        trade = {"symbol": symbol, "strategy_key": sk, "action": "SELL",
                 "shares": shares, "price": price, "total": proceeds,
                 "pnl": pnl, "pnl_pct": pnl_pct,
                 "reason": reason or signal.get("summary", ""),
                 "signal_confidence": signal.get("confidence", ""),
                 "trigger": trigger, "balance_after": bal_after, "timestamp": now}
        await self._run(lambda: self._trades_ref(uid).add(trade))
        await self._sync_total_value(uid)

        logger.info(f"PortfolioService: SELL {symbol}/{sk} @ ${price:.2f} P&L ${pnl:+.2f} for {uid}")
        return {"status": "executed", "action": "SELL", **trade}

    # ── Stop-loss ─────────────────────────────────────────────────────────────

    async def check_stop_losses(self, uid: str, prices: dict) -> list[dict]:
        triggered = []
        for pos in await self.get_all_positions(uid):
            symbol = pos["symbol"]
            sk     = pos.get("strategy_key", "")
            curr   = prices.get(symbol)
            stop   = pos.get("stop_loss_price")
            if not curr or not stop or not sk:
                continue
            strat = await self.get_strategy(uid, sk)
            if strat and strat.get("is_paused"):
                continue
            if curr <= stop:
                reason = f"Stop-loss triggered: ${curr:.2f} ≤ ${stop:.2f} ({pos.get('unrealized_pnl_pct', 0):+.2f}%)"
                result = await self.execute_sell(uid, symbol, sk, curr, {}, trigger="stop_loss", reason=reason)
                triggered.append(result)
        return triggered

    # ── Active users ──────────────────────────────────────────────────────────

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
                        result.append((parts[1], parts[3]))  # (uid, strategy_key)
                return result
            return await self._run(_q)
        except Exception as e:
            logger.error(f"get_active_strategy_users failed: {e}")
            return []

    # ── History ───────────────────────────────────────────────────────────────

    async def get_trades(self, uid: str, sk: str = None, limit: int = 50) -> list[dict]:
        from firebase_admin import firestore as fs
        def _f():
            q = self._trades_ref(uid).order_by("timestamp", direction=fs.Query.DESCENDING)
            if sk:
                q = q.where("strategy_key", "==", sk)
            return [self._ser(d.to_dict() or {}) for d in q.limit(limit).stream()]
        return await self._run(_f)

    async def get_transactions(self, uid: str, limit: int = 50) -> list[dict]:
        from firebase_admin import firestore as fs
        def _f():
            docs = (self._transactions_ref(uid)
                    .order_by("timestamp", direction=fs.Query.DESCENDING)
                    .limit(limit).stream())
            return [self._ser(d.to_dict() or {}) for d in docs]
        return await self._run(_f)

    # ── P&L ───────────────────────────────────────────────────────────────────

    async def get_pnl(self, uid: str, prices: dict = None) -> dict:
        overview = await self.get_portfolio_overview(uid, prices)
        strats   = overview["strategies"]
        total_unrealized = total_realized = total_allocated = total_invested = 0.0
        per_strategy = {}
        for key, s in strats.items():
            alloc = s["allocation"]
            if not alloc:
                continue
            total_allocated  += alloc.get("allocated", 0)
            total_unrealized += alloc.get("unrealized_pnl", 0)
            total_realized   += alloc.get("realized_pnl", 0)
            total_invested   += alloc.get("invested", 0)
            per_strategy[key] = {
                "label": STRATEGIES[key]["label"],
                "allocated": alloc.get("allocated", 0),
                "total_value": alloc.get("total_value", 0),
                "pnl": alloc.get("pnl", 0),
                "pnl_pct": alloc.get("pnl_pct", 0),
                "realized_pnl": alloc.get("realized_pnl", 0),
                "unrealized_pnl": alloc.get("unrealized_pnl", 0),
                "positions": len(s["positions"]),
            }
        total_pnl = self._r2(total_unrealized + total_realized)
        dep       = overview["summary"].get("total_deposited", STARTING_CASH)
        return {
            "total_value":      overview["summary"]["total_value"],
            "available_cash":   overview["summary"]["available_cash"],
            "total_allocated":  self._r2(total_allocated),
            "total_invested":   self._r2(total_invested),
            "total_unrealized": self._r2(total_unrealized),
            "total_realized":   self._r2(total_realized),
            "total_pnl":        total_pnl,
            "total_return_pct": self._r2((total_pnl / dep) * 100) if dep else 0,
            "per_strategy":     per_strategy,
        }

    async def _log_tx(self, uid: str, data: dict) -> None:
        entry = {**data, "timestamp": self._now()}
        await self._run(lambda: self._transactions_ref(uid).add(entry))

    @staticmethod
    def get_strategy_catalogue() -> dict:
        return {k: {**v, "key": k} for k, v in STRATEGIES.items()}