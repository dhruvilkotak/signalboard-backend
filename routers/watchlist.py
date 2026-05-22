"""
routers/watchlist.py
Persists user watchlist to a JSON file on the VM.
No database needed — simple, free, works forever.

GET  /api/watchlist/         → get current watchlist
POST /api/watchlist/add      → add a symbol
POST /api/watchlist/remove   → remove a symbol
POST /api/watchlist/reset    → reset to defaults
GET  /api/watchlist/search   → live search via Yahoo Finance autocomplete
"""
import os, json, logging, httpx
from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "..", "watchlist.json")

DEFAULT_WATCHLIST = [
    "SPY", "VOO", "JEPI", "JEPQ", "SCHD", "SGOV",
    "MSFT", "AAPL", "NVDA", "GOOGL", "AMZN", "META", "HOOD"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

# Yahoo Finance quoteType → readable label
QUOTE_TYPE_MAP = {
    "EQUITY":         "STOCK",
    "ETF":            "ETF",
    "MUTUALFUND":     "FUND",
    "CRYPTOCURRENCY": "CRYPTO",
    "CURRENCY":       "FOREX",
    "INDEX":          "INDEX",
    "FUTURE":         "FUTURES",
}

# ── Persistence helpers ───────────────────────────────────────────────────────

def _load() -> list:
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                return data.get("symbols", DEFAULT_WATCHLIST)
    except Exception as e:
        logger.error(f"Failed to load watchlist: {e}")
    return DEFAULT_WATCHLIST.copy()


def _save(symbols: list):
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({
                "symbols":    symbols,
                "count":      len(symbols),
                "updated_at": datetime.utcnow().isoformat(),
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save watchlist: {e}")


# ── Request model ─────────────────────────────────────────────────────────────

class SymbolRequest(BaseModel):
    symbol: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/")
def get_watchlist():
    symbols = _load()
    return {"symbols": symbols, "count": len(symbols)}


@router.post("/add")
def add_symbol(req: SymbolRequest):
    symbol = req.symbol.upper().strip()
    symbols = _load()
    if symbol not in symbols:
        symbols.append(symbol)
        _save(symbols)
        logger.info(f"Watchlist: added {symbol}")
    return {"symbols": symbols, "count": len(symbols)}


@router.post("/remove")
def remove_symbol(req: SymbolRequest):
    symbol = req.symbol.upper().strip()
    symbols = _load()
    if symbol in symbols:
        symbols.remove(symbol)
        _save(symbols)
        logger.info(f"Watchlist: removed {symbol}")
    return {"symbols": symbols, "count": len(symbols)}


@router.post("/reset")
def reset_watchlist():
    _save(DEFAULT_WATCHLIST.copy())
    return {"symbols": DEFAULT_WATCHLIST, "count": len(DEFAULT_WATCHLIST)}


@router.get("/search")
async def search_symbols(q: str = ""):
    """
    Live stock/ETF/crypto search powered by Yahoo Finance autocomplete.
    No API key needed. Covers:
    - US + global stocks
    - ETFs worldwide
    - Crypto (BTC-USD, ETH-USD, etc.)
    - Forex pairs (EURUSD=X)
    - Commodities / Futures (GC=F, CL=F)
    - Indices (^GSPC, ^DJI)
    """
    if not q or len(q.strip()) < 1:
        return {"results": [], "source": "yahoo_finance"}

    try:
        async with httpx.AsyncClient(timeout=8, headers=HEADERS) as client:
            res = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={
                    "q":                q,
                    "lang":             "en-US",
                    "region":           "US",
                    "quotesCount":      15,
                    "newsCount":        0,
                    "enableFuzzyQuery": False,
                    "enableCb":         False,
                },
            )
            data = res.json()

        quotes  = data.get("quotes", [])
        results = []

        for quote in quotes:
            raw_type = quote.get("quoteType", "EQUITY")

            # Skip options and warrants
            if raw_type in ("OPTION", "WARRANT"):
                continue

            symbol   = quote.get("symbol", "")
            name     = quote.get("longname") or quote.get("shortname") or symbol
            q_type   = QUOTE_TYPE_MAP.get(raw_type, raw_type)
            exchange = quote.get("exchange", "")
            sector   = quote.get("sector") or quote.get("industry") or exchange or q_type

            results.append({
                "symbol":   symbol,
                "name":     name,
                "type":     q_type,
                "exchange": exchange,
                "sector":   sector,
            })

        return {
            "results": results[:12],
            "source":  "yahoo_finance",
            "query":   q,
        }

    except Exception as e:
        logger.error(f"Yahoo Finance search failed for '{q}': {e}")
        return {"results": [], "source": "error", "error": str(e)}