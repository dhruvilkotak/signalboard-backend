"""
One-off: create config/feature_flags with every flag OFF.
Safe to re-run: existing flags are left untouched, only missing ones are added.

    cd /home/kotakdhruvil/signalboard-backend && source venv/bin/activate
    python scripts/seed_feature_flags.py
"""

import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.feature_flags import FLAG_NAMES  # noqa: E402

CRED_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS", "/home/kotakdhruvil/signalboard-backend/firebase-credentials.json"
)

cred = credentials.Certificate(CRED_PATH)
firebase_admin.get_app() if firebase_admin._apps else firebase_admin.initialize_app(cred)
db = firestore.client()

ref = db.collection("config").document("feature_flags")
snap = ref.get()
existing = (snap.to_dict() or {}) if snap.exists else {}

missing = {
    name: {"mode": "off", "uids": [], "updated_at": firestore.SERVER_TIMESTAMP, "updated_by": "seed"}
    for name in FLAG_NAMES
    if name not in existing
}

if missing:
    ref.set(missing, merge=True)
    print("added (off):", ", ".join(sorted(missing)))
else:
    print("all flags already present; nothing changed")
