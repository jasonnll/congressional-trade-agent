"""
src/broker.py
Alpaca wrapper: market clock, guarded fractional buys, position exits, fill reconciliation.

Every guardrail here is checked in code before an order is sent; none depend on the LLM.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TypeVar

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from src import config

logger = logging.getLogger(__name__)

T = TypeVar("T")
_sleep: Callable[[float], None] = time.sleep
_client: Optional[TradingClient] = None


class BrokerUnavailable(Exception):
    """Credentials missing or the broker cannot be reached."""


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str] = None
    notional: float = 0.0
    filled_qty: float = 0.0
    reason: str = ""
    retryable: bool = False


def get_trading_client() -> TradingClient:
    global _client
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        raise BrokerUnavailable("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set")
    if _client is None:
        _client = TradingClient(api_key=config.ALPACA_API_KEY, secret_key=config.ALPACA_SECRET_KEY,
                                paper=config.ALPACA_PAPER)
    return _client


def set_trading_client(client: Any) -> None:
    """Test hook."""
    global _client
    _client = client


def _status(e: APIError) -> Optional[int]:
    try:
        return e.status_code
    except Exception:
        return None


def with_retry(fn: Callable[[], T], what: str) -> T:
    """Retries 429/5xx with exponential backoff; re-raises anything else (including 404) immediately."""
    for attempt in range(config.HTTP_MAX_RETRIES):
        try:
            return fn()
        except APIError as e:
            code = _status(e)
            if code == 429 or (code is not None and code >= 500):
                logger.warning("Alpaca %s returned %s, retrying (attempt %d)", what, code, attempt + 1)
                _sleep(2 ** attempt)
                continue
            raise
    return fn()


def client_order_id(trade_uid: str, side: str) -> str:
    return f"cta-{side}-{hashlib.sha1(trade_uid.encode()).hexdigest()[:24]}"


def describe_error(e: Exception) -> str:
    """Short, key-free description; Alpaca returns an nginx HTML page for auth failures."""
    code = _status(e) if isinstance(e, APIError) else None
    if code in (401, 403):
        mode = "paper" if config.ALPACA_PAPER else "live"
        return (f"Alpaca rejected the API keys (HTTP {code}). Check ALPACA_API_KEY / ALPACA_SECRET_KEY are "
                f"{mode}-trading keys{' (they start with PK)' if config.ALPACA_PAPER else ''}, pasted without quotes or spaces")
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return f"Alpaca error{f' {code}' if code else ''}: {text[:160]}"


def is_market_open() -> bool:
    """True/False from Alpaca's clock. Raises BrokerUnavailable when the clock can't be read, so a bad key or
    outage is never mistaken for a closed market."""
    try:
        return bool(with_retry(lambda: get_trading_client().get_clock(), "clock").is_open)
    except BrokerUnavailable:
        raise
    except Exception as e:
        msg = describe_error(e)
        logger.error("Failed to fetch market clock: %s", msg)
        raise BrokerUnavailable(msg) from e


def get_account_snapshot() -> Dict[str, Any]:
    acct = with_retry(lambda: get_trading_client().get_account(), "account")
    return {
        "equity": float(acct.equity or 0),
        "cash": float(acct.cash or 0),
        "non_marginable_buying_power": float(acct.non_marginable_buying_power or 0),
        "paper": config.ALPACA_PAPER,
    }


def execute_buy_order(ticker: str, trade_uid: str, open_exposure: float,
                      notional: float = config.BASE_NOTIONAL) -> OrderResult:
    if notional <= 0 or notional > config.MAX_NOTIONAL_PER_TRADE:
        return OrderResult(False, reason=f"order size ${notional:.2f} outside the "
                                         f"${config.MAX_NOTIONAL_PER_TRADE:.2f} per-trade cap")
    if open_exposure + notional > config.MAX_PORTFOLIO_EXPOSURE:
        return OrderResult(False, reason=f"exposure cap: ${open_exposure:.2f} open + ${notional:.2f} "
                                         f"> ${config.MAX_PORTFOLIO_EXPOSURE:.2f}", retryable=True)
    client = get_trading_client()
    try:
        acct = get_account_snapshot()
        # Cash-only: size against non-marginable buying power, never margin.
        cash_power = min(acct["cash"], acct["non_marginable_buying_power"])
        if cash_power < notional:
            return OrderResult(False, reason=f"insufficient cash (${cash_power:.2f})", retryable=True)

        asset = with_retry(lambda: client.get_asset(ticker), "asset")
        if not asset.tradable or not asset.fractionable:
            return OrderResult(False, reason=f"{ticker} not tradable/fractionable on Alpaca")

        req = MarketOrderRequest(symbol=ticker, notional=notional, side=OrderSide.BUY,
                                 time_in_force=TimeInForce.DAY,
                                 client_order_id=client_order_id(trade_uid, "buy"))
        order = with_retry(lambda: client.submit_order(order_data=req), "submit buy")
        logger.info("Market BUY submitted for %s ($%.2f). Order ID: %s", ticker, notional, order.id)
        return OrderResult(True, order_id=str(order.id), notional=notional, filled_qty=float(order.filled_qty or 0))
    except APIError as e:
        code = _status(e)
        if code == 404:
            logger.warning("Asset %s not found on Alpaca", ticker)
            return OrderResult(False, reason=f"{ticker} not found on Alpaca")
        if code == 422 and "client_order_id" in str(e).lower():
            return OrderResult(False, reason="duplicate client_order_id (already submitted)")
        logger.error("Alpaca API error executing BUY for %s: %s", ticker, e)
        return OrderResult(False, reason=f"Alpaca error {code}: {e}", retryable=True)


def get_filled_qty(order_id: str) -> float:
    order = with_retry(lambda: get_trading_client().get_order_by_id(order_id), "order lookup")
    return float(order.filled_qty or 0)


def get_order_fill(order_id: str) -> Optional[Dict[str, Any]]:
    """Average fill price, quantity and time, or None while the order has not filled."""
    order = with_retry(lambda: get_trading_client().get_order_by_id(order_id), "order lookup")
    qty = float(order.filled_qty or 0)
    price = float(order.filled_avg_price or 0)
    if qty <= 0 or price <= 0:
        return None
    filled_at = getattr(order, "filled_at", None)
    return {"price": price, "qty": qty, "filled_at": str(filled_at)[:19] if filled_at else None}


def execute_sell_order(ticker: str, qty: Optional[float], trade_uid: str) -> OrderResult:
    """Sells `qty` shares (capped at what is held), or closes the whole position when qty is None."""
    client = get_trading_client()
    try:
        position = with_retry(lambda: client.get_open_position(ticker), "position lookup")
        held = float(position.qty or 0)
        if qty is None or qty >= held:
            order = with_retry(lambda: client.close_position(ticker), "close position")
        else:
            req = MarketOrderRequest(symbol=ticker, qty=round(qty, 9), side=OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY,
                                     client_order_id=client_order_id(trade_uid, "sell"))
            order = with_retry(lambda: client.submit_order(order_data=req), "submit sell")
        logger.info("SELL submitted for %s. Order ID: %s", ticker, order.id)
        return OrderResult(True, order_id=str(order.id))
    except APIError as e:
        code = _status(e)
        if code == 404 or "position does not exist" in str(e).lower():
            logger.warning("No broker position for %s (closed manually?)", ticker)
            return OrderResult(True, order_id="MANUAL_CLOSE", reason="no broker position")
        logger.error("Alpaca API error selling %s: %s", ticker, e)
        return OrderResult(False, reason=f"Alpaca error {code}: {e}", retryable=True)
