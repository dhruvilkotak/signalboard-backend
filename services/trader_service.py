"""
Trader Service — auto-trades with $100 paper budget on Alpaca
Buys on BUY signals, sells on SELL signals
Maximizes return over 2-5 months
"""
import os, logging
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

logger = logging.getLogger(__name__)
BUDGET = float(os.getenv("PAPER_BUDGET", "100.0"))

class TraderService:
    def __init__(self):
        self.client = TradingClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            paper=True,   # always paper — flip to False for real money
        )
        self.budget = BUDGET
        self._trade_log: list = []

    def get_account(self) -> dict:
        try:
            acct = self.client.get_account()
            return {
                "buying_power": float(acct.buying_power),
                "portfolio_value": float(acct.portfolio_value),
                "cash": float(acct.cash),
                "pnl": float(acct.portfolio_value) - BUDGET,
                "pnl_pct": ((float(acct.portfolio_value) - BUDGET) / BUDGET) * 100,
            }
        except Exception as e:
            logger.error(f"Account fetch error: {e}")
            return {}

    def get_positions(self) -> list:
        try:
            positions = self.client.get_all_positions()
            return [{
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pnl": float(p.unrealized_pl),
                "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
            } for p in positions]
        except Exception as e:
            logger.error(f"Positions fetch error: {e}")
            return []

    def execute_signal(self, symbol: str, signal: dict, current_price: float) -> dict:
        """
        Execute a trade based on AI signal.
        BUY: invest up to 20% of remaining budget per position
        SELL: close position if we hold it
        """
        action = signal.get("signal")
        confidence = signal.get("confidence", "LOW")

        if confidence == "LOW":
            return {"status": "skipped", "reason": "Low confidence signal"}

        try:
            positions = {p["symbol"]: p for p in self.get_positions()}
            account = self.get_account()

            if action == "BUY" and symbol not in positions:
                # Invest 20% of budget per trade (max 5 positions)
                invest_amount = min(account.get("buying_power", 0), BUDGET * 0.20)
                if invest_amount < 1:
                    return {"status": "skipped", "reason": "Insufficient buying power"}

                qty = invest_amount / current_price
                if qty < 0.001:
                    return {"status": "skipped", "reason": "Quantity too small"}

                order = self.client.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    qty=round(qty, 3),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ))
                trade = {
                    "status": "executed",
                    "action": "BUY",
                    "symbol": symbol,
                    "qty": round(qty, 3),
                    "price": current_price,
                    "amount": round(invest_amount, 2),
                    "order_id": str(order.id),
                    "timestamp": datetime.utcnow().isoformat(),
                    "signal_reason": signal.get("summary", ""),
                }
                self._trade_log.append(trade)
                return trade

            elif action == "SELL" and symbol in positions:
                pos = positions[symbol]
                order = self.client.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    qty=pos["qty"],
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                ))
                trade = {
                    "status": "executed",
                    "action": "SELL",
                    "symbol": symbol,
                    "qty": pos["qty"],
                    "price": current_price,
                    "pnl": pos["unrealized_pnl"],
                    "pnl_pct": pos["unrealized_pnl_pct"],
                    "order_id": str(order.id),
                    "timestamp": datetime.utcnow().isoformat(),
                    "signal_reason": signal.get("summary", ""),
                }
                self._trade_log.append(trade)
                return trade

            return {"status": "skipped", "reason": f"No action for {action} signal"}

        except Exception as e:
            logger.error(f"Trade execution error: {e}")
            return {"status": "error", "reason": str(e)}

    def get_trade_log(self) -> list:
        return list(reversed(self._trade_log))

    def get_performance(self) -> dict:
        account = self.get_account()
        buys = [t for t in self._trade_log if t["action"] == "BUY"]
        sells = [t for t in self._trade_log if t["action"] == "SELL"]
        return {
            "budget": BUDGET,
            "current_value": account.get("portfolio_value", BUDGET),
            "pnl": account.get("pnl", 0),
            "pnl_pct": account.get("pnl_pct", 0),
            "total_trades": len(self._trade_log),
            "open_positions": len(self.get_positions()),
            "target": BUDGET * 2,   # $200 goal
            "progress_to_target_pct": (account.get("portfolio_value", BUDGET) / (BUDGET * 2)) * 100,
        }
