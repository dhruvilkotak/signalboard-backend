"""
routers/prices.py — Price endpoints
"""
from fastapi import APIRouter, Depends
from services.price_service import PriceService

router = APIRouter()
_price_service = PriceService()

def get_price_service():
    return _price_service

@router.get("/")
async def get_all_prices(svc: PriceService = Depends(get_price_service)):
    return await svc.get_all()

@router.get("/{symbol}")
async def get_price(symbol: str, svc: PriceService = Depends(get_price_service)):
    return await svc.get_one(symbol.upper())
