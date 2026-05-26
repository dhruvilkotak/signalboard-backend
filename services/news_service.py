"""
News Service — fetches stock news from Yahoo Finance
Real-time headlines, covers stocks AND ETFs, no API key needed
Refreshes every 15 minutes via scheduler
"""
import os
import httpx
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def get_tickers():
    return os.getenv(
        "TICKERS",
        "SPY,VOO,JEPI,JEPQ,SCHD,SGOV,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD"
    ).split(",")


class NewsService:
    def __init__(self):
        self._cache: dict = {}

    async def fetch_and_cache(self):
        """Called every 15 min by scheduler — fetches news for all tickers."""
        tickers = get_tickers()
        grouped = {t: [] for t in tickers}

        async with httpx.AsyncClient(timeout=10) as client:
            for symbol in tickers:
                try:
                    articles = await self._fetch_yahoo(client, symbol)
                    grouped[symbol] = articles
                except Exception as e:
                    logger.warning(f"News fetch failed for {symbol}: {e}")
                    continue

        self._cache = grouped
        total = sum(len(v) for v in grouped.values())
        logger.info(f"News cache updated: {total} articles for {len(tickers)} tickers")

    async def _fetch_yahoo(self, client: httpx.AsyncClient, symbol: str) -> list:
        """Fetch latest news from Yahoo Finance search API."""
        res = await client.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={
                "q":           symbol,
                "newsCount":   8,
                "quotesCount": 0,
                "listsCount":  0,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept":     "application/json",
                "Referer":    "https://finance.yahoo.com",
            },
        )

        if res.status_code != 200:
            logger.warning(f"Yahoo news HTTP {res.status_code} for {symbol}")
            return []

        news_items = res.json().get("news", [])
        articles   = []

        for item in news_items:
            try:
                articles.append({
                    "id":         item.get("uuid", ""),
                    "headline":   item.get("title", ""),
                    "summary":    item.get("summary", "") or "",
                    "url":        item.get("link", ""),
                    "source":     item.get("publisher", "Yahoo Finance"),
                    "created_at": datetime.fromtimestamp(
                        item.get("providerPublishTime", 0),
                        tz=timezone.utc
                    ).isoformat(),
                    "symbols":    item.get("relatedTickers", [symbol]),
                })
            except Exception as e:
                logger.warning(f"Article parse error for {symbol}: {e}")
                continue

        return articles

    async def get_for_symbol(self, symbol: str) -> list:
        """Get cached news for a symbol — fetch if cache empty."""
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache.get(symbol.upper(), [])

    async def get_all(self) -> dict:
        """Get all cached news."""
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache