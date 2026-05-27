"""
routers/signals.py

Feed filtering rules (applied server-side):
  - signal IN (BUY, SELL)             — no HOLD noise
  - confidence IN (HIGH, MEDIUM)      — no LOW/fallback noise
  - generated_at > 7 days ago         — no stale signals (default view)
  - trigger != "fallback"             — never show failed signals
  - feed_eligible == True             — set by SignalEngine

History mode (show_all=true): relaxes freshness filter, shows up to 45 days.

Services injected by main.py:
    signals.signal_svc = signal_svc
    signals.price_svc  = price_svc
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

signal_svc = None
price_svc  = None

STALE_DAYS    = 7
HISTORY_DAYS  = 45


# ── Existing endpoints ────────────────────────────────────────────────────────

@router.get("/")
async def get_all_signals():
    cached = signal_svc.get_all_cached()
    return {"signals": cached, "count": len(cached)}


@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(False)):
    symbol = symbol.upper().strip()
    try:
        from main import get_current_session
        session = get_current_session()
    except Exception:
        session = "market"
    return await signal_svc.get_signal(symbol, force=force, session=session)


# ── Feed endpoint with filtering ──────────────────────────────────────────────

@router.get("/feed")
async def get_signal_feed(
    limit:       int  = Query(default=20, ge=1, le=100),
    after:       Optional[str] = Query(default=None),
    signal_type: Optional[str] = Query(default=None, description="BUY | SELL | HOLD"),
    confidence:  Optional[str] = Query(default=None, description="HIGH | MEDIUM | LOW"),
    show_all:    bool = Query(default=False, description="Show full 45-day history, not just last 7 days"),
):
    """
    Filtered signal feed — BUY/SELL only, HIGH/MEDIUM confidence, fresh signals.
    Use show_all=true to see full 45-day history.
    """
    db = None
    try:
        db = get_db()
    except Exception:
        pass

    cutoff_days  = HISTORY_DAYS if show_all else STALE_DAYS
    cutoff_dt    = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    cutoff_iso   = cutoff_dt.isoformat()

    if db:
        try:
            def _query():
                from google.cloud.firestore_v1 import Query as FSQuery
                q = (
                    db.collection("signals")
                    .order_by("generated_at", direction=FSQuery.DESCENDING)
                )
                if after:
                    try:
                        q = q.start_after({"generated_at": after})
                    except Exception:
                        pass
                # Fetch generously — server-side filters applied below
                fetch_limit = limit * 6
                return list(q.limit(fetch_limit).stream())

            loop     = asyncio.get_event_loop()
            raw_docs = await loop.run_in_executor(None, _query)

            items = []
            for doc in raw_docs:
                d = doc.to_dict() or {}
                d["symbol"] = doc.id
                for k in ("generated_at", "expires_at"):
                    v = d.get(k)
                    if v is not None and hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                items.append(d)

            # ── Apply feed filters ────────────────────────────────────────────
            filtered = []
            for d in items:
                sig  = d.get("signal", "").upper()
                conf = d.get("confidence", "").upper()
                gen  = d.get("generated_at", "")
                trig = d.get("trigger", "")

                # Core quality filters
                if sig not in ("BUY", "SELL"):
                    continue
                if conf not in ("HIGH", "MEDIUM"):
                    continue
                if trig == "fallback":
                    continue

                # Freshness filter
                if gen < cutoff_iso:
                    continue

                # Optional caller filters
                if signal_type and sig != signal_type.upper():
                    continue
                if confidence and conf != confidence.upper():
                    continue

                filtered.append(d)

            page        = filtered[:limit]
            next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

            return {
                "signals":     page,
                "count":       len(page),
                "next_cursor": next_cursor,
                "source":      "firestore",
                "filters": {
                    "signal_type": signal_type,
                    "confidence":  confidence,
                    "show_all":    show_all,
                    "cutoff_days": cutoff_days,
                },
            }

        except Exception as e:
            logger.error(f"Signal feed Firestore error: {e} — falling back to memory")

    # ── Memory cache fallback ─────────────────────────────────────────────────
    logger.info("Signal feed: serving from memory cache")
    cached = signal_svc.get_all_cached()
    items  = []
    for sym, data in cached.items():
        item = dict(data)
        item["symbol"] = sym
        items.append(item)

    items.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

    filtered = []
    for d in items:
        sig  = d.get("signal", "").upper()
        conf = d.get("confidence", "").upper()
        gen  = d.get("generated_at", "")
        trig = d.get("trigger", "")

        if sig not in ("BUY", "SELL"):           continue
        if conf not in ("HIGH", "MEDIUM"):        continue
        if trig == "fallback":                    continue
        if gen < cutoff_iso:                      continue
        if signal_type and sig != signal_type.upper(): continue
        if confidence and conf != confidence.upper():  continue

        filtered.append(d)

    if after:
        filtered = [i for i in filtered if i.get("generated_at", "") < after]

    page        = filtered[:limit]
    next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

    return {
        "signals":     page,
        "count":       len(page),
        "next_cursor": next_cursor,
        "source":      "memory_cache",
        "filters": {
            "signal_type": signal_type,
            "confidence":  confidence,
            "show_all":    show_all,
            "cutoff_days": cutoff_days,
        },
    }