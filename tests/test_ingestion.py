from __future__ import annotations

from types import SimpleNamespace

from src import ingestion

RAW = {
    "amountLabel": "$50,001 - $100,000", "amountMax": 100000, "amountMid": 75001, "amountMin": 50001,
    "assetDescription": "Warner Bros Discover Inc", "assetType": "Stock", "chamber": "house",
    "disclosureDate": "2026-11-09",
    "filingId": "nancy-pelosi|WBD|2026-10-07|Sale|$50,001 - $100,000|Spouse|Stock",
    "memberName": "Nancy Pelosi", "memberSlug": "nancy-pelosi", "owner": "Spouse", "party": "D",
    "sourceLink": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035780.pdf",
    "ticker": "WBD", "transactionDate": "2026-10-07", "type": "Sale",
}
MEMBER = {"slug": "nancy-pelosi", "memberName": "Nancy Pelosi", "chamber": "house", "party": "D"}


def test_normalize_real_tracefour_shape():
    t = ingestion.normalize_trade(RAW, "nancy-pelosi", MEMBER, ["Intelligence"])
    assert t["trade_uid"] == RAW["filingId"]
    assert t["action"] == "SELL"
    assert t["ticker"] == "WBD"
    assert t["transaction_date"] == "2026-10-07" and t["disclosure_date"] == "2026-11-09"
    assert t["chamber"] == "House" and t["party"] == "D" and t["owner"] == "Spouse"
    assert t["committees"] == ["Intelligence"]


def test_action_mapping():
    assert ingestion.normalize_action("Purchase") == "BUY"
    assert ingestion.normalize_action("Sale (Partial)") == "SELL"
    assert ingestion.normalize_action("Sale (Full)") == "SELL"
    assert ingestion.normalize_action("Exchange") is None


def test_rejects_unusable_rows():
    assert ingestion.normalize_trade({**RAW, "ticker": "--"}, "x", {}, []) is None
    assert ingestion.normalize_trade({**RAW, "ticker": ""}, "x", {}, []) is None
    assert ingestion.normalize_trade({**RAW, "type": "Exchange"}, "x", {}, []) is None
    assert ingestion.normalize_trade({**RAW, "transactionDate": None}, "x", {}, []) is None


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, ""

    def json(self):
        return self._body


def test_fetch_parses_nested_data(monkeypatch):
    body = {"data": {"member": MEMBER, "trades": [RAW, {**RAW, "ticker": "N/A"}]}, "meta": {}}
    monkeypatch.setattr(ingestion._session, "get", lambda url, timeout: _Resp(200, body))
    trades = ingestion.fetch_member_trades_tracefour("nancy-pelosi")
    assert len(trades) == 1 and trades[0]["ticker"] == "WBD"


def test_fetch_404_returns_empty(monkeypatch):
    monkeypatch.setattr(ingestion._session, "get", lambda url, timeout: _Resp(404))
    assert ingestion.fetch_member_trades_tracefour("nobody") == []


def test_fetch_retries_429_then_succeeds(monkeypatch):
    responses = [_Resp(429, headers={"Retry-After": "1"}), _Resp(503), _Resp(200, {"data": {"trades": [RAW]}})]
    monkeypatch.setattr(ingestion._session, "get", lambda url, timeout: responses.pop(0))
    assert len(ingestion.fetch_member_trades_tracefour("nancy-pelosi")) == 1


def test_fetch_gives_up_gracefully(monkeypatch):
    monkeypatch.setattr(ingestion._session, "get", lambda url, timeout: _Resp(429))
    assert ingestion.fetch_member_trades_tracefour("nancy-pelosi") == []
