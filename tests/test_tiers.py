"""Two-tier sizing, Congress entry minimums, the multi-member cluster override, and buy/sell asymmetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src import broker, config, database as db
from src.ai_analyst import build_user_payload
from src.main import classify, ingest, run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()


def d(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def congress_buy(uid="u", lag=10, amount="$15,001 - $50,000", amount_min=15001, member="nancy-pelosi", **kw):
    return make_filing(trade_uid=uid, member_slug=member, disclosure_date=d(1), transaction_date=d(1 + lag),
                       amount_range=amount, amount_min=amount_min, **kw)


def ai(score, committee):
    return FakeAI(good_eval(score).model_copy(update={"committee_match": committee}))


def run(filing, ai_client):
    fb = FakeBroker()
    broker.set_trading_client(fb)
    run_pipeline(filings=[filing], ai_client=ai_client, now=NOW)
    return fb, db.get_filing(filing["trade_uid"])


# --- tiers ----------------------------------------------------------------------------

def test_standard_signal_buys_base_unit_with_30_day_hold():
    fb, row = run(congress_buy(), ai(8, False))
    assert row["signal_tier"] == "STANDARD" and fb.orders[0].notional == 100
    [pos] = db.get_all_open_positions()
    assert pos["time_stop_days"] == 30 and pos["signal_tier"] == "STANDARD"


def test_large_buy_with_committee_match_is_high_conviction():
    fb, row = run(congress_buy(amount="$50,001 - $100,000", amount_min=50001, lag=25), ai(7, True))
    assert row["signal_tier"] == "HIGH" and row["committee_match"] == 1
    assert fb.orders[0].notional == 250
    assert db.get_all_open_positions()[0]["time_stop_days"] == 90


def test_large_buy_with_score_9_is_high_conviction_without_committee():
    fb, row = run(congress_buy(amount="$250,001 - $500,000", amount_min=250001), ai(9, False))
    assert row["signal_tier"] == "HIGH" and fb.orders[0].notional == 250


def test_high_score_on_small_buy_stays_standard():
    fb, row = run(congress_buy(), ai(10, True))
    assert row["signal_tier"] == "STANDARD" and fb.orders[0].notional == 100


def test_lag_16_to_30_days_needs_high_conviction():
    fb, row = run(congress_buy(amount="$50,001 - $100,000", amount_min=50001, lag=20), ai(8, False))
    assert row["execution_status"] == "REJECTED" and "needs a high-conviction signal" in row["execution_note"]
    assert fb.orders == []


def test_lag_gates_before_any_ai_call():
    small_late = congress_buy(lag=20)
    assert classify(small_late, TODAY, set())[0] == "SKIPPED"
    too_late = congress_buy(amount="$1,000,001 - $5,000,000", amount_min=1000001, lag=31)
    assert "31d exceeds the 30-day limit" in classify(too_late, TODAY, set())[1]


def test_held_position_exits_after_its_own_time_stop():
    fb, _ = run(congress_buy(amount="$50,001 - $100,000", amount_min=50001), ai(9, True))
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-45 day')")
    assert run_pipeline(filings=[], now=NOW)["time_exits"] == 0  # HIGH: 90 days
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-91 day')")
    assert run_pipeline(filings=[], now=NOW)["time_exits"] == 1
    assert db.all_positions()[0]["exit_reason"] == "TIME_STOP_90D"


def test_insider_buys_trade_in_their_own_sleeve():
    f = make_filing(trade_uid="ins", filer_type="insider", disclosure_date=d(1), transaction_date=d(3),
                    amount_range="$500,000", amount_min=500000,
                    signal_context={"is_officer": True, "is_director": False, "is_10b5_1": False})
    fb, row = run(f, ai(10, True))
    assert row["signal_tier"] == "INSIDER" and fb.orders[0].notional == 100


# --- minimums and cluster override ---------------------------------------------------

def test_smallest_bucket_is_not_a_signal():
    f = congress_buy(amount="$1,001 - $15,000", amount_min=1001)
    assert classify(f, TODAY, set()) == ("SKIPPED", "below $15,001 Congress minimum")


def test_three_member_cluster_lowers_minimum_and_includes_non_targets():
    earlier = [congress_buy(uid=f"c{i}", member=m, party=p, ticker="LMT", amount="$1,001 - $15,000", amount_min=1001)
               for i, (m, p) in enumerate([("jane-doe", "D"), ("john-roe", "R")])]
    ingest(earlier, TODAY, [])  # recorded (as MONITORED / skipped), but they count toward the cluster
    third = congress_buy(uid="c3", member="sam-poe", party="D", ticker="LMT", amount="$1,001 - $15,000",
                         amount_min=1001)
    status, _ = classify(third, TODAY, set())
    assert status == "AWAITING_AI"
    cluster = third["signal_context"]["congress_cluster"]
    assert cluster["members"] == 3 and cluster["bipartisan"] is True
    assert "3 members of Congress bought this stock within 14 days (bipartisan)" in build_user_payload(third)


def test_buys_outside_the_window_do_not_form_a_cluster():
    far = congress_buy(uid="far", member="jane-doe", ticker="LMT", amount="$1,001 - $15,000", amount_min=1001)
    far["transaction_date"] = d(60)
    far["disclosure_date"] = d(40)
    ingest([far, congress_buy(uid="near", member="john-roe", ticker="LMT", amount="$1,001 - $15,000",
                              amount_min=1001)], TODAY, [])
    third = congress_buy(uid="x", member="sam-poe", ticker="LMT", amount="$1,001 - $15,000", amount_min=1001)
    assert classify(third, TODAY, set())[0] == "MONITORED"


# --- sells: no minimums, no staleness ------------------------------------------------

def test_any_sale_of_a_mirrored_ticker_exits_regardless_of_size_or_age():
    fb, _ = run(congress_buy(), ai(8, False))
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-3 day')")
    tiny_late_sale = make_filing(trade_uid="sell", action="SELL", amount_range="$1,001 - $15,000", amount_min=1001,
                                 owner="Spouse", transaction_date=d(80), disclosure_date=d(40))
    stats = run_pipeline(filings=[tiny_late_sale], now=NOW)
    assert db.get_filing("sell")["execution_status"] == "STALE"  # logged as stale...
    assert stats["pol_exits"] == 1 and fb.closed == ["NVDA"]     # ...but the exit still happens
