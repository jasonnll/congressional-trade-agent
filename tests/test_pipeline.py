"""End-to-end pipeline runs against mocked Tracefour data, a mocked Claude client and a fake Alpaca client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import broker, config, database as db
from src.main import run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()  # close enough to ET for date math in fixtures
FRESH = (TODAY - timedelta(days=1)).isoformat()


def fresh_buy(**kw):
    return make_filing(disclosure_date=FRESH, transaction_date=(TODAY - timedelta(days=9)).isoformat(), **kw)


def test_full_run_executes_and_is_idempotent():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    ai = FakeAI(good_eval(8))
    stale = make_filing(trade_uid="old", ticker="AAPL", disclosure_date="2024-01-05", transaction_date="2023-12-10")
    filings = [fresh_buy(), stale]

    stats = run_pipeline(filings=filings, ai_client=ai, now=NOW)
    assert stats["new"] == 2 and stats["stale"] == 1 and stats["evaluated"] == 1 and stats["opened"] == 1
    assert len(fb.orders) == 1 and fb.orders[0].symbol == "NVDA"
    assert db.get_filing("old")["execution_status"] == "STALE"
    assert db.get_open_exposure() == config.MAX_NOTIONAL_PER_TRADE

    journal = Path(config.JOURNAL_PATH).read_text()
    assert "EXECUTED BUY" in journal and "PAPER" in journal

    stats2 = run_pipeline(filings=filings, ai_client=ai, now=NOW)
    assert stats2["new"] == 0 and stats2["opened"] == 0
    assert len(fb.orders) == 1 and len(ai.calls) == 1


def test_market_closed_keeps_signal_pending_then_executes():
    fb = FakeBroker(market_open=False)
    broker.set_trading_client(fb)
    run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval()), now=NOW)
    assert db.get_filing(fresh_buy()["trade_uid"])["execution_status"] == "PENDING" and fb.orders == []

    fb.market_open = True
    stats = run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["opened"] == 1 and len(fb.orders) == 1


def test_low_score_is_rejected():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval(5)), now=NOW)
    assert db.get_filing(fresh_buy()["trade_uid"])["execution_status"] == "REJECTED" and fb.orders == []


def test_missing_ai_key_defers_without_losing_signal(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    broker.set_trading_client(FakeBroker())
    run_pipeline(filings=[fresh_buy()], now=NOW)
    assert db.get_filing(fresh_buy()["trade_uid"])["execution_status"] == "AI_RETRY"


def test_politician_sell_exits_mirror_position_with_pdt_guard():
    fb = FakeBroker(equity=10000)
    broker.set_trading_client(fb)
    run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval()), now=NOW)

    sell = fresh_buy(trade_uid="pelosi-sell", action="SELL")
    run_pipeline(filings=[sell], ai_client=FakeAI(good_eval()), now=NOW)
    pos = db.get_all_open_positions()[0]
    assert pos["pending_exit_reason"] == "POLITICIAN_SELL" and fb.closed == []  # opened today, equity < $25k

    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-2 day')")
    stats = run_pipeline(filings=[sell], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["pol_exits"] == 1 and fb.closed == ["NVDA"] and db.get_all_open_positions() == []


def test_time_stop_closes_old_positions():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval()), now=NOW)
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-31 day')")
    stats = run_pipeline(filings=[], now=NOW)
    assert stats["time_exits"] == 1 and fb.closed == ["NVDA"]


def test_exposure_cap_stops_new_buys():
    fb = FakeBroker(cash=100000)
    broker.set_trading_client(fb)
    cap = int(config.MAX_PORTFOLIO_EXPOSURE // config.MAX_NOTIONAL_PER_TRADE)
    filings = [fresh_buy(trade_uid=f"uid-{i}", ticker=f"T{chr(65 + i)}") for i in range(cap + 3)]
    stats = run_pipeline(filings=filings, ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["opened"] == cap
    assert db.get_open_exposure() <= config.MAX_PORTFOLIO_EXPOSURE
    assert len(db.get_filings_by_status("PENDING")) == 3


def test_no_broker_keys_skips_trading_gracefully(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    stats = run_pipeline(filings=[fresh_buy()], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["opened"] == 0
    assert db.get_filing(fresh_buy()["trade_uid"])["execution_status"] == "PENDING"
