"""routers/trader.py"""
from fastapi import APIRouter
from pydantic import BaseModel
from services.trader_service import TraderService
from services.signal_service import SignalService
from services.price_service import PriceService
from services.news_service import NewsService

router = APIRouter()
_price_svc = PriceService()
_news_svc = NewsService()
_signal_svc = SignalService(_price_svc, _news_svc)
_trader_svc = TraderService()

class TradeRequest(BaseModel):
    symbol: str
    auto: bool = True   # auto=True means AI decides, auto=False means manual

@router.get("/account")
def get_account():
    return _trader_svc.get_account()

@router.get("/positions")
def get_positions():
    return _trader_svc.get_positions()

@router.get("/performance")
def get_performance():
    return _trader_svc.get_performance()

@router.post("/stop-loss/check")
def trigger_stop_loss_check():
    """Manually run the stop-loss sweep and return any triggered sells."""
    return _trader_svc.check_stop_losses()

@router.get("/trades")
def get_trade_log():
    return _trader_svc.get_trade_log()

@router.post("/execute/{symbol}")
async def execute_trade(symbol: str):
    """Get AI signal and execute trade for given symbol"""
    price_data = await _price_svc.get_one(symbol.upper())
    signal = await _signal_svc.get_signal(symbol.upper())
    result = _trader_svc.execute_signal(symbol.upper(), signal, price_data["price"])
    return {"signal": signal, "trade": result}

@router.post("/run-all")
async def run_all_signals():
    """Run AI signals for all tickers and execute trades"""
    signals = await _signal_svc.get_all_signals()
    prices = await _price_svc.get_all()
    results = []
    for symbol, signal in signals.items():
        if signal.get("signal") in ("BUY", "SELL") and signal.get("confidence") != "LOW":
            price = prices.get(symbol, {}).get("price", 0)
            trade = _trader_svc.execute_signal(symbol, signal, price)
            results.append({"symbol": symbol, "signal": signal["signal"], "trade": trade})
    return results
