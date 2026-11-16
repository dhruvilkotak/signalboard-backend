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
    """Approve a pending user, seed their watchlist, and send approval email."""
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

    # Seed default watchlist for the newly approved user
    try:
        from routers.watchlist import seed_watchlist_for_user
        seed_watchlist_for_user(req.uid)
    except Exception as e:
        logger.warning(f"Watchlist seed failed for {req.uid}: {e} — user can still add stocks manually")

    # Start the 7-day trial (no visible effect until the `trial` flag is on).
    # Idempotent: re-approving never restarts or extends an existing trial.
    trial_ends_at = None
    try:
        from services.trial_service import start_trial
        trial_ends_at = start_trial(db, req.uid).isoformat()
    except Exception as e:
        logger.warning(f"Trial start failed for {req.uid}: {e}")

    # Send approval email
    email_sent = await send_approval_email(req.email, req.name)

    return {
        "success":    True,
        "email_sent": email_sent,
        "trial_ends_at": trial_ends_at,
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

# ── Announcements: Admin → 📣 Announce ──────────────────────────────────────
#   GET  /api/admin/announce/recipients   approved users with an email
#   POST /api/admin/announce/preview      {subject, body} -> {html}
#   POST /api/admin/announce              {subject, body, uids, test}
#        test=true  -> only to the admin's own email
#        test=false -> one personal email to each selected approved user
#   Each real send is logged in Firestore announcements/{id}.

from typing import List
from fastapi.concurrency import run_in_threadpool
from services.email_service import email_configured, render_announcement, send_announcement_email

MAX_SUBJECT = 150
MAX_BODY = 10_000


class AnnouncePreview(BaseModel):
    subject: str
    body:    str


class AnnounceRequest(BaseModel):
    subject: str
    body:    str
    uids:    List[str] = []
    test:    bool = False


def _approved_users(db) -> list[dict]:
    out = []
    for snap in db.collection("users").where("status", "==", "approved").stream():
        d = snap.to_dict() or {}
        email = (d.get("email") or "").strip()
        if email:
            out.append({"uid": snap.id, "name": d.get("name") or "", "email": email})
    return sorted(out, key=lambda u: u["email"])


def _check_message(subject: str, body: str) -> tuple[str, str]:
    subject, body = (subject or "").strip(), (body or "").strip()
    if not 3 <= len(subject) <= MAX_SUBJECT:
        raise HTTPException(400, {"code": "INVALID_SUBJECT", "message": f"Subject must be 3–{MAX_SUBJECT} characters."})
    if not 10 <= len(body) <= MAX_BODY:
        raise HTTPException(400, {"code": "INVALID_BODY", "message": f"Message must be 10–{MAX_BODY} characters."})
    return subject, body


def pick_recipients(approved: list[dict], uids: list[str]) -> list[dict]:
    """Only approved users, only the selected ones, each once."""
    wanted = set(uids or [])
    return [u for u in approved if u["uid"] in wanted]


@router.get("/announce/recipients")
async def announce_recipients(admin=Depends(require_admin)):
    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")
    users = await run_in_threadpool(_approved_users, db)
    return {"recipients": users, "email_configured": email_configured()}


@router.post("/announce/preview")
async def announce_preview(req: AnnouncePreview, admin=Depends(require_admin)):
    subject, body = _check_message(req.subject, req.body)
    html, _ = render_announcement("Dhruvil", subject, body)
    return {"html": html}


@router.post("/announce")
async def announce_send(req: AnnounceRequest, admin=Depends(require_admin)):
    subject, body = _check_message(req.subject, req.body)
    if not email_configured():
        raise HTTPException(503, {"code": "EMAIL_UNCONFIGURED", "message": "GMAIL_APP_PASSWORD isn't set on the server."})

    if req.test:
        to = admin.get("email")
        if not to:
            raise HTTPException(400, {"code": "NO_ADMIN_EMAIL", "message": "Your admin account has no email."})
        ok = await run_in_threadpool(send_announcement_email, to, admin.get("name") or "", f"[TEST] {subject}", body)
        return {"test": True, "sent": [to] if ok else [], "failed": [] if ok else [to]}

    db = get_db()
    if not db:
        raise HTTPException(500, "Database not available")
    recipients = pick_recipients(await run_in_threadpool(_approved_users, db), req.uids)
    if not recipients:
        raise HTTPException(400, {"code": "NO_RECIPIENTS", "message": "Select at least one approved user."})

    sent, failed = [], []
    for u in recipients:                       # one personal email each; small list, so sequential
        ok = await run_in_threadpool(send_announcement_email, u["email"], u["name"], subject, body)
        (sent if ok else failed).append(u["email"])

    try:
        from firebase_admin import firestore
        db.collection("announcements").add({
            "subject": subject, "body": body, "sent_by": admin.get("uid"),
            "sent_at": firestore.SERVER_TIMESTAMP, "sent": sent, "failed": failed,
        })
    except Exception:
        logger.exception("announcement log failed")
    logger.info("announcement_sent", extra={"event": "announcement_sent", "sent": len(sent),
                                            "failed": len(failed), "actor_uid": admin.get("uid")})
    return {"test": False, "sent": sent, "failed": failed}
