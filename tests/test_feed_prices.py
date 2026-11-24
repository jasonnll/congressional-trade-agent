"""Price change of each disclosed security since its trade date (the feed's "Since trade" column)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src import broker, config, database as db, performance
from tests.conftest import FakeBroker, FakePrices, make_filing

TODAY = date.today()
NOW = datetime.now(timezone.utc)


def d(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def weekday_ramp(start: float, end: float, days: int = 30):
    pts = [(TODAY - timedelta(days=days - i)) for i in range(days + 1)]
    pts = [p for p in pts if p.weekday() < 5]
    return [(p.isoformat(), round(start + (end - start) * i / (len(pts) - 1), 4)) for i, p in enumerate(pts)]


def record(uid, ticker, traded, disclosed):
    db.record_filing(make_filing(trade_uid=uid, ticker=ticker, transaction_date=traded, disclosure_date=disclosed),
                     "MONITORED")


def test_price_change_and_spy_since_trade():
    traded = next(p for p, _ in weekday_ramp(0, 1)[5:])  # a trading day ~3-4 weeks ago
    record("a", "NVDA", traded, d(1))
    nvda, spy = weekday_ramp(100, 120), weekday_ramp(400, 400 * 1.05)
    broker.set_trading_client(FakeBroker())
    performance.set_price_source(FakePrices({"NVDA": nvda, "SPY": spy}))
    performance.update(NOW)
    row = db.get_filing("a")
    ref = dict(nvda)[traded]
    assert row["trade_price"] == ref and row["latest_price"] == 120
    assert row["price_change_pct"] == round((120 / ref - 1) * 100, 2)
    assert row["spy_change_pct"] == round((400 * 1.05 / dict(spy)[traded] - 1) * 100, 2)


def test_weekend_trade_uses_previous_close():
    saturday = next(TODAY - timedelta(days=i) for i in range(7, 14) if (TODAY - timedelta(days=i)).weekday() == 5)
    friday = (saturday - timedelta(days=1)).isoformat()
    performance.price_filings(TODAY, {"X": [(friday, 50.0), (TODAY.isoformat(), 55.0)], "SPY": [(friday, 1.0)]},
                              [{"trade_uid": "w", "ticker": "X", "transaction_date": saturday.isoformat()}])
    record("w", "X", saturday.isoformat(), d(1))
    performance.price_filings(TODAY, {"X": [(friday, 50.0), (TODAY.isoformat(), 55.0)], "SPY": [(friday, 1.0)]},
                              [{"trade_uid": "w", "ticker": "X", "transaction_date": saturday.isoformat()}])
    assert db.get_filing("w")["trade_price"] == 50.0 and db.get_filing("w")["price_change_pct"] == 10.0


def test_unknown_ticker_stays_blank_and_old_filings_are_not_priced():
    record("u", "ZZZZ", d(20), d(5))
    record("old", "NVDA", d(400), d(380))
    broker.set_trading_client(FakeBroker())
    requested = []

    class OnlySpy:  # market data exists for SPY only, like a delisted or bad symbol
        def daily_closes(self, symbols, start):
            requested.extend(symbols)
            return {"SPY": weekday_ramp(1, 1)}

    performance.set_price_source(OnlySpy())
    performance.update(NOW)
    assert db.get_filing("u")["price_change_pct"] is None
    assert "ZZZZ" in requested and "NVDA" not in requested  # only filings in the lookback are requested


def test_one_bad_symbol_does_not_drop_the_rest_of_the_batch(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "s")
    calls = []

    class Client:
        def get_stock_bars(self, req):
            syms = req.symbol_or_symbols if isinstance(req.symbol_or_symbols, list) else [req.symbol_or_symbols]
            calls.append(list(syms))
            if "BAD" in syms:
                raise ValueError("invalid symbol: BAD")
            bar = SimpleNamespace(timestamp=datetime(2026, 11, 20, tzinfo=timezone.utc), close=10.0)
            return SimpleNamespace(data={s: [bar] for s in syms})

    prices = performance.AlpacaPrices.__new__(performance.AlpacaPrices)
    prices.client = Client()
    out = prices.daily_closes(["AAA", "BAD", "CCC"], TODAY - timedelta(days=10))
    assert set(out) == {"AAA", "CCC"} and calls[0] == ["AAA", "BAD", "CCC"] and len(calls) == 4
