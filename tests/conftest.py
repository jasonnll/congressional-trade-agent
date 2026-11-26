from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src import ai_analyst, broker, config, database, ingestion, performance


class FakePrices:
    """Daily closes keyed by symbol; anything not configured is flat at 100 over the last 60 days."""

    def __init__(self, series=None):
        self.series = series or {}
        self.requests = []

    def daily_closes(self, symbols, start):
        from datetime import date, timedelta
        self.requests.append((list(symbols), start))
        flat = [((date.today() - timedelta(days=i)).isoformat(), 100.0) for i in range(60, -1, -1)]
        return {s: self.series.get(s, flat) for s in symbols}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    performance.set_price_source(FakePrices())
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(config, "JOURNAL_PATH", str(tmp_path / "trade_journal.md"))
    monkeypatch.setattr(config, "COMMITTEES_PATH", str(tmp_path / "committees.json"))
    monkeypatch.setattr(config, "ALPACA_API_KEY", "test-key")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setattr(ingestion, "_sleep", lambda s: None)
    monkeypatch.setattr(ingestion, "_rate_limited_until", 0.0)
    monkeypatch.setattr(config, "TRACEFOUR_API_KEY", "")
    ingestion.failures.clear()
    ingestion.key_warnings.clear()
    ingestion._session.headers.pop("Authorization", None)
    monkeypatch.setattr(broker, "_sleep", lambda s: None)
    # No network in tests: no member is a congressional leader unless a test says so.
    import src.main as main_module
    monkeypatch.setattr(main_module, "load_leaders", lambda: {})
    monkeypatch.setattr(main_module, "load_committees", lambda: {})
    broker.set_trading_client(None)
    database.init_db()
    yield
    broker.set_trading_client(None)


def make_filing(**overrides: Any) -> Dict[str, Any]:
    f = {
        "trade_uid": "nancy-pelosi|NVDA|2026-11-10|Purchase|$250,001 - $500,000|Self|Stock",
        "member_slug": "nancy-pelosi", "member_name": "Nancy Pelosi", "chamber": "House", "party": "D",
        "committees": [], "ticker": "NVDA", "action": "BUY", "amount_range": "$250,001 - $500,000",
        "amount_min": 250001, "transaction_date": "2026-11-10", "disclosure_date": "2026-11-20",
        "owner": "Self", "asset_type": "Stock", "asset_description": "NVIDIA Corporation",
        "source_link": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/1.pdf",
    }
    f.update(overrides)
    return f


class FakeAI:
    """Stands in for anthropic.Anthropic(); returns a fixed evaluation and records calls."""

    def __init__(self, evaluation: ai_analyst.TradeEvaluation, stop_reason: str = "end_turn"):
        self.calls: List[Dict[str, Any]] = []
        outer = self

        class _Messages:
            def parse(self, **kwargs):
                outer.calls.append(kwargs)
                return SimpleNamespace(stop_reason=stop_reason,
                                       parsed_output=None if stop_reason == "refusal" else evaluation)

        self.beta = SimpleNamespace(messages=_Messages())


def good_eval(score: int = 8) -> ai_analyst.TradeEvaluation:
    return ai_analyst.TradeEvaluation(should_trade=True, confidence_score=score, committee_match=True,
                                      is_index_or_cash=False, rationale="Strong, timely committee-aligned buy.")


class FakeBroker:
    """Minimal TradingClient stand-in."""

    def __init__(self, market_open: bool = True, equity: float = 10000.0, cash: float = 5000.0,
                 fractionable: bool = True):
        self.market_open = market_open
        self.equity, self.cash = equity, cash
        self.fractionable = fractionable
        self.orders: List[Any] = []
        self.closed: List[str] = []
        self.positions: Dict[str, float] = {}

    def get_clock(self):
        return SimpleNamespace(is_open=self.market_open)

    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), cash=str(self.cash), non_marginable_buying_power=str(self.cash))

    def get_asset(self, symbol):
        return SimpleNamespace(tradable=True, fractionable=self.fractionable)

    def submit_order(self, order_data):
        self.orders.append(order_data)
        oid = f"order-{len(self.orders)}"
        if str(order_data.side).lower().endswith("buy"):
            self.positions[order_data.symbol] = self.positions.get(order_data.symbol, 0) + 0.5
        return SimpleNamespace(id=oid, filled_qty="0")

    fill_price = "200.00"

    def get_order_by_id(self, order_id):
        return SimpleNamespace(filled_qty="0.5", filled_avg_price=self.fill_price, filled_at=None)

    def get_open_position(self, symbol):
        from alpaca.common.exceptions import APIError
        if symbol not in self.positions:
            raise APIError('{"code": 40410000, "message": "position does not exist"}')
        return SimpleNamespace(qty=str(self.positions[symbol]))

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.positions.pop(symbol, None)
        return SimpleNamespace(id=f"close-{symbol}")
