"""
Signal Board — FastAPI Backend v2.2
Auto-generates AI signals across ALL trading sessions:

PRE-MARKET  (4:00 AM - 9:30 AM EST)  → 4:00, 6:00, 8:00, 9:00 AM
MARKET OPEN (9:30 AM - 4:00 PM EST)  → 9:30, 11:00, 1:00, 2:30, 3:30 PM
POST-MARKET (4:00 PM - 8:00 PM EST)  → 4:15, 5:30, 7:00 PM

Signal universe = admin-managed tickers in Firestore config/signal_tickers.
User watchlists are for display only — no extra signal generation.

Changes from v2.1:
  #32 — TickerService: signal tickers read from Firestore config/signal_tickers (hourly refresh)
  #38 — Price-spike re-signal: >2% move triggers immediate re-signal (30-min cooldown)
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
from services.ticker_service import TickerService    
from services.ondemand_signal_service import OnDemandSignalService

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
ticker_svc = TickerService()                             
ondemand_svc = OnDemandSignalService(price_svc, news_svc)
scheduler  = AsyncIOScheduler(timezone="America/New_York")

# ── Trading session definitions ───────────────────────────────────────────────
SESSIONS = {
    "pre_market":  {"start": 4,  "end": 9,  "label": "Pre-Market"},
    "market":      {"start": 9,  "end": 16, "label": "Market Hours"},
    "post_market": {"start": 16, "end": 20, "label": "Post-Market"},
}

def get_current_session() -> str:
    now_est = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    hour = now_est.hour
    if 4 <= hour < 9:   return "pre_market"
    if 9 <= hour < 16:  return "market"
    if 16 <= hour < 20: return "post_market"
    return "closed"

# ── Shared signal runner — admin tickers only ─────────────────────────────────
async def run_signals_for_admin_tickers(
    session: str,
    force: bool,
    auto_trade: bool = False,
) -> dict:
    """
    Generate signals for the admin-managed ticker list only.
    Called by all three session jobs.
    """
    tickers = ticker_svc.get_tickers()
    logger.info(f"[{session}] Generating signals for {len(tickers)} admin tickers: {tickers}")

    sem = asyncio.Semaphore(10)

    async def _one(sym):
        async with sem:
            return sym, await signal_svc.get_signal(
                sym, force=force, session=session, trigger="scheduled"
            )

    results = await asyncio.gather(*[_one(t) for t in tickers], return_exceptions=True)
    signals = {sym: sig for r in results
               if not isinstance(r, Exception)
               for sym, sig in [r]}

    buy  = sum(1 for s in signals.values() if s.get("signal") == "BUY")
    sell = sum(1 for s in signals.values() if s.get("signal") == "SELL")
    hold = sum(1 for s in signals.values() if s.get("signal") == "HOLD")
    logger.info(f"[{session}] {buy} BUY / {hold} HOLD / {sell} SELL")

    if auto_trade:
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
        logger.info(f"[{session}] {trades} trades executed")

    return signals

# ── Price-spike re-signal job (#38) ──────────────────────────────────────────
_spike_cooldown: dict[str, str] = {}   # symbol → ISO timestamp of last spike signal

async def price_spike_job():
    """
    Runs every 30s. Re-generates a signal for any admin ticker that moved >2%,
    subject to a 30-min per-symbol cooldown.
    Only runs on admin tickers — same universe as scheduled jobs.
    """
    SPIKE_THRESHOLD  = 2.0    # %
    COOLDOWN_SECONDS = 1800   # 30 min

    try:
        prices = await price_svc.get_all()
    except Exception:
        return

    now     = datetime.now(timezone.utc)
    session = get_current_session()

    for symbol in ticker_svc.get_tickers():
        pd         = prices.get(symbol, {})
        change_pct = abs(float(pd.get("change_pct", 0) or 0))

        if change_pct < SPIKE_THRESHOLD:
            continue

        # Cooldown check
        last_ts = _spike_cooldown.get(symbol)
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now - last_dt).total_seconds() < COOLDOWN_SECONDS:
                    continue
            except Exception:
                pass

        try:
            result = await signal_svc.get_signal(
                symbol, force=True, session=session, trigger="price_spike",
            )
            _spike_cooldown[symbol] = now.isoformat()
            logger.info(
                f"SPIKE: {symbol} {change_pct:+.1f}% → "
                f"{result.get('signal')}/{result.get('confidence')} (trigger=price_spike)"
            )
        except Exception as e:
            logger.error(f"price_spike_job: signal failed for {symbol}: {e}")

# ── Signal jobs per session ───────────────────────────────────────────────────
async def pre_market_signal_job():
    logger.info("=== PRE-MARKET signal job started ===")
    try:
        await price_svc.update_cache()
        await news_svc.fetch_and_cache()
        await run_signals_for_admin_tickers("pre_market", force=True, auto_trade=False)
        logger.info("Pre-market: NO auto-trades executed (market not open yet)")
    except Exception as e:
        logger.error(f"Pre-market job failed: {e}")


async def market_hours_signal_job():
    logger.info("=== MARKET HOURS signal job started ===")
    try:
        await price_svc.update_cache()
        await run_signals_for_admin_tickers("market", force=False, auto_trade=True)
    except Exception as e:
        logger.error(f"Market hours job failed: {e}")


async def post_market_signal_job():
    logger.info("=== POST-MARKET signal job started ===")
    try:
        await news_svc.fetch_and_cache()
        await price_svc.update_cache()
        await run_signals_for_admin_tickers("post_market", force=True, auto_trade=False)
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
    logger.info("Starting Signal Board v2.2...")

    firebase_ok = init_firebase()
    if firebase_ok:
        logger.info("Firestore ready ✓")
        from services.firebase_service import get_db
        db = get_db()
        signal_svc.set_db(db)                              # existing
        ticker_svc.set_db(db)                              # #32
        ondemand_svc.set_db(db)
    else:
        logger.warning("Running WITHOUT Firebase — watchlist/auth/tickers disabled")

    # Initial ticker load from Firestore (#32)
    await ticker_svc.refresh()
    logger.info(f"Signal tickers ({len(ticker_svc.get_tickers())}): {ticker_svc.get_tickers()}")

    # Price refresh every 30s
    scheduler.add_job(
        price_refresh_job, "interval",
        seconds=settings.PRICE_REFRESH_SECONDS,
        id="price_refresh",
    )

    # News refresh every 15 min
    scheduler.add_job(
        news_refresh_job, "interval",
        minutes=15,
        id="news_refresh",
    )

    # Ticker list refresh every hour (#32)
    scheduler.add_job(
        ticker_svc.refresh, "interval",
        hours=1,
        id="ticker_refresh",
    )

    # Price-spike re-signal every 30s (#38)
    scheduler.add_job(
        price_spike_job, "interval",
        seconds=30,
        id="price_spike_resignal",
    )

    # ── PRE-MARKET schedule (Mon-Fri) ────────────────────────────────────────
    for hour, minute in [(4,0), (6,0), (8,0), (9,0)]:
        scheduler.add_job(
            pre_market_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                        timezone="America/New_York"),
            id=f"pre_market_{hour}_{minute}",
        )

    # ── MARKET HOURS schedule (Mon-Fri) ─────────────────────────────────────
    for hour, minute in [(9,30), (11,0), (13,0), (14,30), (15,30)]:
        scheduler.add_job(
            market_hours_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                        timezone="America/New_York"),
            id=f"market_{hour}_{minute}",
        )

    # ── POST-MARKET schedule (Mon-Fri) ───────────────────────────────────────
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
    logger.info("Signal Board v2.2 ready ✓")
    yield
    scheduler.shutdown()
    logger.info("Signal Board stopped.")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Signal Board API", version="2.2.0", lifespan=lifespan, redirect_slashes=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inject services into routers
from routers import prices, news, signals, trader, alerts, chat, quote, watchlist, search, admin, ondemand

signals.signal_svc = signal_svc
signals.price_svc  = price_svc
trader.trader_svc  = trader_svc
trader.signal_svc  = signal_svc
trader.price_svc   = price_svc
chat.price_svc     = price_svc
chat.news_svc      = news_svc
prices.price_svc   = price_svc
news.news_svc      = news_svc
search.price_svc   = price_svc
ondemand.ondemand_svc = ondemand_svc

app.include_router(prices.router,    prefix="/api/prices",    tags=["prices"])
app.include_router(news.router,      prefix="/api/news",      tags=["news"])
app.include_router(signals.router,   prefix="/api/signals",   tags=["signals"])
app.include_router(trader.router,    prefix="/api/trader",    tags=["trader"])
app.include_router(alerts.router,    prefix="/api/alerts",    tags=["alerts"])
app.include_router(chat.router,      prefix="/api/chat",      tags=["chat"])
app.include_router(quote.router,     prefix="/api/quote",     tags=["quote"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["watchlist"])
app.include_router(search.router,    prefix="/api/search",    tags=["search"])
app.include_router(admin.router,     prefix="/api/admin",     tags=["admin"])
app.include_router(ondemand.router, prefix="/api/ondemand", tags=["ondemand"])

@app.get("/api/market", tags=["market"])
async def get_market_context():
    return await market_svc.get_market_context(settings.TICKERS)

@app.get("/api/session", tags=["market"])
async def get_session():
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
    """Admin-only: force regenerate signals for all admin tickers."""
    session = get_current_session()
    signals_data = await run_signals_for_admin_tickers(session, force=True, auto_trade=False)
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
        "version": "2.2.0",
        "current_session": session,
        "scheduler_running": scheduler.running,
        "active_jobs": len(jobs),
        "schedule": {
            "pre_market":    "4:00, 6:00, 8:00, 9:00 AM EST (Mon-Fri)",
            "market":        "9:30, 11:00 AM, 1:00, 2:30, 3:30 PM EST (Mon-Fri)",
            "post_market":   "4:15, 5:30, 7:00 PM EST (Mon-Fri)",
            "news_refresh":  "Every 15 minutes",
            "price_refresh": f"Every {settings.PRICE_REFRESH_SECONDS} seconds",
            "ticker_refresh": "Every 60 minutes",
            "spike_resignal": "Every 30 seconds (>2% move, 30-min cooldown per symbol)",
        },
        "tickers": ticker_svc.status(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }