from fastapi import Depends, HTTPException
from middleware.auth import get_current_user
from services.firebase_service import get_db


def require_admin(user=Depends(get_current_user)):
    db = get_db()

    if not db:
        raise HTTPException(500, "Database not available")

    try:
        doc = db.collection("admins").document(user["uid"]).get()

        if not doc.exists:
            raise HTTPException(403, "Admin access required")

        return user

    except HTTPException:
        raise

    except Exception:
        raise HTTPException(500, "Admin verification failed")