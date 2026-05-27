# routers/admin.py
# Admin endpoints — approve/reject users, send emails via Resend

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from services.email_service import send_approval_email, send_rejection_email
from services.firebase_service import get_db
from middleware.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

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


import random
import string
from datetime import datetime, timedelta, timezone

def generate_code():
    chars = string.ascii_uppercase.replace("O","").replace("I","") + "23456789"
    return "SB-" + "".join(random.choices(chars, k=5))


class InviteRequest(BaseModel):
    email: str
    notes: str = ""


@router.post("/invite")
async def send_invite(req: InviteRequest, admin=Depends(require_admin)):
    """Generate invite code, store in Firestore, send email."""
    from services.email_service import send_invite_email
    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")

    code    = generate_code()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    try:
        db.collection("invites").document(code).set({
            "email":      req.email.strip().lower(),
            "used":       False,
            "created_at": __import__("firebase_admin").firestore.SERVER_TIMESTAMP,
            "expires_at": expires,
            "notes":      req.notes,
            "created_by": admin["uid"],
        })
    except Exception as e:
        raise HTTPException(500, f"Failed to store invite: {e}")

    email_sent = await send_invite_email(req.email, code, req.notes)

    return {
        "success":    True,
        "code":       code,
        "email_sent": email_sent,
        "message":    f"Invite {code} created. Email {'sent' if email_sent else 'failed'} to {req.email}.",
    }