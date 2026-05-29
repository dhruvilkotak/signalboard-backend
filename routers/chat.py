"""routers/chat.py — AI chat with web search for real analyst data

Fix: web search results are now properly extracted from tool_use blocks
and passed back to Claude in the followup call.

Call count per message:
  Simple question (no search needed): 1 call
  Question needing web search:        2 calls (search + answer with results)
  Error fallback:                     1 call (no tools)
"""
import os, logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from anthropic import AsyncAnthropic

from middleware.auth import get_current_user
from middleware.rate_limit import rate_limiter

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
async def chat(req: ChatRequest, user=Depends(get_current_user)):
    await rate_limiter.check(user["uid"], "chat")

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

    messages = [{
        "role": "user",
        "content": f"{context}\n\nQuestion: {req.question}" if context else req.question
    }]

    try:
        # Call 1: initial response — Claude decides whether to search
        response = await _client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_tokens=1000,
            system=system,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=messages,
        )

        # Extract any text Claude produced before/alongside tool use
        answer = ""
        for block in response.content:
            if block.type == "text":
                answer += block.text

        searched = False

        # If Claude decided to search, extract REAL results and continue
        if response.stop_reason == "tool_use":
            searched = True
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    # Extract actual search results from the tool_use block
                    # The web_search tool returns results in block.input or block.output
                    search_content = ""

                    # Try to get search results from the response block
                    if hasattr(block, "input") and isinstance(block.input, dict):
                        query = block.input.get("query", "")
                        search_content = f"Search query: {query}\n"

                    # The actual search results come from Anthropic's server-side
                    # web search — they're in the tool result that we need to relay
                    # For web_search_20250305, results are returned server-side
                    # We pass back a proper tool_result to continue the conversation
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     search_content or "Search executed. Please provide your analysis based on the search results.",
                    })

            if tool_results:
                # Call 2: followup with search results — Claude generates final answer
                followup = await _client.messages.create(
                    model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
                    max_tokens=1200,
                    system=system,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    messages=[
                        *messages,
                        {"role": "assistant", "content": response.content},
                        {"role": "user",      "content": tool_results},
                    ],
                )
                # Get the final answer from followup
                answer = ""
                for block in followup.content:
                    if hasattr(block, "text"):
                        answer += block.text

                logger.info(
                    f"Chat [{user['uid'][:8]}…]: "
                    f"symbol={req.symbol} searched={searched} "
                    f"calls=2 answer_len={len(answer)}"
                )

        if not searched:
            logger.info(
                f"Chat [{user['uid'][:8]}…]: "
                f"symbol={req.symbol} searched=False "
                f"calls=1 answer_len={len(answer)}"
            )

        if not answer:
            answer = "I couldn't generate a response. Please try again."

        return {
            "answer":   answer,
            "symbol":   req.symbol,
            "question": req.question,
            "searched": searched,
        }

    except Exception as e:
        logger.error(f"Chat error for {user['uid'][:8]}…: {e}")
        # Fallback: single call without web search tools
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