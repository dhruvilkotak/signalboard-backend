"""
middleware/rate_limit.py

Layer 2 rate limiting — per-user (uid-based), VPN-proof.
Works alongside Nginx Layer 1 (IP-based).

Limits (per design doc §10.1):
  /api/signals/analyze  — 5 requests/min per uid
  /api/chat             — 10 requests/min per uid
  /api/watchlist writes — 30 requests/min per uid
  default               — 60 requests/min per uid

Usage:
  from middleware.rate_limit import RateLimiter
  limiter = RateLimiter()

  @router.post("/analyze")
  async def analyze(user=Depends(get_current_user)):
      await limiter.check(user["uid"], "analyze")
      ...
"""

import time
import logging
from collections import defaultdict
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Limits: requests per minute per uid
RATE_LIMITS = {
    "analyze":   5,
    "chat":      10,
    "watchlist": 30,
    "default":   60,
}


class RateLimiter:
    """
    Sliding window rate limiter stored in memory.
    Keyed by (uid, bucket) — resets each minute.
    Lightweight — no Redis needed for single-instance GCP VM.
    """

    def __init__(self):
        # {(uid, bucket): [timestamp, ...]}
        self._windows: dict = defaultdict(list)

    async def check(self, uid: str, bucket: str = "default") -> None:
        """
        Raise HTTP 429 if uid has exceeded the rate limit for bucket.
        Call this at the top of any rate-limited endpoint.
        """
        limit    = RATE_LIMITS.get(bucket, RATE_LIMITS["default"])
        key      = (uid, bucket)
        now      = time.time()
        window   = 60.0  # 1-minute sliding window

        # Prune timestamps older than 1 minute
        self._windows[key] = [t for t in self._windows[key] if now - t < window]

        if len(self._windows[key]) >= limit:
            logger.warning(f"RateLimiter: {uid[:8]}… exceeded {bucket} limit ({limit}/min)")
            raise HTTPException(
                status_code=429,
                detail={
                    "error":       "rate_limit_exceeded",
                    "bucket":      bucket,
                    "limit":       limit,
                    "window":      "60s",
                    "retry_after": int(window - (now - self._windows[key][0])) + 1,
                },
            )

        self._windows[key].append(now)

    def get_usage(self, uid: str, bucket: str = "default") -> dict:
        """Return current usage stats for a uid/bucket — used by /api/metrics."""
        limit  = RATE_LIMITS.get(bucket, RATE_LIMITS["default"])
        key    = (uid, bucket)
        now    = time.time()
        recent = [t for t in self._windows.get(key, []) if now - t < 60.0]
        return {"bucket": bucket, "used": len(recent), "limit": limit}

    def clear_expired(self):
        """Periodic cleanup — called by scheduler every 5 min."""
        now     = time.time()
        to_del  = [k for k, ts in self._windows.items() if not any(now - t < 60 for t in ts)]
        for k in to_del:
            del self._windows[k]
        if to_del:
            logger.debug(f"RateLimiter: cleared {len(to_del)} expired windows")


# Global singleton — imported by routers
limiter = RateLimiter()