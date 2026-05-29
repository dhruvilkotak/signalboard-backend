"""
Signal Board — FastAPI Backend v2.3
Changes from v2.2:
  unified — SignalEngine: single data pipeline + Claude prompt for all signal generation
  real    — TechnicalService: actual RSI/MACD/SMA/volume (replaces stub)
  rich    — SEC Form 4: transaction type, shares, price, total value, role
  filter  — Feed: BUY/SELL + HIGH/MEDIUM + <7 days fresh only (45d history available)
  ttl     — signals/{symbol}: 45-day TTL (was 30)
  sec     — /api/signals/run-all: admin auth required
  sec     — /api/ondemand/signal: any authenticated user
"""
from services.firebase_service import init_firebase

from dotenv import load_dotenv
load_dotenv()

import os, json, asyncio, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
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
from services.portfolio_service import PortfolioService
from services.auto_trader_service import AutoTraderService
from services.ondemand_signal_service import OnDemandSignalService
from services.signal_engine import SignalEngine
from middleware.admin_auth import require_admin

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Service singletons ────────────────────────────────────────────────────────
price_svc    = PriceService()
news_svc     = NewsService()
tech_svc     = TechnicalService()
market_svc   = MarketService()
signal_svc   = SignalService(price_svc, news_svc, tech_svc, market_svc)
trader_svc   = TraderService()
ticker_svc   = TickerService()
ondemand_svc = OnDemandSignalService(price_svc, news_svc)
portfolio_svc    = PortfolioService()
auto_trader_svc  = AutoTraderService(portfolio_svc, price_svc, signal_svc)

# SignalEngine: shared by both signal services — created after all deps ready
engine       = SignalEngine(price_svc, news_svc, tech_svc)
scheduler    = AsyncIOScheduler(timezone="America/New_York")

# Wire engine into both services
signal_svc.set_engine(engine)
ondemand_svc.set_engine(engine)

# ── Session ───────────────────────────────────────────────────────────────────
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

# ── Signal runner ─────────────────────────────────────────────────────────────
async def run_signals_for_admin_tickers(session: str, force: bool, auto_trade: bool = False) -> dict:
    tickers = ticker_svc.get_tickers()
    logger.info(f"[{session}] Running signals for {len(tickers)} tickers: {tickers}")

    sem = asyncio.Semaphore(10)

    async def _one(sym):
        async with sem:
            return sym, await signal_svc.get_signal(sym, force=force, session=session, trigger="scheduled")

    results = await asyncio.gather(*[_one(t) for t in tickers], return_exceptions=True)
    # Deduplicate by symbol — keep freshest generated_at (same ticker may run
    # across multiple sessions: pre/market/post, producing multiple results)
    raw_signals = [r for r in results if not isinstance(r, Exception)]
    deduped: dict = {}
    for sym, sig in raw_signals:
        existing = deduped.get(sym)
        if not existing or str(sig.get("generated_at", "")) > str(existing.get("generated_at", "")):
            deduped[sym] = sig
    signals = deduped

    buy  = sum(1 for s in signals.values() if s.get("signal") == "BUY")
    sell = sum(1 for s in signals.values() if s.get("signal") == "SELL")
    hold = sum(1 for s in signals.values() if s.get("signal") == "HOLD")
    feed = sum(1 for s in signals.values() if s.get("feed_eligible"))
    logger.info(f"[{session}] {buy} BUY / {hold} HOLD / {sell} SELL — {feed} feed-eligible")

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

# ── Price spike job ───────────────────────────────────────────────────────────
_spike_cooldown: dict[str, str] = {}

async def price_spike_job():
    SPIKE_THRESHOLD  = 2.0
    COOLDOWN_SECONDS = 1800
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
            result = await signal_svc.get_signal(symbol, force=True, session=session, trigger="price_spike")
            _spike_cooldown[symbol] = now.isoformat()
            logger.info(f"SPIKE: {symbol} {change_pct:+.1f}% → {result.get('signal')}/{result.get('confidence')}")
        except Exception as e:
            logger.error(f"price_spike_job failed for {symbol}: {e}")

# ── Session jobs ──────────────────────────────────────────────────────────────
async def pre_market_signal_job():
    logger.info("=== PRE-MARKET signal job ===")
    try:
        await price_svc.update_cache()
        await news_svc.fetch_and_cache()
        await run_signals_for_admin_tickers("pre_market", force=True, auto_trade=False)
    except Exception as e:
        logger.error(f"Pre-market job failed: {e}")

async def market_hours_signal_job():
    logger.info("=== MARKET HOURS signal job ===")
    try:
        await price_svc.update_cache()
        signals = await run_signals_for_admin_tickers("market", force=False, auto_trade=False)  # ← False: stops Alpaca trades
        await auto_trader_svc.run_for_all_users(signals)   # ← new: runs per-user Firestore trades
    except Exception as e:
        logger.error(f"Market hours job failed: {e}")

async def post_market_signal_job():
    logger.info("=== POST-MARKET signal job ===")
    try:
        await news_svc.fetch_and_cache()
        await price_svc.update_cache()
        await run_signals_for_admin_tickers("post_market", force=True, auto_trade=False)
    except Exception as e:
        logger.error(f"Post-market job failed: {e}")

async def news_refresh_job():
    await news_svc.fetch_and_cache()

async def price_refresh_job():
    await price_svc.update_cache()

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Signal Board v2.3...")

    firebase_ok = init_firebase()
    if firebase_ok:
        logger.info("Firestore ready ✓")
        from services.firebase_service import get_db
        db = get_db()
        signal_svc.set_db(db)
        ticker_svc.set_db(db)
        ondemand_svc.set_db(db)
        portfolio_svc.set_db(db)
        auto_trader_svc.set_db(db)
    else:
        logger.warning("Running WITHOUT Firebase")

    await ticker_svc.refresh()
    logger.info(f"Signal tickers ({len(ticker_svc.get_tickers())}): {ticker_svc.get_tickers()}")

    scheduler.add_job(price_refresh_job,  "interval", seconds=settings.PRICE_REFRESH_SECONDS, id="price_refresh")
    scheduler.add_job(news_refresh_job,   "interval", minutes=15,  id="news_refresh")
    scheduler.add_job(ticker_svc.refresh, "interval", hours=1,     id="ticker_refresh")
    scheduler.add_job(price_spike_job,    "interval", seconds=30,  id="price_spike_resignal")
    scheduler.add_job(auto_trader_svc.stop_loss_monitor, "interval", seconds=60, id="stop_loss_monitor")
    for hour, minute in [(4,0),(6,0),(8,0),(9,0)]:
        scheduler.add_job(pre_market_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York"),
            id=f"pre_market_{hour}_{minute}")

    for hour, minute in [(9,30),(11,0),(13,0),(14,30),(15,30)]:
        scheduler.add_job(market_hours_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York"),
            id=f"market_{hour}_{minute}")

    for hour, minute in [(16,15),(17,30),(19,0)]:
        scheduler.add_job(post_market_signal_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York"),
            id=f"post_market_{hour}_{minute}")

    scheduler.start()
    await news_svc.fetch_and_cache()
    await price_svc.update_cache()

    logger.info(f"Scheduler: {len(scheduler.get_jobs())} jobs — Signal Board v2.3 ready ✓")
    yield
    scheduler.shutdown()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Signal Board API", version="2.3.0", lifespan=lifespan, redirect_slashes=False)

app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

from routers import prices, news, signals, trader, alerts, chat, quote, watchlist, search, admin, portfolio
from routers import ondemand

signals.signal_svc    = signal_svc
signals.price_svc     = price_svc
trader.trader_svc     = trader_svc
trader.signal_svc     = signal_svc
trader.price_svc      = price_svc
chat.price_svc        = price_svc
chat.news_svc         = news_svc
prices.price_svc      = price_svc
news.news_svc         = news_svc
search.price_svc      = price_svc
ondemand.ondemand_svc = ondemand_svc
portfolio.portfolio_svc   = portfolio_svc
portfolio.auto_trader_svc = auto_trader_svc
portfolio.price_svc       = price_svc

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
app.include_router(ondemand.router,  prefix="/api/ondemand",  tags=["ondemand"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])

@app.get("/api/market", tags=["market"])
async def get_market_context():
    return await market_svc.get_market_context(settings.TICKERS)

@app.get("/api/session", tags=["market"])
async def get_session():
    session  = get_current_session()
    jobs     = scheduler.get_jobs()
    next_jobs = sorted(
        [{"id": j.id, "next_run": str(j.next_run_time)} for j in jobs if j.next_run_time],
        key=lambda x: x["next_run"]
    )[:3]
    return {"current_session": session, "session_label": SESSIONS.get(session, {}).get("label", "Closed"), "next_scheduled_runs": next_jobs, "timestamp": datetime.now(timezone.utc).isoformat()}

@app.post("/api/signals/run-all", tags=["signals"])
async def trigger_all_signals(admin=Depends(require_admin)):
    """Admin-only: force regenerate signals for all admin tickers."""
    session      = get_current_session()
    signals_data = await run_signals_for_admin_tickers(session, force=True, auto_trade=False)
    feed_count   = sum(1 for s in signals_data.values() if s.get("feed_eligible"))
    return {"signals": signals_data, "session": session, "count": len(signals_data), "feed_eligible_count": feed_count}

@app.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    class CM:
        active = []
        async def connect(self, ws): await ws.accept(); self.active.append(ws)
        def disconnect(self, ws):
            if ws in self.active: self.active.remove(ws)
    mgr = CM()
    await mgr.connect(websocket)
    try:
        while True:
            prices_data    = await price_svc.get_all()
            cached_signals = signal_svc.get_all_cached()
            session        = get_current_session()
            await websocket.send_json({
                "type": "prices", "data": prices_data, "session": session,
                "signals": {k: {"signal": v.get("signal"), "confidence": v.get("confidence")} for k, v in cached_signals.items()},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        mgr.disconnect(websocket)

@app.get("/health")
async def health():                          # ← add async here
    return {
        "status": "ok", "version": "2.3.0",
        "current_session": get_current_session(),
        "scheduler_running": scheduler.running,
        "active_jobs": len(scheduler.get_jobs()),
        "schedule": {
            "pre_market": "4:00,6:00,8:00,9:00 AM EST Mon-Fri",
            "market": "9:30,11:00AM,1:00,2:30,3:30 PM EST Mon-Fri",
            "post_market": "4:15,5:30,7:00 PM EST Mon-Fri",
            "news_refresh": "Every 15 min",
            "price_refresh": f"Every {settings.PRICE_REFRESH_SECONDS}s",
            "ticker_refresh": "Every 60 min",
            "spike_resignal": "Every 30s (>2% move, 30-min cooldown)",
        },
        "tickers":   ticker_svc.status(),
        "auto_trader": await auto_trader_svc.get_status() if auto_trader_svc else {"enabled": True},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }