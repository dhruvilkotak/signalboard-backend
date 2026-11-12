"""
Billing endpoints. Include with NO prefix (paths are full).

    GET  /api/billing/status            user   {enabled, mode}
    POST /api/billing/checkout-session  user   -> {"url"}   (flag: billing)
    POST /api/billing/portal-session    user   -> {"url"}
    POST /api/billing/webhook           Stripe signature, raw body, NO auth

Keep /api/billing/webhook out of nginx rate-limit zones so Stripe retries land.
`billing_svc` and `flag_svc` are injected by main.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from middleware.auth import get_current_user
from services.billing_service import BillingError

logger = logging.getLogger(__name__)
router = APIRouter()
billing_svc = None  # set in main.py
flag_svc = None     # set in main.py


def _http(e: BillingError):
    return HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


def _require_flag(uid: str):
    if flag_svc is None or not flag_svc.is_enabled("billing", uid):
        raise HTTPException(status_code=403, detail={"code": "BILLING_DISABLED", "message": "Subscriptions aren't available yet."})


@router.get("/api/billing/status")
async def billing_status(user=Depends(get_current_user)):
    return {"enabled": bool(flag_svc and flag_svc.is_enabled("billing", user["uid"])), "mode": billing_svc.mode}


@router.post("/api/billing/checkout-session")
async def checkout_session(user=Depends(get_current_user)):
    _require_flag(user["uid"])
    try:
        url = await run_in_threadpool(billing_svc.create_checkout, user["uid"], user.get("email"))
    except BillingError as e:
        raise _http(e)
    return {"url": url}


@router.post("/api/billing/portal-session")
async def portal_session(user=Depends(get_current_user)):
    try:
        url = await run_in_threadpool(billing_svc.create_portal, user["uid"])
    except BillingError as e:
        raise _http(e)
    return {"url": url}


@router.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    try:
        event = billing_svc.construct_event(payload, request.headers.get("stripe-signature"))
        outcome = await run_in_threadpool(billing_svc.handle_event, event)
    except BillingError as e:
        logger.warning("stripe_webhook_rejected", extra={"event": "stripe_webhook_rejected", "code": e.code})
        raise _http(e)
    logger.info("stripe_webhook", extra={"event": "stripe_webhook", "type": event["type"], "outcome": outcome})
    return {"received": True, "outcome": outcome}
