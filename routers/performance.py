"""routers/performance.py — portfolio performance chart data"""
from fastapi import APIRouter
from routers.trader import _trader_svc

router = APIRouter()

@router.get("/chart")
def get_performance_chart():
    """Time-series portfolio value snapshots for charting."""
    return _trader_svc.get_performance_chart()
