# routers/news.py
# News endpoints:
#   GET /api/news/          → all cached signal ticker news (for Claude)
#   GET /api/news/{symbol}  → fresh RSS news for any symbol (for frontend)

import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()
news_svc = None  # injected from main.py


@router.get("/")
async def get_all_news():
    """All cached news for signal tickers — used by Claude signal generation."""
    if not news_svc:
        return {}
    return await news_svc.get_all()


@router.get("/{symbol}")
async def get_news_for_symbol(symbol: str):
    """
    Fresh news for any symbol.
    - Signal tickers: returned from 15-min cache (fast)
    - Custom symbols: fetched live from Yahoo RSS (fresh)
    Works for stocks AND ETFs. Used by frontend News tab.
    """
    if not news_svc:
        return []
    symbol = symbol.upper().strip()
    return await news_svc.get_for_symbol(symbol)