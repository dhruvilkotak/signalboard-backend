"""
services/ticker_service.py  —  Task #32

Reads signal tickers from Firestore config/signal_tickers.
Falls back to settings.TICKERS if Firestore is unavailable or doc missing.

Firestore schema:
    config/signal_tickers  →  { symbols: ["SPY","AAPL",...], updated_at, updated_by_uid }

Usage in main.py:
    from services.ticker_service import TickerService
    ticker_svc = TickerService()
    # after firebase init:
    ticker_svc.set_db(get_db())
    await ticker_svc.refresh()          # initial load
    # scheduler: add_job(ticker_svc.refresh, "interval", hours=1)

    # everywhere else:
    tickers = ticker_svc.get_tickers()  # sync, fast, no IO
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class TickerService:
    def __init__(self):
        self._db = None
        self._tickers: list[str] = list(settings.TICKERS)
        self._last_refresh: Optional[datetime] = None

    def set_db(self, db):
        """Inject Firestore client — called after firebase_service.init_firebase()."""
        self._db = db

    # ── Public API ────────────────────────────────────────────────────────────

    def get_tickers(self) -> list[str]:
        """Return current ticker list. Sync, no IO — safe to call anywhere."""
        return list(self._tickers)

    async def refresh(self):
        """
        Pull config/signal_tickers from Firestore and update in-memory list.
        Scheduled hourly. Safe to call manually (e.g. after admin updates tickers).
        """
        if not self._db:
            logger.debug("TickerService.refresh: no Firestore — keeping fallback tickers")
            return

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            doc = await loop.run_in_executor(
                None,
                lambda: self._db.collection("config").document("signal_tickers").get()
            )

            if not doc.exists:
                logger.warning(
                    "TickerService: config/signal_tickers not found in Firestore "
                    "— keeping current list"
                )
                return

            data = doc.to_dict() or {}
            raw = data.get("symbols", [])

            if not isinstance(raw, list) or not raw:
                logger.warning("TickerService: symbols field empty/invalid — keeping current list")
                return

            # Normalise: uppercase, strip, deduplicate, preserve order
            cleaned = list(dict.fromkeys(s.strip().upper() for s in raw if s and s.strip()))

            if cleaned:
                old = self._tickers
                self._tickers = cleaned
                self._last_refresh = datetime.now(timezone.utc)
                if cleaned != old:
                    logger.info(
                        f"TickerService: updated {len(old)} → {len(cleaned)} tickers: {cleaned}"
                    )
                else:
                    logger.debug(f"TickerService: {len(cleaned)} tickers unchanged")
            else:
                logger.warning("TickerService: normalised list empty — keeping current list")

        except Exception as e:
            logger.error(f"TickerService.refresh failed: {e}")
            # Non-fatal — keep whatever list we had

    def status(self) -> dict:
        """For /health endpoint."""
        return {
            "tickers": self._tickers,
            "count": len(self._tickers),
            "source": "firestore" if self._last_refresh else "env_fallback",
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
        }