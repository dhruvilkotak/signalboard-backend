"""
One-off: put role=admin on the admin's Firebase custom claims and mirror it
to users/{uid}.role. Safe to re-run.

    cd /home/kotakdhruvil/signalboard-backend && source venv/bin/activate
    python scripts/seed_admin_role.py
"""

import os

import firebase_admin
from firebase_admin import auth, credentials, firestore

ADMIN_UID = os.environ.get("ADMIN_UID", "2fKDBFccZVOwlyaU6QIs4rQTNKb2")
CRED_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS", "/home/kotakdhruvil/signalboard-backend/firebase-credentials.json"
)

cred = credentials.Certificate(CRED_PATH)
firebase_admin.get_app() if firebase_admin._apps else firebase_admin.initialize_app(cred)

user = auth.get_user(ADMIN_UID)
claims = dict(user.custom_claims or {})
if claims.get("role") == "admin":
    print(f"{user.email}: already admin")
else:
    claims["role"] = "admin"
    auth.set_custom_user_claims(ADMIN_UID, claims)
    print(f"{user.email}: role=admin set")

firestore.client().collection("users").document(ADMIN_UID).set({"role": "admin"}, merge=True)
print("mirrored to users/{uid}.role")
