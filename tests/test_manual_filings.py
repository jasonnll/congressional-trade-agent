"""Manual transcriptions (data/manual_filings/<DocID>.json) fill in scanned paper PTRs the parser can't read."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src import database as db, house_clerk
from src.ai_analyst import build_user_payload
from src.main import ingest

TODAY = date(2026, 11, 24)
KHANNA_DOC = {"DocID": "9116399", "First": "Rohit", "Last": "Khanna", "StateDst": "CA17", "FilingType": "P",
              "FilingDate": "11/05/2026"}
LEGISLATORS = {"house:CA17": {"slug": "ro-khanna", "last": "khanna", "name": "Ro Khanna", "party": "D", "chamber": "House"}}


def test_khanna_transcription_loads_only_rows_with_a_readable_type():
    manual = house_clerk.manual_transcription("9116399")
    rows = manual["rows"]
    raw = json.loads((house_clerk.MANUAL_DIR / "9116399.json").read_text())["rows"]
    assert len(rows) == len(raw) and all(r["action"] in ("BUY", "SELL") for r in rows)
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert (aapl["owner"], aapl["action"], aapl["transaction_date"], aapl["amount_range"]) == \
        ("Dependent", "BUY", "2026-10-02", "$50,001 - $100,000")
    assert next(r for r in rows if r["ticker"] == "CART")["tx_code"] == "S (partial)"
    assert house_clerk.manual_transcription("does-not-exist") is None


def test_scanned_filing_is_ingested_from_its_transcription(monkeypatch):
    monkeypatch.setattr(house_clerk, "fetch_index", lambda year: [KHANNA_DOC] if year == 2026 else [])
    monkeypatch.setattr(house_clerk, "load_legislators", lambda: LEGISLATORS)
    monkeypatch.setattr(house_clerk, "_get", lambda url, timeout=60: (Path(__file__).parent / "fixtures" / "9116399.pdf").read_bytes())
    filings = house_clerk.poll_house(TODAY)
    assert db.get_document("9116399")["status"] == "NO_TEXT"
    assert filings and all(f["signal_context"]["source"] == "manual_transcription" for f in filings)
    assert {f["member_slug"] for f in filings} == {"ro-khanna"} and filings[0]["party"] == "D"
    assert "manual OCR transcription" in build_user_payload(filings[0])

    # A scanned filing without a transcription still yields nothing (and stays on the review list).
    monkeypatch.setattr(house_clerk, "manual_transcription", lambda doc_id: None)
    assert house_clerk.poll_house(TODAY) == []


def test_old_transcribed_trades_are_recorded_but_not_traded(monkeypatch):
    rows = house_clerk.manual_transcription("9116399")["rows"]
    doc = {**KHANNA_DOC, "filed": "2026-11-05", "year": 2026}
    filings = house_clerk.normalize_house_rows(doc, rows, LEGISLATORS, source="manual_transcription")
    counts = ingest(filings, TODAY, [])
    statuses = {r["execution_status"] for r in db.recent_filings(500) if r["member_slug"] == "ro-khanna"}
    assert statuses == {"STALE"}  # filed 19 days ago: past the 7-day window
    assert counts["new"] + counts["duplicates"] == len(filings)
