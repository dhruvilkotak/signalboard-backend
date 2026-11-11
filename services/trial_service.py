"""
Trial start — one 7-day trial per user, ever.

    start_trial(db, uid)  -> datetime the trial ends (existing value if already started)

Writes users/{uid}/data/access.trial_ends_at (server-only; see firestore.rules).
Called from admin.approve_user, and by scripts/backfill_trials.py at launch.
Starting a trial has no visible effect until the `trial` flag is on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from firebase_admin import firestore

TRIAL_DAYS = 7


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


def access_ref(db, uid: str):
    return db.collection("users").document(uid).collection("data").document("access")


def start_trial(db, uid: str, days: int = TRIAL_DAYS, now: Optional[datetime] = None,
                source: str = "approval") -> datetime:
    """Idempotent: if the user already has trial_ends_at, it is returned unchanged."""
    ref = access_ref(db, uid)
    snap = ref.get()
    existing = _as_utc((snap.to_dict() or {}).get("trial_ends_at")) if snap.exists else None
    if existing:
        return existing

    ends_at = (now or datetime.now(timezone.utc)) + timedelta(days=days)
    ref.set(
        {"trial_ends_at": ends_at, "trial_started_at": firestore.SERVER_TIMESTAMP, "trial_source": source},
        merge=True,
    )
    return ends_at
