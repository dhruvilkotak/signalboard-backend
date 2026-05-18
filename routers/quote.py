"""
routers/quote.py
Fetches live prices from Yahoo Finance server-side.
No CORS issues since backend → Yahoo is server-to-server.
No API key needed. Free forever.
"""
import httpx
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from cachetools import TTLCache

logger = logging.getLogger(__name__)
router = APIRouter()

# Cache prices for 30 seconds to avoid hammering Yahoo
_cache: TTLCache = TTLCache(maxsize=200, ttl=30)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

async def fetch_yahoo(symbol: str) -> dict | None:
    """Fetch price data from Yahoo Finance"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "2d", "includePrePost": "true"}

    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
            res = await client.get(url, params=params)
            res.raise_for_status()
            data = res.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        meta = result[0].get("meta", {})
        price      = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
        change_amt = price - prev_close

        return {
            "symbol":       symbol.upper(),
            "price":        round(price, 2),
            "open":         round(meta.get("regularMarketOpen", price), 2),
            "high":         round(meta.get("regularMarketDayHigh", price), 2),
            "low":          round(meta.get("regularMarketDayLow", price), 2),
            "volume":       meta.get("regularMarketVolume", 0),
            "prev_close":   round(prev_close, 2),
            "change_pct":   round(change_pct, 2),
            "change_amt":   round(change_amt, 2),
            "mkt_cap":      meta.get("marketCap"),
            "currency":     meta.get("currency", "USD"),
            "market_state": meta.get("marketState", "CLOSED"),
            "pre_market":   round(meta.get("preMarketPrice", 0), 2) or None,
            "post_market":  round(meta.get("postMarketPrice", 0), 2) or None,
            "ext_price":    round(meta.get("postMarketPrice") or meta.get("preMarketPrice") or 0, 2) or None,
            "source":       "yahoo_finance",
        }
    except Exception as e:
        logger.error(f"Yahoo Finance fetch failed for {symbol}: {e}")
        return None


@router.get("/{symbol}")
async def get_quote(symbol: str):
    """Get live price for a single symbol"""
    sym = symbol.upper()
    if sym in _cache:
        return _cache[sym]

    data = await fetch_yahoo(sym)
    if not data:
        raise HTTPException(status_code=404, detail=f"Could not fetch price for {sym}")

    _cache[sym] = data
    return data


@router.get("/batch/{symbols}")
async def get_quotes_batch(symbols: str):
    """
    Get prices for multiple symbols at once.
    Pass comma-separated: /api/quote/batch/AAPL,MSFT,NVDA
    """
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if len(sym_list) > 30:
        raise HTTPException(status_code=400, detail="Max 30 symbols per batch request")

    # Check cache first
    results = {}
    to_fetch = []
    for sym in sym_list:
        if sym in _cache:
            results[sym] = _cache[sym]
        else:
            to_fetch.append(sym)

    # Fetch uncached in parallel
    if to_fetch:
        fetched = await asyncio.gather(*[fetch_yahoo(sym) for sym in to_fetch])
        for sym, data in zip(to_fetch, fetched):
            if data:
                _cache[sym] = data
                results[sym] = data

    return results
