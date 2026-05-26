"""
News Service — two responsibilities:
1. Scheduled cache for admin signal tickers (feeds Claude signal generation)
2. On-demand fetch for any symbol (feeds frontend News tab)
"""
import os
import httpx
import logging
import email.utils
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = "SPY,VOO,JEPI,JEPQ,SCHD,SGOV,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD"

def get_signal_tickers():
    """Get admin-managed signal tickers — from Firestore config or env fallback."""
    return os.getenv("TICKERS", DEFAULT_TICKERS).split(",")


async def fetch_rss(symbol: str, client: httpx.AsyncClient) -> list:
    """Fetch Yahoo Finance RSS news for any symbol."""
    try:
        res = await client.get(
            "https://feeds.finance.yahoo.com/rss/2.0/headline",
            params={"s": symbol, "region": "US", "lang": "en-US"},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SignalBoard/1.0)",
                "Accept":     "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )
        if res.status_code != 200:
            return []

        root     = ET.fromstring(res.text)
        articles = []

        for item in root.findall(".//item"):
            title  = item.findtext("title") or ""
            link   = item.findtext("link") or "#"
            desc   = item.findtext("description") or ""
            pub    = item.findtext("pubDate") or ""
            guid   = item.findtext("guid") or ""
            source = item.findtext("source") or "Yahoo Finance"

            try:
                dt = email.utils.parsedate_to_datetime(pub)
                created_at = dt.isoformat()
            except Exception:
                created_at = datetime.now(timezone.utc).isoformat()

            articles.append({
                "id":         guid,
                "headline":   title.strip(),
                "summary":    desc.strip()[:200],
                "url":        link.strip(),
                "source":     source.strip(),
                "created_at": created_at,
                "symbols":    [symbol],
            })

        return articles[:10]

    except Exception as e:
        logger.warning(f"RSS fetch failed for {symbol}: {e}")
        return []


class NewsService:
    """
    Scheduled news cache — only for signal tickers (feeds Claude).
    On-demand fetch available for any symbol via fetch_for_symbol().
    """
    def __init__(self):
        self._cache: dict = {}  # signal tickers only

    async def fetch_and_cache(self):
        """
        Called every 15 min by scheduler.
        Fetches news ONLY for admin signal tickers — used by Claude.
        """
        tickers = get_signal_tickers()
        grouped = {t: [] for t in tickers}

        async with httpx.AsyncClient(timeout=10) as client:
            for symbol in tickers:
                try:
                    articles = await fetch_rss(symbol, client)
                    grouped[symbol] = articles
                except Exception as e:
                    logger.warning(f"Scheduled news failed for {symbol}: {e}")

        self._cache = grouped
        total = sum(len(v) for v in grouped.values())
        logger.info(f"News cache updated: {total} articles for {len(tickers)} signal tickers")

    async def get_for_symbol(self, symbol: str) -> list:
        """
        Get news for a symbol.
        - If in signal ticker cache → return cached (used by Claude)
        - Otherwise → fetch fresh from RSS (on-demand for any symbol)
        """
        symbol = symbol.upper().strip()

        # Return from cache if available (signal tickers)
        if symbol in self._cache:
            return self._cache[symbol]

        # On-demand fetch for custom symbols
        async with httpx.AsyncClient(timeout=8) as client:
            return await fetch_rss(symbol, client)

    async def get_all(self) -> dict:
        """Get all cached signal ticker news."""
        if not self._cache:
            await self.fetch_and_cache()
        return self._cache