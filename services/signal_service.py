"""
services/signal_service.py

3-tier signal retrieval:
  1. In-memory cache     → return if fresh (< SIGNAL_CACHE_TTL seconds, default 30 min)
  2. Firestore           → load into cache if exists, bump expires_at, return
  3. Claude Haiku        → generate fresh, store in Firestore + cache, return

Price data for custom symbols (not in price_service cache):
  → fetched on-demand via /api/quote (Yahoo Finance)

Signal metadata always includes:
  generated_at, price_at_signal, session, trigger, expires_at
"""

import json
import logging
import os
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
from anthropic import AsyncAnthropic
from config import settings

logger = logging.getLogger(__name__)

# ── Semaphore — max 10 concurrent Claude calls (protects e2-micro RAM) ────────
_semaphore = asyncio.Semaphore(10)

# ── How long to keep signal fresh in memory ───────────────────────────────────
CACHE_TTL_SECONDS = getattr(settings, "SIGNAL_CACHE_TTL", 1800)  # 30 min default

# ── Firestore TTL — 30 days sliding ───────────────────────────────────────────
FIRESTORE_TTL_DAYS = 30

SESSION_CONTEXT = {
    "pre_market": {
        "label": "Pre-Market (4:00-9:30 AM EST)",
        "instructions": """
You are analyzing in PRE-MARKET hours (before 9:30 AM EST).
Key considerations:
- Focus on overnight news, earnings releases, and futures direction
- Pre-market volume is thin — be MORE cautious with signals
- Earnings surprises (beats/misses) are the strongest pre-market catalyst
- Prefer MEDIUM/LOW confidence unless there is a clear earnings catalyst
- Signal is for preparation — actual trades execute at market open
""",
    },
    "market": {
        "label": "Market Hours (9:30 AM - 4:00 PM EST)",
        "instructions": """
You are analyzing during REGULAR MARKET HOURS.
Key considerations:
- Full liquidity — signals can be acted on immediately
- Technical indicators are most reliable during market hours
- Volume confirmation is important
- HIGH confidence signals can be auto-traded
""",
    },
    "post_market": {
        "label": "Post-Market (4:00-8:00 PM EST)",
        "instructions": """
You are analyzing in POST-MARKET hours (after 4:00 PM EST).
Key considerations:
- Focus on earnings results just released after close
- After-hours price moves indicate next day opening direction
- Earnings beats with raised guidance = strong BUY signal for next open
- Earnings misses = strong SELL signal for next open
- This signal is for NEXT DAY preparation, not immediate trading
""",
    },
    "closed": {
        "label": "Market Closed",
        "instructions": "Markets are currently closed. Providing informational signal only.",
    },
}


class SignalService:
    def __init__(
        self,
        price_service,
        news_service,
        technical_service=None,
        market_service=None,
    ):
        self.client        = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.price_service = price_service
        self.news_service  = news_service
        self.technical_svc = technical_service
        self.market_svc    = market_service
        self._cache: dict  = {}   # symbol → signal dict
        self._history: dict = {}  # symbol → list of last 20 signals
        self._db           = None  # injected after Firebase init

    def set_db(self, db):
        """Inject Firestore client after Firebase initialises."""
        self._db = db
        logger.info("SignalService: Firestore connected ✓")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_signal(
        self,
        symbol: str,
        force: bool = False,
        session: str = "market",
        trigger: str = "manual",
    ) -> dict:
        """
        3-tier lookup:
          1. In-memory cache (if fresh and not forced)
          2. Firestore       (if cache miss)
          3. Claude Haiku    (if Firestore miss or force=True)
        """
        symbol = symbol.upper().strip()

        # ── Tier 1: memory cache ──────────────────────────────────────────────
        if not force:
            cached = self._cache.get(symbol)
            if cached and self._is_fresh(cached):
                return cached

        # ── Tier 2: Firestore ─────────────────────────────────────────────────
        if not force and self._db:
            stored = await self._load_from_firestore(symbol)
            if stored:
                self._cache[symbol] = stored
                await self._bump_expiry(symbol)
                return stored

        # ── Tier 3: Generate via Claude ───────────────────────────────────────
        return await self._generate_signal(symbol, session=session, trigger=trigger)

    async def get_all_signals(
        self,
        force: bool = False,
        session: str = "market",
    ) -> dict:
        """Generate/retrieve signals for all tracked symbols."""
        try:
            prices = await self.price_service.get_all()
        except Exception:
            prices = {}

        market_ctx = await self._get_market_context()
        results    = {}

        for symbol in prices:
            cached = self._cache.get(symbol)
            if cached and not force and self._is_fresh(cached):
                results[symbol] = cached
                continue
            signal = await self._generate_signal(
                symbol, market_ctx=market_ctx, session=session, trigger="scheduled"
            )
            results[symbol] = signal
        return results

    def get_signal_history(self, symbol: str) -> list:
        return self._history.get(symbol.upper(), [])

    def get_all_cached(self) -> dict:
        return self._cache.copy()

    def invalidate(self, symbol: str):
        """Force cache miss on next access (call on price spike)."""
        self._cache.pop(symbol.upper(), None)

    # ── Price fetch (custom symbols not in price_service cache) ───────────────

    async def _fetch_price_on_demand(self, symbol: str) -> dict:
        """Fetch price from Yahoo Finance for any symbol not in price cache."""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                res = await client.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params={"interval": "1d", "range": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                data = res.json()
                meta = (
                    data.get("chart", {})
                    .get("result", [{}])[0]
                    .get("meta", {})
                )
                price     = meta.get("regularMarketPrice")
                prev      = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
                volume    = meta.get("regularMarketVolume", 0)
                high      = meta.get("regularMarketDayHigh", price)
                low       = meta.get("regularMarketDayLow", price)
                change_pct = round(((price - prev) / prev * 100), 2) if prev else 0

                return {
                    "price":      price,
                    "prev_close": prev,
                    "change_pct": change_pct,
                    "change_amt": round(price - prev, 2) if prev else 0,
                    "volume":     volume,
                    "high":       high,
                    "low":        low,
                }
        except Exception as e:
            logger.warning(f"On-demand price fetch failed for {symbol}: {e}")
            return {}

    # ── Signal generation ─────────────────────────────────────────────────────

    async def _generate_signal(
        self,
        symbol: str,
        market_ctx: dict = None,
        session: str = "market",
        trigger: str = "scheduled",
    ) -> dict:
        try:
            async with _semaphore:
                # Get price — try cache first, then on-demand fetch
                price_data = {}
                try:
                    price_data = await self.price_service.get_one(symbol)
                except Exception:
                    pass

                if not price_data or not price_data.get("price"):
                    price_data = await self._fetch_price_on_demand(symbol)

                if not price_data or not price_data.get("price"):
                    logger.warning(f"No price data for {symbol} — returning fallback")
                    return self._fallback_signal(session)

                news_items = await self.news_service.get_for_symbol(symbol)
                headlines  = [a["headline"] for a in news_items[:8]]

                tech = {}
                if self.technical_svc:
                    try:
                        tech = await self.technical_svc.get_technicals(symbol)
                    except Exception:
                        pass

                if not market_ctx and self.market_svc:
                    try:
                        market_ctx = await self.market_svc.get_market_context([symbol])
                    except Exception:
                        pass

                signal = await self._call_claude(
                    symbol, price_data, headlines, tech, market_ctx or {}, session
                )

                now = datetime.now(timezone.utc)
                signal.update({
                    "price_at_signal": price_data.get("price", 0),
                    "generated_at":    now.isoformat(),
                    "expires_at":      (now + timedelta(days=FIRESTORE_TTL_DAYS)).isoformat(),
                    "session":         session,
                    "session_label":   SESSION_CONTEXT.get(session, {}).get("label", ""),
                    "trigger":         trigger,   # scheduled | price_spike | news_event | manual
                })

                # Store in memory cache
                self._cache[symbol] = signal

                # Store in Firestore
                await self._save_to_firestore(symbol, signal)

                # Append to in-memory history (last 20)
                if symbol not in self._history:
                    self._history[symbol] = []
                self._history[symbol].append({
                    "signal":       signal["signal"],
                    "confidence":   signal["confidence"],
                    "price":        price_data.get("price", 0),
                    "session":      session,
                    "trigger":      trigger,
                    "generated_at": signal["generated_at"],
                })
                self._history[symbol] = self._history[symbol][-20:]

                logger.info(
                    f"[{session.upper()}] {symbol} → {signal['signal']} "
                    f"({signal['confidence']}) trigger={trigger}"
                )
                return signal

        except Exception as e:
            logger.error(f"Signal generation failed for {symbol}: {e}")
            return self._fallback_signal(session)

    # ── Claude API call ───────────────────────────────────────────────────────

    async def _call_claude(
        self, symbol, price, headlines, tech, market_ctx, session
    ) -> dict:
        session_ctx = SESSION_CONTEXT.get(session, SESSION_CONTEXT["market"])

        tech_text = ""
        if tech:
            tech_text = f"""
Technical Indicators:
- RSI: {tech.get('rsi', 'N/A')} ({tech.get('rsi_signal', 'N/A')})
- MACD: {tech.get('macd_crossover', 'NONE')} crossover
- Trend: {tech.get('trend', 'NEUTRAL')}
- 5d momentum: {tech.get('momentum_5d', 0):.2f}%
- Volume: {tech.get('volume_spike', {}).get('label', 'Normal')}"""

        mkt_text = ""
        if market_ctx:
            fg  = market_ctx.get("fear_greed", {})
            vix = market_ctx.get("vix", {})
            mkt_text = f"""
Market Context:
- Fear & Greed: {fg.get('score', 50)}/100 ({fg.get('label', 'Neutral')})
- VIX: {vix.get('value', 20)} ({vix.get('label', 'Normal')})
- Regime: {market_ctx.get('market_regime', 'NEUTRAL')}"""
            earn = market_ctx.get("earnings_upcoming", {})
            if symbol in earn:
                e = earn[symbol]
                mkt_text += f"\n- EARNINGS IN {e['days_away']} DAYS ({e['date']})"

        news_text = (
            "\n".join(f"- {h}" for h in headlines)
            if headlines else "- No recent news"
        )

        prompt = f"""Analyze {symbol} as an expert stock analyst. Session: {session_ctx['label']}

{session_ctx['instructions']}

PRICE DATA:
- Price: ${price.get('price', 0)} ({price.get('change_pct', 0):+.2f}% today)
- Range: ${price.get('low', 0)} - ${price.get('high', 0)}
- Volume: {price.get('volume', 0):,}
- Prev close: ${price.get('prev_close', 0)}
{tech_text}
{mkt_text}

RECENT NEWS:
{news_text}

Respond ONLY with valid JSON, no markdown:
{{"signal":"BUY|HOLD|SELL","confidence":"HIGH|MEDIUM|LOW","target_price":0.0,"expected_return_pct":0.0,"timeframe":"1-3 months","summary":"2-3 sentences with specific evidence","risk":"LOW|MEDIUM|HIGH","key_factors":["factor1","factor2"],"session_note":"one sentence about session consideration"}}"""

        try:
            res = await self.client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            text = res.content[0].text.strip()
            text = text.lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude error for {symbol}: {e}")
            return self._fallback_signal(session)

    # ── Firestore helpers ─────────────────────────────────────────────────────

    async def _load_from_firestore(self, symbol: str) -> dict | None:
        if not self._db:
            return None
        try:
            doc = self._db.collection("signals").document(symbol).get()
            if doc.exists:
                data = doc.to_dict()
                # Check not expired
                expires_at = data.get("expires_at")
                if expires_at:
                    exp = datetime.fromisoformat(expires_at)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp:
                        logger.info(f"Firestore signal for {symbol} expired — regenerating")
                        return None
                logger.info(f"Loaded signal for {symbol} from Firestore")
                return data
        except Exception as e:
            logger.warning(f"Firestore load failed for {symbol}: {e}")
        return None

    async def _save_to_firestore(self, symbol: str, signal: dict):
        if not self._db:
            return
        try:
            from firebase_admin import firestore as fs
            ref = self._db.collection("signals").document(symbol)

            # Check if existing signal changed
            existing = ref.get()
            should_add_history = True
            if existing.exists:
                old = existing.to_dict()
                if old.get("signal") == signal.get("signal"):
                    should_add_history = False  # same signal — just bump expiry

            # Always overwrite current
            ref.set(signal)

            # Only write history if signal changed or new
            if should_add_history:
                ref.collection("history").add(signal)
                logger.info(f"Firestore: saved signal + history for {symbol}")
            else:
                logger.info(f"Firestore: refreshed expiry for {symbol} (signal unchanged)")

        except Exception as e:
            logger.warning(f"Firestore save failed for {symbol}: {e}")

    async def _bump_expiry(self, symbol: str):
        """Extend TTL by 30 days on each access — keeps active symbols alive."""
        if not self._db:
            return
        try:
            from firebase_admin import firestore as fs
            new_expiry = (
                datetime.now(timezone.utc) + timedelta(days=FIRESTORE_TTL_DAYS)
            ).isoformat()
            self._db.collection("signals").document(symbol).update(
                {"expires_at": new_expiry}
            )
        except Exception as e:
            logger.warning(f"Failed to bump expiry for {symbol}: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_fresh(self, signal: dict) -> bool:
        """True if signal is younger than CACHE_TTL_SECONDS."""
        gen = signal.get("generated_at")
        if not gen:
            return False
        try:
            t = datetime.fromisoformat(gen)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t).total_seconds()
            return age < CACHE_TTL_SECONDS
        except Exception:
            return False

    async def _get_market_context(self) -> dict:
        if self.market_svc:
            try:
                return await self.market_svc.get_market_context([])
            except Exception:
                pass
        return {}

    def _fallback_signal(self, session: str = "market") -> dict:
        return {
            "signal":             "HOLD",
            "confidence":         "LOW",
            "target_price":       None,
            "expected_return_pct": 0,
            "timeframe":          "unknown",
            "risk":               "HIGH",
            "summary":            "Signal generation failed. Using fallback HOLD.",
            "key_factors":        ["data_unavailable"],
            "session":            session,
            "session_note":       "Fallback signal — retry when data is available.",
            "generated_at":       datetime.now(timezone.utc).isoformat(),
            "trigger":            "fallback",
        }