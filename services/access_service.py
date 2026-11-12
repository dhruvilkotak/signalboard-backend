"""
AccessService — the single place that decides what a user can use.

    access = access_svc.get_access(uid)
    access["features"]["ai_chat"]   -> bool

Precedence (first match wins):
    1. admin role                          -> everything
    2. pro role   (flag: pro_role)         -> every user feature
    3. paid subscription (active/trialing) -> every user feature
    4. coupon_until in the future          -> every user feature
    5. trial_ends_at in the future         -> ai_signals, ai_chat, autotrader
    6. free                                -> 5 lifetime on-demand signals

Each feature is only gated while its flag is on for that user:
    ondemand                          gated by flag `paywall`
    ai_signals / ai_chat / autotrader gated by flag `trial`
With both flags off the user gets plan "legacy" = everything, i.e. today's
behaviour. That is what makes it safe to ship before launch.

Role source of truth: Firebase custom claim `role`, read server-side with
firebase_admin.auth.get_user (cached 5 min), so a role change needs no client
token refresh. `role` is mirrored to users/{uid}.role only for the admin list.

Wiring (main.py), same pattern as the other services:
    access_svc = AccessService(flag_svc)   # module level
    access_svc.set_db(db)                  # in lifespan(), after init_firebase()
    access_deps.access_svc = access_svc    # middleware/access.py
    access_router.access_svc = access_svc  # routers/access.py
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from firebase_admin import auth as fb_auth
from firebase_admin import firestore

logger = logging.getLogger(__name__)

FEATURES: tuple[str, ...] = ("ondemand", "ai_signals", "ai_chat", "autotrader")
TRIAL_FEATURES: tuple[str, ...] = ("ai_signals", "ai_chat", "autotrader")
FREE_ONDEMAND_LIMIT = 5
ASSIGNABLE_ROLES: tuple[str, ...] = ("user", "pro")      # admin is never set via API
ACTIVE_SUB_STATUSES = ("active", "trialing")

# Kept as a fallback so the admin never locks themself out before the claim exists.
ADMIN_UID = os.environ.get("ADMIN_UID", "2fKDBFccZVOwlyaU6QIs4rQTNKb2")

ROLE_TTL_SECONDS = 300
ACCESS_TTL_SECONDS = 30


def _as_utc(value) -> Optional[datetime]:
    """Firestore timestamps come back as tz-aware datetimes; accept ISO strings too."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _iso_dict(d) -> Optional[dict]:
    if not isinstance(d, dict):
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}


def _default_role_lookup(uid: str) -> Optional[str]:
    claims = fb_auth.get_user(uid).custom_claims or {}
    return claims.get("role")


class AccessService:
    def __init__(
        self,
        flag_svc,
        db=None,
        role_lookup: Callable[[str], Optional[str]] = _default_role_lookup,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._flags = flag_svc
        self._db = db
        self._role_lookup = role_lookup
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._roles: dict[str, tuple[float, str]] = {}
        self._access: dict[str, tuple[float, dict]] = {}

    def set_db(self, db) -> None:
        self._db = db
        self.invalidate()

    def invalidate(self, uid: Optional[str] = None) -> None:
        with self._lock:
            if uid is None:
                self._roles.clear()
                self._access.clear()
            else:
                self._roles.pop(uid, None)
                self._access.pop(uid, None)

    # ── role ─────────────────────────────────────────────────────────────

    def get_role(self, uid: str) -> str:
        if uid == ADMIN_UID:
            return "admin"
        now = self._clock()
        with self._lock:
            hit = self._roles.get(uid)
            if hit and now - hit[0] < ROLE_TTL_SECONDS:
                return hit[1]
        try:
            role = self._role_lookup(uid) or "user"
        except Exception:
            logger.exception("role_lookup_failed uid=%s", uid)
            return "user"            # fail closed, don't cache the failure
        if role not in ("admin", "pro", "user"):
            role = "user"
        with self._lock:
            self._roles[uid] = (now, role)
        return role

    def set_role(self, uid: str, role: str, actor_uid: str) -> str:
        if role not in ASSIGNABLE_ROLES:
            raise ValueError(f"role must be one of {ASSIGNABLE_ROLES}")
        if uid == ADMIN_UID:
            raise ValueError("cannot change the admin's role")

        user = fb_auth.get_user(uid)                       # raises UserNotFoundError
        claims = dict(user.custom_claims or {})
        before = claims.get("role", "user")
        if role == "user":
            claims.pop("role", None)
        else:
            claims["role"] = role
        fb_auth.set_custom_user_claims(uid, claims or None)

        if self._db is not None:
            self._db.collection("users").document(uid).set(
                {"role": role, "role_updated_at": firestore.SERVER_TIMESTAMP, "role_updated_by": actor_uid},
                merge=True,
            )
        self.invalidate(uid)
        logger.info(
            "role_changed",
            extra={"event": "role_changed", "uid": uid, "from_role": before, "to_role": role, "actor_uid": actor_uid},
        )
        # TODO(B7 #54): when pro -> user, call AccessExpiryService.handle(uid, "pro_removed").
        return role

    # ── per-user docs ────────────────────────────────────────────────────

    def _load_docs(self, uid: str) -> tuple[dict, dict, dict]:
        if self._db is None:
            return {}, {}, {}
        base = self._db.collection("users").document(uid).collection("data")
        refs = [base.document("access"), base.document("subscription"), base.document("usage")]
        out = {}
        for snap in self._db.get_all(refs):
            out[snap.id] = (snap.to_dict() or {}) if snap.exists else {}
        return out.get("access", {}), out.get("subscription", {}), out.get("usage", {})

    # ── decision ─────────────────────────────────────────────────────────

    def get_access(self, uid: str, use_cache: bool = True) -> dict:
        now_m = self._clock()
        if use_cache:
            with self._lock:
                hit = self._access.get(uid)
                if hit and now_m - hit[0] < ACCESS_TTL_SECONDS:
                    return hit[1]

        result = self._compute(uid)
        with self._lock:
            self._access[uid] = (now_m, result)
        return result

    def _compute(self, uid: str) -> dict:
        now = self._now()
        role = self.get_role(uid)
        paywall_on = self._flags.is_enabled("paywall", uid)
        trial_on = self._flags.is_enabled("trial", uid)

        try:
            access_doc, sub_doc, usage_doc = self._load_docs(uid)
        except Exception:
            logger.exception("access_docs_load_failed uid=%s", uid)
            access_doc, sub_doc, usage_doc = {}, {}, {}

        trial_ends_at = _as_utc(access_doc.get("trial_ends_at"))
        coupon_until = _as_utc(access_doc.get("coupon_until"))
        sub_status = sub_doc.get("status")
        used = int(usage_doc.get("ondemand_used", 0) or 0)

        liquidation = {
            "pending": bool(access_doc.get("pending_liquidation")),
            "last": _iso_dict(access_doc.get("last_liquidation")),
        }

        def full(plan: str) -> dict:
            return self._result(plan, role, {f: True for f in FEATURES}, None, used,
                                trial_ends_at, coupon_until, sub_status, liquidation)

        if role == "admin":
            return full("admin")
        if role == "pro" and self._flags.is_enabled("pro_role", uid):
            return full("pro")
        if sub_status in ACTIVE_SUB_STATUSES:
            return full("paid")
        if coupon_until and coupon_until > now:
            return full("coupon")

        trial_active = bool(trial_ends_at and trial_ends_at > now)
        features = {f: (not trial_on) or trial_active for f in TRIAL_FEATURES}
        features["ondemand"] = (not paywall_on) or used < FREE_ONDEMAND_LIMIT

        if trial_on and trial_active:
            plan = "trial"
        elif paywall_on or trial_on:
            plan = "free"
        else:
            plan = "legacy"

        limit = FREE_ONDEMAND_LIMIT if paywall_on else None
        return self._result(plan, role, features, limit, used, trial_ends_at, coupon_until, sub_status, liquidation)

    @staticmethod
    def _result(plan, role, features, limit, used, trial_ends_at, coupon_until, sub_status,
                liquidation=None) -> dict:
        return {
            "plan": plan,                      # admin | pro | paid | coupon | trial | free | legacy
            "role": role,
            "features": features,
            "ondemand": {
                "used": used,
                "limit": limit,                # None = unlimited
                "left": None if limit is None else max(limit - used, 0),
            },
            "trial_ends_at": _iso(trial_ends_at),
            "coupon_until": _iso(coupon_until),
            "subscription_status": sub_status,
            "liquidation": liquidation or {"pending": False, "last": None},   # Auto-Trader access-expiry state
        }
