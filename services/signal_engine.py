"""
services/signal_engine.py

Shared signal generation engine used by BOTH:
  - signal_service.py      (scheduler, auto-trader, feed)
  - ondemand_signal_service.py (Live Prices Signal tab)

Data sources:
  1. Yahoo Finance price
  2. TechnicalService (real RSI/MACD/SMA/volume)
  3. NewsService (recent headlines)
  4. SEC EDGAR Form 4 (insider transactions: type, shares, price, value, role)
  5. StockTwits (retail sentiment)

Feed eligibility: signal in (BUY,SELL) AND confidence == HIGH AND trigger != fallback
"""

import json
import logging
import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from anthropic import AsyncAnthropic
from config import settings

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(10)


class SignalEngine:
    def __init__(self, price_service, news_service, technical_service):
        self.price_svc = price_service
        self.news_svc  = news_service
        self.tech_svc  = technical_service
        self.client    = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def generate(self, symbol: str, session: str = "market", trigger: str = "scheduled") -> dict:
        symbol = symbol.upper().strip()
        async with _semaphore:
            price_data, news_items, tech_data, insider_trades, sentiment = await asyncio.gather(
                self._fetch_price(symbol),
                self._fetch_news(symbol),
                self.tech_svc.get_technicals(symbol),
                self._fetch_insider_trades(symbol),
                self._fetch_stocktwits(symbol),
                return_exceptions=True,
            )

            if isinstance(price_data,     Exception): price_data     = {}
            if isinstance(news_items,     Exception): news_items     = []
            if isinstance(tech_data,      Exception): tech_data      = {}
            if isinstance(insider_trades, Exception): insider_trades = []
            if isinstance(sentiment,      Exception): sentiment      = {}

            if not price_data or not price_data.get("price"):
                logger.warning(f"SignalEngine [{symbol}]: no price data — fallback")
                return self._fallback(symbol, session, trigger)

            headlines = [a["headline"] for a in (news_items or [])[:6]]

            signal = await self._call_claude(
                symbol, price_data, tech_data, headlines, insider_trades, sentiment, session
            )

            if signal.get("trigger") == "fallback":
                return self._fallback(symbol, session, trigger)

            now = datetime.now(timezone.utc)
            signal.update({
                "symbol":          symbol,
                "price_at_signal": price_data.get("price", 0),
                "generated_at":    now.isoformat(),
                "session":         session,
                "trigger":         trigger,
                "insider_trades":  insider_trades,
                "sentiment":       sentiment,
                "feed_eligible":   (
                    signal.get("signal") in ("BUY", "SELL") and
                    signal.get("confidence") == "HIGH" and
                    trigger != "fallback"
                ),
            })

            logger.info(
                f"SignalEngine [{symbol}]: {signal['signal']}/{signal['confidence']} "
                f"score={signal.get('conviction_score','?')} "
                f"feed={'✓' if signal['feed_eligible'] else '✗'} "
                f"trigger={trigger}"
            )
            return signal

    # ── Data fetchers ─────────────────────────────────────────────────────────

    async def _fetch_price(self, symbol: str) -> dict:
        try:
            p = await self.price_svc.get_one(symbol)
            if p and p.get("price"):
                return p
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                res = await c.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params={"interval": "1d", "range": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                meta  = res.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("chartPreviousClose") or meta.get("previousClose", 0)
                return {
                    "price":      price,
                    "prev_close": prev,
                    "change_pct": round(((price - prev) / prev * 100), 2) if prev else 0,
                    "change_amt": round(price - prev, 2) if prev else 0,
                    "volume":     meta.get("regularMarketVolume", 0),
                    "high":       meta.get("regularMarketDayHigh", price),
                    "low":        meta.get("regularMarketDayLow", price),
                    "open":       meta.get("regularMarketOpen", price),
                }
        except Exception as e:
            logger.warning(f"Price fetch failed for {symbol}: {e}")
            return {}

    async def _fetch_news(self, symbol: str) -> list:
        try:
            return await self.news_svc.get_for_symbol(symbol)
        except Exception:
            return []

    async def _fetch_insider_trades(self, symbol: str) -> list[dict]:
        """
        SEC EDGAR Form 4 — fetch filing index then parse XML for transaction details.

        Correct URL pattern:
          Search:  efts.sec.gov/LATEST/search-index?q="SYMBOL"&forms=4
          XML:     sec.gov/Archives/edgar/data/{CIK}/{accession_nodash}/{filename}

        The _id field in search results is "{accession}:{filename}" — use both parts.
        Company CIK is ciks[1] (index 1), insider CIK is ciks[0].
        """
        trades = []
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                # Step 1: search for Form 4 filings
                search_res = await c.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={
                        "q":         f'"{symbol}"',
                        "dateRange": "custom",
                        "startdt":   (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),
                        "enddt":     datetime.now().strftime("%Y-%m-%d"),
                        "forms":     "4",
                    },
                    headers={"User-Agent": "SignalBoard research@signalboard.app"},
                )
                if not search_res.is_success:
                    return []

                hits = search_res.json().get("hits", {}).get("hits", [])[:8]

                async def _parse_filing(hit: dict) -> Optional[dict]:
                    try:
                        src          = hit.get("_source", {})
                        hit_id       = hit.get("_id", "")        # e.g. "0001140361-26-020871:form4.xml"
                        file_date    = src.get("file_date", "")
                        display_names = src.get("display_names", [])
                        ciks         = src.get("ciks", [])

                        # Insider is ciks[0], company is ciks[1]
                        insider_cik  = ciks[0] if ciks else ""
                        company_cik  = ciks[1] if len(ciks) > 1 else (ciks[0] if ciks else "")

                        # Insider name — first display_name that matches insider_cik
                        insider_name = "Unknown"
                        for dn in display_names:
                            if insider_cik and insider_cik.lstrip("0") in dn:
                                insider_name = dn.split("(CIK")[0].strip()
                                break
                        if insider_name == "Unknown" and display_names:
                            insider_name = display_names[0].split("(CIK")[0].strip()

                        # Build XML URL from _id: "{accession}:{filename}"
                        if ":" in hit_id:
                            accession_raw, filename = hit_id.split(":", 1)
                        else:
                            return None

                        # Remove dashes from accession for URL path
                        accession_nodash = accession_raw.replace("-", "")
                        # Strip leading zeros from company CIK for URL
                        company_cik_int  = str(int(company_cik)) if company_cik else ""

                        xml_url = (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{company_cik_int}/{accession_nodash}/{filename}"
                        )

                        xml_res = await c.get(
                            xml_url,
                            headers={"User-Agent": "SignalBoard research@signalboard.app"},
                            follow_redirects=True,
                        )

                        if not xml_res.is_success:
                            # Return metadata-only entry (no transaction details)
                            return {
                                "name":        insider_name,
                                "role":        "Insider",
                                "date":        file_date,
                                "type":        "Unknown",
                                "type_code":   "U",
                                "shares":      None,
                                "price":       None,
                                "total_value": None,
                                "form":        "4",
                            }

                        xml = xml_res.text

                        def _extract(tag: str) -> Optional[str]:
                            """Extract value from nested <tag><value>X</value></tag> or <tag>X</tag>."""
                            # Try nested <value> first (standard Form 4 XML)
                            m = re.search(rf"<{tag}[^>]*>\\s*<value>([^<]+)</value>", xml)
                            if m:
                                return m.group(1).strip()
                            # Fallback: direct content
                            m = re.search(rf"<{tag}[^>]*>([^<
]+)</{tag}>", xml)
                            return m.group(1).strip() if m else None

                        # Transaction code is direct (no nested <value>)
                        trans_code = re.search(r"<transactionCode>([^<]+)</transactionCode>", xml)
                        trans_code = trans_code.group(1).strip() if trans_code else "U"

                        shares_str = _extract("transactionShares")
                        price_str  = _extract("transactionPricePerShare")
                        role       = _extract("officerTitle") or "Insider"

                        shares = float(shares_str) if shares_str else None
                        price  = float(price_str)  if price_str  else None
                        total  = round(shares * price, 2) if shares and price else None

                        # Rule 10b5-1 plan = pre-scheduled, uninformative sale
                        is_10b5 = "<aff10b5One>true</aff10b5One>" in xml or "<aff10b5Two>true</aff10b5Two>" in xml

                        type_map = {
                            "P": "Purchase",
                            "S": "Sale (10b5-1)" if is_10b5 else "Sale",
                            "A": "Award",
                            "D": "Disposal",
                            "F": "Tax withholding",
                            "M": "Option exercise",
                            "G": "Gift",
                        }

                        return {
                            "name":        insider_name,
                            "role":        role.title() if role else "Insider",
                            "date":        file_date,
                            "type":        type_map.get(trans_code, f"Code {trans_code}"),
                            "type_code":   trans_code,
                            "shares":      shares,
                            "price":       price,
                            "total_value": total,
                            "is_10b5":     is_10b5,
                            "form":        "4",
                        }
                    except Exception as e:
                        logger.debug(f"Form 4 parse error: {e}")
                        return None

                parsed = await asyncio.gather(*[_parse_filing(h) for h in hits])
                trades = [t for t in parsed if t is not None]

        except Exception as e:
            logger.warning(f"SEC insider fetch failed for {symbol}: {e}")

        return trades[:6]

    async def _fetch_stocktwits(self, symbol: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                res = await c.get(
                    f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if not res.is_success:
                    return {}

                data     = res.json()
                sym_data = data.get("symbol", {})
                messages = data.get("messages", [])

                # Sentiment is at entities.sentiment.basic (not top-level)
                bull = sum(
                    1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}) and
                    m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish"
                )
                bear = sum(
                    1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}) and
                    m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish"
                )
                total    = bull + bear
                bull_pct = round(bull / total * 100) if total > 0 else 50
                bear_pct = 100 - bull_pct
                label    = "Bullish" if bull_pct >= 60 else "Bearish" if bear_pct >= 60 else "Mixed"

                return {
                    "bullish_pct":     bull_pct,
                    "bearish_pct":     bear_pct,
                    "message_volume":  len(messages),
                    "watchlist_count": sym_data.get("watchlist_count", 0),
                    "sentiment_label": label,
                }
        except Exception as e:
            logger.warning(f"StockTwits failed for {symbol}: {e}")
            return {}

    # ── Claude ────────────────────────────────────────────────────────────────

    async def _call_claude(self, symbol, price, tech, headlines, insider_trades, sentiment, session) -> dict:

        price_text = (
            f"${price.get('price', 0):.2f} "
            f"({price.get('change_pct', 0):+.2f}% today)\n"
            f"Open: ${price.get('open', 0):.2f}  "
            f"High: ${price.get('high', 0):.2f}  "
            f"Low: ${price.get('low', 0):.2f}  "
            f"Volume: {price.get('volume', 0):,.0f}"
        )

        if tech.get("rsi") is not None:
            tech_text = (
                f"RSI(14): {tech['rsi']} [{tech.get('rsi_signal','?')}]\n"
                f"MACD: {tech.get('macd_crossover','NONE')} crossover  "
                f"Histogram: {tech.get('macd_histogram', 0):.4f}\n"
                f"SMA20: ${tech.get('sma_20') or 'N/A'}  "
                f"SMA50: ${tech.get('sma_50') or 'N/A'}  "
                f"Above SMA20: {tech.get('above_sma20', False)}\n"
                f"Momentum 5d: {tech.get('momentum_5d', 0):.2f}%  "
                f"Momentum 20d: {tech.get('momentum_20d', 0):.2f}%\n"
                f"Volume: {tech.get('volume_spike', {}).get('label', 'Normal')} "
                f"({tech.get('volume_spike', {}).get('ratio', 1.0):.1f}x avg)\n"
                f"Trend: {tech.get('trend', 'NEUTRAL')}"
            )
        else:
            tech_text = "Technical data unavailable."

        news_text = (
            "\n".join(f"- {h}" for h in headlines)
            if headlines else "- No recent news available"
        )

        if insider_trades:
            insider_lines = []
            for t in insider_trades:
                shares_str = f"{t['shares']:,.0f} shares" if t.get("shares") else "? shares"
                price_str  = f"@ ${t['price']:.2f}" if t.get("price") else ""
                value_str  = f"(${t['total_value']:,.0f})" if t.get("total_value") else ""
                insider_lines.append(
                    f"- {t['name']} [{t.get('role','Insider')}]: "
                    f"{t['type']} {shares_str} {price_str} {value_str} on {t['date']}"
                )
            insider_text = "\n".join(insider_lines)
        else:
            insider_text = "No insider filings in last 90 days."

        if sentiment and sentiment.get("message_volume", 0) > 0:
            sentiment_text = (
                f"StockTwits: {sentiment.get('sentiment_label','?')} — "
                f"{sentiment.get('bullish_pct', 50)}% bullish / "
                f"{sentiment.get('bearish_pct', 50)}% bearish  "
                f"({sentiment.get('message_volume', 0)} messages, "
                f"{sentiment.get('watchlist_count', 0):,} watching)"
            )
        else:
            sentiment_text = "Retail sentiment data unavailable."

        session_instructions = {
            "pre_market":  "PRE-MARKET: Focus on overnight catalysts, futures, earnings. Be conservative — thin liquidity.",
            "market":      "MARKET HOURS: Full liquidity. HIGH confidence signals may be auto-traded.",
            "post_market": "POST-MARKET: Focus on earnings results. Signal is for NEXT DAY preparation.",
            "closed":      "MARKET CLOSED: Provide forward-looking analysis. Do NOT default to HOLD — if data supports BUY or SELL, say so.",
        }
        session_ctx = session_instructions.get(session, session_instructions["market"])

        prompt = f"""You are an expert stock analyst generating a high-quality investment signal.

SYMBOL: {symbol}
SESSION: {session_ctx}

PRICE DATA:
{price_text}

TECHNICAL INDICATORS:
{tech_text}

RECENT NEWS:
{news_text}

SEC INSIDER ACTIVITY (Form 4 — last 90 days):
{insider_text}

RETAIL SENTIMENT:
{sentiment_text}

INSTRUCTIONS:
- Synthesise ALL data sources holistically
- RSI >70 = overbought (lean SELL), RSI <30 = oversold (lean BUY)
- Insider PURCHASES are strongly bullish; SALES are mixed
- MACD bullish crossover + above SMA20 + positive momentum = strong BUY
- conviction_score 1-10: only score 7+ warrants HIGH confidence
- HIGH confidence requires 3+ indicators aligned. MEDIUM if 2 align. LOW if unclear.
- Do NOT default to HOLD when market is closed if data is directional

Respond ONLY with valid JSON, no markdown:
{{
  "signal": "BUY|HOLD|SELL",
  "confidence": "HIGH|MEDIUM|LOW",
  "conviction_score": <int 1-10>,
  "target_price": <float>,
  "stop_loss": <float>,
  "expected_return_pct": <float>,
  "timeframe": "1-2 weeks|1-3 months|3-6 months",
  "timeframe_days": <int>,
  "summary": "<2-3 sentences citing specific data>",
  "risk": "LOW|MEDIUM|HIGH",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "insider_summary": "<one sentence about insider activity>",
  "sentiment_summary": "<one sentence about retail sentiment>",
  "price_targets": {{"week1": <float>, "week2": <float>, "month1": <float>, "month3": <float>}},
  "bull_case": "<one sentence>",
  "bear_case": "<one sentence>"
}}"""

        try:
            res = await self.client.messages.create(
                model=settings.ANTHROPIC_MODEL,
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            text = res.content[0].text.strip()
            text = text.lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(text)
        except Exception as e:
            logger.error(f"Claude error for {symbol}: {e}")
            return self._fallback(symbol, session, "fallback")

    def _fallback(self, symbol: str, session: str = "market", trigger: str = "fallback") -> dict:
        now = datetime.now(timezone.utc)
        return {
            "symbol":              symbol,
            "signal":              "HOLD",
            "confidence":          "LOW",
            "conviction_score":    0,
            "target_price":        None,
            "stop_loss":           None,
            "expected_return_pct": 0,
            "timeframe":           "unknown",
            "timeframe_days":      30,
            "summary":             "Signal generation failed — data unavailable.",
            "risk":                "HIGH",
            "key_factors":         ["data_unavailable"],
            "insider_summary":     "No data.",
            "sentiment_summary":   "No data.",
            "price_targets":       {},
            "bull_case":           "Insufficient data.",
            "bear_case":           "Insufficient data.",
            "insider_trades":      [],
            "sentiment":           {},
            "generated_at":        now.isoformat(),
            "session":             session,
            "trigger":             "fallback",
            "feed_eligible":       False,
        }