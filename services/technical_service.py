"""
services/technical_service.py

Real technical indicators calculated from Yahoo Finance historical OHLCV data.
Replaces the stub that returned hardcoded RSI=50 for everything.

Indicators calculated:
  - RSI (14-period)
  - MACD (12/26/9 EMA) + signal line + histogram + crossover direction
  - SMA 20, SMA 50, SMA 200
  - Price vs SMA20/50 position
  - Volume spike (today vs 20-day average)
  - 5-day and 20-day price momentum %
  - Trend (BULLISH / BEARISH / NEUTRAL)

Data source: Yahoo Finance /v8/finance/chart — free, no API key
Cache: 15 minutes in memory (avoids redundant fetches during bulk signal runs)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 15-minute in-memory cache
_cache: dict[str, dict] = {}
_cache_ts: dict[str, datetime] = {}
CACHE_TTL_SECONDS = 900


class TechnicalService:

    async def get_technicals(self, symbol: str) -> dict:
        """Returns technical indicators for symbol. Cached 15 min."""
        symbol = symbol.upper().strip()

        # Check cache
        if symbol in _cache:
            age = (datetime.now(timezone.utc) - _cache_ts[symbol]).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return _cache[symbol]

        try:
            result = await self._fetch_and_calculate(symbol)
        except Exception as e:
            logger.warning(f"TechnicalService [{symbol}]: failed — {e}")
            result = self._neutral(symbol)

        _cache[symbol] = result
        _cache_ts[symbol] = datetime.now(timezone.utc)
        return result

    async def _fetch_and_calculate(self, symbol: str) -> dict:
        """Fetch 6 months of daily OHLCV from Yahoo Finance and calculate indicators."""
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={
                    "interval": "1d",
                    "range":    "6mo",
                    "includePrePost": "false",
                },
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if not res.is_success:
                raise ValueError(f"Yahoo Finance returned {res.status_code}")

            data   = res.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                raise ValueError("Empty chart result")

            chart     = result[0]
            meta      = chart.get("meta", {})
            quotes    = chart.get("indicators", {}).get("quote", [{}])[0]
            timestamps = chart.get("timestamp", [])

            closes  = [c for c in (quotes.get("close")  or []) if c is not None]
            volumes = [v for v in (quotes.get("volume") or []) if v is not None]
            highs   = [h for h in (quotes.get("high")   or []) if h is not None]
            lows    = [l for l in (quotes.get("low")    or []) if l is not None]

            if len(closes) < 26:
                raise ValueError(f"Not enough data: {len(closes)} candles")

            # ── Indicators ────────────────────────────────────────────────────
            rsi_val     = self._rsi(closes, 14)
            macd_data   = self._macd(closes)
            sma20       = self._sma(closes, 20)
            sma50       = self._sma(closes, 50) if len(closes) >= 50 else None
            sma200      = self._sma(closes, 200) if len(closes) >= 200 else None
            current     = closes[-1]
            mom5        = self._momentum(closes, 5)
            mom20       = self._momentum(closes, 20)
            vol_spike   = self._volume_spike(volumes)

            # RSI signal
            if rsi_val >= 70:
                rsi_signal = "OVERBOUGHT"
            elif rsi_val <= 30:
                rsi_signal = "OVERSOLD"
            elif rsi_val >= 55:
                rsi_signal = "BULLISH"
            elif rsi_val <= 45:
                rsi_signal = "BEARISH"
            else:
                rsi_signal = "NEUTRAL"

            # Trend
            above_sma20  = current > sma20 if sma20 else False
            above_sma50  = current > sma50 if sma50 else False
            bull_signals = sum([above_sma20, above_sma50, mom5 > 0, macd_data["histogram"] > 0])
            if bull_signals >= 3:
                trend = "BULLISH"
            elif bull_signals <= 1:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"

            return {
                "symbol":          symbol,
                "rsi":             round(rsi_val, 1),
                "rsi_signal":      rsi_signal,
                "macd":            round(macd_data["macd"], 4),
                "macd_signal":     round(macd_data["signal"], 4),
                "macd_histogram":  round(macd_data["histogram"], 4),
                "macd_crossover":  macd_data["crossover"],
                "sma_20":          round(sma20, 2) if sma20 else None,
                "sma_50":          round(sma50, 2) if sma50 else None,
                "sma_200":         round(sma200, 2) if sma200 else None,
                "above_sma20":     above_sma20,
                "above_sma50":     above_sma50,
                "momentum_5d":     round(mom5, 2),
                "momentum_20d":    round(mom20, 2),
                "trend":           trend,
                "volume_spike":    vol_spike,
                "current_price":   round(current, 2),
                "data_points":     len(closes),
            }

    # ── Indicator calculations ─────────────────────────────────────────────────

    def _rsi(self, closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        # Initial averages
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Smoothed (Wilder's)
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _ema(self, values: list[float], period: int) -> list[float]:
        if len(values) < period:
            return [values[-1]] if values else [0]
        k      = 2 / (period + 1)
        ema    = [sum(values[:period]) / period]
        for v in values[period:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def _macd(self, closes: list[float]) -> dict:
        if len(closes) < 26:
            return {"macd": 0, "signal": 0, "histogram": 0, "crossover": "NONE"}

        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)

        # Align lengths
        diff  = len(ema12) - len(ema26)
        ema12 = ema12[diff:] if diff > 0 else ema12
        ema26 = ema26[-diff:] if diff < 0 else ema26

        macd_line = [m - e for m, e in zip(ema12, ema26)]

        if len(macd_line) < 9:
            return {"macd": macd_line[-1], "signal": 0, "histogram": macd_line[-1], "crossover": "NONE"}

        signal_line = self._ema(macd_line, 9)
        histogram   = macd_line[-1] - signal_line[-1]

        # Crossover detection (last 2 bars)
        crossover = "NONE"
        if len(macd_line) >= 2 and len(signal_line) >= 2:
            prev_diff = macd_line[-2] - signal_line[-2]
            curr_diff = macd_line[-1] - signal_line[-1]
            if prev_diff < 0 and curr_diff >= 0:
                crossover = "BULLISH"
            elif prev_diff > 0 and curr_diff <= 0:
                crossover = "BEARISH"

        return {
            "macd":       macd_line[-1],
            "signal":     signal_line[-1],
            "histogram":  histogram,
            "crossover":  crossover,
        }

    def _sma(self, closes: list[float], period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    def _momentum(self, closes: list[float], period: int) -> float:
        if len(closes) <= period:
            return 0.0
        old = closes[-(period + 1)]
        if old == 0:
            return 0.0
        return (closes[-1] - old) / old * 100

    def _volume_spike(self, volumes: list[float]) -> dict:
        if len(volumes) < 21:
            return {"ratio": 1.0, "is_spike": False, "label": "Normal"}
        avg20  = sum(volumes[-21:-1]) / 20
        today  = volumes[-1]
        if avg20 == 0:
            return {"ratio": 1.0, "is_spike": False, "label": "Normal"}
        ratio  = today / avg20
        if ratio >= 3.0:
            label = "Extreme spike"
        elif ratio >= 2.0:
            label = "High volume"
        elif ratio >= 1.5:
            label = "Above average"
        elif ratio <= 0.5:
            label = "Very low volume"
        else:
            label = "Normal"
        return {
            "ratio":    round(ratio, 2),
            "is_spike": ratio >= 2.0,
            "label":    label,
        }

    def _neutral(self, symbol: str) -> dict:
        """Fallback when data fetch fails — clearly marked as unavailable."""
        return {
            "symbol":         symbol,
            "rsi":            None,
            "rsi_signal":     "UNAVAILABLE",
            "macd":           None,
            "macd_signal":    None,
            "macd_histogram": None,
            "macd_crossover": "NONE",
            "sma_20":         None,
            "sma_50":         None,
            "sma_200":        None,
            "above_sma20":    False,
            "above_sma50":    False,
            "momentum_5d":    None,
            "momentum_20d":   None,
            "trend":          "UNAVAILABLE",
            "volume_spike":   {"ratio": 1.0, "is_spike": False, "label": "Unavailable"},
            "current_price":  None,
            "data_points":    0,
        }