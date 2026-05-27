"""
services/ondemand_signal_service.py

On-demand signal generation for the Live Prices → Signal tab.
Completely separate from the scheduler signals used for auto-trading.

Data sources fed into Claude:
  1. Yahoo Finance price (via price_service cache or on-demand fetch)
  2. SEC EDGAR Form 4 — insider buy/sell transactions (last 90 days)
  3. StockTwits — retail sentiment bullish/bearish ratio + message volume
  4. Recent news headlines (via news_service)

Firestore path: signals_ondemand/{symbol}
Cache TTL:      24 hours fixed (not sliding)
  - Keeps costs low: at most 1 Claude call per ticker per user per day
  - Fixed TTL because stock conditions change each trading day

Usage:
  svc = OnDemandSignalService(price_service, news_service)
  svc.set_db(get_db())
  result = await svc.get_signal("TSLA")
"""

import json
import logging
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
from anthropic import AsyncAnthropic
from config import settings

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 24
_semaphore = asyncio.Semaphore(5)   # lighter limit — user-facing, not bulk


class OnDemandSignalService:
    def __init__(self, price_service, news_service):
        self.price_svc = price_service
        self.news_svc  = news_service
        self.client    = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._db       = None
        self._cache: dict = {}  # symbol → {signal, cached_at}

    def set_db(self, db):
        self._db = db
        logger.info("OnDemandSignalService: Firestore connected ✓")

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_signal(self, symbol: str) -> dict:
        """
        Returns cached signal if < 24h old, otherwise generates a fresh one.
        Checks: memory cache → Firestore → generate.
        """
        symbol = symbol.upper().strip()

        # 1. Memory cache
        cached = self._cache.get(symbol)
        if cached and self._is_fresh(cached):
            logger.info(f"OnDemand [{symbol}]: serving from memory cache")
            return cached

        # 2. Firestore cache
        if self._db:
            stored = await self._load_from_firestore(symbol)
            if stored:
                self._cache[symbol] = stored
                logger.info(f"OnDemand [{symbol}]: serving from Firestore cache")
                return stored

        # 3. Generate fresh
        return await self._generate(symbol)

    # ── Data fetchers ─────────────────────────────────────────────────────────

    async def _fetch_price(self, symbol: str) -> dict:
        """Try price_service cache first, fall back to Yahoo Finance direct."""
        try:
            p = await self.price_svc.get_one(symbol)
            if p and p.get("price"):
                return p
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                res = await c.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params={"interval": "1d", "range": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                meta = res.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
                return {
                    "price":      price,
                    "prev_close": prev,
                    "change_pct": round(((price - prev) / prev * 100), 2) if prev else 0,
                    "change_amt": round(price - prev, 2) if prev else 0,
                    "volume":     meta.get("regularMarketVolume", 0),
                    "high":       meta.get("regularMarketDayHigh", price),
                    "low":        meta.get("regularMarketDayLow", price),
                    "open":       meta.get("regularMarketOpen", price),
                }
        except Exception as e:
            logger.warning(f"Price fetch failed for {symbol}: {e}")
            return {}

    async def _fetch_insider_trades(self, symbol: str) -> list[dict]:
        """
        SEC EDGAR Form 4 — last 90 days of insider transactions.
        Uses the free EDGAR full-text search API. No auth required.
        Returns list of: { name, role, type (Buy/Sell), shares, value, date }
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # Search EDGAR for Form 4 filings for this ticker
                res = await c.get(
                    "https://efts.sec.gov/LATEST/search-index?q=%22" + symbol + "%22&dateRange=custom"
                    "&startdt=" + (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d") +
                    "&enddt=" + datetime.now().strftime("%Y-%m-%d") +
                    "&forms=4",
                    headers={"User-Agent": "SignalBoard research@signalboard.app"},
                )
                if not res.is_success:
                    return []

                hits = res.json().get("hits", {}).get("hits", [])[:10]
                trades = []
                for hit in hits:
                    src = hit.get("_source", {})
                    display_names = src.get("display_names", [])
                    name = display_names[0] if display_names else "Unknown"
                    filed = src.get("file_date", "")
                    # Transaction type from form description
                    form_type = src.get("form_type", "4")
                    trades.append({
                        "name":  name,
                        "date":  filed,
                        "form":  form_type,
                        "url":   "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&dateb=&owner=include&count=10&search_text=&ticker=" + symbol,
                    })
                return trades[:5]
        except Exception as e:
            logger.warning(f"SEC insider fetch failed for {symbol}: {e}")
            return []

    async def _fetch_stocktwits_sentiment(self, symbol: str) -> dict:
        """
        StockTwits public API — no auth required for public data.
        Returns: { bullish_pct, bearish_pct, message_volume, sentiment_label }
        """
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                res = await c.get(
                    f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if not res.is_success:
                    return {}

                data   = res.json()
                symbol_data = data.get("symbol", {})
                watchlist_count = symbol_data.get("watchlist_count", 0)

                messages = data.get("messages", [])
                bull = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
                bear = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
                total = bull + bear

                if total == 0:
                    return {
                        "message_volume": len(messages),
                        "watchlist_count": watchlist_count,
                        "sentiment_label": "Neutral",
                        "bullish_pct": 50,
                        "bearish_pct": 50,
                    }

                bull_pct = round(bull / total * 100)
                bear_pct = 100 - bull_pct

                return {
                    "bullish_pct":     bull_pct,
                    "bearish_pct":     bear_pct,
                    "message_volume":  len(messages),
                    "watchlist_count": watchlist_count,
                    "sentiment_label": "Bullish" if bull_pct >= 60 else "Bearish" if bear_pct >= 60 else "Mixed",
                }
        except Exception as e:
            logger.warning(f"StockTwits fetch failed for {symbol}: {e}")
            return {}

    # ── Signal generation ─────────────────────────────────────────────────────

    async def _generate(self, symbol: str) -> dict:
        async with _semaphore:
            # Fetch all data sources concurrently
            price_data, news_items, insider_trades, sentiment = await asyncio.gather(
                self._fetch_price(symbol),
                self.news_svc.get_for_symbol(symbol),
                self._fetch_insider_trades(symbol),
                self._fetch_stocktwits_sentiment(symbol),
                return_exceptions=True,
            )

            # Sanitise gather exceptions
            if isinstance(price_data, Exception):    price_data     = {}
            if isinstance(news_items, Exception):    news_items     = []
            if isinstance(insider_trades, Exception): insider_trades = []
            if isinstance(sentiment, Exception):     sentiment      = {}

            if not price_data or not price_data.get("price"):
                logger.warning(f"OnDemand [{symbol}]: no price data — returning fallback")
                return self._fallback(symbol)

            headlines = [a["headline"] for a in news_items[:6]] if news_items else []

            signal = await self._call_claude(
                symbol, price_data, headlines, insider_trades, sentiment
            )

            now = datetime.now(timezone.utc)
            signal.update({
                "symbol":          symbol,
                "price_at_signal": price_data.get("price", 0),
                "generated_at":    now.isoformat(),
                "expires_at":      (now + timedelta(hours=CACHE_TTL_HOURS)).isoformat(),
                "trigger":         "on_demand",
                "source":          "ondemand",
                # Attach enrichment data for frontend display
                "insider_trades":  insider_trades,
                "sentiment":       sentiment,
            })

            self._cache[symbol] = signal
            await self._save_to_firestore(symbol, signal)

            logger.info(
                f"OnDemand [{symbol}]: {signal['signal']}/{signal['confidence']} generated"
            )
            return signal

    async def _call_claude(
        self, symbol, price, headlines, insider_trades, sentiment
    ) -> dict:
        price_line = (
            f"${price.get('price', 0):.2f} "
            f"({price.get('change_pct', 0):+.2f}% today, "
            f"H ${price.get('high', 0):.2f} / L ${price.get('low', 0):.2f}, "
            f"Vol {price.get('volume', 0):,.0f})"
        )

        news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "- No recent news available"

        insider_text = "No recent insider filings found."
        if insider_trades:
            insider_text = "\n".join(
                f"- {t.get('name','Unknown')} filed Form {t.get('form','4')} on {t.get('date','')}"
                for t in insider_trades
            )

        sentiment_text = "StockTwits data unavailable."
        if sentiment:
            sentiment_text = (
                f"Retail sentiment: {sentiment.get('sentiment_label','Unknown')} "
                f"({sentiment.get('bullish_pct',50)}% bullish / {sentiment.get('bearish_pct',50)}% bearish) "
                f"from {sentiment.get('message_volume',0)} recent messages. "
                f"Watchlist count: {sentiment.get('watchlist_count',0):,}"
            )

        prompt = f"""You are an expert stock analyst generating a detailed investment signal for retail investors.

SYMBOL: {symbol}

PRICE DATA:
{price_line}

RECENT NEWS:
{news_text}

SEC INSIDER ACTIVITY (Form 4 — last 90 days):
{insider_text}

RETAIL SENTIMENT (StockTwits):
{sentiment_text}

Analyze all signals holistically. Consider:
- Price momentum and technical position
- News catalysts (positive/negative)
- Insider activity direction (buying = bullish, selling = mixed/bearish)
- Retail sentiment as a contrarian or confirming indicator
- Risk/reward at current price

Respond ONLY with valid JSON, no markdown, no explanation outside the JSON:
{{
  "signal": "BUY|HOLD|SELL",
  "confidence": "HIGH|MEDIUM|LOW",
  "target_price": <float>,
  "stop_loss": <float>,
  "expected_return_pct": <float>,
  "timeframe": "1-2 weeks|1-3 months|3-6 months",
  "timeframe_days": <int>,
  "summary": "<2-3 sentences with specific evidence from the data above>",
  "risk": "LOW|MEDIUM|HIGH",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "insider_summary": "<one sentence about insider activity>",
  "sentiment_summary": "<one sentence about retail sentiment>",
  "price_targets": {{
    "week1": <float>,
    "week2": <float>,
    "month1": <float>,
    "month3": <float>
  }},
  "bull_case": "<one sentence>",
  "bear_case": "<one sentence>"
}}"""

        try:
            res = await self.client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            text = res.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude error for {symbol}: {e}")
            return self._fallback(symbol)

    # ── Firestore ─────────────────────────────────────────────────────────────

    async def _load_from_firestore(self, symbol: str) -> dict | None:
        if not self._db:
            return None
        try:
            loop = asyncio.get_event_loop()
            doc = await loop.run_in_executor(
                None,
                lambda: self._db.collection("signals_ondemand").document(symbol).get()
            )
            if not doc.exists:
                return None
            data = doc.to_dict()
            # Normalise Timestamps
            for k in ("generated_at", "expires_at"):
                v = data.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    data[k] = v.isoformat()
            if self._is_fresh(data):
                return data
            logger.info(f"OnDemand [{symbol}]: Firestore cache expired — regenerating")
            return None
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_fresh(self, signal: dict) -> bool:
        """True if signal is younger than 24h."""
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

    def _fallback(self, symbol: str) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "symbol":          symbol,
            "signal":          "HOLD",
            "confidence":      "LOW",
            "target_price":    None,
            "stop_loss":       None,
            "expected_return_pct": 0,
            "timeframe":       "unknown",
            "timeframe_days":  30,
            "summary":         "Signal generation failed. Data unavailable.",
            "risk":            "HIGH",
            "key_factors":     ["data_unavailable"],
            "insider_summary": "No insider data available.",
            "sentiment_summary": "No sentiment data available.",
            "price_targets":   {},
            "bull_case":       "Insufficient data.",
            "bear_case":       "Insufficient data.",
            "insider_trades":  [],
            "sentiment":       {},
            "generated_at":    now.isoformat(),
            "expires_at":      (now + timedelta(hours=CACHE_TTL_HOURS)).isoformat(),
            "trigger":         "fallback",
            "source":          "ondemand",
        }