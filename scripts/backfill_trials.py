"""
Launch-day backfill: give every approved user without a trial a 7-day trial
starting now. Skips admins and Pro users. Safe to re-run (never extends or
restarts an existing trial).

    cd /home/kotakdhruvil/signalboard-backend && source venv/bin/activate
    python scripts/backfill_trials.py            # dry run: prints who would get a trial
    python scripts/backfill_trials.py --apply    # writes

Run it right before turning the `trial` flag on.
"""

import os
import sys

import firebase_admin
from firebase_admin import auth, credentials, firestore

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.trial_service import access_ref, start_trial  # noqa: E402

APPLY = "--apply" in sys.argv
CRED_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS", "/home/kotakdhruvil/signalboard-backend/firebase-credentials.json"
)

cred = credentials.Certificate(CRED_PATH)
firebase_admin.get_app() if firebase_admin._apps else firebase_admin.initialize_app(cred)
db = firestore.client()

started, skipped = 0, 0
for doc in db.collection("users").where("status", "==", "approved").stream():
    uid, data = doc.id, doc.to_dict() or {}
    email = data.get("email", uid)
    try:
        role = (auth.get_user(uid).custom_claims or {}).get("role", "user")
    except Exception:
        role = data.get("role", "user")
    if role in ("admin", "pro"):
        print(f"skip  {email:40s} role={role}")
        skipped += 1
        continue
    snap = access_ref(db, uid).get()
    if snap.exists and (snap.to_dict() or {}).get("trial_ends_at"):
        print(f"skip  {email:40s} already has trial_ends_at")
        skipped += 1
        continue
    if APPLY:
        ends = start_trial(db, uid, source="launch_backfill")
        print(f"start {email:40s} ends {ends.isoformat()}")
    else:
        print(f"would start  {email}")
    started += 1

print(f"\n{'started' if APPLY else 'would start'}: {started}   skipped: {skipped}")
if not APPLY:
    print("Dry run only. Re-run with --apply to write.")
