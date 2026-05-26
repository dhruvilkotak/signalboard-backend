"""
News Service — fetches stock news from Alpaca News API
Refreshes every 15 minutes via scheduler
"""
import os, logging
from datetime import datetime, timedelta, timezone
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

logger = logging.getLogger(__name__)

def get_tickers():
    return os.getenv(
        "TICKERS",
        "SPY,VOO,JEPI,JEPQ,SCHD,SGOV,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD"
    ).split(",")

class NewsService:
    def __init__(self):
        self.client = NewsClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
        )
        self._cache: dict = {}

    async def fetch_and_cache(self):
        """Called every 15 min by scheduler"""
        try:
            tickers = get_tickers()
            since   = datetime.now(timezone.utc) - timedelta(hours=8)
            grouped = {t: [] for t in tickers}

            for symbol in tickers:
                try:
                    request = NewsRequest(
                        symbols=symbol,
                        start=since,
                        limit=10,
                    )
                    news = self.client.get_news(request)

                    # NewsClient returns NewsSet — iterate .data dict values
                    articles = []
                    if hasattr(news, "data"):
                        # data is dict: { "news": [NewsArticle, ...] }
                        for key, val in news.data.items():
                            if isinstance(val, list):
                                articles.extend(val)
                    elif hasattr(news, "__iter__"):
                        for item in news:
                            # Could be tuple (symbol, article) or just article
                            if isinstance(item, tuple):
                                articles.append(item[-1])
                            else:
                                articles.append(item)

                    for article in articles:
                        try:
                            grouped[symbol].append({
                                "id":         str(getattr(article, "id", "")),
                                "headline":   getattr(article, "headline", ""),
                                "summary":    getattr(article, "summary", "") or "",
                                "url":        getattr(article, "url", ""),
                                "source":     getattr(article, "source", ""),
                                "created_at": getattr(article, "created_at", datetime.now(timezone.utc)).isoformat(),
                                "symbols":    getattr(article, "symbols", [symbol]) or [symbol],
                            })
                        except Exception as e:
                            logger.warning(f"Article parse error for {symbol}: {e}")
                            continue

                except Exception as e:
                    logger.warning(f"News fetch failed for {symbol}: {e}")
                    continue

            self._cache = grouped
            total = sum(len(v) for v in grouped.values())
            logger.info(f"News cache updated: {total} articles for {len(tickers)} tickers")

        except Exception as e:
            logger.error(f"News fetch failed: {e}")

    async def get_for_symbol(self, symbol: str) -> list:
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache.get(symbol.upper(), [])

    async def get_all(self) -> dict:
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache