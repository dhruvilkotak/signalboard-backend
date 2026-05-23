"""
middleware/auth.py

FastAPI dependency for Firebase Auth.

Usage in any router:
    from middleware.auth import get_current_user, optional_user

    # Protected — 401 if no valid token
    @router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return {"uid": user["uid"], "email": user.get("email")}

    # Optional — returns None if no token (public endpoints that can be personalized)
    @router.get("/signals")
    async def signals(user: dict | None = Depends(optional_user)):
        uid = user["uid"] if user else None
"""

import logging
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth as firebase_auth

logger = logging.getLogger(__name__)

# Tells FastAPI to look for "Authorization: Bearer <token>" header
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """
    Required auth dependency.
    Raises HTTP 401 if token is missing or invalid.
    Returns decoded Firebase token dict: { uid, email, name, ... }
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        decoded = firebase_auth.verify_id_token(credentials.credentials)
        return decoded
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired — please sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.error(f"Auth error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict | None:
    """
    Optional auth dependency.
    Returns decoded token if present and valid, None otherwise.
    Use for endpoints that work for both guests and logged-in users.
    """
    if not credentials:
        return None
    try:
        return firebase_auth.verify_id_token(credentials.credentials)
    except Exception:
        return None