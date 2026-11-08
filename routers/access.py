"""
Access + role endpoints.

    GET /api/me/access                    signed-in user -> plan, features, dates, signals left
    PUT /api/admin/users/{uid}/role       admin          -> {"role": "user" | "pro"}

`access_svc` is injected by main.py. Include with NO prefix (paths are full).
"""

from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth as fb_auth
from pydantic import BaseModel

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin
from services.access_service import ASSIGNABLE_ROLES

router = APIRouter()
access_svc = None  # set in main.py


class RoleUpdate(BaseModel):
    role: str


@router.get("/api/me/access")
def my_access(user=Depends(get_current_user)):
    return access_svc.get_access(user["uid"], use_cache=False)


@router.put("/api/admin/users/{uid}/role")
def set_user_role(uid: str, body: RoleUpdate, admin=Depends(require_admin)):
    if body.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail={"code": "INVALID_ROLE", "allowed": list(ASSIGNABLE_ROLES)})
    try:
        role = access_svc.set_role(uid, body.role, actor_uid=admin["uid"])
    except fb_auth.UserNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "uid": uid})
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "ROLE_CHANGE_REFUSED", "message": str(e)})
    return {"uid": uid, "role": role}
