"""
Terms / consent endpoints. Include with NO prefix (paths are full).

    GET  /api/legal                  public  -> current version + summary of changes
    GET  /api/me/consent             user    -> current, accepted, needs_acceptance, gate_enabled
    POST /api/consent/accept         user    -> {"terms_version": "v1.1", "platform": "web"}
    POST /api/admin/legal/publish    admin   -> {"terms_version": "v1.1", "summary_of_changes": "..."}

These routes must NOT use require_terms_accepted (users need them to accept).
`legal_svc` and `flag_svc` are injected by main.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin
from services.legal_service import TermsVersionMismatch

router = APIRouter()
legal_svc = None  # set in main.py
flag_svc = None   # set in main.py


class AcceptIn(BaseModel):
    terms_version: str
    platform: str = "web"


class PublishIn(BaseModel):
    terms_version: str
    summary_of_changes: str


@router.get("/api/legal")
def current_legal():
    return legal_svc.get_current()


@router.get("/api/me/consent")
def my_consent(user=Depends(get_current_user)):
    uid = user["uid"]
    st = legal_svc.status(uid, use_cache=False)
    return {**st, "gate_enabled": flag_svc.is_enabled("terms_gate", uid)}


@router.post("/api/consent/accept")
def accept_terms(body: AcceptIn, request: Request, user=Depends(get_current_user)):
    platform = body.platform if body.platform in ("web", "ios", "android") else "web"
    try:
        return legal_svc.accept(
            user["uid"], body.terms_version, platform=platform,
            user_agent=request.headers.get("user-agent", ""),
        )
    except TermsVersionMismatch:
        current = legal_svc.get_current()["terms_version"]
        raise HTTPException(
            status_code=409,
            detail={"code": "TERMS_VERSION_MISMATCH", "current_version": current,
                    "message": "The Terms were updated. Refresh the page to read the latest version."},
        )
    except RuntimeError:
        raise HTTPException(status_code=503, detail={"code": "LEGAL_UNAVAILABLE"})


@router.post("/api/admin/legal/publish")
def publish_terms(body: PublishIn, admin=Depends(require_admin)):
    try:
        return legal_svc.publish(body.terms_version, body.summary_of_changes, actor_uid=admin["uid"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PUBLISH", "message": str(e)})
    except RuntimeError:
        raise HTTPException(status_code=503, detail={"code": "LEGAL_UNAVAILABLE"})
