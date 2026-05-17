"""
Signal Board — FastAPI Backend
Runs on Oracle Cloud ARM / GCP e2-micro (free, always-on)
"""
import os, json, asyncio, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from routers import prices, news, signals, trader, alerts, chat
from services.price_service import PriceService
from services.news_service import NewsService

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

price_service = PriceService()
news_service = NewsService()
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background jobs on boot
    scheduler.add_job(news_service.fetch_and_cache, "interval", minutes=30, id="news_fetch")
    scheduler.add_job(price_service.update_cache, "interval", seconds=30, id="price_refresh")
    scheduler.start()
    await news_service.fetch_and_cache()   # warm cache on start
    logger.info("Background jobs started")
    yield
    scheduler.shutdown()
    logger.info("Shutdown complete")

app = FastAPI(title="Signal Board API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your Vercel URL in production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(prices.router,  prefix="/api/prices",  tags=["prices"])
app.include_router(news.router,    prefix="/api/news",    tags=["news"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(trader.router,  prefix="/api/trader",  tags=["trader"])
app.include_router(alerts.router,  prefix="/api/alerts",  tags=["alerts"])
app.include_router(chat.router,    prefix="/api/chat",    tags=["chat"])

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

@app.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            prices = await price_service.get_all()
            await websocket.send_json({"type": "prices", "data": prices})
            await asyncio.sleep(15)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "signal-board"}
