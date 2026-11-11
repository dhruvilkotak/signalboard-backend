"""
require_feature(...) — FastAPI dependency that blocks a route unless the user
has the feature. Returns the user dict, so it can replace get_current_user:

    from middleware.access import require_feature

    @router.post("/message")
    async def chat(body: ChatIn, user=Depends(require_feature("ai_chat"))):
        ...

require_feature_optional(...) — same, for routes that use optional_user
(guests allowed). Guests are let through exactly as before UNLESS the
feature's flag is fully "on" for everyone, in which case they get 401.

Missing access -> 402 {"code": "PAYWALL", "feature": ..., "plan": ...}.
While the `paywall` / `trial` flags are off every user gets plan "legacy",
so adding this to a route changes nothing until launch.

`access_svc` and `flag_svc` are injected by main.py.
"""

from fastapi import Depends, HTTPException

from middleware.auth import get_current_user, optional_user
from services.access_service import FEATURES

access_svc = None  # set in main.py
flag_svc = None    # set in main.py

# Which flag gates which feature (mirrors AccessService).
FEATURE_FLAG = {"ondemand": "paywall", "ai_signals": "trial", "ai_chat": "trial", "autotrader": "trial"}


def _check(user: dict, feature: str) -> None:
    access = access_svc.get_access(user["uid"])
    if not access["features"].get(feature):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "PAYWALL",
                "feature": feature,
                "plan": access["plan"],
                "ondemand": access["ondemand"],
                "trial_ends_at": access["trial_ends_at"],
            },
        )


def require_feature(feature: str):
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")

    def _dep(user=Depends(get_current_user)):
        _check(user, feature)
        return user

    return _dep


def require_feature_optional(feature: str):
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")

    def _dep(user=Depends(optional_user)):
        if user is None:
            if flag_svc is not None and flag_svc.resolve_public().get(FEATURE_FLAG[feature]):
                raise HTTPException(status_code=401, detail="Sign in required")
            return None
        _check(user, feature)
        return user

    return _dep
