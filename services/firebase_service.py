"""
services/firebase_service.py

Initializes Firebase Admin SDK once at startup.
Provides get_db() for Firestore access anywhere in the app.
"""

import logging
import firebase_admin
from firebase_admin import credentials, firestore, auth

from config import settings

logger = logging.getLogger(__name__)

_db = None


def init_firebase() -> bool:
    """
    Call once at app startup (inside lifespan).
    Returns True if Firebase initialized successfully, False if skipped.
    """
    global _db

    if not settings.FIREBASE_CREDENTIALS:
        logger.warning("FIREBASE_CREDENTIALS_PATH not set — Firebase disabled")
        return False

    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(cred)

        _db = firestore.client()
        logger.info("Firebase Admin SDK initialized ✓")
        return True

    except Exception as e:
        logger.error(f"Firebase init failed: {e}")
        return False


def get_db():
    """Return Firestore client. Raises if Firebase not initialized."""
    if _db is None:
        raise RuntimeError("Firestore not initialized — check FIREBASE_CREDENTIALS_PATH")
    return _db


def verify_token(id_token: str) -> dict:
    """
    Verify a Firebase ID token.
    Returns decoded token dict with uid, email, etc.
    Raises firebase_admin.auth.InvalidIdTokenError on failure.
    """
    return auth.verify_id_token(id_token)