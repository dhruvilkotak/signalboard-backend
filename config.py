"""
config.py — central settings loaded from .env
Import this instead of os.getenv() scattered everywhere
"""
import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Alpaca
    ALPACA_API_KEY: str        = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str     = os.getenv("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL: str       = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    ALPACA_PAPER: bool         = "paper" in os.getenv("ALPACA_BASE_URL", "paper")

    # Anthropic
    ANTHROPIC_API_KEY: str     = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str       = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

    # Firebase
    FIREBASE_CREDENTIALS: str  = os.getenv("FIREBASE_CREDENTIALS_PATH", "./firebase-credentials.json")

    # SendGrid
    SENDGRID_API_KEY: str      = os.getenv("SENDGRID_API_KEY", "")
    ALERT_EMAIL_FROM: str      = os.getenv("ALERT_EMAIL_FROM", "")
    ALERT_EMAIL_TO: str        = os.getenv("ALERT_EMAIL_TO", "")

    # App
    PORT: int                  = int(os.getenv("PORT", "8000"))
    ENVIRONMENT: str           = os.getenv("ENVIRONMENT", "development")
    CORS_ORIGINS: list[str]    = os.getenv("CORS_ORIGINS", "*").split(",")

    # Tickers
    TICKERS: list[str]         = os.getenv(
        "TICKERS",
        "SPY,VOO,JEPI,JEPQ,SCHD,SGOV,MSFT,AAPL,NVDA,GOOGL,AMZN,META,HOOD"
    ).split(",")

    # Trading
    PAPER_BUDGET: float        = float(os.getenv("PAPER_BUDGET", "100.0"))
    MAX_POSITION_PCT: float    = float(os.getenv("MAX_POSITION_PCT", "0.20"))  # 20% per trade

    # Signal cache
    SIGNAL_CACHE_TTL: int      = int(os.getenv("SIGNAL_CACHE_TTL", "1800"))    # 30 min
    PRICE_CHANGE_THRESHOLD: float = float(os.getenv("PRICE_CHANGE_THRESHOLD", "1.5"))  # %

    # Scheduler
    NEWS_REFRESH_MINUTES: int  = int(os.getenv("NEWS_REFRESH_MINUTES", "30"))
    PRICE_REFRESH_SECONDS: int = int(os.getenv("PRICE_REFRESH_SECONDS", "30"))

settings = Settings()
