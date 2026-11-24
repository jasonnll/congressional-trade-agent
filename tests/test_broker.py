from __future__ import annotations

from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from src import broker, config
from tests.conftest import FakeBroker


def test_buy_respects_exposure_cap():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    res = broker.execute_buy_order("NVDA", "uid-1", open_exposure=config.MAX_PORTFOLIO_EXPOSURE - 50)
    assert not res.ok and res.retryable and "exposure" in res.reason
    assert fb.orders == []


def test_buy_requires_cash_not_margin():
    fb = FakeBroker(cash=40)
    broker.set_trading_client(fb)
    res = broker.execute_buy_order("NVDA", "uid-1", open_exposure=0)
    assert not res.ok and "cash" in res.reason and fb.orders == []


def test_buy_skips_non_fractionable():
    fb = FakeBroker(fractionable=False)
    broker.set_trading_client(fb)
    res = broker.execute_buy_order("BRK.A", "uid-1", open_exposure=0)
    assert not res.ok and not res.retryable


def test_buy_submits_capped_notional_with_idempotent_id():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    res = broker.execute_buy_order("NVDA", "uid-1", open_exposure=0)
    assert res.ok and res.notional == config.BASE_NOTIONAL
    order = fb.orders[0]
    assert order.notional == config.BASE_NOTIONAL
    assert order.client_order_id == broker.client_order_id("uid-1", "buy")
    assert broker.client_order_id("uid-1", "buy") == broker.client_order_id("uid-1", "buy")


def test_order_above_per_trade_cap_is_refused():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    res = broker.execute_buy_order("NVDA", "uid-1", open_exposure=0, notional=config.MAX_NOTIONAL_PER_TRADE + 1)
    assert not res.ok and not res.retryable and fb.orders == []
    ok = broker.execute_buy_order("NVDA", "uid-2", open_exposure=0, notional=config.MAX_NOTIONAL_PER_TRADE)
    assert ok.ok and fb.orders[0].notional == config.MAX_NOTIONAL_PER_TRADE


def test_sell_missing_position_is_graceful():
    broker.set_trading_client(FakeBroker())
    res = broker.execute_sell_order("NVDA", None, "uid-1")
    assert res.ok and res.order_id == "MANUAL_CLOSE"


def test_partial_sell_when_quantity_given():
    fb = FakeBroker()
    fb.positions["NVDA"] = 1.0
    broker.set_trading_client(fb)
    res = broker.execute_sell_order("NVDA", 0.25, "uid-1")
    assert res.ok and fb.orders[0].qty == 0.25 and fb.closed == []


def test_retry_on_429_then_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            err = APIError('{"message": "rate limited"}', http_error=SimpleNamespace(response=SimpleNamespace(status_code=429)))
            raise err
        return "ok"

    assert broker.with_retry(flaky, "test") == "ok" and calls["n"] == 3


def test_missing_credentials(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    with pytest.raises(broker.BrokerUnavailable):
        broker.is_market_open()
