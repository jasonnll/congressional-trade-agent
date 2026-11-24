"""Regression tests against real House Clerk PTR PDFs (public-domain government records) in tests/fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src import config, database as db, house_clerk, ingestion
from src.main import classify, ingest
from tests.conftest import make_filing

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 24)
LEGISLATORS = {
    "house:HI01": {"slug": "ed-case", "name": "Ed Case", "party": "D", "chamber": "House"},
    "house:CA17": {"slug": "ro-khanna", "name": "Ro Khanna", "party": "D", "chamber": "House"},
    "house:TX19": {"slug": "jodey-arrington", "name": "Jodey C. Arrington", "party": "R", "chamber": "House"},
}


def rows(doc_id: str):
    return house_clerk.parse_ptr_text(house_clerk.extract_text((FIX / f"{doc_id}.pdf").read_bytes()))


def doc(doc_id, first, last, sd, filed="2026-11-21"):
    return {"DocID": doc_id, "First": first, "Last": last, "StateDst": sd, "filed": filed, "year": 2026}


def test_ed_case_spouse_aapl_dividend_reinvestment():
    [r] = rows("20036891")
    assert (r["owner"], r["ticker"], r["asset_code"], r["action"]) == ("Spouse", "AAPL", "ST", "BUY")
    assert (r["transaction_date"], r["notification_date"]) == ("2026-11-16", "2026-11-21")
    assert r["amount_range"] == "$1,001 - $15,000"
    assert r["description"] == "Automatic stock dividend reinvestment"


def test_multi_page_filing_parses_every_row_including_page_break():
    parsed = rows("20035788")
    assert len(parsed) == 65
    nclh = [r for r in parsed if r["ticker"] == "NCLH"]
    assert nclh and nclh[0]["asset_name"].startswith("Norwegian Cruise Line Holdings")
    assert {r["tx_code"] for r in parsed} >= {"P", "S", "S (partial)"}


def test_split_account_rows_collapse_into_one_signal():
    filings = house_clerk.normalize_house_rows(doc("20035831", "David J.", "Taylor", "OH02"), rows("20035831"), {})
    googl = [f for f in filings if f["ticker"] == "GOOGL"]
    assert len(googl) == 1 and googl[0]["signal_context"]["split_rows"] == 2


def test_scanned_paper_filing_has_no_text():
    assert len(house_clerk.extract_text((FIX / "9116399.pdf").read_bytes()).strip()) < 200


def test_member_resolution_uses_legislators_and_targets():
    [f] = house_clerk.normalize_house_rows(doc("20036891", "Ed", "Case", "HI01"), rows("20036891"), LEGISLATORS)
    assert (f["member_slug"], f["member_name"], f["party"], f["chamber"]) == ("ed-case", "Ed Case", "D", "House")
    assert f["owner"] == "Spouse" and f["disclosure_date"] == "2026-11-21"
    assert f["source_link"].endswith("/ptr-pdfs/2026/20036891.pdf")
    kean = house_clerk._resolve_member(doc("1", "Thomas H.", "Kean", "NJ07"),
                                       {"house:NJ07": {"slug": "tom-kean", "last": "kean", "name": "Thomas H. Kean Jr.", "party": "R"}})
    assert kean["party"] == "R" and kean["name"] == "Thomas H. Kean Jr."
    # Clerk spells him "Rohit"; the configured target slug wins so positions and filters line up.
    k = house_clerk._resolve_member(doc("1", "Rohit", "Khanna", "CA17"), {})
    assert k["slug"] == "ro-khanna"


def test_ed_case_trade_is_shown_but_not_traded():
    [f] = house_clerk.normalize_house_rows(doc("20036891", "Ed", "Case", "HI01"), rows("20036891"), LEGISLATORS)
    f["watchlist_group"] = "Congress family"
    status, note = classify(f, TODAY, set())
    assert status == "SKIPPED" and "non-discretionary" in note


def test_non_target_member_is_monitored_not_mirrored(monkeypatch):
    f = make_filing(member_slug="ed-case", owner="Self", disclosure_date="2026-11-21", transaction_date="2026-11-16")
    assert classify(f, TODAY, set())[0] == "MONITORED"
    monkeypatch.setattr(config, "CONGRESS_MIRROR_ALL_MEMBERS", True)
    assert classify(f, TODAY, set())[0] == "AWAITING_AI"


def test_same_trade_from_tracefour_and_clerk_is_recorded_once():
    tracefour = make_filing(trade_uid="nancy-pelosi|WBD|2026-10-07|Sale|...", ticker="WBD", action="SELL",
                            transaction_date="2026-10-07", amount_range="$50,001 - $100,000")
    clerk = dict(tracefour, trade_uid="house|20035780|0", owner="Spouse")
    stats = ingest([tracefour], TODAY, [])
    stats = ingest([clerk], TODAY, [])
    assert stats["duplicates"] == 1 and db.get_filing("house|20035780|0") is None


def test_poll_house_downloads_each_pdf_once_and_flags_paper(monkeypatch):
    index = [
        {"DocID": "20036891", "First": "Ed", "Last": "Case", "StateDst": "HI01", "FilingType": "P", "FilingDate": "11/21/2026"},
        {"DocID": "9116399", "First": "Rohit", "Last": "Khanna", "StateDst": "CA17", "FilingType": "P", "FilingDate": "11/05/2026"},
        {"DocID": "1", "First": "Old", "Last": "Report", "StateDst": "CA01", "FilingType": "P", "FilingDate": "01/05/2026"},
        {"DocID": "2", "First": "Annual", "Last": "Report", "StateDst": "CA01", "FilingType": "O", "FilingDate": "11/20/2026"},
    ]
    downloads = []
    monkeypatch.setattr(house_clerk, "fetch_index", lambda year: index if year == 2026 else [])
    monkeypatch.setattr(house_clerk, "load_legislators", lambda: LEGISLATORS)

    def fake_get(url, timeout=60):
        downloads.append(url)
        return (FIX / url.rsplit("/", 1)[-1]).read_bytes()

    monkeypatch.setattr(house_clerk, "_get", fake_get)
    first = house_clerk.poll_house(TODAY)
    assert [f["ticker"] for f in first] == ["AAPL"]
    assert len(downloads) == 2  # old filing and non-PTR are never fetched
    assert db.get_document("9116399")["status"] == "NO_TEXT"
    assert [d["doc_id"] for d in db.documents_needing_review()] == ["9116399"]

    second = house_clerk.poll_house(TODAY)
    assert [f["trade_uid"] for f in second] == [f["trade_uid"] for f in first]
    assert len(downloads) == 2  # served from the cache


def test_unreachable_clerk_raises_instead_of_returning_nothing(monkeypatch):
    def down(url, timeout=60):
        raise ConnectionError("House Clerk unreachable")

    monkeypatch.setattr(house_clerk, "_get", down)
    with pytest.raises(ConnectionError):
        house_clerk.fetch_index(2026)


def test_source_health_reports_failures_without_stopping_other_sources(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_EXECUTIVE", False)
    monkeypatch.setattr(house_clerk, "load_legislators", lambda: LEGISLATORS)
    monkeypatch.setattr(config, "TARGET_MEMBERS", ["ed-case"])
    monkeypatch.setattr(house_clerk, "poll_house", lambda today: (_ for _ in ()).throw(ConnectionError("Clerk down")))
    monkeypatch.setattr(ingestion, "fetch_insider_clusters", lambda: {})

    def rate_limited(today, clusters):
        ingestion.failures.append("rate limited for 900s")
        return []

    monkeypatch.setattr(ingestion, "poll_insider_buys", rate_limited)
    from src import watchlist
    monkeypatch.setattr(watchlist, "poll_watchlist_insiders", lambda c: ([], []))
    monkeypatch.setattr(watchlist, "load_watchlist", lambda: {"groups": [], "congress_family": {"enabled": True, "all_members": False}})
    ingestion.poll_all_sources(TODAY, [], full_congress=True)
    health = json.loads(db.get_meta("source_health"))
    assert health["house_clerk"]["ok"] is False and "Clerk down" in health["house_clerk"]["errors"][0]
    assert health["insider_form4"]["ok"] is False and health["watchlist"]["ok"] is True
    assert "senate_targets" not in health  # ed-case is a House member: the Clerk covers him
