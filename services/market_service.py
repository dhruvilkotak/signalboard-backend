"""
services/market_service.py
Stub — full implementation optional
"""
import logging
logger = logging.getLogger(__name__)

class MarketService:
    async def get_fear_greed(self) -> dict:
        return {"score": 50, "rating": "Neutral", "label": "Neutral"}

    async def get_vix(self) -> dict:
        return {"value": 20.0, "label": "Normal Volatility"}

    async def get_market_context(self, symbols: list) -> dict:
        return {
            "fear_greed": {"score": 50, "label": "Neutral"},
            "vix": {"value": 20.0, "label": "Normal"},
            "earnings_upcoming": {},
            "market_regime": "NEUTRAL",
        }