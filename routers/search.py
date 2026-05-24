# routers/search.py
import httpx
import logging
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()
price_svc = None  # injected from main.py

@router.get("/")
async def search_symbols(q: str = Query(..., min_length=1)):
    q = q.strip()
    if not q:
        return []
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 8, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.yahoo.com"}
            )
            data = res.json()
            quotes = data.get("quotes", [])
            quotes = [x for x in quotes if x.get("quoteType") in
                      ("EQUITY", "ETF", "MUTUALFUND", "CRYPTOCURRENCY")][:6]

        results = []
        for item in quotes:
            sym = item["symbol"]
            # Try price from cache first
            price, change_pct, change_amt = None, 0.0, 0.0
            try:
                if price_svc and hasattr(price_svc, "cache") and sym in price_svc.cache:
                    cached     = price_svc.cache[sym]
                    price      = cached.get("price")
                    change_pct = cached.get("change_pct", 0.0)
                    change_amt = cached.get("change_amt", 0.0)
            except Exception:
                pass

            results.append({
                "symbol":     sym,
                "name":       item.get("longname") or item.get("shortname") or sym,
                "price":      price,
                "change_pct": change_pct,
                "change_amt": change_amt,
                "type":       item.get("quoteType", "EQUITY"),
                "exchange":   item.get("exchDisp") or item.get("exchange", ""),
            })
        return results

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []
