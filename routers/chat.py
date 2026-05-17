"""routers/chat.py — AI chat for any stock question"""
import os
from fastapi import APIRouter
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from services.price_service import PriceService
from services.news_service import NewsService

router = APIRouter()
_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_price_svc = PriceService()
_news_svc = NewsService()

class ChatRequest(BaseModel):
    question: str
    symbol: str | None = None   # optional context symbol

@router.post("/")
async def chat(req: ChatRequest):
    context = ""
    if req.symbol:
        symbol = req.symbol.upper()
        try:
            price_data = await _price_svc.get_one(symbol)
            news = await _news_svc.get_for_symbol(symbol)
            headlines = [a["headline"] for a in news[:5]]
            context = f"""
Current data for {symbol}:
- Price: ${price_data['price']} ({price_data['change_pct']:+.2f}% today)
- High: ${price_data['high']} | Low: ${price_data['low']}
- Volume: {price_data['volume']:,}

Recent headlines:
{chr(10).join(f'- {h}' for h in headlines)}
"""
        except Exception:
            pass

    system = """You are a concise stock analysis assistant. 
Answer questions about stocks clearly and factually.
Always caveat that this is not financial advice.
Keep responses under 150 words unless more detail is specifically requested."""

    response = await _client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_tokens=400,
        system=system,
        messages=[{
            "role": "user",
            "content": f"{context}\n\nQuestion: {req.question}"
        }]
    )
    return {
        "answer": response.content[0].text,
        "symbol": req.symbol,
        "question": req.question,
    }
