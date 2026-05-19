"""routers/chat.py — AI chat with web search for real analyst data"""
import os, logging
from fastapi import APIRouter
from pydantic import BaseModel
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
router = APIRouter()

# Injected from main.py
price_svc = None
news_svc  = None

_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

class ChatRequest(BaseModel):
    question: str
    symbol: str | None = None

@router.post("/")
async def chat(req: ChatRequest):
    # Build price + news context if symbol provided
    context = ""
    if req.symbol and price_svc and news_svc:
        symbol = req.symbol.upper()
        try:
            price_data = await price_svc.get_one(symbol)
            news       = await news_svc.get_for_symbol(symbol)
            headlines  = [a["headline"] for a in news[:5]]
            context = f"""
Current market data for {symbol}:
- Price: ${price_data.get('price', 'N/A')} ({price_data.get('change_pct', 0):+.2f}% today)
- High: ${price_data.get('high', 'N/A')} | Low: ${price_data.get('low', 'N/A')}
- Volume: {price_data.get('volume', 0):,}
- Prev close: ${price_data.get('prev_close', 'N/A')}

Recent news headlines:
{chr(10).join(f'- {h}' for h in headlines) if headlines else '- No recent headlines'}
"""
        except Exception as e:
            logger.warning(f"Context fetch failed for {symbol}: {e}")

    system = """You are an expert stock analyst assistant for Signal Board — an AI trading dashboard.

Your role:
- Answer questions about stocks, ETFs, market conditions, and trading signals
- Search the web when asked about current prices, analyst targets, earnings, or recent news
- Provide specific, data-backed answers with numbers when possible
- Include analyst consensus, price targets, and key metrics when relevant
- Always note this is not financial advice

Style:
- Be concise but thorough
- Use markdown formatting (bold for key numbers, bullet points for lists)
- Include a brief disclaimer at the end
- Never refuse to discuss stocks or give analysis — that's your job"""

    # Build messages with web search tool
    messages = [{
        "role": "user",
        "content": f"{context}\n\nQuestion: {req.question}" if context else req.question
    }]

    try:
        # Use web search for up-to-date analyst data
        response = await _client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_tokens=1000,
            system=system,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
            }],
            messages=messages,
        )

        # Extract text from response (may have tool_use blocks)
        answer = ""
        for block in response.content:
            if block.type == "text":
                answer += block.text

        # If web search was used, get the final answer
        if response.stop_reason == "tool_use":
            # Continue conversation to get final answer after search
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Search completed"
                    })

            if tool_results:
                followup = await _client.messages.create(
                    model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
                    max_tokens=1000,
                    system=system,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    messages=[
                        *messages,
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": tool_results},
                    ],
                )
                answer = ""
                for block in followup.content:
                    if hasattr(block, "text"):
                        answer += block.text

        if not answer:
            answer = "I couldn't generate a response. Please try again."

        return {
            "answer":   answer,
            "symbol":   req.symbol,
            "question": req.question,
            "searched": response.stop_reason == "tool_use",
        }

    except Exception as e:
        logger.error(f"Chat error: {e}")
        # Fallback without web search
        try:
            fallback = await _client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
                max_tokens=800,
                system=system,
                messages=messages,
            )
            return {
                "answer":   fallback.content[0].text,
                "symbol":   req.symbol,
                "question": req.question,
                "searched": False,
            }
        except Exception as e2:
            return {
                "answer":   f"Error: {str(e2)}. Please check your API key.",
                "symbol":   req.symbol,
                "question": req.question,
                "searched": False,
            }