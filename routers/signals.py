"""
routers/signals.py — v2

Feed reads from signal_snapshots/{symbol} — one doc per symbol, no duplicates.
History reads from signal_snapshots/{symbol}/history — subcollection, DESC order.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from firebase_admin import firestore as fs

from middleware.auth       import get_current_user, optional_user
from middleware.admin_auth import require_admin
from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

signal_svc = None   # injected by main.py


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_iso(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if hasattr(val, "seconds"):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(val.seconds, tz=timezone.utc).isoformat()
    return str(val)


def _ser(data: dict) -> dict:
    return {k: _to_iso(v) if not isinstance(v, (int, float, bool, list, dict, type(None))) else v
            for k, v in (data or {}).items()}


async def _attach_current_status(db, items: list) -> list:
    """
    Attach current_signal and current_confidence from signals/{symbol}
    to each snapshot item. Sets signal_changed=True if different from snapshot.
    """
    if not db or not items:
        return items

    import asyncio
    loop = asyncio.get_running_loop()
    symbols = list({item.get("symbol", "") for item in items if item.get("symbol")})

    def _load_current():
        current = {}
        for sym in symbols:
            try:
                doc = db.collection("signals").document(sym).get()
                if doc.exists:
                    current[sym] = doc.to_dict() or {}
            except Exception:
                pass
        return current

    current_map = await loop.run_in_executor(None, _load_current)

    for item in items:
        sym = item.get("symbol", "")
        cur = current_map.get(sym, {})
        item["current_signal"]     = cur.get("signal",     item.get("signal"))
        item["current_confidence"] = cur.get("confidence", item.get("confidence"))
        item["signal_changed"]     = (
            item["current_signal"]     != item.get("signal") or
            item["current_confidence"] != item.get("confidence")
        )

    return items


# ── Feed — one doc per symbol ─────────────────────────────────────────────────

@router.get("/stream")
async def get_signal_feed(
    confidence: str  = Query(default="HIGH"),
    show_all:   bool = Query(default=False),
    cutoff_days: int = Query(default=7),
    user=Depends(optional_user),
):
    """
    Signal feed — one card per symbol, no duplicates.
    Reads signal_snapshots/{symbol} collection directly.
    Sorted by generated_at DESC (most recent first).
    """
    db = get_db()
    if not db:
        raise HTTPException(503, "Database not available")

    try:
        import asyncio
        from datetime import datetime, timezone, timedelta
        loop   = asyncio.get_running_loop()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cutoff_days if not show_all else 45)).isoformat()

        def _query():
            q = db.collection("signal_snapshots")
            if not show_all:
                q = q.where("feed_eligible", "==", True)
            if confidence and confidence != "ALL":
                q = q.where("confidence", "==", confidence)
            q = q.where("generated_at", ">", cutoff)
            docs = q.stream()
            results = []
            for doc in docs:
                d = doc.to_dict() or {}
                d["snapshot_doc_id"] = doc.id   # symbol
                results.append(_ser(d))
            # Sort by generated_at DESC in Python (Firestore needs composite index for multi-where + order_by)
            results.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
            return results

        raw = await loop.run_in_executor(None, _query)
        items = await _attach_current_status(db, raw)

        logger.info(f"Signal feed: {len(items)} symbols returned")
        return {
            "signals":    items,
            "count":      len(items),
            "next_cursor": None,
            "source":     "firestore_signal_snapshots",
            "filters":    {
                "confidence": confidence,
                "show_all":   show_all,
                "cutoff_days": cutoff_days,
            },
        }

    except Exception as e:
        logger.error(f"Signal feed error: {e}")
        # Fallback to memory cache
        if signal_svc:
            cached = list(signal_svc.get_all_cached().values())
            cached = [s for s in cached if s.get("feed_eligible")]
            cached.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
            return {"signals": cached, "count": len(cached), "source": "memory_cache"}
        raise HTTPException(500, "Signal feed unavailable")


# ── History — subcollection per symbol ───────────────────────────────────────

@router.get("/{symbol}/history")
async def get_signal_history(
    symbol: str,
    user=Depends(optional_user),
):
    """
    Signal history for one symbol.
    Reads signal_snapshots/{symbol}/history ordered by generated_at DESC.
    Feed-eligible only, last 20 days.
    """
    db = get_db()
    if not db:
        raise HTTPException(503, "Database not available")

    try:
        import asyncio
        loop = asyncio.get_running_loop()
        sym  = symbol.upper().strip()

        def _fetch():
            snap_ref = db.collection("signal_snapshots").document(sym)
            docs     = (
                snap_ref.collection("history")
                .order_by("generated_at", direction=fs.Query.DESCENDING)
                .limit(50)
                .stream()
            )
            return [_ser(d.to_dict() or {}) for d in docs]

        history = await loop.run_in_executor(None, _fetch)
        return {
            "symbol":  sym,
            "history": history,
            "count":   len(history),
        }

    except Exception as e:
        logger.error(f"Signal history error for {symbol}: {e}")
        raise HTTPException(500, f"History unavailable for {symbol}")


# ── Current signal (single) ───────────────────────────────────────────────────

@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(default=False),
                     user=Depends(optional_user)):
    """Get or generate signal for one symbol."""
    if not signal_svc:
        raise HTTPException(503, "Signal service not available")
    try:
        sig = await signal_svc.get_signal(symbol.upper(), force=force)
        return sig
    except Exception as e:
        logger.error(f"get_signal failed for {symbol}: {e}")
        raise HTTPException(500, str(e))


@router.get("/")
async def get_all_signals(user=Depends(optional_user)):
    if not signal_svc:
        raise HTTPException(503, "Signal service not available")
    return signal_svc.get_all_cached()


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.post("/run-all")
async def run_all_signals(force: bool = Query(default=False),
                          admin=Depends(require_admin)):
    if not signal_svc:
        raise HTTPException(503, "Signal service not available")
    try:
        results = await signal_svc.get_all_signals(force=force)
        return {"generated": len(results), "symbols": list(results.keys())}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/snapshot/{snapshot_id}")
async def delete_signal_snapshot(snapshot_id: str, admin=Depends(require_admin)):
    """Delete a snapshot doc — snapshot_id is the symbol for new schema."""
    db = get_db()
    if not db:
        raise HTTPException(503, "Database not available")
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        # snapshot_id is the symbol (doc ID in new schema)
        sym = snapshot_id.split("_")[0] if "_" in snapshot_id else snapshot_id
        await loop.run_in_executor(
            None,
            lambda: db.collection("signal_snapshots").document(sym).delete(),
        )
        if signal_svc:
            signal_svc.invalidate(sym)
        return {"deleted": sym, "status": "ok"}
    except Exception as e:
        logger.error(f"Delete snapshot failed for {snapshot_id}: {e}")
        raise HTTPException(500, str(e))