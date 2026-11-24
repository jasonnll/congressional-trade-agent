from __future__ import annotations

import anthropic
import httpx
import pytest

from src import ai_analyst, config
from src.ai_analyst import AnalystUnavailable, TradeEvaluation, enforce_rules, evaluate_filing_signal
from tests.conftest import FakeAI, good_eval, make_filing


def test_index_etf_rejected_without_llm():
    ai = FakeAI(good_eval())
    ev = evaluate_filing_signal(make_filing(ticker="SPY"), client=ai)
    assert ev.is_index_or_cash and not ev.should_trade
    assert ai.calls == []


def test_sell_never_opens_position_and_skips_llm():
    ai = FakeAI(good_eval())
    ev = evaluate_filing_signal(make_filing(action="SELL"), client=ai)
    assert not ev.should_trade and ai.calls == []


def test_high_confidence_buy_passes():
    ai = FakeAI(good_eval(8))
    ev = evaluate_filing_signal(make_filing(), client=ai)
    assert ev.should_trade and ev.confidence_score == 8
    call = ai.calls[0]
    assert call["model"] == config.ANTHROPIC_MODEL
    assert call["output_format"] is TradeEvaluation
    assert "Reporting Delay (days): 10" in call["messages"][0]["content"]


def test_llm_cannot_force_trade_below_threshold():
    ev = enforce_rules(make_filing(), good_eval(6))
    assert not ev.should_trade


def test_llm_cannot_force_trade_on_sell_or_index():
    assert not enforce_rules(make_filing(action="SELL"), good_eval(9)).should_trade
    assert not enforce_rules(make_filing(ticker="QQQ"), good_eval(9)).should_trade


def test_score_is_clamped():
    assert enforce_rules(make_filing(), good_eval(42)).confidence_score == 10


def test_refusal_is_a_rejection():
    ev = evaluate_filing_signal(make_filing(), client=FakeAI(good_eval(), stop_reason="refusal"))
    assert not ev.should_trade


def test_missing_key_defers(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(AnalystUnavailable):
        evaluate_filing_signal(make_filing())


def test_api_errors_defer():
    class Boom:
        class beta:
            class messages:
                @staticmethod
                def parse(**kwargs):
                    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                    raise anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)

    with pytest.raises(AnalystUnavailable):
        evaluate_filing_signal(make_filing(), client=Boom())


def test_latency_calculation():
    assert ai_analyst.reporting_latency_days(make_filing(transaction_date="2026-10-01", disclosure_date="2026-11-15")) == 45
