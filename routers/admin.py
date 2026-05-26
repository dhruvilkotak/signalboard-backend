# routers/admin.py
# Admin endpoints — approve/reject users, send emails via Resend

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from middleware.auth import get_current_user
from services.email_service import send_approval_email, send_rejection_email
from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# Admin UIDs — hardcoded for security
ADMIN_UIDS = {"2fKDBFccZVOwlyaU6QIs4rQTNKb2"}


def require_admin(user=Depends(get_current_user)):
    if user["uid"] not in ADMIN_UIDS:
        raise HTTPException(403, "Admin access required")
    return user


class ApproveRequest(BaseModel):
    uid:   str
    email: str
    name:  str


@router.post("/approve")
async def approve_user(req: ApproveRequest, admin=Depends(require_admin)):
    """Approve a pending user and send approval email."""
    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")

    try:
        from firebase_admin import firestore
        db.collection("users").document(req.uid).update({
            "status":      "approved",
            "approved_at": firestore.SERVER_TIMESTAMP,
            "approved_by": admin["uid"],
        })
    except Exception as e:
        logger.error(f"Firestore update failed for {req.uid}: {e}")
        raise HTTPException(500, f"Failed to update user: {e}")

    # Send approval email
    email_sent = await send_approval_email(req.email, req.name)

    return {
        "success":    True,
        "email_sent": email_sent,
        "message":    f"User {req.name} approved. Email {'sent' if email_sent else 'failed — check RESEND_API_KEY'}.",
    }


@router.post("/reject")
async def reject_user(req: ApproveRequest, admin=Depends(require_admin)):
    """Reject and delete a pending user."""
    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")

    try:
        db.collection("users").document(req.uid).delete()
    except Exception as e:
        raise HTTPException(500, f"Failed to delete user: {e}")

    # Send rejection email
    await send_rejection_email(req.email, req.name)

    return {"success": True, "message": f"User {req.name} rejected and removed."}


@router.get("/pending-users")
async def get_pending_users(admin=Depends(require_admin)):
    """Get all pending users."""
    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")

    try:
        docs = db.collection("users").where("status", "==", "pending").stream()
        users = []
        for doc in docs:
            data = doc.to_dict()
            data["uid"] = doc.id
            users.append(data)
        return {"users": users}
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch users: {e}")