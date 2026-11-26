"""STANDARD and HIGH trade as separate sleeves: own $ cap, own one-per-stock rule, own performance."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from src import broker, config, database as db, performance
from src.main import run_pipeline
from tests.conftest import FakeAI, FakeBroker, FakePrices, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
FRESH, TRADED = (TODAY - timedelta(days=1)).isoformat(), (TODAY - timedelta(days=6)).isoformat()

STANDARD_AI = FakeAI(good_eval(8).model_copy(update={"committee_match": False}))
HIGH_AI = FakeAI(good_eval(9))


def buys(n, prefix, amount="$15,001 - $50,000", amount_min=15001):
    # Trade dates spread out so no single day looks like a 15+ trade batch filing.
    return [make_filing(trade_uid=f"{prefix}{i}", ticker=f"{prefix.upper()}{i:03d}", disclosure_date=FRESH,
                        transaction_date=(TODAY - timedelta(days=2 + i % 10)).isoformat(),
                        amount_range=amount, amount_min=amount_min) for i in range(n)]


def test_each_sleeve_has_its_own_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_AI_EVALUATIONS_PER_RUN", 1000)
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    fits = int(config.MAX_SLEEVE_EXPOSURE // 100)
    run_pipeline(filings=buys(fits + 2, "s"), ai_client=STANDARD_AI, now=NOW)  # two more than the cap holds
    assert db.get_open_exposure("STANDARD") == config.MAX_SLEEVE_EXPOSURE
    assert len(db.get_filings_by_status("PENDING")) == 2  # deferred, not lost

    # A full STANDARD sleeve doesn't block HIGH buys.
    high_fits = int(config.MAX_SLEEVE_EXPOSURE // 250)
    run_pipeline(filings=buys(high_fits + 1, "h", "$50,001 - $100,000", 50001), ai_client=HIGH_AI, now=NOW)
    assert db.get_open_exposure("HIGH") == high_fits * 250  # the last one waits
    assert db.get_open_exposure() == 2 * config.MAX_SLEEVE_EXPOSURE


def test_same_stock_can_be_held_once_per_sleeve():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    small = make_filing(trade_uid="std", ticker="NVDA", disclosure_date=FRESH, transaction_date=TRADED,
                        amount_range="$15,001 - $50,000", amount_min=15001)
    big = make_filing(trade_uid="high", member_slug="ro-khanna", ticker="NVDA", disclosure_date=FRESH,
                      transaction_date=TRADED)
    run_pipeline(filings=[small], ai_client=STANDARD_AI, now=NOW)
    run_pipeline(filings=[big], ai_client=HIGH_AI, now=NOW)
    sleeves = sorted(p["signal_tier"] for p in db.get_all_open_positions())
    assert sleeves == ["HIGH", "STANDARD"] and len(fb.orders) == 2
    again = make_filing(trade_uid="std2", member_slug="michael-mccaul", ticker="NVDA", disclosure_date=FRESH,
                        transaction_date=TRADED, amount_range="$15,001 - $50,000", amount_min=15001)
    run_pipeline(filings=[again], ai_client=STANDARD_AI, now=NOW)
    assert "STANDARD sleeve already holds NVDA" in db.get_filing("std2")["execution_note"]


def test_existing_untagged_positions_join_standard():
    db.record_filing(make_filing(trade_uid="old"), "EXECUTED")
    db.record_position_opened("old", "nancy-pelosi", "NVDA", "o1", 100, 0)
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET signal_tier = NULL")
    db.init_db()
    assert db.get_all_open_positions()[0]["signal_tier"] == "STANDARD"


def test_old_snapshot_table_is_recreated_with_sleeves():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("DROP TABLE performance_snapshots")
        conn.execute("CREATE TABLE performance_snapshots (snapshot_date TEXT PRIMARY KEY, invested REAL)")
    db.init_db()
    with sqlite3.connect(config.DB_PATH) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(performance_snapshots)")}
    assert "sleeve" in cols


def test_performance_is_reported_per_sleeve():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    run_pipeline(filings=buys(1, "s"), ai_client=STANDARD_AI, now=NOW)
    run_pipeline(filings=buys(1, "h", "$50,001 - $100,000", 50001), ai_client=HIGH_AI, now=NOW)
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-10 day'), filled_at = NULL, "
                     "entry_price = NULL")
    ramp = lambda a, b: [((date.today() - timedelta(days=10 - i)).isoformat(), a + (b - a) * i / 10) for i in range(11)]  # noqa: E731
    fb.fill_price = "100"
    performance.set_price_source(FakePrices({"S000": ramp(100, 110), "H000": ramp(100, 95), "SPY": ramp(100, 102)}))
    s = performance.update(NOW)
    std, high = s["sleeves"]["STANDARD"], s["sleeves"]["HIGH"]
    assert std["positions"] == 1 and std["pnl_pct"] == 10.0 and std["spy_pnl_pct"] == 2.0
    assert high["positions"] == 1 and high["pnl_pct"] == -5.0 and high["open_exposure"] == 250
    assert std["history"] and high["history"] and all(h["sleeve"] == "HIGH" for h in high["history"])
    assert len(db.snapshots("ALL")) == len(std["history"])


def insider_buys(n, prefix):
    return [make_filing(trade_uid=f"{prefix}{i}", member_slug=f"ceo-{i}", filer_type="insider",
                        ticker=f"{prefix.upper()}{i:03d}", disclosure_date=FRESH, transaction_date=FRESH,
                        amount_range="$500,000", amount_min=500000,
                        signal_context={"is_officer": True, "is_director": False, "is_10b5_1": False})
            for i in range(n)]


def test_insiders_cannot_use_congress_money(monkeypatch):
    monkeypatch.setattr(config, "MAX_AI_EVALUATIONS_PER_RUN", 1000)
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    fits = int(config.INSIDER_SLEEVE_EXPOSURE // 100)
    run_pipeline(filings=insider_buys(fits + 3, "i"), ai_client=STANDARD_AI, now=NOW)
    assert db.get_open_exposure("INSIDER") == config.INSIDER_SLEEVE_EXPOSURE
    assert db.get_open_exposure("STANDARD") == 0  # the full insider sleeve spilled nothing into Congress
    # Congress buys still go through with the insider sleeve full.
    run_pipeline(filings=buys(2, "c"), ai_client=STANDARD_AI, now=NOW)
    assert db.get_open_exposure("STANDARD") == 200


def test_existing_insider_positions_move_to_the_insider_sleeve():
    f = insider_buys(1, "old")[0]
    db.record_filing(f, "EXECUTED")
    db.update_ai_evaluation(f["trade_uid"], True, 8, "x", committee_match=False, signal_tier="STANDARD")
    db.record_position_opened(f["trade_uid"], f["member_slug"], f["ticker"], "o1", 100, 0, signal_tier="STANDARD")
    db.init_db()
    assert db.get_all_open_positions()[0]["signal_tier"] == "INSIDER"
    assert db.get_filing(f["trade_uid"])["signal_tier"] == "INSIDER"
    assert db.get_open_exposure("STANDARD") == 0
