"""Two-step rating: a cheaper model screens first; only promising signals reach the main model."""

from __future__ import annotations

from types import SimpleNamespace

from src import ai_analyst, config
from tests.conftest import good_eval, make_filing


class TwoModelAI:
    def __init__(self, scores):
        self.scores, self.calls = scores, []
        outer = self

        class _Messages:
            def parse(self, **kw):
                outer.calls.append(kw["model"])
                ev = good_eval(outer.scores[kw["model"]]).model_copy(update={"committee_match": False})
                return SimpleNamespace(stop_reason="end_turn", parsed_output=ev)

        self.beta = SimpleNamespace(messages=_Messages())


def test_weak_signal_stops_at_the_cheap_model(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_MODEL", "claude-haiku-5-5")
    ai = TwoModelAI({"claude-haiku-5-5": 4, config.ANTHROPIC_MODEL: 9})
    ev = ai_analyst.evaluate_filing_signal(make_filing(), client=ai)
    assert ai.calls == ["claude-haiku-5-5"] and not ev.should_trade and "screened out" in ev.rationale


def test_promising_signal_gets_the_main_model_score(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_MODEL", "claude-haiku-5-5")
    ai = TwoModelAI({"claude-haiku-5-5": 6, config.ANTHROPIC_MODEL: 8})
    ev = ai_analyst.evaluate_filing_signal(make_filing(), client=ai)
    assert ai.calls == ["claude-haiku-5-5", config.ANTHROPIC_MODEL]
    assert ev.should_trade and ev.confidence_score == 8


def test_screen_outage_falls_back_to_the_main_model(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_MODEL", "claude-haiku-5-5")
    ai = TwoModelAI({config.ANTHROPIC_MODEL: 8})  # the screen model raises KeyError -> treat as unavailable

    def flaky_score(client, model, filing):
        if model == "claude-haiku-5-5":
            raise ai_analyst.AnalystUnavailable("screen down")
        return good_eval(8).model_copy(update={"committee_match": False})

    monkeypatch.setattr(ai_analyst, "_score", flaky_score)
    ev = ai_analyst.evaluate_filing_signal(make_filing(), client=ai)
    assert ev.should_trade and ev.confidence_score == 8
