"""
routers/watchlist.py — v3

Per-user watchlist stored in Firestore.
Default symbols are seeded once at user approval time (via admin.py).
This router never falls back to defaults — it only reads/writes what's in Firestore.
Limit: 25 symbols total.

Firestore:
    users/{uid}/data/watchlist → { symbols: [...], updated_at }
"""

import re
import logging
from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import firestore as fs

from middleware.auth import get_current_user
from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

WATCHLIST_LIMIT = 25

# Injected by main.py after ticker_svc is initialised
ticker_svc = None

# Regex: 1-5 uppercase letters, optionally followed by . and 1-2 letters (e.g. BRK.B)
SYMBOL_RE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')


def _watchlist_ref(uid: str):
    return get_db().collection("users").document(uid).collection("data").document("watchlist")


def get_default_tickers() -> list[str]:
    """Get default tickers from TickerService (Firestore) or fallback to config.
    Called only at approval time — never during normal watchlist reads.
    """
    if ticker_svc:
        tickers = ticker_svc.get_tickers()
        if tickers:
            return tickers
    from config import settings
    return list(settings.TICKERS)


def seed_watchlist_for_user(uid: str) -> list[str]:
    """Write default tickers to Firestore for a newly approved user.
    Called once from approve_user in admin.py. Returns the seeded list.
    """
    defaults = get_default_tickers()
    _watchlist_ref(uid).set({"symbols": defaults, "updated_at": fs.SERVER_TIMESTAMP})
    logger.info(f"Watchlist: {uid[:8]}… seeded with {len(defaults)} defaults at approval")
    return defaults


@router.get("/")
async def get_watchlist(user: dict = Depends(get_current_user)):
    """Return user's watchlist. Returns empty list if not seeded yet."""
    uid = user["uid"]
    try:
        doc = _watchlist_ref(uid).get()
        symbols = doc.to_dict().get("symbols", []) if doc.exists else []
        return {"symbols": symbols, "limit": WATCHLIST_LIMIT}
    except Exception as e:
        logger.error(f"get_watchlist failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch watchlist")


@router.post("/{symbol}")
async def add_symbol(symbol: str, user: dict = Depends(get_current_user)):
    """Add symbol to watchlist. Max 25 total."""
    uid    = user["uid"]
    symbol = symbol.upper().strip()

    if not SYMBOL_RE.match(symbol):
        raise HTTPException(400, {
            "error":   "invalid_symbol",
            "message": f"'{symbol}' is not a valid ticker symbol",
        })

    try:
        ref     = _watchlist_ref(uid)
        doc     = ref.get()
        symbols = doc.to_dict().get("symbols", []) if doc.exists else []

        if symbol in symbols:
            return {"symbols": symbols, "added": symbol, "limit": WATCHLIST_LIMIT}

        if len(symbols) >= WATCHLIST_LIMIT:
            raise HTTPException(400, {
                "error":   "watchlist_limit",
                "message": f"Watchlist limit of {WATCHLIST_LIMIT} symbols reached. Remove a symbol to add a new one.",
                "limit":   WATCHLIST_LIMIT,
                "current": len(symbols),
            })

        symbols.append(symbol)
        ref.set({"symbols": symbols, "updated_at": fs.SERVER_TIMESTAMP})
        logger.info(f"Watchlist: {uid[:8]}… added {symbol} ({len(symbols)}/{WATCHLIST_LIMIT})")
        return {"symbols": symbols, "added": symbol, "limit": WATCHLIST_LIMIT}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_symbol {symbol} failed for {uid}: {e}")
        raise HTTPException(500, "Failed to add symbol")


@router.delete("/{symbol}")
async def remove_symbol(symbol: str, user: dict = Depends(get_current_user)):
    """Remove symbol from watchlist."""
    uid    = user["uid"]
    symbol = symbol.upper().strip()

    try:
        ref     = _watchlist_ref(uid)
        doc     = ref.get()
        symbols = doc.to_dict().get("symbols", []) if doc.exists else []

        if symbol in symbols:
            symbols.remove(symbol)
            ref.set({"symbols": symbols, "updated_at": fs.SERVER_TIMESTAMP})
            logger.info(f"Watchlist: {uid[:8]}… removed {symbol} ({len(symbols)}/{WATCHLIST_LIMIT})")

        return {"symbols": symbols, "removed": symbol, "limit": WATCHLIST_LIMIT}

    except Exception as e:
        logger.error(f"remove_symbol {symbol} failed for {uid}: {e}")
        raise HTTPException(500, "Failed to remove symbol")