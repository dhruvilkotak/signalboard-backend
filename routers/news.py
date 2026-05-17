"""routers/news.py"""
from fastapi import APIRouter
from services.news_service import NewsService

router = APIRouter()
_svc = NewsService()

@router.get("/")
async def get_all_news():
    return await _svc.get_all()

@router.get("/{symbol}")
async def get_news(symbol: str):
    return await _svc.get_for_symbol(symbol.upper())
