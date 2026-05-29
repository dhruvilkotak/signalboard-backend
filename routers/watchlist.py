"""
routers/watchlist.py — v2

Per-user watchlist stored in Firestore.
Default symbols seeded from TickerService (Firestore config/signal_tickers).
Limit: 25 symbols total (Option A — includes admin default tickers).

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

WATCHLIST_LIMIT = 25  # total symbols including admin defaults

# Injected by main.py after ticker_svc is initialised
ticker_svc = None

# Regex: 1-5 uppercase letters, optionally followed by . and 1-2 letters (e.g. BRK.B)
SYMBOL_RE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')


def _watchlist_ref(uid: str):
    return get_db().collection("users").document(uid).collection("data").document("watchlist")


def _default_tickers() -> list[str]:
    """Get default tickers from TickerService (Firestore) or fallback to config."""
    if ticker_svc:
        tickers = ticker_svc.get_tickers()
        if tickers:
            return tickers
    # Fallback to config if ticker_svc not available
    from config import settings
    return list(settings.TICKERS)


@router.get("/")
async def get_watchlist(user: dict = Depends(get_current_user)):
    """Return user's watchlist. Seeds from Firestore config on first visit."""
    uid = user["uid"]
    try:
        doc = _watchlist_ref(uid).get()
        if doc.exists:
            return {"symbols": doc.to_dict().get("symbols", []),
                    "limit": WATCHLIST_LIMIT}
        # First visit — seed with admin tickers from Firestore
        defaults = _default_tickers()
        return {"symbols": defaults, "limit": WATCHLIST_LIMIT}
    except Exception as e:
        logger.error(f"get_watchlist failed for {uid}: {e}")
        raise HTTPException(500, "Failed to fetch watchlist")


@router.post("/{symbol}")
async def add_symbol(symbol: str, user: dict = Depends(get_current_user)):
    """Add symbol to watchlist. Max 25 total."""
    uid    = user["uid"]
    symbol = symbol.upper().strip()

    # Validate symbol format
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(400, {
            "error":   "invalid_symbol",
            "message": f"'{symbol}' is not a valid ticker symbol",
        })

    try:
        ref     = _watchlist_ref(uid)
        doc     = ref.get()
        symbols = doc.to_dict().get("symbols", _default_tickers()) if doc.exists else _default_tickers()

        # Already in watchlist
        if symbol in symbols:
            return {"symbols": symbols, "added": symbol, "limit": WATCHLIST_LIMIT}

        # Enforce limit
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