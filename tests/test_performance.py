from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src import broker, database as db, performance
from tests.conftest import FakeBroker, FakePrices, make_filing

TODAY = date.today()
NOW = datetime.now(timezone.utc)


def d(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def ramp(start: float, end: float, days: int = 20):
    """Daily closes from `days` ago to today, moving linearly from start to end."""
    return [(d(days - i), round(start + (end - start) * i / days, 4)) for i in range(days + 1)]


def open_position(uid="u1", ticker="NVDA", opened_days_ago=20, notional=100.0, filer_type="congress",
                  group=None, order_id="ord-1"):
    db.record_filing(make_filing(trade_uid=uid, ticker=ticker, filer_type=filer_type, watchlist_group=group), "PENDING")
    db.record_position_opened(uid, "nancy-pelosi", ticker, order_id, notional, 0)
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', ?) WHERE trade_uid = ?",
                     (f"-{opened_days_ago} day", uid))
    return db.get_open_positions_by_member("nancy-pelosi", ticker)[-1]["id"]


@pytest.fixture
def fb():
    b = FakeBroker()
    broker.set_trading_client(b)
    return b


def test_open_position_return_vs_spy(fb):
    fb.fill_price = "200"
    open_position()
    performance.set_price_source(FakePrices({"NVDA": ramp(200, 220), "SPY": ramp(100, 105)}))
    s = performance.update(NOW)
    [m] = s["positions"].values()
    assert m["return_pct"] == 10.0 and m["spy_return_pct"] == 5.0 and m["excess_pct"] == 5.0
    assert m["pnl"] == 10.0 and m["spy_pnl"] == 5.0 and not m["closed"]
    t = s["totals"]
    assert t["invested"] == 100 and t["pnl"] == 10.0 and t["spy_pnl"] == 5.0 and t["excess"] == 5.0
    assert t["win_rate"] is None  # only closed positions count toward win rate


def test_closed_position_uses_exit_fill_and_spy_over_same_dates(fb):
    fb.fill_price = "200"
    pid = open_position()
    performance.set_price_source(FakePrices({"NVDA": ramp(200, 240), "SPY": ramp(100, 120)}))
    performance.update(NOW)
    fb.fill_price = "180"
    db.record_position_closed(pid, "POLITICIAN_SELL", "exit-1")
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET closed_at = datetime('now', '-10 day') WHERE id = ?", (pid,))
    s = performance.update(NOW)
    m = s["positions"][pid]
    assert m["closed"] and m["return_pct"] == -10.0
    assert m["spy_return_pct"] == 10.0  # SPY from entry (100) to the exit day (110), not to today (120)
    row = [p for p in db.positions_with_filings() if p["id"] == pid][0]
    assert row["realized_pnl"] == pytest.approx(0.5 * 180 - 100)
    assert s["totals"]["win_rate"] == 0 and s["totals"]["closed"] == 1


def test_position_closed_outside_bot_is_priced_from_close_and_flagged(fb):
    fb.fill_price = "200"
    pid = open_position()
    db.record_position_closed(pid, "TIME_STOP_30D", "MANUAL_CLOSE")
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET closed_at = datetime('now', '-5 day') WHERE id = ?", (pid,))
    performance.set_price_source(FakePrices({"NVDA": ramp(200, 220), "SPY": ramp(100, 100)}))
    s = performance.update(NOW)
    m = s["positions"][pid]
    assert m["closed"] and m["estimated"] and m["return_pct"] == 7.5  # close 5 days before the end of the ramp


def test_unfilled_orders_are_excluded_but_counted(fb):
    fb.fill_price = "0"
    open_position()
    s = performance.update(NOW)
    assert s["positions"] == {} and s["totals"]["unfilled"] == 1 and s["totals"]["invested"] == 0


def test_daily_history_rebuilt_from_closes(fb):
    fb.fill_price = "200"
    open_position(opened_days_ago=10)
    performance.set_price_source(FakePrices({"NVDA": ramp(180, 220), "SPY": ramp(100, 100)}))
    performance.update(NOW)
    hist = db.snapshots()
    assert hist[0]["snapshot_date"] == d(10) and hist[-1]["snapshot_date"] == d(0)
    assert all(h["invested"] == 100 for h in hist) and all(h["spy_pnl"] == 0 for h in hist)
    # Fill was 200; the close on the fill day was 200 on this ramp, and 220 today -> +$10 on 0.5 shares
    assert hist[-1]["pnl"] == pytest.approx(0.5 * 220 - 100)


def test_ai_filter_check_scores_approved_and_rejected(fb):
    for uid, ticker, approved in (("a", "WIN", True), ("r", "LOSE", False)):
        db.record_filing(make_filing(trade_uid=uid, ticker=ticker), "PENDING" if approved else "REJECTED")
        db.update_ai_evaluation(uid, approved, 8 if approved else 4, "test")
        with db.get_db_connection() as conn:
            conn.execute("UPDATE processed_filings SET created_at = datetime('now', '-20 day') WHERE trade_uid = ?", (uid,))
    performance.set_price_source(FakePrices({"WIN": ramp(100, 120), "LOSE": ramp(100, 90), "SPY": ramp(100, 105)}))
    s = performance.update(NOW)
    ai = s["ai_filter"]
    assert ai["APPROVED"]["scored"] == 1 and ai["APPROVED"]["avg_return_pct"] == 20.0
    assert ai["APPROVED"]["avg_excess_pct"] == 15.0 and ai["APPROVED"]["beat_spy_rate"] == 100
    assert ai["REJECTED"]["avg_return_pct"] == -10.0 and ai["REJECTED"]["beat_spy_rate"] == 0


def test_breakdown_by_source_and_watchlist(fb):
    fb.fill_price = "200"
    open_position("c1", "NVDA")
    open_position("i1", "AMD", filer_type="insider", order_id="ord-2")
    open_position("w1", "DJT", filer_type="insider", group="Trump family", order_id="ord-3")
    performance.set_price_source(FakePrices({"NVDA": ramp(200, 220), "AMD": ramp(200, 180), "DJT": ramp(200, 200)}))
    s = performance.update(NOW)
    by = {r["name"]: r for r in s["by_source"]}
    assert set(by) == {"Congress", "Corporate insiders", "Trump family"}
    assert by["Congress"]["avg_return_pct"] == 10.0 and by["Corporate insiders"]["avg_return_pct"] == -10.0


def test_missing_benchmark_is_an_error_not_a_silent_zero(fb):
    class NoSpy:
        def daily_closes(self, symbols, start):
            return {}
    performance.set_price_source(NoSpy())
    with pytest.raises(RuntimeError, match="SPY"):
        performance.update(NOW)
