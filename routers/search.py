# routers/search.py
import httpx
import logging
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/")
async def search_symbols(q: str = Query(..., min_length=1)):
    q = q.strip()
    if not q:
        return []

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            search_res = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 8, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            quotes = search_res.json().get("quotes", [])
            quotes = [x for x in quotes if x.get("quoteType") in
                      ("EQUITY", "ETF", "MUTUALFUND", "CRYPTOCURRENCY")][:6]

            if not quotes:
                return []

            symbols = ",".join(x["symbol"] for x in quotes)

            price_res = await client.get(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": symbols, "fields": "regularMarketPrice,regularMarketChangePercent,regularMarketChange"},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            price_data = price_res.json()
            detail_map = {}
            for item in price_data.get("quoteResponse", {}).get("result", []):
                detail_map[item["symbol"]] = item

        results = []
        for item in quotes:
            sym = item["symbol"]
            detail = detail_map.get(sym, {})
            results.append({
                "symbol":     sym,
                "name":       item.get("longname") or item.get("shortname") or sym,
                "price":      detail.get("regularMarketPrice"),
                "change_pct": round(detail.get("regularMarketChangePercent", 0), 2),
                "change_amt": round(detail.get("regularMarketChange", 0), 2),
                "type":       item.get("quoteType", "EQUITY"),
                "exchange":   item.get("exchDisp") or item.get("exchange", ""),
            })
        return results

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []
