"""
routers/signals.py

Feed filtering rules:
  - Reads immutable snapshots from signal_snapshots
  - snapshot signal IN (BUY, SELL)
  - confidence == HIGH only
  - generated_at > 7 days ago by default / 45 days when show_all=true
  - trigger != "fallback"
  - DOES NOT deduplicate by symbol, because snapshots should remain visible
    even if the current live signal changes to HOLD later.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Depends
from middleware.admin_auth import require_admin
from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

signal_svc = None
price_svc = None

STALE_DAYS = 7
HISTORY_DAYS = 45

def _to_iso(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if hasattr(val, "isoformat"):
        dt = val
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if hasattr(val, "timestamp"):
        return datetime.fromtimestamp(val.timestamp(), tz=timezone.utc).isoformat()
    return str(val)


def _is_fresh(generated_at_val, cutoff_iso: str) -> bool:
    iso = _to_iso(generated_at_val)
    if not iso:
        return False
    try:
        gen_dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)

        cut_dt = datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00"))
        if cut_dt.tzinfo is None:
            cut_dt = cut_dt.replace(tzinfo=timezone.utc)

        return gen_dt > cut_dt
    except Exception:
        return False


async def _attach_current_status(db, items: list) -> list:
    """
    Adds:
      current_signal
      current_confidence
      signal_changed
    by comparing immutable snapshot with current signals/{symbol}.
    """
    if not db or not items:
        return items

    symbols = list({i.get("symbol") for i in items if i.get("symbol")})

    def _load_current():
        out = {}
        for sym in symbols:
            try:
                doc = db.collection("signals").document(sym).get()
                if doc.exists:
                    out[sym] = doc.to_dict() or {}
            except Exception:
                pass
        return out

    loop = asyncio.get_event_loop()
    current_map = await loop.run_in_executor(None, _load_current)

    for item in items:
        sym = item.get("symbol")
        current = current_map.get(sym, {})
        current_signal = current.get("signal")
        current_conf = current.get("confidence")

        item["current_signal"] = current_signal
        item["current_confidence"] = current_conf
        item["signal_changed"] = bool(
            current_signal and current_signal != item.get("signal")
        )

    return items


@router.get("/")
async def get_all_signals():
    cached = signal_svc.get_all_cached()
    return {"signals": cached, "count": len(cached)}


@router.get("/stream")
async def get_signal_feed(
    limit: int = Query(default=20, ge=1, le=100),
    after: Optional[str] = Query(default=None),
    signal_type: Optional[str] = Query(default=None, description="BUY | SELL"),
    show_all: bool = Query(default=False, description="Show full 45-day history"),
):
    """
    Immutable snapshot feed.

    Important:
      This reads signal_snapshots, not signals.
      So if TQQQ was SELL HIGH 2 hours ago and is now HOLD,
      the old SELL snapshot remains visible.
    """
    db = None
    try:
        db = get_db()
    except Exception:
        pass

    cutoff_days = HISTORY_DAYS if show_all else STALE_DAYS
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    cutoff_iso = cutoff_dt.isoformat()

    if db:
        try:
            def _query():
                from google.cloud.firestore_v1 import Query as FSQuery

                q = (
                    db.collection("signal_snapshots")
                    .order_by("generated_at", direction=FSQuery.DESCENDING)
                )

                if after:
                    q = q.start_after({"generated_at": after})

                # Fetch generously because Python filters below may reduce it.
                return list(q.limit(200).stream())

            loop = asyncio.get_event_loop()
            raw_docs = await loop.run_in_executor(None, _query)

            items = []
            for doc in raw_docs:
                d = doc.to_dict() or {}
                d["snapshot_doc_id"] = doc.id

                for k in ("generated_at", "expires_at"):
                    d[k] = _to_iso(d.get(k))

                items.append(d)

            filtered = []
            for d in items:
                sig = d.get("signal", "").upper()
                conf = d.get("confidence", "").upper()
                trig = d.get("trigger", "")
                gen = d.get("generated_at", "")

                if sig not in ("BUY", "SELL"):
                    continue
                if conf != "HIGH":
                    continue
                if trig == "fallback":
                    continue
                if not _is_fresh(gen, cutoff_iso):
                    continue
                if signal_type and sig != signal_type.upper():
                    continue

                filtered.append(d)

            filtered.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

            page = filtered[:limit]
            next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

            page = await _attach_current_status(db, page)

            logger.info(
                f"Signal snapshot feed: {len(raw_docs)} fetched "
                f"→ {len(filtered)} after filter → {len(page)} returned"
            )

            return {
                "signals": page,
                "count": len(page),
                "next_cursor": next_cursor,
                "source": "firestore_signal_snapshots",
                "filters": {
                    "signal_type": signal_type,
                    "confidence": "HIGH",
                    "show_all": show_all,
                    "cutoff_days": cutoff_days,
                },
            }

        except Exception as e:
            logger.error(f"Signal snapshot feed Firestore error: {e} — falling back to memory")

    # Fallback: current memory cache only. This cannot preserve historical snapshots.
    logger.info("Signal feed: serving from memory cache fallback")
    cached = signal_svc.get_all_cached()

    items = []
    for sym, data in cached.items():
        item = dict(data)
        item["symbol"] = sym
        for k in ("generated_at", "expires_at"):
            item[k] = _to_iso(item.get(k))
        items.append(item)

    items.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

    filtered = []
    for d in items:
        sig = d.get("signal", "").upper()
        conf = d.get("confidence", "").upper()
        trig = d.get("trigger", "")
        gen = d.get("generated_at", "")

        if sig not in ("BUY", "SELL"):
            continue
        if conf != "HIGH":
            continue
        if trig == "fallback":
            continue
        if not _is_fresh(gen, cutoff_iso):
            continue
        if signal_type and sig != signal_type.upper():
            continue

        filtered.append(d)

    if after:
        filtered = [i for i in filtered if i.get("generated_at", "") < after]

    page = filtered[:limit]
    next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

    return {
        "signals": page,
        "count": len(page),
        "next_cursor": next_cursor,
        "source": "memory_cache",
        "filters": {
            "signal_type": signal_type,
            "confidence": "HIGH",
            "show_all": show_all,
            "cutoff_days": cutoff_days,
        },
    }

@router.delete("/snapshot/{snapshot_id}")
async def delete_signal_snapshot(
    snapshot_id: str,
    admin=Depends(require_admin),
):
    db = get_db()

    try:
        loop = asyncio.get_event_loop()

        await loop.run_in_executor(
            None,
            lambda: db.collection("signal_snapshots").document(snapshot_id).delete()
        )

        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "deleted": "signal_snapshots",
            "deleted_by": admin["uid"],
        }

    except Exception as e:
        logger.error(f"Delete signal snapshot failed for {snapshot_id}: {e}")
        raise HTTPException(status_code=500, detail="Delete snapshot failed")
        
@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(False)):
    symbol = symbol.upper().strip()
    try:
        from main import get_current_session
        session = get_current_session()
    except Exception:
        session = "market"
    return await signal_svc.get_signal(symbol, force=force, session=session)
