"""The budget goes to the best-rated trades: ranking, rotation out of a full sleeve, duplicate cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import broker, config, database as db
from src.main import _ai_priority, run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
FRESH, TRADED = (TODAY - timedelta(days=1)).isoformat(), (TODAY - timedelta(days=6)).isoformat()
AI7 = FakeAI(good_eval(7).model_copy(update={"committee_match": False}))
AI9 = FakeAI(good_eval(8).model_copy(update={"committee_match": False, "confidence_score": 9}))


@pytest.fixture(autouse=True)
def no_ai_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_AI_EVALUATIONS_PER_RUN", 1000)


def std_buy(uid: str, ticker: str, member: str = "nancy-pelosi"):
    # Trade dates spread out so no single day looks like a 15+ trade batch filing.
    return make_filing(trade_uid=uid, member_slug=member, ticker=ticker, disclosure_date=FRESH,
                       transaction_date=(TODAY - timedelta(days=2 + sum(map(ord, uid)) % 10)).isoformat(),
                       amount_range="$15,001 - $50,000", amount_min=15001)


def fill_standard_sleeve(score_ai):
    n = int(config.MAX_SLEEVE_EXPOSURE // 100)
    run_pipeline(filings=[std_buy(f"f{i}", f"T{i:03d}") for i in range(n)], ai_client=score_ai, now=NOW)
    with db.get_db_connection() as conn:  # opened yesterday, so rotation may sell them today
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-1 day')")
    assert db.get_open_exposure("STANDARD") == config.MAX_SLEEVE_EXPOSURE
    return n


def test_stronger_signal_replaces_weakest_position_when_full():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    n = fill_standard_sleeve(AI7)
    run_pipeline(filings=[std_buy("strong", "BEST")], ai_client=AI9, now=NOW)
    assert db.get_filing("strong")["execution_status"] == "EXECUTED"
    rotated = [p for p in db._rows("SELECT * FROM active_positions WHERE exit_reason = 'ROTATED_OUT'")]
    assert len(rotated) == 1
    assert db.get_open_exposure("STANDARD") == config.MAX_SLEEVE_EXPOSURE  # still within the cap
    assert len(db.get_all_open_positions()) == n


def test_equal_or_weaker_signal_waits_instead_of_rotating():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    fill_standard_sleeve(AI7)
    run_pipeline(filings=[std_buy("same", "SAME")], ai_client=AI7, now=NOW)
    assert db.get_filing("same")["execution_status"] == "PENDING"
    assert not db._rows("SELECT 1 FROM active_positions WHERE exit_reason = 'ROTATED_OUT'")


def test_positions_opened_today_are_never_rotated_out():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    n = int(config.MAX_SLEEVE_EXPOSURE // 100)
    run_pipeline(filings=[std_buy(f"f{i}", f"T{i:03d}") for i in range(n)], ai_client=AI7, now=NOW)
    run_pipeline(filings=[std_buy("strong", "BEST")], ai_client=AI9, now=NOW)
    assert db.get_filing("strong")["execution_status"] == "PENDING"


def test_duplicate_ticker_positions_are_exited_keeping_the_best():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    for uid, score in (("dup-a", 7), ("dup-b", 8)):
        db.record_filing(std_buy(uid, "RMIX"), "EXECUTED")
        db.update_ai_evaluation(uid, True, score, "x", committee_match=False, signal_tier="STANDARD")
        db.record_position_opened(uid, "someone", "RMIX", f"o-{uid}", 100, 14.3, signal_tier="STANDARD")
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-1 day')")
    run_pipeline(filings=[], ai_client=AI7, now=NOW)
    open_rmix = [p for p in db.get_all_open_positions() if p["ticker"] == "RMIX"]
    assert [p["trade_uid"] for p in open_rmix] == ["dup-b"]


def test_ai_queue_scores_clusters_and_bigger_trades_first():
    rows = [
        {"amount_min": 15001, "signal_context": {}},
        {"amount_min": 1000001, "signal_context": {}},
        {"amount_min": 1001, "signal_context": {"congress_cluster": {"members": 3}}},
    ]
    ordered = sorted(rows, key=_ai_priority)
    assert [r["amount_min"] for r in ordered] == [1001, 1000001, 15001]
