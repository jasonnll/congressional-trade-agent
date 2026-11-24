from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src import broker, config, database as db, ingestion
from src.ai_analyst import build_user_payload
from src.main import classify, executive_shortlist, run_pipeline
from tests.conftest import FakeAI, FakeBroker, good_eval

NOW = datetime.now(timezone.utc)
TODAY = (NOW - timedelta(hours=5)).date()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731


def form4_row(idx=0, **kw):
    row = {
        "cik": "1266806", "filedAt": f"{D(1)}T12:04:30.000Z", "filingId": f"0001753926-26-002241-{idx}",
        "is10b5_1": False, "isDirector": True, "isOfficer": False, "isTenPercentOwner": True, "otherText": None,
        "issuerName": "Vivani Medical, Inc.", "issuerTicker": "VANI", "ownerName": "Williams Gregg", "ownerTitle": "",
        "ownershipType": "I", "pricePerShare": 1.137, "shares": 227959, "sharesOwnedAfter": 37339258,
        "totalValue": 259189.383, "transactionCode": "P", "transactionDate": D(4),
        "rawFilingUrl": "https://www.sec.gov/Archives/edgar/data/1624919/000175392626002241/0001753926-26-002241-index.htm",
    }
    row.update(kw)
    return row


TWO_LINES = [form4_row(0), form4_row(1, shares=4950496, sharesOwnedAfter=42289754, totalValue=5000000.96,
                                    transactionDate=D(3))]


def test_form4_line_items_collapse_into_one_signal():
    clusters = {"VANI": {"insider_count": 3, "total_value": 6e6, "seniority_mix": {"director": 3}}}
    [sig] = ingestion.normalize_insider_rows(TWO_LINES, clusters)
    assert sig["trade_uid"] == "form4|0001753926-26-002241|1266806|P"
    assert sig["filer_type"] == "insider" and sig["member_slug"] == "insider-1266806"
    assert sig["action"] == "BUY" and sig["transaction_date"] == D(4) and sig["disclosure_date"] == D(1)
    assert round(sig["amount_min"]) == 5259190
    ctx = sig["signal_context"]
    assert ctx["line_items"] == 2 and ctx["position_change_pct"] == 14.0
    assert ctx["cluster"]["insider_count"] == 3
    assert sig["filer_role"] == "Director, 10% Owner"


def test_form4_ignores_non_open_market_and_bad_tickers():
    rows = [form4_row(transactionCode="A"), form4_row(issuerTicker="NONE1"), form4_row(issuerTicker="")]
    assert ingestion.normalize_insider_rows(rows) == []


def _insider(**ctx_overrides):
    [sig] = ingestion.normalize_insider_rows(TWO_LINES)
    sig["signal_context"].update(ctx_overrides)
    return sig


def test_insider_filters():
    assert classify(_insider(), TODAY, set())[0] == "AWAITING_AI"
    assert classify(_insider(is_10b5_1=True), TODAY, set()) == ("SKIPPED", "pre-scheduled 10b5-1 plan purchase")
    assert classify(_insider(is_director=False, is_officer=False), TODAY, set())[0] == "SKIPPED"
    small = dict(_insider(), amount_min=50_000)
    assert "insider minimum" in classify(small, TODAY, set())[1]
    assert classify(dict(_insider(), ticker="SAFTX"), TODAY, set()) == ("SKIPPED", "mutual fund (not mirrored)")


OGE = {
    "identity": {"slug": "trump-portfolio", "displayName": "Donald J. Trump",
                 "role": "President · disclosed via OGE Form 278-T (trustee-managed accounts)"},
    "filingHistory": [{"filedAt": D(2), "sourceUrl": "https://www.whitehouse.gov/report.pdf"}],
    "ogeTransactions": [
        {"ticker": "META", "issuerName": "Meta Platforms", "transactionType": "Purchase", "valueLow": 5000001,
         "valueHigh": 25000000, "valueLabel": "$5M–$25M", "filedDate": D(2), "tradedDate": D(30)},
        {"ticker": "META", "issuerName": "Meta Platforms", "transactionType": "Purchase", "valueLow": 5000001,
         "valueHigh": 25000000, "valueLabel": "$5M–$25M", "filedDate": D(2), "tradedDate": D(30)},
        {"ticker": None, "issuerName": "TARRANT CNTY BOND", "transactionType": "Purchase", "valueLow": 1000001,
         "valueHigh": 5000000, "valueLabel": "$1M–$5M", "filedDate": D(2), "tradedDate": D(31)},
        {"ticker": "AAPL", "issuerName": "Apple", "transactionType": "Sale (Partial)", "valueLow": 15001,
         "valueHigh": 50000, "valueLabel": "$15K–$50K", "filedDate": D(2), "tradedDate": D(29)},
    ],
}


def test_oge_tracker_normalization():
    sigs = ingestion.normalize_oge_tracker(OGE)
    assert [(s["ticker"], s["action"]) for s in sigs] == [("META", "BUY"), ("AAPL", "SELL")]
    meta = sigs[0]
    assert meta["filer_type"] == "executive" and meta["member_slug"] == "trump-portfolio"
    assert meta["signal_context"]["duplicate_lines"] == 2
    assert meta["source_link"] == "https://www.whitehouse.gov/report.pdf"
    assert meta["owner"] == "Trustee-managed"


def test_executive_shortlist_keeps_only_largest_and_min_value(monkeypatch):
    monkeypatch.setattr(config, "MAX_EXECUTIVE_SIGNALS_PER_BATCH", 2)
    base = ingestion.normalize_oge_tracker(OGE)[0]
    batch = [dict(base, trade_uid=f"u{i}", ticker=f"T{chr(65 + i)}", amount_min=v)
             for i, v in enumerate([1_000_001, 25_000_001, 5_000_001, 50_001])]
    allowed = executive_shortlist(batch, TODAY)
    assert allowed == {"u1", "u2"}
    assert classify(batch[0], TODAY, allowed)[1].startswith("outside the 2 largest")
    assert "executive minimum" in classify(batch[3], TODAY, allowed)[1]


def test_ai_payloads_carry_source_context():
    p = build_user_payload(dict(_insider(), signal_context={**_insider()["signal_context"],
                                                           "cluster": {"insider_count": 4, "total_value": 1e6,
                                                                       "seniority_mix": {"ceoCfo": 1}}}))
    assert "CORPORATE INSIDER" in p and "Position Change: +14.0%" in p and "4 distinct insiders" in p
    e = build_user_payload(ingestion.normalize_oge_tracker(OGE)[0])
    assert "EXECUTIVE BRANCH" in e and "Repeated Lines" in e


def test_insider_buy_then_insider_sell_exits():
    fb = FakeBroker()
    broker.set_trading_client(fb)
    [buy] = ingestion.normalize_insider_rows(TWO_LINES)
    buy["signal_context"]["is_ten_percent_owner"] = False
    run_pipeline(filings=[buy], ai_client=FakeAI(good_eval()), now=NOW)
    assert db.get_filing(buy["trade_uid"])["execution_status"] == "EXECUTED"
    assert db.get_filing(buy["trade_uid"])["filer_type"] == "insider"

    with db.get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET opened_at = datetime('now', '-3 day')")
    [sell] = ingestion.normalize_insider_rows([form4_row(0, filingId="0009999999-26-000001-0", transactionCode="S")])
    stats = run_pipeline(filings=[sell], ai_client=FakeAI(good_eval()), now=NOW)
    assert stats["pol_exits"] == 1 and fb.closed == ["VANI"]
    assert db.all_positions()[0]["exit_reason"] == "INSIDER_SELL"


def test_ai_cost_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_AI_EVALUATIONS_PER_RUN", 3)
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    from tests.conftest import make_filing
    filings = [make_filing(trade_uid=f"c{i}", ticker=f"T{chr(65 + i)}", disclosure_date=D(i % 3),
                           transaction_date=D(10)) for i in range(5)]
    ai = FakeAI(good_eval(5))
    stats = run_pipeline(filings=filings, ai_client=ai, now=NOW)
    assert stats["evaluated"] == 3 and len(ai.calls) == 3
    assert len(db.get_filings_by_status("AWAITING_AI")) == 2


def test_migrates_ledger_created_before_new_columns():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.execute("DROP TABLE processed_filings")
        conn.execute("""CREATE TABLE processed_filings (trade_uid TEXT PRIMARY KEY, member_slug TEXT NOT NULL,
            member_name TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, amount_range TEXT,
            transaction_date TEXT NOT NULL, disclosure_date TEXT NOT NULL, ai_evaluated BOOLEAN NOT NULL DEFAULT 0,
            should_trade BOOLEAN DEFAULT 0, confidence_score INTEGER DEFAULT 0, ai_rationale TEXT,
            executed BOOLEAN DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, chamber TEXT, party TEXT,
            owner TEXT, asset_type TEXT, asset_description TEXT, source_link TEXT,
            execution_status TEXT NOT NULL DEFAULT 'AWAITING_AI', execution_note TEXT)""")
        conn.execute("INSERT INTO processed_filings (trade_uid, member_slug, member_name, ticker, action, "
                     "transaction_date, disclosure_date) VALUES ('old', 'x', 'X', 'NVDA', 'BUY', '2026-01-01', '2026-01-02')")
    db.init_db()
    row = db.get_filing("old")
    assert row["filer_type"] == "congress" and "signal_context" in row
