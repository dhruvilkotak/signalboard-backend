"""
services/signal_service.py

Scheduler signal service — uses SignalEngine for unified signal generation.
Writes latest signal to Firestore signals/{symbol} with 45-day sliding TTL.

Snapshot behavior:
  - Writes immutable feed snapshots to signal_snapshots only for meaningful HIGH BUY/SELL signals.
  - Avoids duplicate near-identical snapshots for the same symbol within the last 15 days.
  - Creates a new snapshot when signal/confidence/risk/timeframe/target/stop/return/score changes meaningfully.

3-tier lookup:
  1. Memory cache  (30 min)
  2. Firestore     (45 day TTL)
  3. SignalEngine  (generates fresh via Claude)
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta

from config import settings

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = getattr(settings, "SIGNAL_CACHE_TTL", 1800)  # 30 min memory
FIRESTORE_TTL_DAYS = 45
SNAPSHOT_DEDUP_DAYS = 15


class SignalService:
    def __init__(
        self,
        price_service,
        news_service,
        technical_service=None,
        market_service=None,
    ):
        self.price_service = price_service
        self.news_service = news_service
        self.tech_svc = technical_service
        self.market_svc = market_service
        self._cache: dict = {}
        self._db = None
        self._engine = None

    def set_db(self, db):
        self._db = db
        logger.info("SignalService: Firestore connected ✓")

    def set_engine(self, engine):
        """Inject SignalEngine after all services are initialised."""
        self._engine = engine
        logger.info("SignalService: SignalEngine connected ✓")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_signal(
        self,
        symbol: str,
        force: bool = False,
        session: str = "market",
        trigger: str = "scheduled",
    ) -> dict:
        symbol = symbol.upper().strip()

        # Tier 1: memory cache
        if not force:
            cached = self._cache.get(symbol)
            if cached and self._is_fresh(cached):
                return cached

        # Tier 2: Firestore
        if not force and self._db:
            stored = await self._load_from_firestore(symbol)
            if stored:
                self._cache[symbol] = stored
                await self._bump_expiry(symbol)
                return stored

        # Tier 3: generate
        return await self._generate(symbol, session, trigger)

    async def get_all_signals(self, force: bool = False, session: str = "market") -> dict:
        """Legacy method — kept for compatibility. Uses price_service ticker list."""
        try:
            prices = await self.price_service.get_all()
        except Exception:
            prices = {}

        results = {}
        for symbol in prices:
            results[symbol] = await self.get_signal(symbol, force=force, session=session)
        return results

    def get_all_cached(self) -> dict:
        return self._cache.copy()

    def get_signal_history(self, symbol: str) -> list:
        return []

    def invalidate(self, symbol: str):
        self._cache.pop(symbol.upper(), None)

    # ── Generation ────────────────────────────────────────────────────────────

    _BLOCKED = {"FEED", "STREAM", "SIGNAL", "ALL", "TSLA"}

    async def _generate(self, symbol: str, session: str, trigger: str) -> dict:
        if symbol in self._BLOCKED:
            logger.warning(f"Blocked signal generation for non-admin symbol: {symbol}")
            return self._fallback(symbol, session)

        if not self._engine:
            logger.error("SignalService: SignalEngine not set — returning fallback")
            return self._fallback(symbol, session)

        try:
            signal = await self._engine.generate(symbol, session=session, trigger=trigger)
        except Exception as e:
            logger.error(f"SignalService generate failed for {symbol}: {e}")
            return self._fallback(symbol, session)

        now = datetime.now(timezone.utc)
        signal["expires_at"] = (now + timedelta(days=FIRESTORE_TTL_DAYS)).isoformat()

        self._cache[symbol] = signal

        await self._save_to_firestore(symbol, signal)
        await self._save_snapshot_if_feed_eligible(symbol, signal)

        return signal

    # ── Firestore current signal ──────────────────────────────────────────────

    async def _load_from_firestore(self, symbol: str):
        if not self._db:
            return None

        try:
            loop = asyncio.get_running_loop()
            doc = await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals").document(symbol).get(),
            )

            if not doc.exists:
                return None

            data = doc.to_dict() or {}

            for k in ("generated_at", "expires_at"):
                v = data.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    data[k] = v.isoformat()

            expires = data.get("expires_at")
            if expires:
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)

                if datetime.now(timezone.utc) > exp:
                    return None

            return data

        except Exception as e:
            logger.warning(f"Firestore load failed for {symbol}: {e}")
            return None

    async def _save_to_firestore(self, symbol: str, signal: dict):
        if not self._db:
            return

        try:
            loop = asyncio.get_running_loop()
            ref = self._db.collection("signals").document(symbol)

            existing = await loop.run_in_executor(None, ref.get)
            await loop.run_in_executor(None, lambda: ref.set(signal))

            # Write history only when signal direction changes.
            if existing.exists:
                old = existing.to_dict() or {}
                if old.get("signal") != signal.get("signal"):
                    await loop.run_in_executor(
                        None,
                        lambda: ref.collection("history").add(signal),
                    )
            else:
                await loop.run_in_executor(
                    None,
                    lambda: ref.collection("history").add(signal),
                )

        except Exception as e:
            logger.warning(f"Firestore save failed for {symbol}: {e}")

    async def _bump_expiry(self, symbol: str):
        if not self._db:
            return

        try:
            new_exp = (datetime.now(timezone.utc) + timedelta(days=FIRESTORE_TTL_DAYS)).isoformat()
            loop = asyncio.get_running_loop()

            await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals").document(symbol).update(
                    {"expires_at": new_exp}
                ),
            )

        except Exception:
            pass

    # ── Snapshot feed logic ───────────────────────────────────────────────────

    async def _save_snapshot_if_feed_eligible(self, symbol: str, signal: dict):
        """
        Save immutable feed snapshot only when meaningful.

        Rules:
          - Only BUY/SELL + HIGH confidence + non-fallback
          - Look at most recent snapshot for same symbol in last 15 days
          - If almost identical, skip writing duplicate snapshot
          - If signal/confidence/risk/timeframe/target/stop/return/score changed meaningfully, write snapshot
        """
        if not self._db:
            return

        sig = (signal.get("signal") or "").upper()
        conf = (signal.get("confidence") or "").upper()
        trigger = signal.get("trigger", "")

        if sig not in ("BUY", "SELL"):
            return
        if conf != "HIGH":
            return
        if trigger == "fallback":
            return

        try:
            loop = asyncio.get_running_loop()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_DEDUP_DAYS)).isoformat()

            def _latest_snapshot():
                from google.cloud.firestore_v1 import Query as FSQuery

                docs = (
                    self._db.collection("signal_snapshots")
                    .where("symbol", "==", symbol)
                    .where("generated_at", ">=", cutoff)
                    .order_by("generated_at", direction=FSQuery.DESCENDING)
                    .limit(1)
                    .stream()
                )

                for doc in docs:
                    return doc.to_dict() or {}

                return None

            latest = await loop.run_in_executor(None, _latest_snapshot)

            if latest and self._is_snapshot_similar(latest, signal):
                logger.info(
                    f"Skipped duplicate snapshot for {symbol}: similar to latest "
                    f"{SNAPSHOT_DEDUP_DAYS}-day snapshot"
                )
                return

            snapshot = dict(signal)
            snapshot["symbol"] = symbol
            snapshot["snapshot_type"] = "feed_signal"
            snapshot["snapshot_id"] = f"{symbol}_{snapshot.get('generated_at', '')}"
            snapshot["snapshot_created_at"] = datetime.now(timezone.utc).isoformat()

            await loop.run_in_executor(
                None,
                lambda: self._db.collection("signal_snapshots").add(snapshot),
            )

            logger.info(f"Saved feed snapshot for {symbol}: {sig} {conf}")

        except Exception as e:
            logger.warning(f"Snapshot save failed for {symbol}: {e}")

    def _is_snapshot_similar(self, old: dict, new: dict) -> bool:
        """
        True means: do not create a duplicate snapshot.
        Compares decision fields only, not long text such as summary/bull/bear cases.
        """
        old_sig = (old.get("signal") or "").upper()
        new_sig = (new.get("signal") or "").upper()

        old_conf = (old.get("confidence") or "").upper()
        new_conf = (new.get("confidence") or "").upper()

        if old_sig != new_sig:
            return False

        if old_conf != new_conf:
            return False

        old_risk = (old.get("risk") or "").upper()
        new_risk = (new.get("risk") or "").upper()

        if old_risk != new_risk:
            return False

        if old.get("timeframe") != new.get("timeframe"):
            return False

        def pct_diff(a, b):
            try:
                if a is None or b is None:
                    return 0 if a == b else 999

                a = float(a)
                b = float(b)

                if a == 0:
                    return abs(b)

                return abs((b - a) / a) * 100
            except Exception:
                return 999

        # Material thresholds
        if pct_diff(old.get("target_price"), new.get("target_price")) > 2:
            return False

        if pct_diff(old.get("stop_loss"), new.get("stop_loss")) > 2:
            return False

        old_ret = old.get("expected_return_pct")
        new_ret = new.get("expected_return_pct")

        try:
            if old_ret is not None and new_ret is not None:
                if abs(float(new_ret) - float(old_ret)) > 2:
                    return False
            elif old_ret != new_ret:
                return False
        except Exception:
            return False

        old_score = old.get("conviction_score")
        new_score = new.get("conviction_score")

        try:
            if old_score is not None and new_score is not None:
                if abs(float(new_score) - float(old_score)) >= 2:
                    return False
            elif old_score != new_score:
                return False
        except Exception:
            return False

        return True

    # ── Freshness / fallback ──────────────────────────────────────────────────

    def _is_fresh(self, signal: dict) -> bool:
        gen = signal.get("generated_at")

        if not gen:
            return False

        try:
            t = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)

            return (datetime.now(timezone.utc) - t).total_seconds() < CACHE_TTL_SECONDS

        except Exception:
            return False

    def _fallback(self, symbol: str, session: str = "market") -> dict:
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "confidence": "LOW",
            "conviction_score": 0,
            "target_price": None,
            "stop_loss": None,
            "expected_return_pct": 0,
            "timeframe": "unknown",
            "timeframe_days": 30,
            "summary": "Signal generation failed. Using fallback HOLD.",
            "risk": "HIGH",
            "key_factors": ["data_unavailable"],
            "insider_summary": "No data.",
            "sentiment_summary": "No data.",
            "price_targets": {},
            "bull_case": "Insufficient data.",
            "bear_case": "Insufficient data.",
            "insider_trades": [],
            "sentiment": {},
            "session": session,
            "trigger": "fallback",
            "feed_eligible": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
