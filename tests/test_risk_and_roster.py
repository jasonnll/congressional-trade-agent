"""Circuit breaker, sector cap, smarter time stops, rules version, Senate roster, departed members, backtest."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from src import backtest, broker, config, database as db, house_clerk, ingestion, performance, sectors
from src.main import run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval, make_filing

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
FRESH, TRADED = (TODAY - timedelta(days=1)).isoformat(), (TODAY - timedelta(days=6)).isoformat()
AI = FakeAI(good_eval(8).model_copy(update={"committee_match": False}))


def buy(uid, ticker, member="nancy-pelosi", **kw):
    return make_filing(trade_uid=uid, member_slug=member, ticker=ticker, disclosure_date=FRESH,
                       transaction_date=kw.pop("transaction_date", TRADED), amount_range="$15,001 - $50,000",
                       amount_min=15001, **kw)


def test_circuit_breaker_pauses_new_buys_and_resumes(monkeypatch):
    broker.set_trading_client(FakeBroker(cash=100_000))
    db.init_db()
    db.set_meta("risk_state", json.dumps({"paused": True, "excess_pct": -18.0, "invested": 4000}))
    run_pipeline(filings=[buy("p1", "NVDA")], ai_client=AI, now=NOW)
    assert db.get_filing("p1")["execution_status"] == "PENDING" and not db.get_all_open_positions()

    monkeypatch.setattr(config, "BREAKER_MIN_INVESTED", 2000.0)
    db.set_meta("risk_state", json.dumps({"paused": True, "excess_pct": -18.0, "invested": 4000}))
    log = []
    performance.update_risk_state({"totals": {"invested": 4000, "excess": -300}}, NOW, log)  # -7.5 pts: recovered
    assert not json.loads(db.get_meta("risk_state"))["paused"] and "OFF" in log[0]
    performance.update_risk_state({"totals": {"invested": 4000, "excess": -480}}, NOW, log)  # -12: still on
    assert not json.loads(db.get_meta("risk_state"))["paused"]
    performance.update_risk_state({"totals": {"invested": 4000, "excess": -640}}, NOW, log)  # -16: trips
    assert json.loads(db.get_meta("risk_state"))["paused"]


def test_sector_cap_defers_buys_in_a_crowded_sector(monkeypatch):
    monkeypatch.setattr(sectors, "sector", lambda t: "Technology")
    monkeypatch.setattr(config, "SECTOR_MAX_SHARE", 0.02)  # 2% of $15,000 = $300: three $100 buys
    monkeypatch.setattr(config, "MAX_AI_EVALUATIONS_PER_RUN", 100)
    broker.set_trading_client(FakeBroker(cash=100_000))
    filings = [buy(f"s{i}", f"TEC{i}", transaction_date=(TODAY - timedelta(days=2 + i)).isoformat()) for i in range(5)]
    run_pipeline(filings=filings, ai_client=AI, now=NOW)
    assert len(db.get_all_open_positions()) == 3
    deferred = [r for r in db.get_filings_by_status("PENDING")]
    assert len(deferred) == 2 and "sector cap: Technology" in deferred[0]["execution_note"]


def test_time_stop_extends_when_still_being_bought():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    run_pipeline(filings=[buy("old", "NVDA")], ai_client=AI, now=NOW)
    hold = config.TIERS["STANDARD"]["time_stop_days"]
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', ?)", (f"-{hold + 1} day",))
    # another member's newer buy of the same stock keeps the thesis alive
    db.record_filing(buy("newer", "NVDA", member="ro-khanna"), "REJECTED")
    stats = run_pipeline(filings=[], now=NOW)
    pos = db.get_all_open_positions()[0]
    assert stats["time_exits"] == 0 and pos["time_stop_days"] == hold + config.TIME_STOP_EXTENSION_DAYS


def test_time_stop_sells_when_nobody_is_buying():
    fb = FakeBroker(cash=100_000)
    broker.set_trading_client(fb)
    run_pipeline(filings=[buy("old", "NVDA")], ai_client=AI, now=NOW)
    hold = config.TIERS["STANDARD"]["time_stop_days"]
    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', ?)", (f"-{hold + 1} day",))
    assert run_pipeline(filings=[], now=NOW)["time_exits"] == 1


def test_rules_version_is_stamped():
    broker.set_trading_client(FakeBroker(cash=100_000))
    run_pipeline(filings=[buy("v1", "NVDA")], ai_client=AI, now=NOW)
    assert db.get_filing("v1")["rules_version"] == config.RULES_VERSION
    assert db.get_all_open_positions()[0]["rules_version"] == config.RULES_VERSION


def test_senate_roster_includes_departed_senators_for_90_days(monkeypatch):
    db.init_db()
    long_ago = (NOW.date() - timedelta(days=config.OUTGOING_MEMBER_MONITOR_DAYS + 5)).isoformat()
    recent = (NOW.date() - timedelta(days=20)).isoformat()
    db.set_meta("senate_roster", json.dumps({"gone-long": long_ago, "gone-recent": recent}))
    monkeypatch.setattr(ingestion, "fetch_member_directory",
                        lambda: [{"slug": "a-senator", "chamber": "senate"}, {"slug": "a-rep", "chamber": "house"}])
    roster = ingestion.senate_roster(NOW)
    assert "a-senator" in roster and "gone-recent" in roster and "gone-long" not in roster and "a-rep" not in roster


def test_senate_rotation_covers_everyone_on_the_anonymous_tier(monkeypatch):
    db.init_db()
    slugs = [f"sen-{i:02d}" for i in range(60)]
    monkeypatch.setattr(ingestion, "senate_roster", lambda now: slugs)
    polled = []
    monkeypatch.setattr(ingestion, "fetch_member_trades_tracefour", lambda slug, c=None: polled.append(slug) or [])
    for _ in range(3):
        ingestion.poll_senate(NOW)
    assert set(polled) == set(slugs) and len(polled) == 3 * config.SENATE_POLL_PER_RUN_ANONYMOUS


def test_departed_house_member_keeps_their_slug():
    db.init_db()
    db.set_meta("member_history", json.dumps({"nancy-pelosi": {
        "slug": "nancy-pelosi", "last": "pelosi", "name": "Nancy Pelosi", "party": "D", "state": "CA",
        "district": 11, "chamber": "House", "last_seen": (date.today() - timedelta(days=10)).isoformat()}}))
    doc = {"StateDst": "CA11", "First": "Nancy", "Last": "Pelosi"}
    successor = {"house:CA11": {"slug": "new-member", "last": "member", "name": "New Member", "party": "D"}}
    assert house_clerk._resolve_member(doc, successor)["slug"] == "nancy-pelosi"


def test_backtest_replays_rules_against_spy():
    db.init_db()
    for i, (member, amount, lag) in enumerate([("nancy-pelosi", 15001, 10), ("ro-khanna", 250001, 5),
                                               ("ro-khanna", 1001, 5)]):
        t = date(2025, 1, 6) + timedelta(days=i)
        db.record_filing(make_filing(trade_uid=f"h{i}", member_slug=member, ticker="NVDA",
                                     transaction_date=t.isoformat(),
                                     disclosure_date=(t + timedelta(days=lag)).isoformat(),
                                     amount_range=f"${amount:,} - $X", amount_min=amount), "STALE")
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(400)]
    closes = {"NVDA": [(d, 100 + i * 0.1) for i, d in enumerate(days)], "SPY": [(d, 100.0) for d in days]}
    report = backtest.run(closes=closes, leaders={}, write=False)
    s = report["scenarios"]
    assert s["Every Congress stock buy, 90-day hold"]["trades"] == 3
    cur = s["Current rules (sized, rule-based holds)"]
    assert cur["trades"] == 2 and cur["avg_excess_pct"] > 0  # the $1,001 buy is below the minimum
