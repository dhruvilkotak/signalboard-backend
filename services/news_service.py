"""
News Service — fetches stock news from Alpaca News API
Caches in Firebase Firestore, refreshes every 30 min
"""
import os, logging
from datetime import datetime, timedelta, timezone
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest

logger = logging.getLogger(__name__)
TICKERS = os.getenv("TICKERS", "SPY,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD").split(",")

class NewsService:
    def __init__(self):
        self.client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
        )
        self._cache: dict = {}   # symbol -> list of articles

    async def fetch_and_cache(self):
        """Called every 30 min by scheduler"""
        try:
            since = datetime.now(timezone.utc) - timedelta(hours=6)
            request = NewsRequest(
                symbols=TICKERS,
                start=since,
                limit=50,
            )
            news = self.client.get_news(request)
            grouped: dict = {t: [] for t in TICKERS}
            for article in news.news:
                for sym in (article.symbols or []):
                    if sym in grouped:
                        grouped[sym].append({
                            "id": article.id,
                            "headline": article.headline,
                            "summary": article.summary or "",
                            "url": article.url,
                            "source": article.source,
                            "created_at": article.created_at.isoformat(),
                            "symbols": article.symbols,
                        })
            self._cache = grouped
            logger.info(f"News cache updated: {sum(len(v) for v in grouped.values())} articles")
        except Exception as e:
            logger.error(f"News fetch failed: {e}")

    async def get_for_symbol(self, symbol: str) -> list:
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache.get(symbol, [])

    async def get_all(self) -> dict:
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache
