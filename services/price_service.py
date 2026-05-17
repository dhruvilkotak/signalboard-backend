"""
Price Service — fetches real-time prices from Alpaca
Caches in memory, refreshes every 30s
"""
import os, logging
from datetime import datetime
from cachetools import TTLCache
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestBarRequest

logger = logging.getLogger(__name__)

TICKERS = os.getenv("TICKERS", "SPY,VOO,JEPI,JEPQ,SCHD,SGOV,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD").split(",")

class PriceService:
    def __init__(self):
        self.client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
        )
        self._cache: dict = {}
        self._prev_prices: dict = {}

    async def update_cache(self):
        """Called every 30s by scheduler"""
        try:
            data = await self._fetch_latest()
            self._cache = data
            logger.info(f"Prices updated for {len(data)} tickers")
        except Exception as e:
            logger.error(f"Price update failed: {e}")

    async def _fetch_latest(self) -> dict:
        request = StockLatestBarRequest(symbol_or_symbols=TICKERS)
        bars = self.client.get_stock_latest_bar(request)

        result = {}
        for symbol, bar in bars.items():
            prev = self._prev_prices.get(symbol, bar.close)
            change_pct = ((bar.close - prev) / prev * 100) if prev else 0
            result[symbol] = {
                "symbol": symbol,
                "price": round(bar.close, 2),
                "open": round(bar.open, 2),
                "high": round(bar.high, 2),
                "low": round(bar.low, 2),
                "volume": bar.volume,
                "change_pct": round(change_pct, 2),
                "prev_close": round(prev, 2),
                "timestamp": bar.timestamp.isoformat(),
            }
            self._prev_prices[symbol] = bar.close
        return result

    async def get_all(self) -> dict:
        if not self._cache:
            await self.update_cache()
        return self._cache

    async def get_one(self, symbol: str) -> dict:
        all_prices = await self.get_all()
        if symbol not in all_prices:
            raise ValueError(f"Unknown symbol: {symbol}")
        return all_prices[symbol]

    def should_refresh_signal(self, symbol: str, cached_price: float) -> bool:
        """Returns True if price moved more than threshold since last signal"""
        threshold = float(os.getenv("PRICE_CHANGE_THRESHOLD", "1.5"))
        current = self._cache.get(symbol, {}).get("price", cached_price)
        if not cached_price:
            return True
        change = abs((current - cached_price) / cached_price * 100)
        return change >= threshold
