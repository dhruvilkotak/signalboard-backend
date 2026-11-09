"""
require_terms_accepted — blocks signed-in API calls until the user has
accepted the current Terms version (only while flag `terms_gate` is on).

Apply per router in main.py, ONLY on routers that already require login
(it depends on get_current_user, so it would 401 public endpoints):

    from middleware.terms import require_terms_accepted
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"],
                       dependencies=[Depends(require_terms_accepted)])

Blocked -> 403 {"code": "TERMS_REQUIRED", "current_version", "accepted_version"}.
`legal_svc` and `flag_svc` are injected by main.py.
"""

from fastapi import Depends, HTTPException

from middleware.auth import get_current_user

legal_svc = None  # set in main.py
flag_svc = None   # set in main.py


def require_terms_accepted(user=Depends(get_current_user)):
    if legal_svc is None or flag_svc is None:
        return user
    uid = user["uid"]
    if not flag_svc.is_enabled("terms_gate", uid):
        return user
    st = legal_svc.status(uid)
    if st["needs_acceptance"]:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "TERMS_REQUIRED",
                "current_version": st["terms_version"],
                "accepted_version": st["accepted_version"],
            },
        )
    return user
