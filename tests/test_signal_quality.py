"""Batch filings, partial sales, committee data for the AI, and the liquidity floor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import broker, config, database as db, performance
from src.main import run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
FRESH, TRADED = (TODAY - timedelta(days=1)).isoformat(), (TODAY - timedelta(days=6)).isoformat()
AI = FakeAI(good_eval(8).model_copy(update={"committee_match": False}))


def buy(uid, ticker, day=TRADED, **kw):
    return make_filing(trade_uid=uid, ticker=ticker, disclosure_date=FRESH, transaction_date=day,
                       amount_range="$15,001 - $50,000", amount_min=15001, **kw)


def sell(uid, ticker, tx_code="S", day=TRADED):
    return make_filing(trade_uid=uid, ticker=ticker, action="SELL", disclosure_date=FRESH, transaction_date=day,
                       amount_range="$15,001 - $50,000", amount_min=15001,
                       signal_context={"tx_code": tx_code, "description": ""})


def hold_position(ticker="NVDA"):
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    run_pipeline(filings=[buy("b1", ticker, day=(TODAY - timedelta(days=8)).isoformat())], ai_client=AI, now=NOW)
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-3 day')")
    assert len(db.get_all_open_positions()) == 1
    return fb


def test_batch_filing_buys_are_skipped_before_the_ai():
    broker.set_trading_client(FakeBroker(cash=100_000))
    ai = FakeAI(good_eval(8))
    filings = [buy(f"x{i}", f"B{i:03d}") for i in range(config.BATCH_FILING_MIN_TRADES)]
    run_pipeline(filings=filings, ai_client=ai, now=NOW)
    assert not ai.calls and not db.get_all_open_positions()
    assert "batch filing" in db.get_filing("x0")["execution_note"]


def test_a_few_buys_on_one_day_are_not_a_batch():
    broker.set_trading_client(FakeBroker(cash=100_000))
    filings = [buy(f"x{i}", f"B{i:03d}") for i in range(3)]
    run_pipeline(filings=filings, ai_client=AI, now=NOW)
    assert len(db.get_all_open_positions()) == 3


def test_partial_sale_keeps_the_position():
    fb = hold_position()
    stats = run_pipeline(filings=[sell("s1", "NVDA", tx_code="S (partial)")], ai_client=AI, now=NOW)
    assert stats["pol_exits"] == 0 and fb.closed == [] and len(db.get_all_open_positions()) == 1


def test_full_sale_still_exits():
    fb = hold_position()
    stats = run_pipeline(filings=[sell("s1", "NVDA", tx_code="S")], ai_client=AI, now=NOW)
    assert stats["pol_exits"] == 1 and fb.closed == ["NVDA"]


def test_sale_inside_a_batch_filing_keeps_the_position():
    fb = hold_position()
    filings = [sell("s0", "NVDA")] + [sell(f"s{i}", f"Z{i:03d}") for i in range(1, config.BATCH_FILING_MIN_TRADES)]
    stats = run_pipeline(filings=filings, ai_client=AI, now=NOW)
    assert stats["pol_exits"] == 0 and fb.closed == []


def test_member_committees_reach_the_ai(monkeypatch):
    import src.main as main_module
    monkeypatch.setattr(main_module, "load_committees",
                        lambda: {"nancy-pelosi": ["House Committee on Energy and Commerce"]})
    broker.set_trading_client(FakeBroker(cash=100_000))
    ai = FakeAI(good_eval(8))
    run_pipeline(filings=[buy("c1", "NVDA")], ai_client=ai, now=NOW)
    assert "Energy and Commerce" in str(ai.calls[0])


class ThinPrices:
    def __init__(self, price, dollar_volume):
        self.stats = (price, dollar_volume)

    def daily_closes(self, symbols, start):
        return {}

    def liquidity(self, ticker):
        return self.stats


def test_penny_stock_is_skipped():
    broker.set_trading_client(FakeBroker(cash=100_000))
    performance.set_price_source(ThinPrices(1.05, 500_000))
    run_pipeline(filings=[buy("p1", "PENY")], ai_client=AI, now=NOW)
    assert "illiquid: price" in db.get_filing("p1")["execution_note"] and not db.get_all_open_positions()


def test_thinly_traded_stock_is_skipped():
    broker.set_trading_client(FakeBroker(cash=100_000))
    performance.set_price_source(ThinPrices(12.0, 3_000))
    run_pipeline(filings=[buy("t1", "THIN")], ai_client=AI, now=NOW)
    assert "IEX dollar volume" in db.get_filing("t1")["execution_note"]


def test_liquid_stock_buys():
    broker.set_trading_client(FakeBroker(cash=100_000))
    performance.set_price_source(ThinPrices(150.0, 5_000_000))
    run_pipeline(filings=[buy("l1", "LIQD")], ai_client=AI, now=NOW)
    assert len(db.get_all_open_positions()) == 1
