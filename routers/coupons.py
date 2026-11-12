"""
Coupon endpoints. Include with NO prefix (paths are full).

    POST   /api/coupons/redeem           user   {"code": "SBC-ABCD-EFGH"}  (flag: coupons)
    GET    /api/admin/coupons            admin  list with usage + status
    POST   /api/admin/coupons            admin  {"duration_days", "max_redemptions", "expires_in_days", "note", "code"?}
    DELETE /api/admin/coupons/{code}     admin  deactivate (kept for history)

`coupon_svc` and `flag_svc` are injected by main.py.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from middleware.auth import get_current_user
from middleware.admin_auth import require_admin
from services.coupon_service import CouponError

router = APIRouter()
coupon_svc = None  # set in main.py
flag_svc = None    # set in main.py


class RedeemIn(BaseModel):
    code: str


class CreateIn(BaseModel):
    duration_days: int = 30
    max_redemptions: Optional[int] = 1
    expires_in_days: Optional[int] = 30
    note: str = ""
    code: Optional[str] = None


def _http(e: CouponError):
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


@router.post("/api/coupons/redeem")
async def redeem_coupon(body: RedeemIn, user=Depends(get_current_user)):
    if flag_svc is None or not flag_svc.is_enabled("coupons", user["uid"]):
        raise HTTPException(status_code=403, detail={"code": "COUPONS_DISABLED", "message": "Coupons aren't available yet."})
    try:
        return await run_in_threadpool(coupon_svc.redeem, user["uid"], body.code)
    except CouponError as e:
        raise _http(e)


@router.get("/api/admin/coupons")
async def list_coupons(admin=Depends(require_admin)):
    try:
        return {"coupons": await run_in_threadpool(coupon_svc.list)}
    except CouponError as e:
        raise _http(e)


@router.post("/api/admin/coupons")
async def create_coupon(body: CreateIn, admin=Depends(require_admin)):
    try:
        return await run_in_threadpool(
            coupon_svc.create, admin["uid"], body.duration_days, body.max_redemptions,
            body.expires_in_days, body.note, body.code,
        )
    except CouponError as e:
        raise _http(e)


@router.delete("/api/admin/coupons/{code}")
async def deactivate_coupon(code: str, admin=Depends(require_admin)):
    try:
        return await run_in_threadpool(coupon_svc.deactivate, code, admin["uid"])
    except CouponError as e:
        raise _http(e)
