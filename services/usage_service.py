"""
UsageService — on-demand signal quota.

Rules (only while flag `paywall` is on for the user; otherwise nothing is counted):
    admin                      -> unlimited, never counted
    pro / paid / coupon        -> fair-use cap: 50 new signals per day (ET)
    trial / free               -> 5 lifetime on-demand signals
Re-opening a symbol whose signal is still current is free: the quota counts
distinct signals, not page loads.

Flow per request (routers/ondemand.py):
    res = usage_svc.reserve(uid, symbol)     # atomic; raises QuotaExceeded
    signal = await ondemand_svc.get_signal(symbol)
    failed  -> usage_svc.release(uid, res)   # give the slot back
    success -> usage_svc.confirm(uid, res, signal["expires_at"])

Firestore: users/{uid}/data/usage (server-write only; see firestore.rules)
    ondemand_used     lifetime count (free/trial)
    ondemand_day      "YYYY-MM-DD" (ET) of ondemand_today
    ondemand_today    count for that day (pro/paid/coupon)
    ondemand_symbols  {SYMBOL: expires_at ISO} — free re-views until expiry

Wiring (main.py):
    usage_svc = UsageService(access_svc, flag_svc)
    usage_svc.set_db(db)                     # lifespan()
    ondemand.usage_svc = usage_svc
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from firebase_admin import firestore

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
FREE_LIFETIME_LIMIT = 5
DAILY_FAIR_USE_LIMIT = 50
FULL_PLANS = ("pro", "paid", "coupon")


class QuotaExceeded(Exception):
    def __init__(self, status: int, detail: dict):
        super().__init__(detail.get("code"))
        self.status = status
        self.detail = detail


@dataclass
class Reservation:
    symbol: str
    kind: str                      # "none" | "lifetime" | "daily"
    counted: bool                  # False = free re-view or uncounted plan
    quota: dict = field(default_factory=dict)


def _parse(iso) -> Optional[datetime]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def decide(data: dict, kind: str, symbol: str, now: datetime) -> tuple[dict, Reservation]:
    """
    Pure decision: given the current usage doc, return (new_doc, reservation)
    or raise QuotaExceeded. No I/O — this is what the tests cover.
    """
    doc = dict(data or {})
    symbols = {s: e for s, e in (doc.get("ondemand_symbols") or {}).items()
               if (_parse(e) or now) > now}                     # prune expired
    doc["ondemand_symbols"] = symbols

    if kind == "lifetime":
        used = int(doc.get("ondemand_used", 0) or 0)
        quota = {"used": used, "limit": FREE_LIFETIME_LIMIT, "left": max(FREE_LIFETIME_LIMIT - used, 0)}
        if symbol in symbols:
            return doc, Reservation(symbol, kind, counted=False, quota=quota)
        if used >= FREE_LIFETIME_LIMIT:
            raise QuotaExceeded(402, {"code": "PAYWALL", "feature": "ondemand", "ondemand": quota})
        used += 1
        doc["ondemand_used"] = used
        symbols[symbol] = "pending"
        quota = {"used": used, "limit": FREE_LIFETIME_LIMIT, "left": FREE_LIFETIME_LIMIT - used}
        return doc, Reservation(symbol, kind, counted=True, quota=quota)

    if kind == "daily":
        today = now.astimezone(ET).date().isoformat()
        count = int(doc.get("ondemand_today", 0) or 0) if doc.get("ondemand_day") == today else 0
        quota = {"used_today": count, "daily_limit": DAILY_FAIR_USE_LIMIT}
        if symbol in symbols:
            return doc, Reservation(symbol, kind, counted=False, quota=quota)
        if count >= DAILY_FAIR_USE_LIMIT:
            raise QuotaExceeded(429, {"code": "DAILY_LIMIT", "feature": "ondemand", "daily_limit": DAILY_FAIR_USE_LIMIT})
        doc["ondemand_day"] = today
        doc["ondemand_today"] = count + 1
        symbols[symbol] = "pending"
        return doc, Reservation(symbol, kind, counted=True,
                                quota={"used_today": count + 1, "daily_limit": DAILY_FAIR_USE_LIMIT})

    return doc, Reservation(symbol, "none", counted=False)


class UsageService:
    def __init__(self, access_svc, flag_svc, db=None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._access = access_svc
        self._flags = flag_svc
        self._db = db
        self._now = now

    def set_db(self, db) -> None:
        self._db = db

    def _ref(self, uid: str):
        return self._db.collection("users").document(uid).collection("data").document("usage")

    def _kind_for(self, uid: str) -> tuple[str, dict]:
        if not self._flags.is_enabled("paywall", uid):
            return "none", {}
        access = self._access.get_access(uid, use_cache=False)
        if access["plan"] == "admin":
            return "none", access
        if access["plan"] in FULL_PLANS:
            return "daily", access
        return "lifetime", access

    def _transact(self, uid: str, fn):
        """Run fn(doc) -> (new_doc, result) inside a Firestore transaction."""
        ref = self._ref(uid)

        @firestore.transactional
        def _txn(transaction):
            snap = ref.get(transaction=transaction)
            current = (snap.to_dict() or {}) if snap.exists else {}
            new_doc, result = fn(current)
            if new_doc is not None:
                transaction.set(ref, new_doc)
            return result

        return _txn(self._db.transaction())

    def reserve(self, uid: str, symbol: str) -> Reservation:
        symbol = symbol.upper().strip()
        kind, access = self._kind_for(uid)
        if kind == "none" or self._db is None:
            return Reservation(symbol, "none", counted=False)
        try:
            res = self._transact(uid, lambda doc: decide(doc, kind, symbol, self._now()))
        except QuotaExceeded as e:
            e.detail.setdefault("plan", access.get("plan"))
            e.detail.setdefault("trial_ends_at", access.get("trial_ends_at"))
            logger.info("paywall_hit", extra={"event": "paywall_hit", "uid": uid, "symbol": symbol,
                                               "code": e.detail.get("code"), "plan": access.get("plan")})
            raise
        if res.counted:
            logger.info("ondemand_quota_used", extra={"event": "ondemand_quota_used", "uid": uid,
                                                       "symbol": symbol, "kind": kind, **res.quota})
        return res

    def confirm(self, uid: str, res: Reservation, expires_at: Optional[str]) -> None:
        """Signal delivered: remember its expiry so re-views are free."""
        if res.kind == "none" or self._db is None:
            return

        def fn(doc):
            doc = dict(doc)
            symbols = dict(doc.get("ondemand_symbols") or {})
            symbols[res.symbol] = expires_at or "pending"
            doc["ondemand_symbols"] = symbols
            return doc, None

        self._transact(uid, fn)
        self._access.invalidate(uid)

    def release(self, uid: str, res: Reservation) -> None:
        """Generation failed: give back a counted slot."""
        if not res.counted or self._db is None:
            return

        def fn(doc):
            doc = dict(doc)
            symbols = dict(doc.get("ondemand_symbols") or {})
            symbols.pop(res.symbol, None)
            doc["ondemand_symbols"] = symbols
            if res.kind == "lifetime":
                doc["ondemand_used"] = max(int(doc.get("ondemand_used", 0) or 0) - 1, 0)
            elif res.kind == "daily":
                doc["ondemand_today"] = max(int(doc.get("ondemand_today", 0) or 0) - 1, 0)
            return doc, None

        self._transact(uid, fn)
        self._access.invalidate(uid)
