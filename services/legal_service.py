"""
LegalService — versioned Terms of Use & Privacy Policy acceptance.

Firestore:
    config/legal                       {terms_version, summary_of_changes, published_at, published_by}
    users/{uid}/data/consent           {terms_version, accepted_at, platform}       (backend-written)
    users/{uid}/consent_log/{auto}     add-only history: version, time, platform, user agent
    users/{uid}                        agreed_terms_version / agreed_terms_at kept in sync
                                       (signup still writes these client-side for v1.0)

Accepted version = consent doc if present, else users/{uid}.agreed_terms_version,
so everyone who signed up under v1.0 is already covered.

Wiring (main.py), same pattern as the other services:
    legal_svc = LegalService()          # module level
    legal_svc.set_db(db)                # in lifespan(), after init_firebase()
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

from firebase_admin import firestore

logger = logging.getLogger(__name__)

DEFAULT_TERMS_VERSION = "v1.0"
CONFIG_TTL_SECONDS = 60
STATUS_TTL_SECONDS = 60
VERSION_RE = re.compile(r"^v\d+(\.\d+){0,2}$")      # v1, v1.1, v2.0.1


class TermsVersionMismatch(Exception):
    """User accepted text that is not the currently published version."""


class LegalService:
    def __init__(self, db=None, clock: Callable[[], float] = time.monotonic):
        self._db = db
        self._clock = clock
        self._lock = threading.Lock()
        self._config: Optional[tuple[float, dict]] = None
        self._status: dict[str, tuple[float, dict]] = {}

    def set_db(self, db) -> None:
        self._db = db
        self.invalidate()

    def invalidate(self, uid: Optional[str] = None) -> None:
        with self._lock:
            if uid is None:
                self._config = None
                self._status.clear()
            else:
                self._status.pop(uid, None)

    # ── current published version ────────────────────────────────────────

    def _legal_doc(self):
        return self._db.collection("config").document("legal")

    def get_current(self) -> dict:
        default = {"terms_version": DEFAULT_TERMS_VERSION, "summary_of_changes": "", "published_at": None}
        if self._db is None:
            return default
        now = self._clock()
        with self._lock:
            if self._config and now - self._config[0] < CONFIG_TTL_SECONDS:
                return self._config[1]
        try:
            snap = self._legal_doc().get()
            data = (snap.to_dict() or {}) if snap.exists else {}
        except Exception:
            logger.exception("legal_config_load_failed")
            return self._config[1] if self._config else default
        current = {
            "terms_version": data.get("terms_version") or DEFAULT_TERMS_VERSION,
            "summary_of_changes": data.get("summary_of_changes") or "",
            "published_at": _iso(data.get("published_at")),
        }
        with self._lock:
            self._config = (now, current)
        return current

    # ── per-user status ──────────────────────────────────────────────────

    def get_accepted(self, uid: str) -> Optional[str]:
        if self._db is None:
            return None
        user_ref = self._db.collection("users").document(uid)
        consent = user_ref.collection("data").document("consent").get()
        if consent.exists and (consent.to_dict() or {}).get("terms_version"):
            return consent.to_dict()["terms_version"]
        user = user_ref.get()
        if user.exists:
            return (user.to_dict() or {}).get("agreed_terms_version")
        return None

    def status(self, uid: str, use_cache: bool = True) -> dict:
        now = self._clock()
        if use_cache:
            with self._lock:
                hit = self._status.get(uid)
                if hit and now - hit[0] < STATUS_TTL_SECONDS:
                    return hit[1]
        current = self.get_current()
        try:
            accepted = self.get_accepted(uid)
        except Exception:
            logger.exception("consent_load_failed uid=%s", uid)
            # Fail open on a read error: a Firestore blip must not lock everyone out.
            return {**current, "accepted_version": None, "needs_acceptance": False}
        result = {
            **current,
            "accepted_version": accepted,
            "needs_acceptance": accepted != current["terms_version"],
        }
        with self._lock:
            self._status[uid] = (now, result)
        return result

    # ── writes ───────────────────────────────────────────────────────────

    def accept(self, uid: str, version: str, platform: str = "web", user_agent: str = "") -> dict:
        if self._db is None:
            raise RuntimeError("legal service unavailable: Firestore not initialised")
        current = self.get_current()
        if version != current["terms_version"]:
            raise TermsVersionMismatch(f"accepted {version}, current is {current['terms_version']}")

        user_ref = self._db.collection("users").document(uid)
        batch = self._db.batch()
        batch.set(
            user_ref.collection("data").document("consent"),
            {"terms_version": version, "accepted_at": firestore.SERVER_TIMESTAMP, "platform": platform},
        )
        batch.set(
            user_ref.collection("consent_log").document(),
            {
                "terms_version": version,
                "accepted_at": firestore.SERVER_TIMESTAMP,
                "platform": platform,
                "user_agent": (user_agent or "")[:300],
            },
        )
        batch.set(
            user_ref,
            {"agreed_terms": True, "agreed_terms_version": version, "agreed_terms_at": firestore.SERVER_TIMESTAMP},
            merge=True,
        )
        batch.commit()
        self.invalidate(uid)
        logger.info("terms_accepted", extra={"event": "terms_accepted", "uid": uid, "terms_version": version, "platform": platform})
        return {"terms_version": version}

    def publish(self, version: str, summary: str, actor_uid: str) -> dict:
        if self._db is None:
            raise RuntimeError("legal service unavailable: Firestore not initialised")
        version = (version or "").strip()
        if not VERSION_RE.match(version):
            raise ValueError("version must look like v1.1 or v2.0")
        current = self.get_current()
        if version == current["terms_version"]:
            raise ValueError(f"{version} is already the current version")
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary_of_changes is required")
        self._legal_doc().set(
            {
                "terms_version": version,
                "summary_of_changes": summary[:2000],
                "published_at": firestore.SERVER_TIMESTAMP,
                "published_by": actor_uid,
                "previous_version": current["terms_version"],
            }
        )
        self.invalidate()
        logger.info(
            "terms_published",
            extra={"event": "terms_published", "from_version": current["terms_version"], "to_version": version, "actor_uid": actor_uid},
        )
        return {"terms_version": version, "summary_of_changes": summary}


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value
