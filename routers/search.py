# routers/search.py
import httpx
import logging
from fastapi import APIRouter, Query
from services.price_service import PriceService

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
            quotes = res.json().get("quotes", [])
            quotes = [x for x in quotes if x.get("quoteType") in
                      ("EQUITY", "ETF", "MUTUALFUND", "CRYPTOCURRENCY")][:6]
        if not quotes:
            return []

        # Use existing price cache for known tickers, fetch for others
        results = []
        for item in quotes:
            sym = item["symbol"]
            cached = price_svc.cache.get(sym) if price_svc else None
            price      = cached.get("price")      if cached else None
            change_pct = cached.get("change_pct") if cached else 0.0
            change_amt = cached.get("change_amt") if cached else 0.0

            # If not in cache, try quick fetch via quote endpoint
            if price is None:
                try:
                    async with httpx.AsyncClient(timeout=5) as c:
                        qr = await c.get(
                            f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}",
                            params={"interval": "1d", "range": "1d"},
                            headers={"User-Agent": "Mozilla/5.0"}
                        )
                        qdata = qr.json()
                        meta  = qdata.get("chart", {}).get("result", [{}])[0].get("meta", {})
                        price      = meta.get("regularMarketPrice")
                        prev       = meta.get("chartPreviousClose") or meta.get("previousClose")
                        if price and prev:
                            change_amt = round(price - prev, 2)
                            change_pct = round((change_amt / prev) * 100, 2)
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
