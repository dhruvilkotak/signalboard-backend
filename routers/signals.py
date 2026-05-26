"""
routers/signals.py  —  Tasks #35 + #36

Drop-in replacement for the existing routers/signals.py.

Existing endpoints preserved:
    GET  /          — all cached signals (memory)
    GET  /{symbol}  — signal for one ticker (force=true to bypass cache)

New endpoints:
    GET  /feed      — paginated signal feed from Firestore      (#35)
    POST /analyze   — on-demand single-stock analyze for users  (#36)

NOTE: POST /api/signals/run-all lives in main.py (admin only).

On-demand caching rule:
    User calls /analyze → check 30-min memory cache first.
    Only call Claude if cache is stale. This avoids redundant LLM calls
    when multiple users analyze the same symbol close together.
    HIGH/MEDIUM result → persisted to Firestore (appears in feed).
    LOW result         → memory cache only, deleted from Firestore.

Services injected by main.py:
    signals.signal_svc = signal_svc
    signals.price_svc  = price_svc
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.firebase_service import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Injected by main.py
signal_svc = None
price_svc  = None


# ── Existing endpoints (unchanged behaviour) ──────────────────────────────────

@router.get("/")
async def get_all_signals():
    """All signals from memory cache."""
    cached = signal_svc.get_all_cached()
    return {"signals": cached, "count": len(cached)}


@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(False)):
    """Signal for one ticker. force=true bypasses 30-min cache."""
    symbol = symbol.upper().strip()
    try:
        from main import get_current_session
        session = get_current_session()
    except Exception:
        session = "market"
    return await signal_svc.get_signal(symbol, force=force, session=session)


# ── Task #35 — GET /feed ──────────────────────────────────────────────────────

@router.get("/feed")
async def get_signal_feed(
    limit: int = Query(default=20, ge=1, le=100),
    after: Optional[str] = Query(
        default=None,
        description="Cursor: generated_at ISO string of the last item on the previous page",
    ),
    signal_type: Optional[str] = Query(default=None, description="BUY | HOLD | SELL"),
    confidence:  Optional[str] = Query(default=None, description="HIGH | MEDIUM | LOW"),
    watchlist_only: bool = Query(default=False),
):
    """
    Paginated signal feed sorted by generated_at DESC.
    Reads from Firestore signals/{symbol} documents.
    Falls back to in-memory cache when Firestore is unavailable.
    """
    db = None
    try:
        db = get_db()
    except Exception:
        pass

    # ── Firestore path ────────────────────────────────────────────────────────
    if db:
        try:
            def _query():
                from google.cloud.firestore_v1 import Query as FSQuery
                q = db.collection("signals").order_by(
                    "generated_at", direction=FSQuery.DESCENDING
                )
                if after:
                    try:
                        q = q.start_after({"generated_at": after})
                    except Exception:
                        pass
                # Fetch extra to absorb client-side filter losses
                fetch_limit = limit * 4 if (signal_type or confidence) else limit + 1
                docs = list(q.limit(fetch_limit).stream())
                results = []
                for doc in docs:
                    d = doc.to_dict() or {}
                    d["symbol"] = doc.id
                    for k in ("generated_at", "expires_at"):
                        v = d.get(k)
                        if v is not None and hasattr(v, "isoformat"):
                            d[k] = v.isoformat()
                    results.append(d)
                return results

            loop = asyncio.get_event_loop()
            all_docs = await loop.run_in_executor(None, _query)

            filtered = all_docs
            if signal_type:
                st = signal_type.upper()
                filtered = [d for d in filtered if d.get("signal", "").upper() == st]
            if confidence:
                cf = confidence.upper()
                filtered = [d for d in filtered if d.get("confidence", "").upper() == cf]

            page = filtered[:limit]
            next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

            return {"signals": page, "count": len(page), "next_cursor": next_cursor, "source": "firestore"}

        except Exception as e:
            logger.error(f"Signal feed Firestore error: {e} — falling back to memory cache")

    # ── Fallback: memory cache ────────────────────────────────────────────────
    logger.info("Signal feed: serving from memory cache (Firestore unavailable)")
    cached = signal_svc.get_all_cached()

    items = []
    for sym, data in cached.items():
        item = dict(data)
        item["symbol"] = sym
        items.append(item)

    items.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

    if after:
        items = [i for i in items if i.get("generated_at", "") < after]
    if signal_type:
        items = [i for i in items if i.get("signal", "").upper() == signal_type.upper()]
    if confidence:
        items = [i for i in items if i.get("confidence", "").upper() == confidence.upper()]

    page = items[:limit]
    next_cursor = page[-1]["generated_at"] if len(items) > limit else None

    return {"signals": page, "count": len(page), "next_cursor": next_cursor, "source": "memory_cache"}


# ── Task #36 — POST /analyze (user on-demand) ─────────────────────────────────

class AnalyzeRequest(BaseModel):
    symbol: str


@router.post("/analyze")
async def analyze_symbol(req: AnalyzeRequest):
    """
    On-demand signal for a single symbol — available to all authenticated users.

    Caching behaviour (reduces LLM calls):
      - Checks 30-min memory cache first (signal_svc._is_fresh).
      - Only calls Claude if cache is stale or empty.
      - force=False so cached result is returned immediately when fresh.

    Persistence:
      - HIGH or MEDIUM → kept in Firestore (appears in feed).
      - LOW → memory cache only; Firestore doc deleted if written.

    Rate limiting enforced at Nginx layer (5 req/min per IP, design doc §10.1).
    """
    symbol = req.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    try:
        from main import get_current_session
        session = get_current_session()
    except Exception:
        session = "market"

    # force=False → respect 30-min cache (avoids duplicate Claude calls)
    result = await signal_svc.get_signal(
        symbol,
        force=False,
        session=session,
        trigger="on_demand",
    )

    confidence_val = result.get("confidence", "LOW")
    persisted = confidence_val in ("HIGH", "MEDIUM")

    # LOW confidence → remove from Firestore (signal_svc writes unconditionally)
    if not persisted:
        try:
            db = get_db()
            if db:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: db.collection("signals").document(symbol).delete()
                )
                logger.info(f"analyze: LOW confidence {symbol} — removed from Firestore")
        except Exception as e:
            logger.warning(f"analyze: could not remove LOW signal from Firestore for {symbol}: {e}")

    return {
        **result,
        "symbol": symbol,
        "trigger": result.get("trigger", "on_demand"),  # may be "scheduled" if served from cache
        "persisted": persisted,
        "persist_reason": (
            "HIGH/MEDIUM confidence — saved to Firestore feed"
            if persisted
            else "LOW confidence — memory cache only (30 min)"
        ),
    }