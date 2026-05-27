"""
routers/signals.py

Feed filtering rules:
  - signal IN (BUY, SELL)
  - confidence == HIGH only
  - generated_at > 7 days ago (default) / 45 days (show_all=true)
  - trigger != "fallback"
  - Deduplicated by symbol (latest signal per symbol only)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from services.firebase_service import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

signal_svc = None
price_svc  = None

STALE_DAYS   = 7
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


def _dedup_by_symbol(items: list) -> list:
    """Keep only the most recent signal per symbol."""
    seen = {}
    for item in items:
        sym = item.get("symbol", "")
        if sym not in seen:
            seen[sym] = item
        else:
            # Keep whichever has a newer generated_at
            existing_iso = _to_iso(seen[sym].get("generated_at", ""))
            current_iso  = _to_iso(item.get("generated_at", ""))
            if current_iso > existing_iso:
                seen[sym] = item
    return list(seen.values())


# ── Existing endpoints ────────────────────────────────────────────────────────

@router.get("/")
async def get_all_signals():
    cached = signal_svc.get_all_cached()
    return {"signals": cached, "count": len(cached)}


@router.get("/stream")
async def get_signal_feed(
    limit:       int  = Query(default=20, ge=1, le=100),
    after:       Optional[str] = Query(default=None),
    signal_type: Optional[str] = Query(default=None, description="BUY | SELL"),
    show_all:    bool = Query(default=False, description="Show full 45-day history"),
):
    """
    Filtered signal feed — HIGH confidence BUY/SELL only, deduplicated by symbol.
    Default: last 7 days. Use show_all=true for 45-day history.
    """
    db = None
    try:
        db = get_db()
    except Exception:
        pass

    cutoff_days = HISTORY_DAYS if show_all else STALE_DAYS
    cutoff_dt   = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    cutoff_iso  = cutoff_dt.isoformat()

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
                # Fetch generously — dedup + filter will reduce
                return list(q.limit(200).stream())

            loop     = asyncio.get_event_loop()
            raw_docs = await loop.run_in_executor(None, _query)

            items = []
            for doc in raw_docs:
                d = doc.to_dict() or {}
                d["symbol"] = doc.id
                for k in ("generated_at", "expires_at"):
                    d[k] = _to_iso(d.get(k))
                items.append(d)

            # ── Deduplicate by symbol first (latest per symbol) ───────────────
            items = _dedup_by_symbol(items)

            # ── Sort by generated_at DESC after dedup ─────────────────────────
            items.sort(key=lambda x: x.get("generated_at", ""), reverse=True)

            # ── Apply feed filters ────────────────────────────────────────────
            filtered = []
            for d in items:
                sig  = d.get("signal", "").upper()
                conf = d.get("confidence", "").upper()
                trig = d.get("trigger", "")
                gen  = d.get("generated_at", "")

                if sig  not in ("BUY", "SELL"):   continue
                if conf != "HIGH":                 continue  # HIGH only
                if trig == "fallback":             continue
                if not _is_fresh(gen, cutoff_iso): continue
                if signal_type and sig != signal_type.upper(): continue

                filtered.append(d)

            page        = filtered[:limit]
            next_cursor = page[-1]["generated_at"] if len(filtered) > limit else None

            logger.info(
                f"Signal feed: {len(raw_docs)} fetched → {len(items)} after dedup "
                f"→ {len(filtered)} after filter → {len(page)} returned"
            )

            return {
                "signals":     page,
                "count":       len(page),
                "next_cursor": next_cursor,
                "source":      "firestore",
                "filters": {
                    "signal_type": signal_type,
                    "confidence":  "HIGH",
                    "show_all":    show_all,
                    "cutoff_days": cutoff_days,
                },
            }

        except Exception as e:
            logger.error(f"Signal feed Firestore error: {e} — falling back to memory")

    # ── Memory cache fallback ─────────────────────────────────────────────────
    logger.info("Signal feed: serving from memory cache")
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
        sig  = d.get("signal", "").upper()
        conf = d.get("confidence", "").upper()
        trig = d.get("trigger", "")
        gen  = d.get("generated_at", "")

        if sig  not in ("BUY", "SELL"):   continue
        if conf != "HIGH":                 continue
        if trig == "fallback":             continue
        if not _is_fresh(gen, cutoff_iso): continue
        if signal_type and sig != signal_type.upper(): continue

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
            "confidence":  "HIGH",
            "show_all":    show_all,
            "cutoff_days": cutoff_days,
        },
    }


@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(False)):
    symbol = symbol.upper().strip()
    try:
        from main import get_current_session
        session = get_current_session()
    except Exception:
        session = "market"
    return await signal_svc.get_signal(symbol, force=force, session=session)