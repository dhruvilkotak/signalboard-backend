"""
models/schemas.py — all Pydantic request/response models
Single source of truth for data shapes shared across routers
"""
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ── Prices ──────────────────────────────────────────────
class PriceData(BaseModel):
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: int
    change_pct: float
    prev_close: float
    timestamp: str


# ── News ─────────────────────────────────────────────────
class NewsArticle(BaseModel):
    id: Optional[str] = None
    headline: str
    summary: Optional[str] = ""
    url: str
    source: str
    created_at: str
    symbols: list[str] = []


# ── Signals ───────────────────────────────────────────────
class SignalResponse(BaseModel):
    signal: str                     # BUY | HOLD | SELL
    confidence: str                 # HIGH | MEDIUM | LOW
    target_price: Optional[float] = None
    expected_return_pct: Optional[float] = None
    timeframe: Optional[str] = "1-3 months"
    summary: str
    risk: str                       # LOW | MEDIUM | HIGH
    price_at_signal: Optional[float] = None
    generated_at: Optional[str] = None


# ── Trader ────────────────────────────────────────────────
class AccountInfo(BaseModel):
    buying_power: float
    portfolio_value: float
    cash: float
    pnl: float
    pnl_pct: float


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


class TradeRecord(BaseModel):
    status: str
    action: str
    symbol: str
    qty: float
    price: float
    amount: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    order_id: Optional[str] = None
    timestamp: str
    signal_reason: Optional[str] = ""


class PerformanceSummary(BaseModel):
    budget: float
    current_value: float
    pnl: float
    pnl_pct: float
    total_trades: int
    open_positions: int
    target: float
    progress_to_target_pct: float


# ── Alerts ────────────────────────────────────────────────
class AlertConfig(BaseModel):
    symbol: str
    condition: str      # "above" | "below"
    price: float
    email: str


# ── Chat ─────────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    symbol: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    symbol: Optional[str] = None
    question: str
