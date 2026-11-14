"""
AccessExpiryService — when a user loses Auto-Trader access (trial or coupon
ended, subscription cancelled, Pro removed), convert their Auto-Trader
holdings to cash.

Runs ONCE per trading day (main.py: Mon–Fri 9:45 AM ET). A failed user is
retried at the next day's run; after MAX_RETRIES retries (3 attempts in all)
it is marked failed, a job_failures doc is written and an alert goes out
(structured log `autotrader_liquidation_failed` + email to ADMIN_ALERT_EMAIL).
Failed users are skipped until an admin fixes the cause and resets them:
see docs/runbooks/autotrader-liquidation.md and scripts/run_access_expiry.py.

reconcile(uid), for every user with strategies:
    still has autotrader access
        -> nothing; if a liquidation was pending, resume the strategies we paused
    lost access, flag `autotrader_liquidation` off
        -> log "would liquidate" only (dry run)
    marked failed (3 attempts used)
        -> skip ("failed"); strategies stay paused, nothing trades
    lost access, flag on
        1. pause every running strategy (no new trades)            pending_liquidation
        2. if trading is allowed now: stop_strategy(trigger="access_expired")
           on each one — sells all positions, returns the cash, removes it
           (outside trading hours, e.g. a manual run at night: stays paused, "pending")
        -> users/{uid}/data/access.last_liquidation = {at, positions_closed, returned, reason}

State on users/{uid}/data/access:
    pending_liquidation, paused_by_expiry, liquidation_attempts,
    last_liquidation_error, last_liquidation_attempt_at, liquidation_failed

Manual positions are never touched. Idempotent: strategies already sold are
gone, so a retry only sells what is left.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from firebase_admin import firestore

from utils.market_hours import is_trading_allowed

logger = logging.getLogger(__name__)

MAX_RETRIES = 2                       # after the first attempt -> 3 attempts in all
MAX_ATTEMPTS = 1 + MAX_RETRIES
ADMIN_ALERT_EMAIL = os.getenv("ADMIN_ALERT_EMAIL", os.getenv("GMAIL_USER", "kotakdhruvil@gmail.com"))
RUNBOOK = "docs/runbooks/autotrader-liquidation.md"


def default_alert(record: dict) -> None:
    """Log for GCP alerting and email the admin. Never raises."""
    logger.error("autotrader_liquidation_failed", extra={"event": "autotrader_liquidation_failed", **{
        k: v for k, v in record.items() if k != "created_at"}})
    try:
        from services.email_service import _send_email
        uid = record.get("uid", "?")
        lines = [f"{k}: {v}" for k, v in record.items() if k != "created_at"]
        text = ("Auto-Trader liquidation failed and will not be retried automatically.\n\n"
                + "\n".join(lines) + f"\n\nRunbook: {RUNBOOK}")
        html = "<pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
        _send_email(ADMIN_ALERT_EMAIL, f"[SignalBoard] Liquidation failed: {uid}", html, text)
    except Exception:
        logger.exception("alert email failed")


class AccessExpiryService:
    def __init__(self, portfolio_svc, price_svc, access_svc, flag_svc, db=None,
                 trading_allowed: Callable[[], bool] = is_trading_allowed,
                 alert: Callable[[dict], None] = default_alert):
        self.portfolio = portfolio_svc
        self.prices = price_svc
        self.access = access_svc
        self.flags = flag_svc
        self._db = db
        self._trading_allowed = trading_allowed
        self._alert = alert

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

    def _record_failure(self, record: dict) -> None:
        self._db.collection("job_failures").add(record)

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

    async def run(self, uids: Optional[list[str]] = None) -> dict:
        """Daily job (and scripts/run_access_expiry.py). uids=None -> every user with strategies."""
        if self._db is None:
            return {"skipped": "no_db"}
        try:
            if uids is None:
                uids = await self._run(self._uids_with_strategies)
        except Exception as e:                              # whole job can't start
            self._alert({"job": "access_expiry", "uid": None, "status": "job_failed",
                         "error": f"{type(e).__name__}: {e}"})
            raise
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
            if pending or state.get("liquidation_failed") or state.get("liquidation_attempts"):
                return await self._restore(uid, state)
            return "ok"

        strategies = await self.portfolio.get_all_strategies(uid)
        if not strategies:
            if pending or state.get("liquidation_failed"):
                await self._run(self._write_state, uid, {
                    "pending_liquidation": False, "paused_by_expiry": [],
                    "liquidation_failed": False, "liquidation_attempts": 0})
            return "none"

        if not self.flags.is_enabled("autotrader_liquidation", uid):
            logger.info("autotrader_liquidation_dry_run",
                        extra={"event": "autotrader_liquidation_dry_run", "uid": uid,
                               "plan": access.get("plan"), "strategies": sorted(strategies)})
            return "dry_run"

        if state.get("liquidation_failed"):
            return "failed"                                 # waiting for an admin (runbook)

        try:
            return await self._expire(uid, access, state, strategies)
        except Exception as e:
            return await self._attempt_failed(uid, access, state, [f"{type(e).__name__}: {e}"], None)

    async def _expire(self, uid: str, access: dict, state: dict, strategies: dict) -> str:
        if not state.get("pending_liquidation"):
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
        return await self._liquidate(uid, access, state, strategies)

    async def _liquidate(self, uid: str, access: dict, state: dict, strategies: dict) -> str:
        plan = access.get("plan")
        prices = await self._price_map()
        closed, returned, sold, errors = 0, 0.0, [], []
        for sk in sorted(strategies):
            try:
                r = await self.portfolio.stop_strategy(uid, sk, prices, trigger="access_expired")
            except Exception as e:
                errors.append(f"{sk}: {type(e).__name__}: {e}")
                continue
            closed += int(r.get("positions_closed", 0))
            returned += float(r.get("returned", 0))
            sold.append(sk)

        partial = {"positions_closed": closed, "returned": round(returned, 2), "strategies": sold}
        if errors:
            return await self._attempt_failed(uid, access, state, errors, partial)

        summary = {"at": datetime.now(timezone.utc), **partial, "reason": f"{plan}_no_autotrader",
                   "attempts": int(state.get("liquidation_attempts", 0) or 0) + 1}
        await self._run(self._write_state, uid, {
            "pending_liquidation": False, "paused_by_expiry": [], "last_liquidation": summary,
            "liquidation_attempts": 0, "liquidation_failed": False, "last_liquidation_error": None,
        })
        self.access.invalidate(uid)
        logger.info("autotrader_liquidated",
                    extra={"event": "autotrader_liquidated", "uid": uid, "positions_closed": closed,
                           "returned": summary["returned"], "strategies": sold})
        return "liquidated"

    async def _attempt_failed(self, uid: str, access: dict, state: dict,
                              errors: list[str], partial: Optional[dict]) -> str:
        attempts = int(state.get("liquidation_attempts", 0) or 0) + 1
        gave_up = attempts >= MAX_ATTEMPTS
        now = datetime.now(timezone.utc)
        update = {
            "liquidation_attempts": attempts,
            "last_liquidation_error": "; ".join(errors)[:1000],
            "last_liquidation_attempt_at": now,
            "liquidation_failed": gave_up,
        }
        if partial and partial.get("strategies"):
            update["last_partial_liquidation"] = {"at": now, **partial}
        await self._run(self._write_state, uid, update)
        logger.warning("autotrader_liquidation_attempt_failed",
                       extra={"event": "autotrader_liquidation_attempt_failed", "uid": uid,
                              "attempt": attempts, "max_attempts": MAX_ATTEMPTS, "errors": errors})
        if not gave_up:
            return "retry"                                  # next trading day's run tries again

        record = {"job": "access_expiry", "uid": uid, "status": "failed", "plan": access.get("plan"),
                  "attempts": attempts, "error": update["last_liquidation_error"],
                  "partial": partial or {}, "runbook": RUNBOOK, "resolved": False,
                  "created_at": firestore.SERVER_TIMESTAMP}
        try:
            await self._run(self._record_failure, record)
        except Exception:
            logger.exception("could not write job_failures for uid=%s", uid)
        self._alert(record)
        return "failed"

    async def _restore(self, uid: str, state: dict) -> str:
        current = await self.portfolio.get_all_strategies(uid)
        resumed = []
        for sk in state.get("paused_by_expiry") or []:
            if sk in current and current[sk].get("is_paused"):
                await self.portfolio.pause_strategy(uid, sk, False)
                resumed.append(sk)
        await self._run(self._write_state, uid, {
            "pending_liquidation": False, "paused_by_expiry": [],
            "liquidation_attempts": 0, "liquidation_failed": False, "last_liquidation_error": None})
        self.access.invalidate(uid)
        logger.info("autotrader_liquidation_cancelled",
                    extra={"event": "autotrader_liquidation_cancelled", "uid": uid, "resumed": resumed})
        return "restored"

    # ── admin / runbook ──────────────────────────────────────────────────

    def reset_failure(self, uid: str) -> None:
        """Clear the failed mark so the next run (daily or manual) tries again, with 3 fresh attempts."""
        self._write_state(uid, {"liquidation_failed": False, "liquidation_attempts": 0,
                                "last_liquidation_error": None})
        for doc in (self._db.collection("job_failures").where("uid", "==", uid)
                    .where("resolved", "==", False).stream()):
            doc.reference.set({"resolved": True, "resolved_at": firestore.SERVER_TIMESTAMP}, merge=True)
