"""
Signal Service — generates BUY/HOLD/SELL signals using Claude Haiku
Caches signals, only refreshes if price moved >1.5% or cache expired
"""
import os, json, logging
from datetime import datetime
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

class SignalService:
    def __init__(self, price_service, news_service):
        self.client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
        self.price_service = price_service
        self.news_service = news_service
        self._cache: dict = {}   # symbol -> {signal, price_at_signal, timestamp, ...}

    async def get_signal(self, symbol: str, force: bool = False) -> dict:
        cached = self._cache.get(symbol)
        # Return cache unless price moved significantly or force refresh
        if cached and not force:
            price_at_signal = cached.get("price_at_signal", 0)
            if not self.price_service.should_refresh_signal(symbol, price_at_signal):
                return cached

        price_data = await self.price_service.get_one(symbol)
        news_articles = await self.news_service.get_for_symbol(symbol)
        headlines = [a["headline"] for a in news_articles[:5]]

        signal = await self._call_claude(symbol, price_data, headlines)
        signal["price_at_signal"] = price_data["price"]
        signal["generated_at"] = datetime.utcnow().isoformat()
        self._cache[symbol] = signal
        return signal

    async def get_all_signals(self) -> dict:
        """Batch all signals - called on demand"""
        results = {}
        prices = await self.price_service.get_all()
        news_all = await self.news_service.get_all()

        for symbol in prices:
            cached = self._cache.get(symbol)
            if cached and not self.price_service.should_refresh_signal(symbol, cached.get("price_at_signal", 0)):
                results[symbol] = cached
                continue
            price_data = prices[symbol]
            headlines = [a["headline"] for a in news_all.get(symbol, [])[:5]]
            signal = await self._call_claude(symbol, price_data, headlines)
            signal["price_at_signal"] = price_data["price"]
            signal["generated_at"] = datetime.utcnow().isoformat()
            self._cache[symbol] = signal
            results[symbol] = signal
        return results

    async def _call_claude(self, symbol: str, price_data: dict, headlines: list) -> dict:
        news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "No recent news"
        prompt = f"""Analyze {symbol} and give a trading signal.

Current price: ${price_data['price']}
Today's change: {price_data['change_pct']}%
High: ${price_data['high']} | Low: ${price_data['low']}
Volume: {price_data['volume']:,}

Recent headlines:
{news_text}

Respond ONLY with valid JSON, no markdown:
{{
  "signal": "BUY" or "HOLD" or "SELL",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "target_price": <number, expected price in 1-3 months>,
  "expected_return_pct": <number>,
  "timeframe": "1-3 months",
  "summary": "<2 sentence reason citing specific data or news>",
  "risk": "LOW" or "MEDIUM" or "HIGH"
}}"""
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text
            # Strip any accidental markdown
            text = text.strip().lstrip("```json").rstrip("```").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude signal error for {symbol}: {e}")
            return {
                "signal": "HOLD",
                "confidence": "LOW",
                "target_price": price_data["price"],
                "expected_return_pct": 0,
                "timeframe": "unknown",
                "summary": "Signal generation failed. Please retry.",
                "risk": "HIGH"
            }
