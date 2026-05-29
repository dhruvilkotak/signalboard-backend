"""
services/signal_service.py — v2 (per-symbol history subcollection)

Schema change:
  OLD: signal_snapshots/{auto_id}  — flat collection, duplicates per symbol
  NEW: signal_snapshots/{symbol}   — one doc per symbol (latest signal)
       signal_snapshots/{symbol}/history/{snapshot_id}  — subcollection, 20-day TTL

Feed reads from signal_snapshots (one doc per symbol, no duplicates).
History reads from signal_snapshots/{symbol}/history ordered by generated_at DESC.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta

from config import settings

logger = logging.getLogger(__name__)

# Lazy import to avoid circular — metrics module imports nothing from services
def _metrics_increment(key: str):
    try:
        from routers.metrics import increment
        increment(key)
    except Exception:
        pass

CACHE_TTL_SECONDS  = getattr(settings, "SIGNAL_CACHE_TTL", 1800)  # 30 min
FIRESTORE_TTL_DAYS = 45
HISTORY_TTL_DAYS   = 20   # keep history subcollection clean


class SignalService:
    def __init__(self, price_service, news_service,
                 technical_service=None, market_service=None):
        self.price_service = price_service
        self.news_service  = news_service
        self.tech_svc      = technical_service
        self.market_svc    = market_service
        self._cache: dict  = {}
        self._db           = None
        self._engine       = None

    def set_db(self, db):
        self._db = db
        logger.info("SignalService v2: Firestore connected ✓")

    def set_engine(self, engine):
        self._engine = engine
        logger.info("SignalService v2: SignalEngine connected ✓")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_signal(self, symbol: str, force: bool = False,
                         session: str = "market", trigger: str = "scheduled") -> dict:
        symbol = symbol.upper().strip()

        if not force:
            cached = self._cache.get(symbol)
            if cached and self._is_fresh(cached):
                return cached

        if not force and self._db:
            stored = await self._load_from_firestore(symbol)
            if stored:
                self._cache[symbol] = stored
                _metrics_increment("cache_hits_today")
                await self._bump_expiry(symbol)
                return stored

        return await self._generate(symbol, session, trigger)

    async def get_all_signals(self, force: bool = False,
                              session: str = "market") -> dict:
        tickers = await self._get_tickers()
        results = {}
        for sym in tickers:
            try:
                results[sym] = await self.get_signal(sym, force=force, session=session)
            except Exception as e:
                logger.error(f"SignalService: get_signal failed for {sym}: {e}")
        return results

    def get_all_cached(self) -> dict:
        return dict(self._cache)

    def get_signal_history(self, symbol: str) -> list:
        return []

    def invalidate(self, symbol: str):
        self._cache.pop(symbol.upper(), None)

    # ── Generation ────────────────────────────────────────────────────────────

    async def _generate(self, symbol: str, session: str, trigger: str) -> dict:
        if not self._engine:
            raise RuntimeError("SignalEngine not initialised")

        loop   = asyncio.get_running_loop()
        signal = await loop.run_in_executor(
            None,
            lambda: self._engine.generate(symbol, session=session, trigger=trigger),
        )
        signal["symbol"]  = symbol
        signal["trigger"] = trigger
        signal["session"] = session

        self._cache[symbol] = signal
        _metrics_increment("signals_generated_today")
        _metrics_increment("claude_calls_today")
        await self._save_to_firestore(symbol, signal)
        await self._save_snapshot_if_feed_eligible(symbol, signal)
        return signal

    # ── Firestore — signals/{symbol} (current signal, TTL) ───────────────────

    async def _load_from_firestore(self, symbol: str):
        try:
            loop = asyncio.get_running_loop()
            doc  = await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals").document(symbol).get(),
            )
            if not doc.exists:
                return None
            data = doc.to_dict() or {}
            if not self._is_fresh(data):
                return None
            return data
        except Exception as e:
            logger.error(f"SignalService: Firestore load failed for {symbol}: {e}")
            return None

    async def _save_to_firestore(self, symbol: str, signal: dict):
        if not self._db:
            return
        try:
            loop = asyncio.get_running_loop()
            ref  = self._db.collection("signals").document(symbol)
            doc  = await loop.run_in_executor(None, ref.get)
            existing = doc.to_dict() if doc.exists else None

            if existing:
                changed = existing.get("signal") != signal.get("signal")
                await loop.run_in_executor(None, lambda: ref.set(signal))
                if changed:
                    await loop.run_in_executor(
                        None, lambda: ref.collection("history").add(signal)
                    )
            else:
                await loop.run_in_executor(None, lambda: ref.set(signal))
                await loop.run_in_executor(
                    None, lambda: ref.collection("history").add(signal)
                )
        except Exception as e:
            logger.error(f"SignalService: Firestore save failed for {symbol}: {e}")

    async def _bump_expiry(self, symbol: str):
        if not self._db:
            return
        try:
            loop    = asyncio.get_running_loop()
            new_exp = (datetime.now(timezone.utc) + timedelta(days=FIRESTORE_TTL_DAYS)).isoformat()
            await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals").document(symbol).update(
                    {"expires_at": new_exp}
                ),
            )
        except Exception:
            pass

    # ── Firestore — signal_snapshots/{symbol} (latest) ───────────────────────
    # NEW SCHEMA:
    #   signal_snapshots/{symbol}            — latest signal doc (upsert)
    #   signal_snapshots/{symbol}/history/{id} — subcollection, 20-day TTL

    async def _save_snapshot_if_feed_eligible(self, symbol: str, signal: dict):
        """
        Upsert signal_snapshots/{symbol} with latest signal.
        Append to signal_snapshots/{symbol}/history subcollection.
        Prune history entries older than HISTORY_TTL_DAYS.
        Only for feed-eligible signals (HIGH BUY/SELL).
        """
        sig  = signal.get("signal", "HOLD")
        conf = signal.get("confidence", "LOW")

        if sig not in ("BUY", "SELL") or conf != "HIGH":
            return
        if not signal.get("feed_eligible"):
            return
        if not self._db:
            return

        try:
            loop = asyncio.get_running_loop()
            now  = datetime.now(timezone.utc)

            snapshot = dict(signal)
            snapshot["symbol"]              = symbol
            snapshot["snapshot_type"]       = "feed_signal"
            snapshot["snapshot_id"]         = f"{symbol}_{signal.get('generated_at', '')}"
            snapshot["snapshot_created_at"] = now.isoformat()

            # 1. Upsert signal_snapshots/{symbol} with latest signal
            snap_ref = self._db.collection("signal_snapshots").document(symbol)
            await loop.run_in_executor(None, lambda: snap_ref.set(snapshot))

            # 2. Append to history subcollection
            history_entry = {
                "signal":              sig,
                "confidence":          conf,
                "conviction_score":    signal.get("conviction_score", 0),
                "price_at_signal":     signal.get("price_at_signal", 0),
                "expected_return_pct": signal.get("expected_return_pct", 0),
                "target_price":        signal.get("target_price", 0),
                "stop_loss":           signal.get("stop_loss", 0),
                "session":             signal.get("session", ""),
                "generated_at":        signal.get("generated_at", now.isoformat()),
                "snapshot_id":         snapshot["snapshot_id"],
                "feed_eligible":       True,
            }
            hist_ref = snap_ref.collection("history")
            await loop.run_in_executor(None, lambda: hist_ref.add(history_entry))

            # 3. Prune history entries older than 20 days
            cutoff = (now - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
            await loop.run_in_executor(
                None, lambda: self._prune_history(hist_ref, cutoff)
            )

            logger.info(
                f"SignalService: upserted snapshot + history for {symbol} {sig} {conf}"
            )

        except Exception as e:
            logger.error(f"SignalService: snapshot save failed for {symbol}: {e}")

    def _prune_history(self, hist_ref, cutoff_iso: str):
        """Delete history entries older than cutoff. Runs in executor."""
        try:
            old_docs = (
                hist_ref
                .where("generated_at", "<", cutoff_iso)
                .stream()
            )
            for doc in old_docs:
                doc.reference.delete()
        except Exception as e:
            logger.warning(f"SignalService: history prune failed: {e}")

    # ── History read ──────────────────────────────────────────────────────────

    async def get_signal_snapshot_history(self, symbol: str) -> list:
        """
        Return history subcollection for symbol ordered by generated_at DESC.
        Feed-eligible only (already filtered on write).
        """
        if not self._db:
            return []
        try:
            from firebase_admin import firestore as fs
            loop    = asyncio.get_running_loop()
            snap_ref = self._db.collection("signal_snapshots").document(symbol.upper())

            def _fetch():
                docs = (
                    snap_ref.collection("history")
                    .where("feed_eligible", "==", True)
                    .order_by("generated_at", direction=fs.Query.DESCENDING)
                    .limit(50)
                    .stream()
                )
                return [self._ser(d.to_dict() or {}) for d in docs]

            return await loop.run_in_executor(None, _fetch)
        except Exception as e:
            logger.error(f"SignalService: history fetch failed for {symbol}: {e}")
            return []

    # ── Feed read (one doc per symbol, no duplicates) ─────────────────────────

    async def get_feed(self, cutoff_days: int = 7, confidence: str = "HIGH") -> list:
        """
        Read signal_snapshots collection — one doc per symbol.
        Returns list sorted by generated_at DESC.
        """
        if not self._db:
            return list(self._cache.values())
        try:
            from firebase_admin import firestore as fs
            loop    = asyncio.get_running_loop()
            cutoff  = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).isoformat()

            def _fetch():
                docs = (
                    self._db.collection("signal_snapshots")
                    .where("feed_eligible", "==", True)
                    .where("confidence",    "==", confidence)
                    .where("generated_at",  ">",  cutoff)
                    .order_by("generated_at", direction=fs.Query.DESCENDING)
                    .stream()
                )
                return [self._ser(d.to_dict() or {}) for d in docs]

            return await loop.run_in_executor(None, _fetch)
        except Exception as e:
            logger.error(f"SignalService: feed fetch failed: {e}")
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_fresh(self, signal: dict) -> bool:
        try:
            exp = signal.get("expires_at", "")
            if not exp:
                return False
            if isinstance(exp, str):
                exp = datetime.fromisoformat(exp)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > datetime.now(timezone.utc)
        except Exception:
            return False

    def _ser(self, data: dict) -> dict:
        out = {}
        for k, v in (data or {}).items():
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            elif hasattr(v, "seconds"):
                out[k] = datetime.fromtimestamp(
                    v.seconds, tz=timezone.utc
                ).isoformat()
            else:
                out[k] = v
        return out

    async def _get_tickers(self) -> list:
        from services.portfolio_service import PortfolioService
        catalogue = PortfolioService.get_strategy_catalogue()
        tickers   = set()
        for cfg in catalogue.values():
            tickers.update(cfg.get("universe", []))
        return sorted(tickers)