"""routers/signals.py"""
from fastapi import APIRouter, Query
from services.signal_service import SignalService
from services.price_service import PriceService
from services.news_service import NewsService

router = APIRouter()
_price_svc = PriceService()
_news_svc = NewsService()
_signal_svc = SignalService(_price_svc, _news_svc)

@router.get("/")
async def get_all_signals():
    return await _signal_svc.get_all_signals()

@router.get("/{symbol}")
async def get_signal(symbol: str, force: bool = Query(False)):
    return await _signal_svc.get_signal(symbol.upper(), force=force)
