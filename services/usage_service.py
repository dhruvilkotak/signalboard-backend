"""
UsageService — on-demand signal quota.

Rules (only while flag `paywall` is on for the user; otherwise nothing is counted):
    admin                -> unlimited, never counted
    free / trial         -> 5 new on-demand signals in total, never reset
                            -> 402 PAYWALL when used up (upgrade prompt)
    pro / paid / coupon  -> 5 new on-demand signals per week; resets every
                            Monday 00:00 America/New_York
                            -> 429 WEEKLY_LIMIT when used up (no upsell)
Re-opening a symbol whose signal is still current is free: the quota counts
distinct signals, not page loads.

Flow per request (routers/ondemand.py):
    res = usage_svc.reserve(uid, symbol)     # atomic; raises QuotaExceeded
    signal = await ondemand_svc.get_signal(symbol)
    failed  -> usage_svc.release(uid, res)   # give the slot back
    success -> usage_svc.confirm(uid, res, signal["expires_at"])

Firestore: users/{uid}/data/usage (server-write only; see firestore.rules)
    ondemand_used        lifetime count (free / trial)
    ondemand_week        "YYYY-MM-DD" of the Monday (ET) the weekly count belongs to
    ondemand_week_count  signals counted that week (pro / paid / coupon)
    ondemand_symbols     {SYMBOL: expires_at ISO} — free re-views until expiry

Wiring (main.py):
    usage_svc = UsageService(access_svc, flag_svc)
    usage_svc.set_db(db)                     # lifespan()
    ondemand.usage_svc = usage_svc
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from firebase_admin import firestore

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
FREE_LIFETIME_LIMIT = 5
WEEKLY_LIMIT = 5
FULL_PLANS = ("pro", "paid", "coupon")


def week_key(now: datetime) -> str:
    """Monday (ET) of the week containing `now`, as YYYY-MM-DD."""
    d = now.astimezone(ET).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def week_resets_at(now: datetime) -> datetime:
    """Next Monday 00:00 ET, in UTC."""
    d = now.astimezone(ET).date()
    monday = d - timedelta(days=d.weekday()) + timedelta(days=7)
    return datetime(monday.year, monday.month, monday.day, tzinfo=ET).astimezone(timezone.utc)


def week_used(usage_doc: dict, now: datetime) -> int:
    doc = usage_doc or {}
    return int(doc.get("ondemand_week_count", 0) or 0) if doc.get("ondemand_week") == week_key(now) else 0


class QuotaExceeded(Exception):
    def __init__(self, status: int, detail: dict):
        super().__init__(detail.get("code"))
        self.status = status
        self.detail = detail


@dataclass
class Reservation:
    symbol: str
    kind: str                      # "none" | "lifetime" | "weekly"
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


def decide(data: dict, kind: str, symbol: str, now: datetime, plan: str = "free") -> tuple[dict, Reservation]:
    """
    Pure decision: given the current usage doc, return (new_doc, reservation)
    or raise QuotaExceeded. No I/O — this is what the tests cover.
    kind: "lifetime" (free/trial), "weekly" (pro/paid/coupon) or "none".
    """
    doc = dict(data or {})
    symbols = {s: e for s, e in (doc.get("ondemand_symbols") or {}).items()
               if (_parse(e) or now) > now}                     # prune expired
    doc["ondemand_symbols"] = symbols

    if kind == "lifetime":
        used = int(doc.get("ondemand_used", 0) or 0)
        quota = lambda u: {"used": u, "limit": FREE_LIFETIME_LIMIT,
                           "left": max(FREE_LIFETIME_LIMIT - u, 0), "period": "total", "resets_at": None}
        if symbol in symbols:
            return doc, Reservation(symbol, kind, counted=False, quota=quota(used))
        if used >= FREE_LIFETIME_LIMIT:
            raise QuotaExceeded(402, {"code": "PAYWALL", "feature": "ondemand", "ondemand": quota(used)})
        doc["ondemand_used"] = used + 1
        symbols[symbol] = "pending"
        return doc, Reservation(symbol, kind, counted=True, quota=quota(used + 1))

    if kind == "weekly":
        week = week_key(now)
        used = week_used(doc, now)
        resets = week_resets_at(now).isoformat()
        quota = lambda u: {"used": u, "limit": WEEKLY_LIMIT, "left": max(WEEKLY_LIMIT - u, 0),
                           "period": "week", "resets_at": resets}
        if symbol in symbols:
            return doc, Reservation(symbol, kind, counted=False, quota=quota(used))
        if used >= WEEKLY_LIMIT:
            raise QuotaExceeded(429, {"code": "WEEKLY_LIMIT", "feature": "ondemand", "ondemand": quota(used)})
        doc["ondemand_week"] = week
        doc["ondemand_week_count"] = used + 1
        symbols[symbol] = "pending"
        return doc, Reservation(symbol, kind, counted=True, quota=quota(used + 1))

    return doc, Reservation(symbol, "none", counted=False)


def peek_decision(data: dict, kind: str, symbol: str, now: datetime) -> dict:
    """
    Read-only preview of what opening `symbol` would cost, without writing.
    status: "unlimited" (never counted) | "free" (already viewed, still current)
            | "uses_one" (would use 1 slot) | "blocked" (no slots left).
    """
    if kind == "none":
        return {"symbol": symbol, "status": "unlimited", "quota": None, "code": None}
    try:
        _, res = decide(data, kind, symbol, now)
    except QuotaExceeded as e:
        return {"symbol": symbol, "status": "blocked", "quota": e.detail["ondemand"], "code": e.detail["code"]}
    if not res.counted:
        return {"symbol": symbol, "status": "free", "quota": res.quota, "code": None}
    q = dict(res.quota)                       # decide() returned the post-use count; report the current one
    q["used"] -= 1
    q["left"] = min(q["left"] + 1, q["limit"])
    return {"symbol": symbol, "status": "uses_one", "quota": q, "code": None}


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
        if access["plan"] == "admin" or access.get("role") == "admin":
            return "none", access
        if access["plan"] in FULL_PLANS:
            return "weekly", access
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
            res = self._transact(uid, lambda doc: decide(doc, kind, symbol, self._now(), access.get("plan", "free")))
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

    def peek(self, uid: str, symbol: str) -> dict:
        """What would GET /api/ondemand/signal/{symbol} cost this user? No writes."""
        symbol = symbol.upper().strip()
        kind, _ = self._kind_for(uid)
        if kind == "none" or self._db is None:
            return peek_decision({}, "none", symbol, self._now())
        snap = self._ref(uid).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        return peek_decision(data, kind, symbol, self._now())

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
            elif res.kind == "weekly" and doc.get("ondemand_week") == week_key(self._now()):
                doc["ondemand_week_count"] = max(int(doc.get("ondemand_week_count", 0) or 0) - 1, 0)
            return doc, None

        self._transact(uid, fn)
        self._access.invalidate(uid)
