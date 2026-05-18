"""
services/signal_service.py
Session-aware signal generation:
- Pre-market:  overnight news, futures, earnings focus
- Market:      full technical + news + sentiment
- Post-market: earnings results, AH moves, next-day prep
"""
import json, logging
from datetime import datetime, timezone
from anthropic import AsyncAnthropic
from config import settings

logger = logging.getLogger(__name__)

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
        technical_service=None,   # optional
        market_service=None,      # optional
    ):
        self.client          = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.price_service   = price_service
        self.news_service    = news_service
        self.technical_svc   = technical_service
        self.market_svc      = market_service
        self._cache: dict    = {}
        self._history: dict  = {}

    async def get_signal(self, symbol: str, force: bool = False, session: str = "market") -> dict:
        cached = self._cache.get(symbol)
        if cached and not force:
            if not self.price_service.should_refresh_signal(symbol, cached.get("price_at_signal", 0)):
                return cached
        return await self._generate_signal(symbol, session=session)

    async def get_all_signals(self, force: bool = False, session: str = "market") -> dict:
        try:
            prices     = await self.price_service.get_all()
        except Exception:
            prices = {}
        market_ctx = await self._get_market_context()
        results    = {}

        for symbol in prices:
            cached = self._cache.get(symbol)
            if cached and not force:
                if not self.price_service.should_refresh_signal(symbol, cached.get("price_at_signal", 0)):
                    results[symbol] = cached
                    continue
            signal = await self._generate_signal(symbol, market_ctx=market_ctx, session=session)
            results[symbol] = signal
        return results

    async def _generate_signal(self, symbol: str, market_ctx: dict = None, session: str = "market") -> dict:
        try:
            price_data = await self.price_service.get_one(symbol)
            news_items = await self.news_service.get_for_symbol(symbol)
            headlines  = [a["headline"] for a in news_items[:8]]

            # Optional technical indicators
            tech = {}
            if self.technical_svc:
                try:
                    tech = await self.technical_svc.get_technicals(symbol)
                except Exception:
                    pass

            # Optional market context
            if not market_ctx and self.market_svc:
                try:
                    market_ctx = await self.market_svc.get_market_context([symbol])
                except Exception:
                    pass

            signal = await self._call_claude(symbol, price_data, headlines, tech, market_ctx or {}, session)
            signal["price_at_signal"] = price_data.get("price", 0)
            signal["generated_at"]    = datetime.now(timezone.utc).isoformat()
            signal["session"]         = session
            signal["session_label"]   = SESSION_CONTEXT.get(session, {}).get("label", "")

            self._cache[symbol] = signal

            if symbol not in self._history:
                self._history[symbol] = []
            self._history[symbol].append({
                "signal":       signal["signal"],
                "confidence":   signal["confidence"],
                "price":        price_data.get("price", 0),
                "session":      session,
                "generated_at": signal["generated_at"],
            })
            self._history[symbol] = self._history[symbol][-20:]

            logger.info(f"[{session.upper()}] {symbol} → {signal['signal']} ({signal['confidence']})")
            return signal

        except Exception as e:
            logger.error(f"Signal failed for {symbol}: {e}")
            return self._fallback_signal(session)

    async def _call_claude(self, symbol, price, headlines, tech, market_ctx, session) -> dict:
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
            fg   = market_ctx.get("fear_greed", {})
            vix  = market_ctx.get("vix", {})
            earn = market_ctx.get("earnings_upcoming", {})
            mkt_text = f"""
Market Context:
- Fear & Greed: {fg.get('score', 50)}/100 ({fg.get('label', 'Neutral')})
- VIX: {vix.get('value', 20)} ({vix.get('label', 'Normal')})
- Regime: {market_ctx.get('market_regime', 'NEUTRAL')}"""
            if symbol in earn:
                e = earn[symbol]
                mkt_text += f"\n- EARNINGS IN {e['days_away']} DAYS ({e['date']})"

        news_text = "\n".join(f"- {h}" for h in headlines) if headlines else "- No recent news"

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
                messages=[{"role": "user", "content": prompt}]
            )
            text = res.content[0].text.strip()
            text = text.lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude error for {symbol}: {e}")
            return self._fallback_signal(session)

    async def _get_market_context(self) -> dict:
        if self.market_svc:
            try:
                return await self.market_svc.get_market_context(
                    os.getenv("TICKERS", "SPY,MSFT,AAPL").split(",")
                    if hasattr(self, '_tickers') else []
                )
            except Exception:
                pass
        return {}

    def get_signal_history(self, symbol: str) -> list:
        return self._history.get(symbol, [])

    def get_all_cached(self) -> dict:
        return self._cache.copy()

    def _fallback_signal(self, session: str = "market") -> dict:
        return {
            "signal": "HOLD", "confidence": "LOW",
            "target_price": None, "expected_return_pct": 0,
            "timeframe": "unknown", "risk": "HIGH",
            "summary": "Signal generation failed. Using fallback HOLD.",
            "key_factors": ["data_unavailable"],
            "session": session,
            "session_note": "Fallback signal — retry when data is available.",
        }


import os