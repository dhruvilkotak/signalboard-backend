"""
require_feature(...) — FastAPI dependency that blocks a route unless the user
has the feature. Returns the user dict, so it can replace get_current_user:

    from middleware.access import require_feature

    @router.post("/message")
    async def chat(body: ChatIn, user=Depends(require_feature("ai_chat"))):
        ...

Missing access -> 402 {"code": "PAYWALL", "feature": ..., "plan": ...}.
While the `paywall` / `trial` flags are off every user gets plan "legacy",
so adding this to a route changes nothing until launch.

`access_svc` is injected by main.py.
"""

from fastapi import Depends, HTTPException

from middleware.auth import get_current_user
from services.access_service import FEATURES

access_svc = None  # set in main.py


def require_feature(feature: str):
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")

    def _dep(user=Depends(get_current_user)):
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
        return user

    return _dep
