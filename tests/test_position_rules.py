from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import broker, database as db
from src.main import classify, run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
FRESH = (TODAY - timedelta(days=1)).isoformat()


def buy(uid, ticker="RMIX", member="nancy-pelosi", **kw):
    return make_filing(trade_uid=uid, ticker=ticker, member_slug=member, disclosure_date=FRESH,
                       transaction_date=(TODAY - timedelta(days=5)).isoformat(), **kw)


def test_two_filers_buying_same_stock_open_one_position():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    stats = run_pipeline(filings=[buy("a"), buy("b", member="ro-khanna")], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["opened"] == 1 and len(fb.orders) == 1
    skipped = [r for r in db.get_filings_by_status("SKIPPED") if r["trade_uid"] in ("a", "b")]
    assert len(skipped) == 1 and "already holding RMIX" in skipped[0]["execution_note"]


def test_higher_score_wins_the_ticker():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    db.init_db()
    for uid, score, member in (("low", 7, "nancy-pelosi"), ("high", 9, "ro-khanna")):
        db.record_filing(buy(uid, member=member), "PENDING")
        db.update_ai_evaluation(uid, True, score, "t")
        db.set_execution_status(uid, "PENDING")
    run_pipeline(filings=[], now=NOW, use_ai=False)
    assert db.get_filing("high")["execution_status"] == "EXECUTED"
    assert db.get_filing("low")["execution_status"] == "SKIPPED"


def test_existing_position_blocks_a_later_buy():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    run_pipeline(filings=[buy("first")], ai_client=FakeAI(good_eval()), now=NOW)
    stats = run_pipeline(filings=[buy("second", member="ro-khanna")], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["opened"] == 0 and len(fb.orders) == 1


def test_small_family_trade_of_non_target_member_is_skipped():
    small = buy("s", member="harriet-hageman", owner="Spouse", watchlist_group="Congress family",
                amount_range="$15,001 - $50,000", amount_min=15001)
    big = dict(small, trade_uid="b", amount_range="$50,001 - $100,000", amount_min=50001)
    target = dict(small, trade_uid="t", member_slug="nancy-pelosi")
    assert classify(small, TODAY, set()) == ("SKIPPED", "below $50,001 Congress-family minimum")
    assert classify(big, TODAY, set())[0] == "AWAITING_AI"
    assert classify(target, TODAY, set())[0] == "AWAITING_AI"  # target members are mirrored in full


def test_already_queued_small_family_signals_are_cleared_without_ai_calls():
    broker.set_trading_client(FakeBroker())
    db.record_filing(buy("q", member="harriet-hageman", owner="Spouse", watchlist_group="Congress family",
                         amount_range="$1,001 - $15,000"), "AWAITING_AI")
    ai = FakeAI(good_eval())
    run_pipeline(filings=[], ai_client=ai, now=NOW, trade=False)
    assert ai.calls == [] and db.get_filing("q")["execution_status"] == "SKIPPED"
