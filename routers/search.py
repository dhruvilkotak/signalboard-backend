# routers/search.py
# Typeahead search — returns multiple matches with name, price, change_pct
# Uses Yahoo Finance search API (no key needed)

import httpx
import asyncio
import logging
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/")
async def search_symbols(q: str = Query(..., min_length=1)):
    """
    Typeahead search — returns up to 8 matches with name + price.
    Example: GET /api/search?q=tes
    Returns: [{ symbol, name, price, change_pct, type, exchange }, ...]
    """
    q = q.strip().upper()
    if not q:
        return []

    try:
        # Step 1 — Yahoo Finance symbol search
        async with httpx.AsyncClient(timeout=5) as client:
            search_res = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 8, "newsCount": 0, "listsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            search_data = search_res.json()

        quotes = search_data.get("quotes", [])
        if not quotes:
            return []

        # Filter to stocks and ETFs only
        quotes = [
            q for q in quotes
            if q.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND", "CURRENCY", "CRYPTOCURRENCY")
        ][:8]

        symbols = [q["symbol"] for q in quotes]

        # Step 2 — batch price fetch
        async with httpx.AsyncClient(timeout=8) as client:
            price_res = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/spark",
                params={
                    "symbols": ",".join(symbols),
                    "range": "1d",
                    "interval": "1d",
                },
                headers={"User-Agent": "Mozilla/5.0"}
            )
            # Also get quote details
            detail_res = await client.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(symbols)},
                headers={"User-Agent": "Mozilla/5.0"}
            )

        detail_data = detail_res.json()
        detail_map = {}
        for item in detail_data.get("quoteResponse", {}).get("result", []):
            detail_map[item["symbol"]] = item

        results = []
        for q in quotes:
            sym = q["symbol"]
            detail = detail_map.get(sym, {})
            results.append({
                "symbol":     sym,
                "name":       q.get("longname") or q.get("shortname") or sym,
                "price":      detail.get("regularMarketPrice"),
                "change_pct": round(detail.get("regularMarketChangePercent", 0), 2),
                "change_amt": round(detail.get("regularMarketChange", 0), 2),
                "type":       q.get("quoteType", "EQUITY"),
                "exchange":   q.get("exchDisp") or q.get("exchange", ""),
            })

        return results

    except Exception as e:
        logger.error(f"Search failed for '{q}': {e}")
        return []