"""
Signal Board — FastAPI Backend v2.1
Auto-generates AI signals across ALL trading sessions:

PRE-MARKET  (4:00 AM - 9:30 AM EST)  → 4:00, 6:00, 8:00, 9:00 AM
MARKET OPEN (9:30 AM - 4:00 PM EST)  → 9:30, 11:00, 1:00, 2:30, 3:30 PM
POST-MARKET (4:00 PM - 8:00 PM EST)  → 4:15, 5:30, 7:00 PM

Each session uses different signal logic:
- Pre-market:  focus on overnight news, futures, earnings releases
- Market:      full technical + sentiment analysis
- Post-market: focus on earnings results, after-hours price moves
"""
from services.firebase_service import init_firebase

from dotenv import load_dotenv
load_dotenv()

import os, json, asyncio, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from services.price_service import PriceService
from services.news_service import NewsService
from services.signal_service import SignalService
from services.trader_service import TraderService
from services.technical_service import TechnicalService
from services.market_service import MarketService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── Service singletons ────────────────────────────────────────────────────────
price_svc  = PriceService()
news_svc   = NewsService()
tech_svc   = TechnicalService()
market_svc = MarketService()
signal_svc = SignalService(price_svc, news_svc, tech_svc, market_svc)
trader_svc = TraderService()
scheduler  = AsyncIOScheduler(timezone="America/New_York")

# ── Trading session definitions ───────────────────────────────────────────────
SESSIONS = {
    "pre_market":  {"start": 4,    "end": 9,   "label": "Pre-Market"},
    "market":      {"start": 9,    "end": 16,  "label": "Market Hours"},
    "post_market": {"start": 16,   "end": 20,  "label": "Post-Market"},
}

def get_current_session() -> str:
    """Returns current trading session name"""
    now_est = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    hour = now_est.hour
    if 4 <= hour < 9:    return "pre_market"
    if 9 <= hour < 16:   return "market"
    if 16 <= hour < 20:  return "post_market"
    return "closed"

# ── Signal jobs per session ───────────────────────────────────────────────────
async def pre_market_signal_job():
    """
    Pre-market signal (4 AM - 9:30 AM EST)
    Focus: overnight news, futures direction, earnings releases
    Strategy: lighter trading, informational signals only (no auto-trade before 9:30)
    """
    logger.info("=== PRE-MARKET signal job started ===")
    try:
        await price_svc.update_cache()
        await news_svc.fetch_and_cache()   # fresh news sweep — crucial for pre-market

        # Generate signals with pre-market context flag
        signals = await signal_svc.get_all_signals(
            force=True,
            session="pre_market"
        )
        buy  = sum(1 for s in signals.values() if s.get("signal") == "BUY")
        sell = sum(1 for s in signals.values() if s.get("signal") == "SELL")
        hold = sum(1 for s in signals.values() if s.get("signal") == "HOLD")
        logger.info(f"Pre-market signals: {buy} BUY, {hold} HOLD, {sell} SELL")
        logger.info("Pre-market: NO auto-trades executed (market not open yet)")

    except Exception as e:
        logger.error(f"Pre-market job failed: {e}")


async def market_hours_signal_job():
    """
    Market hours signal (9:30 AM - 4:00 PM EST)
    Focus: full technical analysis + live prices + news
    Strategy: full auto-trade on HIGH/MEDIUM confidence
    """
    logger.info("=== MARKET HOURS signal job started ===")
    try:
        await price_svc.update_cache()
        signals = await signal_svc.get_all_signals(
            force=False,
            session="market"
        )
        buy  = sum(1 for s in signals.values() if s.get("signal") == "BUY")
        sell = sum(1 for s in signals.values() if s.get("signal") == "SELL")
        hold = sum(1 for s in signals.values() if s.get("signal") == "HOLD")
        logger.info(f"Market signals: {buy} BUY, {hold} HOLD, {sell} SELL")

        # Auto-execute trades during market hours
        prices = await price_svc.get_all()
        trades = 0
        for symbol, signal in signals.items():
            if (signal.get("signal") in ("BUY", "SELL") and
                    signal.get("confidence") in ("HIGH", "MEDIUM")):
                price = prices.get(symbol, {}).get("price", 0)
                if price > 0:
                    result = trader_svc.execute_signal(symbol, signal, price)
                    if result.get("status") == "executed":
                        trades += 1
                        logger.info(f"Trade: {symbol} {signal['signal']} @ ${price}")
        logger.info(f"Market hours: {trades} trades executed")

    except Exception as e:
        logger.error(f"Market hours job failed: {e}")


async def post_market_signal_job():
    """
    Post-market signal (4:00 PM - 8:00 PM EST)
    Focus: earnings results, after-hours price moves, next-day preparation
    Strategy: signals only — no trades (market closed, but prepare for next open)
    """
    logger.info("=== POST-MARKET signal job started ===")
    try:
        await news_svc.fetch_and_cache()   # critical — earnings drop after close
        await price_svc.update_cache()     # after-hours prices if available

        signals = await signal_svc.get_all_signals(
            force=True,
            session="post_market"
        )
        buy  = sum(1 for s in signals.values() if s.get("signal") == "BUY")
        sell = sum(1 for s in signals.values() if s.get("signal") == "SELL")
        hold = sum(1 for s in signals.values() if s.get("signal") == "HOLD")
        logger.info(f"Post-market signals: {buy} BUY, {hold} HOLD, {sell} SELL")
        logger.info("Post-market: signals saved for next market open")

    except Exception as e:
        logger.error(f"Post-market job failed: {e}")


async def news_refresh_job():
    logger.info("News refresh...")
    await news_svc.fetch_and_cache()

async def price_refresh_job():
    await price_svc.update_cache()

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Signal Board v2.1...")

    firebase_ok = init_firebase()
    if firebase_ok:
        logger.info("Firestore ready ✓")
    else:
        logger.warning("Running WITHOUT Firebase — watchlist/auth disabled")
 
    # Price refresh every 30s (all hours)
    scheduler.add_job(
        price_refresh_job, "interval",
        seconds=settings.PRICE_REFRESH_SECONDS,
        id="price_refresh",
    )

    # News refresh every 15 min (more frequent than before)
    scheduler.add_job(
        news_refresh_job, "interval",
        minutes=15,
        id="news_refresh",
    )

    # ── PRE-MARKET schedule (Mon-Fri, 4 AM - 9 AM EST) ──────────────────────
    # 4:00 AM — overnight news + futures check
    # 6:00 AM — early morning sweep
    # 8:00 AM — final pre-market check
    # 9:00 AM — 30 min before open preparation
    for hour, minute in [(4,0), (6,0), (8,0), (9,0)]:
        scheduler.add_job(
            pre_market_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                       timezone="America/New_York"),
            id=f"pre_market_{hour}_{minute}",
        )

    # ── MARKET HOURS schedule (Mon-Fri, 9:30 AM - 3:30 PM EST) ─────────────
    # 9:30 AM  — market open
    # 11:00 AM — mid-morning
    # 1:00 PM  — post-lunch
    # 2:30 PM  — power hour setup
    # 3:30 PM  — final 30 min before close
    for hour, minute in [(9,30), (11,0), (13,0), (14,30), (15,30)]:
        scheduler.add_job(
            market_hours_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                       timezone="America/New_York"),
            id=f"market_{hour}_{minute}",
        )

    # ── POST-MARKET schedule (Mon-Fri, 4:15 PM - 7 PM EST) ─────────────────
    # 4:15 PM — right after close, catch earnings releases
    # 5:30 PM — mid post-market sweep
    # 7:00 PM — final post-market check before overnight
    for hour, minute in [(16,15), (17,30), (19,0)]:
        scheduler.add_job(
            post_market_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                       timezone="America/New_York"),
            id=f"post_market_{hour}_{minute}",
        )

    scheduler.start()

    # Warm up on boot
    await news_svc.fetch_and_cache()
    await price_svc.update_cache()

    logger.info(f"Scheduler running: {len(scheduler.get_jobs())} jobs active")
    logger.info("Signal Board v2.1 ready ✓")
    yield
    scheduler.shutdown()
    logger.info("Signal Board stopped.")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Signal Board API", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inject services into routers
from routers import prices, news, signals, trader, alerts, chat, quote, watchlist

signals.signal_svc = signal_svc
signals.price_svc  = price_svc
trader.trader_svc  = trader_svc
trader.signal_svc  = signal_svc
trader.price_svc   = price_svc
chat.price_svc     = price_svc
chat.news_svc      = news_svc
prices.price_svc   = price_svc
news.news_svc      = news_svc

app.include_router(prices.router,    prefix="/api/prices",    tags=["prices"])
app.include_router(news.router,      prefix="/api/news",      tags=["news"])
app.include_router(signals.router,   prefix="/api/signals",   tags=["signals"])
app.include_router(trader.router,    prefix="/api/trader",    tags=["trader"])
app.include_router(alerts.router,    prefix="/api/alerts",    tags=["alerts"])
app.include_router(chat.router,      prefix="/api/chat",      tags=["chat"])
app.include_router(quote.router,     prefix="/api/quote",     tags=["quote"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["watchlist"])

@app.get("/api/market", tags=["market"])
async def get_market_context():
    return await market_svc.get_market_context(settings.TICKERS)

@app.get("/api/session", tags=["market"])
async def get_session():
    """Returns current trading session + next scheduled job"""
    session = get_current_session()
    jobs = scheduler.get_jobs()
    next_jobs = sorted(
        [{"id": j.id, "next_run": str(j.next_run_time)} for j in jobs if j.next_run_time],
        key=lambda x: x["next_run"]
    )[:3]
    return {
        "current_session": session,
        "session_label": SESSIONS.get(session, {}).get("label", "Market Closed"),
        "next_scheduled_runs": next_jobs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.post("/api/signals/run-all", tags=["signals"])
async def trigger_all_signals():
    session = get_current_session()
    signals_data = await signal_svc.get_all_signals(force=True, session=session)
    return {
        "signals": signals_data,
        "session": session,
        "count": len(signals_data),
    }

# ── WebSocket ─────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

manager = ConnectionManager()

@app.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            prices_data    = await price_svc.get_all()
            cached_signals = signal_svc.get_all_cached()
            session        = get_current_session()
            await websocket.send_json({
                "type": "prices",
                "data": prices_data,
                "session": session,
                "signals": {
                    k: {
                        "signal": v.get("signal"),
                        "confidence": v.get("confidence"),
                    }
                    for k, v in cached_signals.items()
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    session = get_current_session()
    jobs    = scheduler.get_jobs()
    return {
        "status": "ok",
        "version": "2.1.0",
        "current_session": session,
        "scheduler_running": scheduler.running,
        "active_jobs": len(jobs),
        "schedule": {
            "pre_market":  "4:00, 6:00, 8:00, 9:00 AM EST (Mon-Fri)",
            "market":      "9:30, 11:00 AM, 1:00, 2:30, 3:30 PM EST (Mon-Fri)",
            "post_market": "4:15, 5:30, 7:00 PM EST (Mon-Fri)",
            "news_refresh": "Every 15 minutes",
            "price_refresh": f"Every {settings.PRICE_REFRESH_SECONDS} seconds",
        },
        "tickers": settings.TICKERS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }