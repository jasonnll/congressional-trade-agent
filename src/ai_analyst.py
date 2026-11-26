"""
src/ai_analyst.py
LLM signal evaluation using Claude structured outputs.

The model only scores. The final should_trade decision is recomputed in code from
hardcoded rules, so the LLM can veto a trade but can never force one.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, Optional

import anthropic
from pydantic import BaseModel, Field

from src import config

logger = logging.getLogger(__name__)

# Models that accept the server-side `fallbacks: "default"` form.
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}


# Calls and tokens per model in the current pipeline run (reset by main.run_pipeline, written to the journal).
USAGE: Dict[str, Dict[str, int]] = {}


def record_usage(model: str, response: Any) -> None:
    u = getattr(response, "usage", None)
    m = USAGE.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    m["calls"] += 1
    m["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
    m["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)


def usage_line() -> str:
    if not USAGE:
        return "no AI calls"
    return "; ".join(f"{model}: {u['calls']} calls, {u['input_tokens']:,} in / {u['output_tokens']:,} out tokens"
                     for model, u in sorted(USAGE.items()))


class AnalystUnavailable(Exception):
    """Transient failure (missing key, API error). The filing is retried on the next run."""


class AnalystAuthError(AnalystUnavailable):
    """The key itself was rejected; every further call this run would fail the same way."""


class TradeEvaluation(BaseModel):
    should_trade: bool = Field(description="Final binary decision whether to mirror this trade.")
    confidence_score: int = Field(description="Confidence rating from 1 to 10 based on trade conviction.")
    committee_match: bool = Field(description="True if trade maps to member's committee assignments.")
    is_index_or_cash: bool = Field(description="True if ticker is a broad index ETF, cash equivalent, or treasury.")
    rationale: str = Field(description="Concise 2-sentence rationale for the trade determination.")


SYSTEM_PROMPT = """You are a senior hedge fund compliance officer and quantitative trade analyst who evaluates public U.S. trade disclosures as mirror-trading signals.

Each request is one disclosed BUY from one of three filer types. Return a recommendation (`should_trade`) and a confidence score from 1 to 10. Every disclosure you see is already public; you are judging whether following it is likely to be worthwhile, not whether it was proper.

Rules for every filer type:
1. EXCLUSIONS: Broad index ETFs, money market funds, and Treasury bills are not signals. Set `is_index_or_cash` to true and `should_trade` to false.
2. THRESHOLD: `should_trade` is true only if confidence_score >= 7.
3. Keep the rationale to two sentences, naming the factors that moved the score.

CONGRESS (STOCK Act periodic transaction reports, filed up to 45 days after the trade, amounts in ranges):
- Latency penalty from the reporting delay you are given: <= 15 days high quality; 16-30 days reduce confidence by 2; > 30 days reduce confidence by 4.
- Committee relevance: if the company's sector maps directly to the member's committee assignments (e.g. Armed Services and defense/aerospace; Energy & Commerce and tech/semiconductors/utilities), add 2 and set `committee_match` true. If committees are not listed, rely on your knowledge of the member's assignments only when confident; otherwise set `committee_match` false.
- Larger ranges and purchases by the member directly (rather than a spouse or dependent) are modestly stronger.
- `committee_match` also decides position size (a match on a $50,001+ buy doubles-plus the allocation), so set it true only for a direct, specific link between the company's business and the committee's jurisdiction, never for a loose sector association.
- Multi-member cluster: when the payload shows several members buying the same stock within two weeks, treat it as strong corroboration (+2), and stronger still when the buyers span both parties. A cluster can make even a small individual purchase meaningful.

CORPORATE INSIDER (SEC Form 4 open-market purchase, filed within 2 business days, exact share counts and prices):
- Open-market purchases by officers and directors are among the strongest public signals because insiders sell for many reasons but buy for one.
- Seniority: CEO/CFO strongest, then other executive officers, then directors.
- Size: weigh the dollar value and especially the position change percentage (a purchase that grows the insider's holding substantially shows more conviction than a token add).
- Cluster buying: if several distinct insiders are buying the same stock in the same window, add 2.
- Small or micro-cap issuers with tiny share prices are noisier; be more skeptical.
- `committee_match` does not apply; set it false.
- Reporting delay over 5 days is unusual for Form 4; reduce confidence by 2.

EXECUTIVE BRANCH (OGE Form 278-T periodic transaction reports, filed in monthly batches weeks after the trades, amounts in ranges):
- Trades in trustee-managed accounts reflect a manager's decisions, not necessarily the official's; treat them as a weaker signal than direct trades. Start from a lower base.
- Only very large, concentrated purchases in individual companies deserve a passing score.
- Apply the same latency penalty as Congress.
- Set `committee_match` true only if the company's business directly depends on policy the official controls, and say so in the rationale.

WATCHLIST (applies on top of the filer-type rules when a "Watchlist" line is present):
- The filer is close to senior officials: a relative of a cabinet member or the President, or the spouse or dependent child of a member of Congress. Score the trade with the rules for its filer type first.
- Add 1 only when the company's business depends directly on policy, contracts, regulation, or trade decisions the connected official influences, and name that link in the rationale. Proximity alone adds nothing.
- Family trades in companies the family itself runs (e.g. a director buying stock of the company they direct) are ordinary insider trades; judge them as such."""

_FILER_LABELS = {
    "congress": "CONGRESS (STOCK Act PTR)",
    "insider": "CORPORATE INSIDER (SEC Form 4)",
    "executive": "EXECUTIVE BRANCH (OGE Form 278-T)",
}


def _reject(rationale: str, score: int = 0, index_or_cash: bool = False) -> TradeEvaluation:
    return TradeEvaluation(should_trade=False, confidence_score=score, committee_match=False,
                           is_index_or_cash=index_or_cash, rationale=rationale)


def reporting_latency_days(filing: Dict[str, Any]) -> int:
    return (date.fromisoformat(filing["disclosure_date"]) - date.fromisoformat(filing["transaction_date"])).days


def _context(filing: Dict[str, Any]) -> Dict[str, Any]:
    ctx = filing.get("signal_context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except ValueError:
            ctx = None
    return ctx if isinstance(ctx, dict) else {}


def build_user_payload(filing: Dict[str, Any]) -> str:
    filer_type = filing.get("filer_type") or "congress"
    ctx = _context(filing)
    lines = [f"Filer Type: {_FILER_LABELS.get(filer_type, filer_type)}"]
    if filing.get("watchlist_group"):
        who = ctx.get("watchlist_label") or filing["member_name"]
        lines.append(f"Watchlist: {filing['watchlist_group']} ({who})")

    if filer_type == "congress":
        committees = filing.get("committees") or []
        lines += [
            f"Member: {filing['member_name']} ({filing.get('party') or '?'}, {filing.get('filer_role') or filing.get('chamber') or '?'})",
            f"Committees: {', '.join(committees) if committees else 'Not provided'}",
        ]
        cluster = ctx.get("congress_cluster") or {}
        if cluster.get("members", 0) > 1:
            lines.append(f"Multi-Member Cluster: {cluster['members']} members of Congress bought this stock within "
                         f"14 days ({'bipartisan' if cluster.get('bipartisan') else 'one party'}): "
                         f"{', '.join(cluster.get('names') or [])}")
    else:
        lines.append(f"Filer: {filing['member_name']} | Role: {filing.get('filer_role') or 'Unknown'}")

    lines += [
        f"Ticker: {filing['ticker']}",
        f"Company: {filing.get('asset_description') or 'Unknown'}",
        f"Action: {filing['action']}",
        f"Amount: {filing.get('amount_range') or 'Unknown'}",
        f"Owner: {filing.get('owner') or 'Unknown'}",
        f"Transaction Date: {filing['transaction_date']}",
        f"Disclosure Date: {filing['disclosure_date']}",
        f"Reporting Delay (days): {reporting_latency_days(filing)}",
    ]

    if ctx.get("source") == "manual_transcription":
        lines.append("Data Source: manual OCR transcription of a scanned paper filing; amounts may be missing or misread")

    if filer_type == "insider":
        lines += [
            f"Shares Bought: {ctx.get('shares'):,.0f} at avg ${ctx.get('avg_price')}" if ctx.get("shares") else "Shares Bought: Unknown",
            f"Holding After: {ctx.get('shares_owned_after'):,.0f} shares" if ctx.get("shares_owned_after") else "Holding After: Unknown",
            f"Position Change: +{ctx['position_change_pct']}%" if ctx.get("position_change_pct") is not None else "Position Change: Unknown (new or unreported holding)",
        ]
        cluster = ctx.get("cluster")
        if cluster:
            mix = cluster.get("seniority_mix") or {}
            lines.append(
                f"Cluster Buying: {cluster.get('insider_count')} distinct insiders buying "
                f"(${(cluster.get('total_value') or 0):,.0f} total, {cluster.get('first_filed')} to {cluster.get('last_filed')}; "
                f"CEO/CFO {mix.get('ceoCfo', 0)}, other C-suite {mix.get('cSuite', 0)}, directors {mix.get('director', 0)})"
            )
        else:
            lines.append("Cluster Buying: none detected")
    elif filer_type == "executive":
        if (ctx.get("duplicate_lines") or 1) > 1:
            lines.append(f"Repeated Lines: same trade reported {ctx['duplicate_lines']} times (multiple accounts)")

    return "\n".join(lines)


def enforce_rules(filing: Dict[str, Any], ev: TradeEvaluation) -> TradeEvaluation:
    """Deterministic post-processing: the model may say no, but code decides yes."""
    score = max(0, min(10, int(ev.confidence_score)))
    should = (
        ev.should_trade
        and not ev.is_index_or_cash
        and filing["action"] == "BUY"
        and filing["ticker"].upper() not in config.INDEX_ETFS_AND_CASH
        and score >= config.MIN_CONFIDENCE_TO_TRADE
    )
    return ev.model_copy(update={"should_trade": should, "confidence_score": score})


def evaluate_filing_signal(filing: Dict[str, Any], client: Optional[Any] = None) -> TradeEvaluation:
    """Scores one filing. Raises AnalystUnavailable on transient failures so the caller can retry later."""
    ticker = filing["ticker"].upper()

    if ticker in config.INDEX_ETFS_AND_CASH:
        return _reject(f"Ticker {ticker} is an excluded index ETF or cash equivalent.", score=1, index_or_cash=True)
    if filing["action"] != "BUY":
        return _reject("Only BUY disclosures open new mirror positions.")

    if client is None:
        if not config.ANTHROPIC_API_KEY:
            raise AnalystAuthError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4)

    screen_model = config.SCREEN_MODEL
    if screen_model and screen_model != config.ANTHROPIC_MODEL:
        try:
            screen = _score(client, screen_model, filing)
        except AnalystAuthError:
            raise
        except AnalystUnavailable as e:  # the screen is a cost saver; if it's down, the main model decides
            logger.warning("Screening model unavailable, using %s directly: %s", config.ANTHROPIC_MODEL, e)
            screen = None
        if screen is not None and screen.confidence_score < config.SCREEN_PASS_SCORE:
            screen.rationale = f"[screened out by {screen_model}] {screen.rationale}"
            return enforce_rules(filing, screen)
    return enforce_rules(filing, _score(client, config.ANTHROPIC_MODEL, filing))


def _score(client: Any, model: str, filing: Dict[str, Any]) -> TradeEvaluation:
    """One structured scoring call; raises AnalystUnavailable / AnalystAuthError like evaluate_filing_signal."""
    kwargs: Dict[str, Any] = {}
    if model in _FALLBACK_MODELS:
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    try:
        response = client.beta.messages.parse(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_payload(filing)}],
            output_format=TradeEvaluation,
            **kwargs,
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise AnalystAuthError(
            f"Anthropic rejected ANTHROPIC_API_KEY (HTTP {e.status_code}). Check it is pasted without quotes or spaces"
        ) from e
    except anthropic.NotFoundError as e:
        raise AnalystUnavailable(f"Model not found ({model}): {e}") from e
    except anthropic.RateLimitError as e:
        raise AnalystUnavailable(f"Rate limited: {e}") from e
    except anthropic.APIStatusError as e:
        raise AnalystUnavailable(f"API error {e.status_code}: {e}") from e
    except anthropic.APIConnectionError as e:
        raise AnalystUnavailable(f"Connection error: {e}") from e

    record_usage(model, response)
    if response.stop_reason == "refusal":
        return _reject("Model declined to evaluate this filing.")
    parsed = response.parsed_output
    if parsed is None:
        raise AnalystUnavailable(f"No structured output (stop_reason={response.stop_reason})")
    return parsed
