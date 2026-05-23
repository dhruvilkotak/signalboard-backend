"""
routers/watchlist.py

Per-user watchlist — protected by Firebase Auth, stored in Firestore.

Firestore structure:
    users/{uid}/watchlist   →  { symbols: ["AAPL", "NVDA", ...] }

Endpoints:
    GET  /api/watchlist          — get current user's watchlist
    POST /api/watchlist/{symbol} — add symbol
    DELETE /api/watchlist/{symbol} — remove symbol
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from firebase_admin import firestore as fs

from middleware.auth import get_current_user
from services.firebase_service import get_db
from config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _watchlist_ref(uid: str):
    """Returns the Firestore document ref for this user's watchlist."""
    return get_db().collection("users").document(uid).collection("data").document("watchlist")


@router.get("/")
async def get_watchlist(user: dict = Depends(get_current_user)):
    """Returns the authenticated user's watchlist symbols."""
    uid = user["uid"]
    try:
        doc = _watchlist_ref(uid).get()
        if doc.exists:
            return {"symbols": doc.to_dict().get("symbols", [])}
        # First time — seed with default tickers
        return {"symbols": list(settings.TICKERS)}
    except Exception as e:
        logger.error(f"get_watchlist failed for {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch watchlist")


@router.post("/{symbol}")
async def add_symbol(symbol: str, user: dict = Depends(get_current_user)):
    """Add a symbol to the authenticated user's watchlist."""
    uid = user["uid"]
    symbol = symbol.upper().strip()

    try:
        ref = _watchlist_ref(uid)
        doc = ref.get()
        symbols = doc.to_dict().get("symbols", list(settings.TICKERS)) if doc.exists else list(settings.TICKERS)

        if symbol not in symbols:
            symbols.append(symbol)
            ref.set({"symbols": symbols, "updated_at": fs.SERVER_TIMESTAMP})

        return {"symbols": symbols, "added": symbol}
    except Exception as e:
        logger.error(f"add_symbol {symbol} failed for {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to add symbol")


@router.delete("/{symbol}")
async def remove_symbol(symbol: str, user: dict = Depends(get_current_user)):
    """Remove a symbol from the authenticated user's watchlist."""
    uid = user["uid"]
    symbol = symbol.upper().strip()

    try:
        ref = _watchlist_ref(uid)
        doc = ref.get()
        symbols = doc.to_dict().get("symbols", []) if doc.exists else []

        if symbol in symbols:
            symbols.remove(symbol)
            ref.set({"symbols": symbols, "updated_at": fs.SERVER_TIMESTAMP})

        return {"symbols": symbols, "removed": symbol}
    except Exception as e:
        logger.error(f"remove_symbol {symbol} failed for {uid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove symbol")