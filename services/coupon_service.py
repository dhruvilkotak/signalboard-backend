"""
CouponService — admin-created codes that grant full access for N days, no card.

Firestore (backend-only; firestore.rules denies all client access):
    coupons/{CODE}                    duration_days, max_redemptions, redeemed_count,
                                      expires_at, active, note, created_by, created_at
    coupons/{CODE}/redemptions/{uid}  redeemed_at, coupon_until
    users/{uid}/data/access           coupon_until  (read by AccessService)

Redeeming extends from whichever is later, now or the current coupon_until, so
stacking two codes adds their days. One redemption per user per code.
Gated by flag `coupons` (redeem only; admin management always works).

Wiring (main.py):
    coupon_svc = CouponService(access_svc)
    coupon_svc.set_db(db)                     # lifespan()
    coupons_router.coupon_svc = coupon_svc
    coupons_router.flag_svc   = flag_svc
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from firebase_admin import firestore

logger = logging.getLogger(__name__)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,23}$")
MAX_DURATION_DAYS = 365
MAX_REDEMPTIONS = 1000
REDEEM_ATTEMPTS_PER_HOUR = 5


class CouponError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def normalize(code: str) -> str:
    return re.sub(r"\s+", "", (code or "")).upper()


def generate_code() -> str:
    part = lambda: "".join(secrets.choice(ALPHABET) for _ in range(4))
    return f"SBC-{part()}-{part()}"


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def decide_redeem(coupon: Optional[dict], already_redeemed: bool, current_until, now: datetime) -> datetime:
    """Pure check. Returns the new coupon_until or raises CouponError."""
    if coupon is None or not coupon.get("active", True):
        raise CouponError("COUPON_INVALID", "That code isn't valid.")
    expires = _as_utc(coupon.get("expires_at"))
    if expires and expires <= now:
        raise CouponError("COUPON_EXPIRED", "That code has expired.")
    if already_redeemed:
        raise CouponError("COUPON_ALREADY_REDEEMED", "You've already used this code.")
    max_r = coupon.get("max_redemptions")
    if max_r is not None and int(coupon.get("redeemed_count", 0)) >= int(max_r):
        raise CouponError("COUPON_USED_UP", "That code has been fully used.")
    days = int(coupon.get("duration_days", 30))
    start = max(now, _as_utc(current_until) or now)
    return start + timedelta(days=days)


class CouponService:
    def __init__(self, access_svc, db=None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 clock: Callable[[], float] = time.monotonic):
        self._access = access_svc
        self._db = db
        self._now = now
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def set_db(self, db) -> None:
        self._db = db

    def _need_db(self):
        if self._db is None:
            raise CouponError("COUPONS_UNAVAILABLE", "Coupons are unavailable right now.", 503)

    def _coupon_ref(self, code: str):
        return self._db.collection("coupons").document(code)

    # ── rate limit (per process; single VM) ──────────────────────────────

    def _check_rate(self, uid: str) -> None:
        now = self._clock()
        with self._lock:
            recent = [t for t in self._attempts.get(uid, []) if now - t < 3600]
            if len(recent) >= REDEEM_ATTEMPTS_PER_HOUR:
                self._attempts[uid] = recent
                raise CouponError("COUPON_RATE_LIMITED", "Too many attempts. Try again in an hour.", 429)
            recent.append(now)
            self._attempts[uid] = recent

    # ── redeem ───────────────────────────────────────────────────────────

    def redeem(self, uid: str, raw_code: str) -> dict:
        self._need_db()
        code = normalize(raw_code)
        self._check_rate(uid)
        if not CODE_RE.match(code):
            raise CouponError("COUPON_INVALID", "That code isn't valid.")

        coupon_ref = self._coupon_ref(code)
        redemption_ref = coupon_ref.collection("redemptions").document(uid)
        access_ref = self._db.collection("users").document(uid).collection("data").document("access")
        now = self._now()

        @firestore.transactional
        def _txn(transaction):
            c = coupon_ref.get(transaction=transaction)
            r = redemption_ref.get(transaction=transaction)
            a = access_ref.get(transaction=transaction)
            coupon = c.to_dict() if c.exists else None
            current_until = (a.to_dict() or {}).get("coupon_until") if a.exists else None
            new_until = decide_redeem(coupon, r.exists, current_until, now)
            transaction.update(coupon_ref, {"redeemed_count": firestore.Increment(1)})
            transaction.set(redemption_ref, {"redeemed_at": firestore.SERVER_TIMESTAMP, "coupon_until": new_until})
            transaction.set(access_ref, {"coupon_until": new_until, "coupon_code": code}, merge=True)
            return new_until, int(coupon.get("duration_days", 30))

        new_until, days = _txn(self._db.transaction())
        self._access.invalidate(uid)
        logger.info("coupon_redeemed", extra={"event": "coupon_redeemed", "uid": uid, "code": code,
                                               "days": days, "coupon_until": new_until.isoformat()})
        return {"code": code, "days": days, "coupon_until": new_until.isoformat()}

    # ── admin ────────────────────────────────────────────────────────────

    def create(self, actor_uid: str, duration_days: int = 30, max_redemptions: Optional[int] = 1,
               expires_in_days: Optional[int] = 30, note: str = "", code: Optional[str] = None) -> dict:
        self._need_db()
        if not 1 <= int(duration_days) <= MAX_DURATION_DAYS:
            raise CouponError("INVALID_DURATION", f"duration_days must be 1–{MAX_DURATION_DAYS}.")
        if max_redemptions is not None and not 1 <= int(max_redemptions) <= MAX_REDEMPTIONS:
            raise CouponError("INVALID_MAX", f"max_redemptions must be 1–{MAX_REDEMPTIONS}, or empty for unlimited.")
        if expires_in_days is not None and not 1 <= int(expires_in_days) <= MAX_DURATION_DAYS:
            raise CouponError("INVALID_EXPIRY", f"expires_in_days must be 1–{MAX_DURATION_DAYS}, or empty for never.")

        if code:
            code = normalize(code)
            if not CODE_RE.match(code):
                raise CouponError("INVALID_CODE", "Code must be 4–24 letters, digits or dashes.")
            if self._coupon_ref(code).get().exists:
                raise CouponError("CODE_EXISTS", f"{code} already exists.", 409)
        else:
            for _ in range(5):
                code = generate_code()
                if not self._coupon_ref(code).get().exists:
                    break

        now = self._now()
        doc = {
            "duration_days": int(duration_days),
            "max_redemptions": int(max_redemptions) if max_redemptions is not None else None,
            "redeemed_count": 0,
            "expires_at": now + timedelta(days=int(expires_in_days)) if expires_in_days is not None else None,
            "active": True,
            "note": (note or "").strip()[:200],
            "created_by": actor_uid,
            "created_at": firestore.SERVER_TIMESTAMP,
        }
        self._coupon_ref(code).set(doc)
        logger.info("coupon_created", extra={"event": "coupon_created", "code": code, "actor_uid": actor_uid,
                                              "duration_days": doc["duration_days"],
                                              "max_redemptions": doc["max_redemptions"]})
        return self._public(code, {**doc, "created_at": now})

    def list(self) -> list[dict]:
        self._need_db()
        out = [self._public(d.id, d.to_dict() or {}) for d in self._db.collection("coupons").stream()]
        out.sort(key=lambda c: c.get("created_at") or "", reverse=True)
        return out

    def deactivate(self, code: str, actor_uid: str) -> dict:
        self._need_db()
        code = normalize(code)
        ref = self._coupon_ref(code)
        snap = ref.get()
        if not snap.exists:
            raise CouponError("COUPON_NOT_FOUND", f"{code} not found.", 404)
        ref.update({"active": False, "deactivated_by": actor_uid, "deactivated_at": firestore.SERVER_TIMESTAMP})
        logger.info("coupon_deactivated", extra={"event": "coupon_deactivated", "code": code, "actor_uid": actor_uid})
        return self._public(code, {**(snap.to_dict() or {}), "active": False})

    def _public(self, code: str, d: dict) -> dict:
        iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v
        expires = _as_utc(d.get("expires_at"))
        status = ("inactive" if not d.get("active", True)
                  else "expired" if expires and expires <= self._now()
                  else "used_up" if d.get("max_redemptions") is not None
                       and int(d.get("redeemed_count", 0)) >= int(d["max_redemptions"])
                  else "active")
        return {
            "code": code,
            "duration_days": d.get("duration_days"),
            "max_redemptions": d.get("max_redemptions"),
            "redeemed_count": int(d.get("redeemed_count", 0)),
            "expires_at": iso(d.get("expires_at")),
            "note": d.get("note", ""),
            "created_at": iso(d.get("created_at")),
            "status": status,
        }
