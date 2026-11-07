"""
Feature-flag endpoints.

    GET  /api/config                     signed-in user -> {"flags": {name: bool}}
    GET  /api/config/public              no auth        -> only flags fully "on"
    GET  /api/admin/feature-flags        admin          -> modes + allowlists
    PUT  /api/admin/feature-flags/{name} admin          -> change one flag

`flag_svc` is injected by main.py (module-level variable; never import from main).
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.feature_flags import FLAG_NAMES, MODES, MAX_ALLOWLIST

# TODO: point these at your existing auth dependencies.
#   get_current_user -> verifies the Firebase ID token, returns a dict with "uid"
#   require_admin    -> same, but 403s unless the caller is the admin
from utils.auth import get_current_user, require_admin  # noqa: E402

router = APIRouter()
flag_svc = None  # set in main.py


class FlagUpdate(BaseModel):
    mode: str
    uids: List[str] = []


@router.get("/api/config")
def get_config(user=Depends(get_current_user)):
    return {"flags": flag_svc.resolve_for(user["uid"])}


@router.get("/api/config/public")
def get_public_config():
    return {"flags": flag_svc.resolve_public()}


@router.get("/api/admin/feature-flags")
def list_feature_flags(admin=Depends(require_admin)):
    return {"flags": flag_svc.get_all(), "modes": list(MODES)}


@router.put("/api/admin/feature-flags/{name}")
def update_feature_flag(name: str, body: FlagUpdate, admin=Depends(require_admin)):
    if name not in FLAG_NAMES:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_FLAG", "flag": name})
    if body.mode not in MODES:
        raise HTTPException(status_code=400, detail={"code": "INVALID_MODE", "allowed": list(MODES)})
    if len(body.uids) > MAX_ALLOWLIST:
        raise HTTPException(status_code=400, detail={"code": "ALLOWLIST_TOO_LONG", "max": MAX_ALLOWLIST})

    saved = flag_svc.set_flag(name, body.mode, body.uids, actor_uid=admin["uid"])
    return {"flag": name, **saved}
