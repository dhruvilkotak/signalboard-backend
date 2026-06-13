"""
utils/market_hours.py

Single source of truth for market session windows.

US trading window (America/New_York):
  Pre-market  : 7:30 AM – 9:30 AM   (trading allowed, low-volume warning)
  Market open : 9:30 AM – 4:00 PM   (trading allowed, live prices)
  Post-market : 4:00 PM – 6:00 PM   (trading allowed, low-volume warning)
  Closed      : 6:00 PM – 7:30 AM   (trading blocked, prices stale)

Weekends are always CLOSED for trading.
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import HTTPException

ET = ZoneInfo("America/New_York")

_PRE_MARKET_START = (7,  30)
_MARKET_OPEN      = (9,  30)
_MARKET_CLOSE     = (16,  0)
_POST_MARKET_END  = (18,  0)

_TRADING_SESSIONS = {"pre_market", "market", "post_market"}

_SESSION_META = {
    "pre_market":  {"label": "Pre-market",  "trading_allowed": True,  "realtime_prices": True,  "price_note": "Pre-market price · low volume"},
    "market":      {"label": "Market open", "trading_allowed": True,  "realtime_prices": True,  "price_note": None},
    "post_market": {"label": "Post-market", "trading_allowed": True,  "realtime_prices": True,  "price_note": "Post-market price · low volume"},
    "closed":      {"label": "Closed",      "trading_allowed": False, "realtime_prices": False, "price_note": "Prices may be stale"},
}


def _to_mins(h: int, m: int) -> int:
    return h * 60 + m


def get_current_session() -> str:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return "closed"
    mins = _to_mins(now.hour, now.minute)
    if mins < _to_mins(*_PRE_MARKET_START): return "closed"
    if mins < _to_mins(*_MARKET_OPEN):      return "pre_market"
    if mins < _to_mins(*_MARKET_CLOSE):     return "market"
    if mins < _to_mins(*_POST_MARKET_END):  return "post_market"
    return "closed"


def is_trading_allowed() -> bool:
    return get_current_session() in _TRADING_SESSIONS


def _mins_to_next_transition(now: datetime) -> int | None:
    mins    = _to_mins(now.hour, now.minute)
    session = get_current_session()
    if session == "pre_market":  return _to_mins(*_MARKET_OPEN)    - mins
    if session == "market":      return _to_mins(*_MARKET_CLOSE)   - mins
    if session == "post_market": return _to_mins(*_POST_MARKET_END) - mins
    next_open = _to_mins(*_PRE_MARKET_START)
    weekday   = now.weekday()
    if weekday < 4:   return (24 * 60 - mins) + next_open   # Mon–Thu evening → next morning
    if weekday == 4:  return (3 * 24 * 60 - mins) + next_open  # Fri → Monday
    if weekday == 5:  return (2 * 24 * 60 - mins) + next_open  # Saturday → Monday
    return (24 * 60 - mins) + next_open                         # Sunday → Monday


def get_market_status() -> dict:
    """
    Full status dict returned by GET /api/market/status.
    Frontend polls every 60 s to drive trade panel state.
    """
    now     = datetime.now(ET)
    session = get_current_session()
    meta    = _SESSION_META[session]
    mins_to = _mins_to_next_transition(now)

    countdown = None
    if mins_to is not None:
        h, m   = divmod(mins_to, 60)
        countdown = f"{h}h {m:02d}m" if h else f"{m}m"

    return {
        "session":         session,
        "label":           meta["label"],
        "trading_allowed": meta["trading_allowed"],
        "realtime_prices": meta["realtime_prices"],
        "price_note":      meta["price_note"],
        "is_weekend":      now.weekday() >= 5,
        "mins_to_next":    mins_to,
        "countdown":       countdown,
        "server_time_et":  now.strftime("%-I:%M %p ET"),
        "server_time_iso": now.isoformat(),
    }


def assert_market_open(context: str = "Trading") -> None:
    """
    Raise HTTP 403 if trading is not currently allowed.
    Call at the top of any manual buy/sell endpoint.

    Error body:
      { "code": "MARKET_CLOSED", "session": "closed",
        "message": "...", "countdown": "11h 22m" }
    """
    status = get_market_status()
    if not status["trading_allowed"]:
        raise HTTPException(
            status_code=403,
            detail={
                "code":      "MARKET_CLOSED",
                "session":   status["session"],
                "message":   f"{context} is only available 7:30 AM – 6:00 PM ET, Mon–Fri.",
                "countdown": status["countdown"],
            },
        )