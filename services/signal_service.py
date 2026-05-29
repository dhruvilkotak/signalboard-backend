"""
services/signal_service.py

Current signal schema:
  signals/{symbol} — latest active signal, BUY/SELL/HOLD, 45-day TTL
  signals/{symbol}/history/{auto_id} — previous active signals only when direction changes

Rules:
  - Initial HOLD is ignored and not stored in Firestore.
  - Initial BUY/SELL is stored.
  - BUY→BUY, SELL→SELL, HOLD→HOLD update current only.
  - BUY→SELL/HOLD, SELL→BUY/HOLD, HOLD→BUY/SELL archive old signal to history, then update current.
  - History keeps max 20 items and max 15 days.
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
HISTORY_TTL_DAYS   = 15   # keep history subcollection clean
HISTORY_MAX_ITEMS  = 20

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
                # await self._bump_expiry(symbol)
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

        signal = await self._engine.generate(
            symbol,
            session=session,
            trigger=trigger,
        )

        now = datetime.now(timezone.utc)
        signal["symbol"] = symbol
        signal["trigger"] = trigger
        signal["session"] = session
        signal["expires_at"] = (now + timedelta(days=FIRESTORE_TTL_DAYS)).isoformat()

        await self._save_to_firestore(symbol, signal)
        self._cache[symbol] = signal

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
            ref = self._db.collection("signals").document(symbol)

            doc = await loop.run_in_executor(None, ref.get)
            existing = doc.to_dict() if doc.exists else None

            new_signal = (signal.get("signal") or "HOLD").upper()

            # Rule: if no existing signal and new one is HOLD, ignore it.
            if not existing and new_signal == "HOLD":
                logger.info(f"SignalService: ignoring initial HOLD for {symbol}")
                return

            # Archive old active signal only when direction changes.
            if existing:
                old_signal = (existing.get("signal") or "HOLD").upper()

                if old_signal != new_signal:
                    old_snapshot = dict(existing)
                    old_snapshot["symbol"] = symbol
                    old_snapshot["archived_at"] = datetime.now(timezone.utc).isoformat()
                    old_snapshot["history_reason"] = f"{old_signal}_TO_{new_signal}"

                    await loop.run_in_executor(
                        None,
                        lambda: ref.collection("history").add(old_snapshot),
                    )
                    await loop.run_in_executor(
                        None,
                        lambda: self._prune_history(ref.collection("history"))
                    )

            # Always update current active signal if it exists or if new is BUY/SELL.
            await loop.run_in_executor(None, lambda: ref.set(signal))

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
    
    # def _is_meaningful_change(self, old: dict, new: dict) -> bool:
    #     if not old:
    #         return True

    #     if old.get("signal") != new.get("signal"):
    #         return True

    #     if old.get("confidence") != new.get("confidence"):
    #         return True

    #     try:
    #         old_score = float(old.get("conviction_score") or 0)
    #         new_score = float(new.get("conviction_score") or 0)
    #         if abs(new_score - old_score) >= 2:
    #             return True
    #     except Exception:
    #         pass

    #     def pct_changed(a, b, threshold=2.0):
    #         try:
    #             if a is None or b is None:
    #                 return a != b
    #             a = float(a)
    #             b = float(b)
    #             if a == 0:
    #                 return abs(b) > 0
    #             return abs((b - a) / a) * 100 >= threshold
    #         except Exception:
    #             return False

    #     if pct_changed(old.get("target_price"), new.get("target_price")):
    #         return True

    #     if pct_changed(old.get("stop_loss"), new.get("stop_loss")):
    #         return True

    #     try:
    #         old_ret = float(old.get("expected_return_pct") or 0)
    #         new_ret = float(new.get("expected_return_pct") or 0)
    #         if abs(new_ret - old_ret) >= 2:
    #             return True
    #     except Exception:
    #         pass

    #     return False

    # ── Firestore — signal_snapshots/{symbol} (latest) ───────────────────────
    # NEW SCHEMA:
    #   signal_snapshots/{symbol}            — latest signal doc (upsert)
    #   signal_snapshots/{symbol}/history/{id} — subcollection, 20-day TTL. Unused function. 

    # async def _save_snapshot_if_feed_eligible(self, symbol: str, signal: dict):
    #     """
    #     Upsert signal_snapshots/{symbol} with latest signal.
    #     Append to signal_snapshots/{symbol}/history subcollection.
    #     Prune history entries older than HISTORY_TTL_DAYS.
    #     Only for feed-eligible signals (HIGH BUY/SELL).
    #     """
    #     sig  = signal.get("signal", "HOLD")
    #     conf = signal.get("confidence", "LOW")

    #     if sig not in ("BUY", "SELL") or conf != "HIGH":
    #         return
    #     if not signal.get("feed_eligible"):
    #         return
    #     if not self._db:
    #         return

    #     try:
    #         loop = asyncio.get_running_loop()
    #         now  = datetime.now(timezone.utc)

    #         snapshot = dict(signal)
    #         snapshot["symbol"]              = symbol
    #         snapshot["snapshot_type"]       = "feed_signal"
    #         snapshot["snapshot_id"]         = f"{symbol}_{signal.get('generated_at', '')}"
    #         snapshot["snapshot_created_at"] = now.isoformat()

    #         # 1. Upsert signal_snapshots/{symbol} with latest signal
    #         snap_ref = self._db.collection("signal_snapshots").document(symbol)
    #         await loop.run_in_executor(None, lambda: snap_ref.set(snapshot))

    #         # 2. Append to history subcollection
    #         history_entry = {
    #             "signal":              sig,
    #             "confidence":          conf,
    #             "conviction_score":    signal.get("conviction_score", 0),
    #             "price_at_signal":     signal.get("price_at_signal", 0),
    #             "expected_return_pct": signal.get("expected_return_pct", 0),
    #             "target_price":        signal.get("target_price", 0),
    #             "stop_loss":           signal.get("stop_loss", 0),
    #             "session":             signal.get("session", ""),
    #             "generated_at":        signal.get("generated_at", now.isoformat()),
    #             "snapshot_id":         snapshot["snapshot_id"],
    #             "feed_eligible":       True,
    #         }
    #         hist_ref = snap_ref.collection("history")
    #         await loop.run_in_executor(None, lambda: hist_ref.add(history_entry))

    #         # 3. Prune history entries older than 20 days
    #         cutoff = (now - timedelta(days=HISTORY_TTL_DAYS)).isoformat()
    #         await loop.run_in_executor(
    #             None, lambda: self._prune_history(hist_ref, cutoff)
    #         )

    #         logger.info(
    #             f"SignalService: upserted snapshot + history for {symbol} {sig} {conf}"
    #         )

    #     except Exception as e:
    #         logger.error(f"SignalService: snapshot save failed for {symbol}: {e}")

    # # ── History read ──────────────────────────────────────────────────────────

    # async def get_signal_snapshot_history(self, symbol: str) -> list:
    #     """
    #     Return history subcollection for symbol ordered by generated_at DESC.
    #     Feed-eligible only (already filtered on write).
    #     """
    #     if not self._db:
    #         return []
    #     try:
    #         from firebase_admin import firestore as fs
    #         loop    = asyncio.get_running_loop()
    #         snap_ref = self._db.collection("signal_snapshots").document(symbol.upper())

    #         def _fetch():
    #             docs = (
    #                 snap_ref.collection("history")
    #                 .where("feed_eligible", "==", True)
    #                 .order_by("generated_at", direction=fs.Query.DESCENDING)
    #                 .limit(50)
    #                 .stream()
    #             )
    #             return [self._ser(d.to_dict() or {}) for d in docs]

    #         return await loop.run_in_executor(None, _fetch)
    #     except Exception as e:
    #         logger.error(f"SignalService: history fetch failed for {symbol}: {e}")
    #         return []

    # ── Feed read (one doc per symbol, no duplicates) ─────────────────────────

    # async def get_feed(self, cutoff_days: int = 7, confidence: str = "HIGH") -> list:
    #     """
    #     Read signal_snapshots collection — one doc per symbol.
    #     Returns list sorted by generated_at DESC.
    #     """
    #     if not self._db:
    #         return list(self._cache.values())
    #     try:
    #         from firebase_admin import firestore as fs
    #         loop    = asyncio.get_running_loop()
    #         cutoff  = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).isoformat()

    #         def _fetch():
    #             docs = (
    #                 self._db.collection("signal_snapshots")
    #                 .where("feed_eligible", "==", True)
    #                 .where("confidence",    "==", confidence)
    #                 .where("generated_at",  ">",  cutoff)
    #                 .order_by("generated_at", direction=fs.Query.DESCENDING)
    #                 .stream()
    #             )
    #             return [self._ser(d.to_dict() or {}) for d in docs]

    #         return await loop.run_in_executor(None, _fetch)
    #     except Exception as e:
    #         logger.error(f"SignalService: feed fetch failed: {e}")
    #         return []

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
    
    def _prune_history(self, hist_ref):
        try:
            from firebase_admin import firestore as fs

            cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)

            docs = list(
                hist_ref.order_by(
                    "generated_at",
                    direction=fs.Query.DESCENDING
                ).stream()
            )

            # Remove entries older than HISTORY_TTL_DAYS.
            for doc in docs:
                data = doc.to_dict() or {}
                gen_raw = data.get("generated_at")

                if not gen_raw:
                    continue

                try:
                    if isinstance(gen_raw, str):
                        gen = datetime.fromisoformat(gen_raw.replace("Z", "+00:00"))
                    elif hasattr(gen_raw, "isoformat"):
                        gen = gen_raw
                    else:
                        continue

                    if gen.tzinfo is None:
                        gen = gen.replace(tzinfo=timezone.utc)

                    if gen < cutoff:
                        doc.reference.delete()

                except Exception:
                    continue

            # Reload after age cleanup.
            docs = list(
                hist_ref.order_by(
                    "generated_at",
                    direction=fs.Query.DESCENDING
                ).stream()
            )

            # Keep newest HISTORY_MAX_ITEMS only.
            for doc in docs[HISTORY_MAX_ITEMS:]:
                doc.reference.delete()

        except Exception as e:
            logger.warning(f"History prune failed: {e}")