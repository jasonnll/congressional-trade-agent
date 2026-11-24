from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import config, database as db, ingestion, watchlist
from src.ai_analyst import build_user_payload
from src.main import classify, ingest
from tests.conftest import make_filing
from tests.test_insiders_executive import D, TODAY, form4_row

NOW = datetime.now(timezone.utc)

DON_JR = {"identity": {"slug": "trump-donald-j-jr", "ownerName": "Trump Donald J. JR", "tickers": ["PSQH", "DJT"],
                       "lastSeen": "2026-08-18T00:00:00.000Z"},
          "transactions": [
              form4_row(0, cik="1847064", ownerName="Trump Donald J. JR", issuerTicker="PSQH", issuerName="PSQ Holdings",
                        filingId="0002016181-26-000005-0", transactionCode="A", isTenPercentOwner=False),
              form4_row(0, cik="1847064", ownerName="Trump Donald J. JR", issuerTicker="DJT", issuerName="Trump Media",
                        filingId="0002016181-26-000009-0", totalValue=20000, shares=1000, sharesOwnedAfter=50000,
                        isTenPercentOwner=False, isDirector=False),
          ]}


def fake_api(url):
    if "/insiders?q=trump" in url:
        return {"data": [{"slug": "trump-donald-j-jr", "name": "Trump Donald J. JR"}]}
    if url.endswith("/insiders/trump-donald-j-jr"):
        return {"data": DON_JR}
    return None


WL = {"groups": [{"name": "Trump family", "people": [
    {"label": "Donald Trump Jr.", "slug": "trump-donald-j-jr"},
    {"label": "Eric Trump", "query": "trump eric"}]}],
    "congress_family": {"enabled": True, "all_members": True}}


def test_watchlist_resolves_people_and_tags_trades(monkeypatch):
    monkeypatch.setattr(watchlist, "get_json_with_retry", fake_api)
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    filings, status = watchlist.poll_watchlist_insiders({})
    assert [s["label"] for s in status] == ["Donald Trump Jr.", "Eric Trump"]
    assert status[0]["found"] and status[0]["cik"] == "1847064"
    assert status[1]["slug"] is None and not status[1]["found"]  # no Form 4 filer named "Trump Eric"
    [djt] = filings  # the award (code A) is ignored; only open-market trades become signals
    assert djt["ticker"] == "DJT" and djt["watchlist_group"] == "Trump family"
    assert djt["signal_context"]["watchlist_label"] == "Donald Trump Jr."
    assert watchlist.watchlist_insider_slugs(status) == {"insider-1847064": "Trump family"}


def test_watchlist_insider_gets_lower_bar_and_no_officer_requirement(monkeypatch):
    monkeypatch.setattr(watchlist, "get_json_with_retry", fake_api)
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    [djt], _ = watchlist.poll_watchlist_insiders({})
    assert classify(djt, TODAY, set())[0] == "AWAITING_AI"
    untagged = dict(djt, watchlist_group=None)
    assert classify(untagged, TODAY, set())[0] == "SKIPPED"


def test_tagging_feed_rows_and_congress_family():
    feed_row = {"filer_type": "insider", "member_slug": "insider-1847064"}
    spouse = make_filing(owner="Spouse")
    self_trade = make_filing(owner="Self")
    joint = make_filing(owner="Joint")
    watchlist.tag_filings([feed_row, spouse, self_trade, joint], {"insider-1847064": "Trump family"})
    assert feed_row["watchlist_group"] == "Trump family"
    assert spouse["watchlist_group"] == "Congress family"
    assert "watchlist_group" not in self_trade and "watchlist_group" not in joint


def test_senate_family_sweep_requires_api_key(monkeypatch):
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    monkeypatch.setattr(config, "TRACEFOUR_API_KEY", "")
    with pytest.raises(RuntimeError, match="TRACEFOUR_API_KEY"):
        watchlist.poll_congress_family(NOW, [])


def test_partial_sweep_is_not_marked_done(monkeypatch):
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    monkeypatch.setattr(config, "TRACEFOUR_API_KEY", "tf_test")
    monkeypatch.setattr(watchlist, "fetch_member_directory", lambda: [{"slug": "jane-doe", "chamber": "senate"}])

    def failing(slug):
        ingestion.failures.append("rate limited")
        return []

    monkeypatch.setattr(watchlist, "fetch_member_trades_tracefour", failing)
    watchlist.poll_congress_family(NOW, [])
    assert db.get_meta("congress_full_poll_at") is None


def test_congress_family_sweep_keeps_family_and_exits_only(monkeypatch):
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    monkeypatch.setattr(config, "TRACEFOUR_API_KEY", "tf_test")
    monkeypatch.setattr(watchlist, "fetch_member_directory",
                        lambda: [{"slug": "nancy-pelosi", "chamber": "house"}, {"slug": "jane-doe", "chamber": "senate"},
                                 {"slug": "house-person", "chamber": "house"}])
    trades = {
        "jane-doe": [
            make_filing(trade_uid="jd-spouse", member_slug="jane-doe", owner="Spouse", ticker="LMT"),
            make_filing(trade_uid="jd-self", member_slug="jane-doe", owner="Self", ticker="AAPL"),
            make_filing(trade_uid="jd-self-sell", member_slug="jane-doe", owner="Self", ticker="RTX", action="SELL"),
        ],
    }
    calls = []
    monkeypatch.setattr(watchlist, "fetch_member_trades_tracefour", lambda slug: calls.append(slug) or trades.get(slug, []))
    held = [{"member_slug": "jane-doe", "ticker": "RTX"}]
    out = watchlist.poll_congress_family(NOW, held)
    assert calls == ["jane-doe"]  # target members are already polled in full
    assert {f["trade_uid"] for f in out} == {"jd-spouse", "jd-self-sell"}
    assert watchlist.poll_congress_family(NOW + timedelta(hours=1), held) == []  # not due again yet
    assert watchlist.poll_congress_family(NOW + timedelta(hours=25), held) != []


def test_existing_ledger_rows_get_back_tagged():
    f = make_filing(owner="Spouse", disclosure_date="2024-01-02", transaction_date="2023-12-01")
    ingest([f], TODAY, [])
    assert db.get_filing(f["trade_uid"])["watchlist_group"] is None
    watchlist.tag_filings([f], {})
    ingest([f], TODAY, [])
    assert db.get_filing(f["trade_uid"])["watchlist_group"] == "Congress family"


def test_ai_payload_mentions_watchlist(monkeypatch):
    monkeypatch.setattr(watchlist, "get_json_with_retry", fake_api)
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: WL)
    [djt], _ = watchlist.poll_watchlist_insiders({})
    assert "Watchlist: Trump family (Donald Trump Jr.)" in build_user_payload(djt)
    fam = make_filing(owner="Spouse", watchlist_group="Congress family", disclosure_date=D(1))
    assert "Watchlist: Congress family (Nancy Pelosi)" in build_user_payload(fam)
