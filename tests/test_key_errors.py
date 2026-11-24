"""A rejected key must be reported as a rejected key: never as a closed market, never retried pointlessly."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
from alpaca.common.exceptions import APIError

from src import broker, config, database as db, ingestion
from src.main import run_pipeline
from tests.conftest import FakeBroker, make_filing

NOW = datetime.now(timezone.utc)
FRESH = (NOW - timedelta(days=1)).date().isoformat()


def _alpaca_401():
    return APIError("<html><center><h1>401 Authorization Required</h1></center></html>",
                    http_error=SimpleNamespace(response=SimpleNamespace(status_code=401)))


class RejectingBroker(FakeBroker):
    def get_clock(self):
        raise _alpaca_401()


def test_rejected_alpaca_key_is_not_reported_as_market_closed():
    broker.set_trading_client(RejectingBroker())
    run_pipeline(filings=[], now=NOW, use_ai=False)
    journal = Path(config.JOURNAL_PATH).read_text()
    assert "Market closed" not in journal
    assert "Alpaca rejected the API keys (HTTP 401)" in journal and "start with PK" in journal
    health = json.loads(db.get_meta("source_health"))
    assert health["alpaca"]["ok"] is False and "rejected" in health["alpaca"]["errors"][0]


def test_working_alpaca_is_recorded_healthy():
    broker.set_trading_client(FakeBroker(market_open=False))
    run_pipeline(filings=[], now=NOW, use_ai=False)
    assert json.loads(db.get_meta("source_health"))["alpaca"]["ok"] is True
    assert "Market closed" in Path(config.JOURNAL_PATH).read_text()


class RejectingAI:
    def __init__(self):
        self.calls = 0
        outer = self

        class _Messages:
            def parse(self, **kwargs):
                outer.calls += 1
                req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                raise anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(401, request=req), body=None)

        self.beta = SimpleNamespace(messages=_Messages())


def test_rejected_anthropic_key_stops_after_first_call():
    broker.set_trading_client(FakeBroker())
    filings = [make_filing(trade_uid=f"u{i}", ticker=f"T{chr(65 + i)}", disclosure_date=FRESH,
                           transaction_date=FRESH) for i in range(6)]
    ai = RejectingAI()
    stats = run_pipeline(filings=filings, ai_client=ai, now=NOW, trade=False)
    assert ai.calls == 1 and stats["evaluated"] == 0
    assert len(db.get_filings_by_status("AI_RETRY")) == 1 and len(db.get_filings_by_status("AWAITING_AI")) == 5
    health = json.loads(db.get_meta("source_health"))
    assert health["anthropic"]["ok"] is False and "rejected ANTHROPIC_API_KEY" in health["anthropic"]["errors"][0]
    assert "6 signal(s) wait" in Path(config.JOURNAL_PATH).read_text()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers, self.text = status, body, {}, ""

    def json(self):
        return self._body


def test_rejected_tracefour_key_falls_back_to_anonymous(monkeypatch):
    monkeypatch.setattr(config, "TRACEFOUR_API_KEY", "tf_bad")
    ingestion._session.headers["Authorization"] = "Bearer tf_bad"
    ingestion.key_warnings.clear()
    seen_auth = []

    def get(url, timeout):
        has_key = "Authorization" in ingestion._session.headers
        seen_auth.append(has_key)
        return _Resp(401) if has_key else _Resp(200, {"data": {"trades": []}})

    monkeypatch.setattr(ingestion._session, "get", get)
    assert ingestion.get_json_with_retry(f"{config.TRACEFOUR_API}/congress/x") == {"data": {"trades": []}}
    assert seen_auth == [True, False]
    assert ingestion.failures == [] or all("rejected" not in f for f in ingestion.failures)
    assert "TRACEFOUR_API_KEY rejected (HTTP 401)" in ingestion.key_warnings[0]
