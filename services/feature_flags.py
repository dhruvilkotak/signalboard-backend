"""
Feature flags stored in Firestore at config/feature_flags.

Document shape (one key per flag):
    {
      "paywall": {"mode": "allowlist", "uids": ["<uid>", ...],
                  "updated_at": <ts>, "updated_by": "<uid>"},
      ...
    }

Modes:
    off        -> nobody sees the feature (current behaviour)
    allowlist  -> only the UIDs in `uids`
    on         -> everyone

Missing flags, unknown modes and Firestore errors all resolve to OFF, so a bad
config can never switch a feature on by accident.

Wiring (main.py): create ONE instance and inject it into routers as a
module-level variable. Never `from main import flag_svc` inside a router.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from firebase_admin import firestore

logger = logging.getLogger(__name__)

FLAG_NAMES: tuple[str, ...] = (
    "pro_role",
    "terms_gate",
    "paywall",
    "trial",
    "billing",
    "coupons",
    "autotrader_liquidation",
)
MODES: tuple[str, ...] = ("off", "allowlist", "on")
CACHE_TTL_SECONDS = 60
MAX_ALLOWLIST = 50

_OFF = {"mode": "off", "uids": []}


def _normalize(raw) -> dict:
    """Coerce whatever is stored for one flag into {"mode", "uids"}."""
    if not isinstance(raw, dict):
        return dict(_OFF)
    mode = raw.get("mode")
    if mode not in MODES:
        mode = "off"
    uids = raw.get("uids") or []
    if not isinstance(uids, list):
        uids = []
    uids = [u for u in uids if isinstance(u, str) and u]
    return {"mode": mode, "uids": uids}


class FeatureFlagService:
    def __init__(
        self,
        db=None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._db = db if db is not None else firestore.client()
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._loaded_at = 0.0

    # ── reads ────────────────────────────────────────────────────────────

    def _doc(self):
        return self._db.collection("config").document("feature_flags")

    def _load(self) -> dict:
        now = self._clock()
        with self._lock:
            if self._cache is not None and now - self._loaded_at < self._ttl:
                return self._cache
            stale = self._cache

        try:
            snap = self._doc().get()
            raw = (snap.to_dict() or {}) if snap.exists else {}
        except Exception:
            logger.exception("feature_flags_load_failed")
            # Keep serving the last good copy; with none, everything is off.
            return stale if stale is not None else {n: dict(_OFF) for n in FLAG_NAMES}

        flags = {name: _normalize(raw.get(name)) for name in FLAG_NAMES}
        with self._lock:
            self._cache = flags
            self._loaded_at = now
        return flags

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._loaded_at = 0.0

    def is_enabled(self, name: str, uid: Optional[str] = None) -> bool:
        if name not in FLAG_NAMES:
            logger.warning("feature_flag_unknown name=%s", name)
            return False
        flag = self._load()[name]
        if flag["mode"] == "on":
            return True
        if flag["mode"] == "allowlist":
            return bool(uid) and uid in flag["uids"]
        return False

    def resolve_for(self, uid: Optional[str]) -> dict[str, bool]:
        """{flag_name: bool} for one user. Safe to send to the client."""
        return {name: self.is_enabled(name, uid) for name in FLAG_NAMES}

    def resolve_public(self) -> dict[str, bool]:
        """Flags for signed-out pages (signup): only fully-on flags count."""
        flags = self._load()
        return {name: flags[name]["mode"] == "on" for name in FLAG_NAMES}

    def get_all(self) -> dict[str, dict]:
        """Raw modes + allowlists, for the admin panel only."""
        return {name: dict(cfg) for name, cfg in self._load().items()}

    # ── writes (admin) ───────────────────────────────────────────────────

    def set_flag(self, name: str, mode: str, uids: list[str], actor_uid: str) -> dict:
        if name not in FLAG_NAMES:
            raise ValueError(f"unknown flag: {name}")
        if mode not in MODES:
            raise ValueError(f"invalid mode: {mode}")
        clean = sorted({u.strip() for u in (uids or []) if isinstance(u, str) and u.strip()})
        if len(clean) > MAX_ALLOWLIST:
            raise ValueError(f"allowlist limited to {MAX_ALLOWLIST} uids")

        before = self.get_all().get(name, dict(_OFF))
        self._doc().set(
            {
                name: {
                    "mode": mode,
                    "uids": clean,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                    "updated_by": actor_uid,
                }
            },
            merge=True,
        )
        self.invalidate()

        # Structured event for the GCP dashboards. If your utils.logging_setup
        # exposes log_event(event, **fields), swap this for that call.
        logger.info(
            "feature_flag_changed",
            extra={
                "event": "feature_flag_changed",
                "flag": name,
                "from_mode": before["mode"],
                "to_mode": mode,
                "allowlist_size": len(clean),
                "actor_uid": actor_uid,
            },
        )
        return {"mode": mode, "uids": clean}
