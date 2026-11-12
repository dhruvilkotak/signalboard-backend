"""
AccessExpiryService — when a user loses Auto-Trader access (trial or coupon
ended, subscription cancelled, Pro removed), convert their Auto-Trader
holdings to cash.

reconcile(uid), run for every user with strategies every 15 min:
    still has autotrader access
        -> nothing; if a liquidation was pending, resume the strategies we paused
    lost access, flag `autotrader_liquidation` off
        -> log "would liquidate" only (dry run)
    lost access, flag on
        1. pause every running strategy now (no new trades)       pending_liquidation
        2. during a trading session (is_trading_allowed()):
           stop_strategy(trigger="access_expired") on each one:
           sells all positions, returns the cash, removes the strategy
        -> users/{uid}/data/access.last_liquidation = {at, positions_closed, returned, reason}

Manual positions are never touched. Idempotent: a user with no strategies left
is a no-op, so re-runs are safe.

Wiring (main.py):
    expiry_svc = AccessExpiryService(portfolio_svc, price_svc, access_svc, flag_svc)
    expiry_svc.set_db(db)                                  # lifespan()
    scheduler.add_job(access_expiry_job, "interval", minutes=15, id="access_expiry")
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable

from firebase_admin import firestore

from utils.market_hours import is_trading_allowed

logger = logging.getLogger(__name__)


class AccessExpiryService:
    def __init__(self, portfolio_svc, price_svc, access_svc, flag_svc, db=None,
                 trading_allowed: Callable[[], bool] = is_trading_allowed):
        self.portfolio = portfolio_svc
        self.prices = price_svc
        self.access = access_svc
        self.flags = flag_svc
        self._db = db
        self._trading_allowed = trading_allowed

    def set_db(self, db) -> None:
        self._db = db

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args))

    def _access_ref(self, uid: str):
        return self._db.collection("users").document(uid).collection("data").document("access")

    def _read_state(self, uid: str) -> dict:
        snap = self._access_ref(uid).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def _write_state(self, uid: str, data: dict) -> None:
        self._access_ref(uid).set(data, merge=True)

    def _uids_with_strategies(self) -> list[str]:
        uids = set()
        for doc in self._db.collection_group("strategies").stream():
            parts = doc.reference.path.split("/")          # users/{uid}/strategies/{sk}
            if len(parts) >= 4 and parts[0] == "users":
                uids.add(parts[1])
        return sorted(uids)

    async def _price_map(self) -> dict:
        raw = await self.prices.get_all()
        return {s: (d.get("price", 0) if isinstance(d, dict) else float(d or 0)) for s, d in raw.items()}

    # ── scheduled entry point ────────────────────────────────────────────

    async def run(self) -> dict:
        if self._db is None:
            return {"skipped": "no_db"}
        uids = await self._run(self._uids_with_strategies)
        counts: dict[str, int] = {}
        for uid in uids:
            try:
                outcome = await self.reconcile(uid)
            except Exception:
                logger.exception("access_expiry_failed uid=%s", uid)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
        logger.info("access_expiry_run", extra={"event": "access_expiry_run", "users": len(uids), **counts})
        return {"users": len(uids), **counts}

    # ── per user ─────────────────────────────────────────────────────────

    async def reconcile(self, uid: str) -> str:
        access = await self._run(self.access.get_access, uid, False)
        state = await self._run(self._read_state, uid)
        pending = bool(state.get("pending_liquidation"))

        if access["features"].get("autotrader"):
            if pending:
                return await self._restore(uid, state)
            return "ok"

        strategies = await self.portfolio.get_all_strategies(uid)
        if not strategies:
            if pending:
                await self._run(self._write_state, uid, {"pending_liquidation": False, "paused_by_expiry": []})
            return "none"

        if not self.flags.is_enabled("autotrader_liquidation", uid):
            logger.info("autotrader_liquidation_dry_run",
                        extra={"event": "autotrader_liquidation_dry_run", "uid": uid,
                               "plan": access.get("plan"), "strategies": sorted(strategies)})
            return "dry_run"

        if not pending:
            to_pause = sorted(sk for sk, s in strategies.items() if not s.get("is_paused"))
            for sk in to_pause:
                await self.portfolio.pause_strategy(uid, sk, True)
            await self._run(self._write_state, uid, {
                "pending_liquidation": True,
                "paused_by_expiry": to_pause,
                "liquidation_reason": f"{access.get('plan')}_no_autotrader",
                "pending_since": firestore.SERVER_TIMESTAMP,
            })
            logger.info("autotrader_paused_for_expiry",
                        extra={"event": "autotrader_paused_for_expiry", "uid": uid, "strategies": to_pause})

        if not self._trading_allowed():
            return "pending"
        return await self._liquidate(uid, strategies, access.get("plan"))

    async def _liquidate(self, uid: str, strategies: dict, plan) -> str:
        prices = await self._price_map()
        closed, returned = 0, 0.0
        for sk in sorted(strategies):
            r = await self.portfolio.stop_strategy(uid, sk, prices, trigger="access_expired")
            closed += int(r.get("positions_closed", 0))
            returned += float(r.get("returned", 0))
        summary = {
            "at": datetime.now(timezone.utc),
            "positions_closed": closed,
            "returned": round(returned, 2),
            "strategies": sorted(strategies),
            "reason": f"{plan}_no_autotrader",
        }
        await self._run(self._write_state, uid, {
            "pending_liquidation": False, "paused_by_expiry": [], "last_liquidation": summary,
        })
        self.access.invalidate(uid)
        logger.info("autotrader_liquidated",
                    extra={"event": "autotrader_liquidated", "uid": uid, "positions_closed": closed,
                           "returned": summary["returned"], "strategies": summary["strategies"]})
        return "liquidated"

    async def _restore(self, uid: str, state: dict) -> str:
        current = await self.portfolio.get_all_strategies(uid)
        resumed = []
        for sk in state.get("paused_by_expiry") or []:
            if sk in current and current[sk].get("is_paused"):
                await self.portfolio.pause_strategy(uid, sk, False)
                resumed.append(sk)
        await self._run(self._write_state, uid, {"pending_liquidation": False, "paused_by_expiry": []})
        self.access.invalidate(uid)
        logger.info("autotrader_liquidation_cancelled",
                    extra={"event": "autotrader_liquidation_cancelled", "uid": uid, "resumed": resumed})
        return "restored"
