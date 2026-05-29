"""
services/ondemand_signal_service.py

On-demand signal service for Live Prices → AI Signal tab.
Uses SignalEngine for identical signal generation as signal_service.py.
Writes to Firestore signals_ondemand/{symbol} with session-based TTL.
Shared cache — one Claude call per ticker per session across ALL users.

TTL strategy (session-based, not flat 24h):
  pre_market  signal → expires when market opens  (9:30am ET)
  market      signal → expires when market closes (4:00pm ET)
  post_market signal → expires next pre-market    (4:00am ET next day)
  closed      signal → expires next pre-market    (4:00am ET next day)
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 24   # fallback only
ET = ZoneInfo("America/New_York")


def _session_expiry() -> datetime:
    """
    Return the UTC datetime when the current signal should expire.
    Signals expire at the end of the current market session.
    """
    now_et = datetime.now(ET)
    hour   = now_et.hour

    if 4 <= hour < 9:
        # Pre-market → expires at market open 9:30am ET today
        exp_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elif 9 <= hour < 16:
        # Market hours → expires at close 4:00pm ET today
        exp_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    elif 16 <= hour < 20:
        # Post-market → expires at 4:00am ET next day (next pre-market)
        exp_et = (now_et + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    else:
        # Overnight closed → expires at 4:00am ET next day
        exp_et = (now_et + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)

    return exp_et.astimezone(timezone.utc)


def _current_session() -> str:
    hour = datetime.now(ET).hour
    if 4  <= hour < 9:  return "pre_market"
    if 9  <= hour < 16: return "market"
    if 16 <= hour < 20: return "post_market"
    return "closed"


class OnDemandSignalService:
    def __init__(self, price_service, news_service):
        self.price_svc = price_service
        self.news_svc  = news_service
        self._cache: dict = {}
        self._db          = None
        self._engine      = None

    def set_db(self, db):
        self._db = db
        logger.info("OnDemandSignalService: Firestore connected ✓")

    def set_engine(self, engine):
        self._engine = engine
        logger.info("OnDemandSignalService: SignalEngine connected ✓")

    async def get_signal(self, symbol: str) -> dict:
        symbol = symbol.upper().strip()

        cached = self._cache.get(symbol)
        if cached and self._is_fresh(cached):
            logger.info(f"OnDemand [{symbol}]: memory cache hit")
            return cached

        if self._db:
            stored = await self._load_from_firestore(symbol)
            if stored:
                self._cache[symbol] = stored
                logger.info(f"OnDemand [{symbol}]: Firestore cache hit")
                return stored

        return await self._generate(symbol)

    async def _generate(self, symbol: str) -> dict:
        if not self._engine:
            logger.error("OnDemandSignalService: SignalEngine not set")
            return self._fallback(symbol)
        try:
            signal = await self._engine.generate(symbol, session="market", trigger="on_demand")
        except Exception as e:
            logger.error(f"OnDemand generate failed for {symbol}: {e}")
            return self._fallback(symbol)

        now    = datetime.now(timezone.utc)
        exp    = _session_expiry()
        signal["expires_at"] = exp.isoformat()
        signal["session"]    = _current_session()
        signal["source"]     = "ondemand"
        logger.info(f"OnDemand [{symbol}]: TTL until {exp.isoformat()} ({_current_session()})")

        self._cache[symbol] = signal
        await self._save_to_firestore(symbol, signal)
        logger.info(f"OnDemand [{symbol}]: {signal['signal']}/{signal['confidence']} generated")
        return signal

    async def _load_from_firestore(self, symbol: str):
        if not self._db:
            return None
        try:
            loop = asyncio.get_event_loop()
            doc  = await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals_ondemand").document(symbol).get()
            )
            if not doc.exists:
                return None
            data = doc.to_dict()
            for k in ("generated_at", "expires_at"):
                v = data.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    data[k] = v.isoformat()
            return data if self._is_fresh(data) else None
        except Exception as e:
            logger.warning(f"OnDemand Firestore load failed for {symbol}: {e}")
            return None

    async def _save_to_firestore(self, symbol: str, signal: dict):
        if not self._db:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals_ondemand").document(symbol).set(signal)
            )
        except Exception as e:
            logger.warning(f"OnDemand Firestore save failed for {symbol}: {e}")

    def _is_fresh(self, signal: dict) -> bool:
        expires = signal.get("expires_at")
        if not expires:
            return False
        try:
            exp = datetime.fromisoformat(expires)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < exp
        except Exception:
            return False

    def invalidate(self, symbol: str):
        """Force-expire cache for symbol — called on price spike >2%."""
        sym = symbol.upper().strip()
        if sym in self._cache:
            del self._cache[sym]
            logger.info(f"OnDemand [{sym}]: cache invalidated (price spike)")

    def _fallback(self, symbol: str) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "symbol": symbol, "signal": "HOLD", "confidence": "LOW",
            "conviction_score": 0, "target_price": None, "stop_loss": None,
            "expected_return_pct": 0, "timeframe": "unknown", "timeframe_days": 30,
            "summary": "Signal generation failed — data unavailable.", "risk": "HIGH",
            "key_factors": ["data_unavailable"], "insider_summary": "No data.",
            "sentiment_summary": "No data.", "price_targets": {},
            "bull_case": "Insufficient data.", "bear_case": "Insufficient data.",
            "insider_trades": [], "sentiment": {},
            "generated_at": now.isoformat(),
            "expires_at": _session_expiry().isoformat(),
            "trigger": "fallback", "source": "ondemand", "feed_eligible": False,
        }