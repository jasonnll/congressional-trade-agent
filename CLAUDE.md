# CLAUDE.md - System Development Guidelines

## Hardcoded System Safety Guardrails (Non-Modifiable)
These are enforced in code (`src/config.py`, `src/broker.py`, `src/main.py`, `src/ai_analyst.enforce_rules`). They CANNOT be overridden by LLM output or environment variables.

1. **Idempotency**: a `trade_uid` (Tracefour `filingId`) is recorded once in `processed_filings` and acted on at most once. Broker orders also carry a deterministic `client_order_id`.
2. **Position limits**:
   - Long holds by design (since 2026-11-25; never under 60 days): Congress STANDARD 90 days, Congress HIGH 180 days, INSIDER 180 days (`config.TIERS`). Open positions are extended to their sleeve's hold in `database.init_db`.
   - Size by evidence (`main.position_size`, all <= `MAX_NOTIONAL_PER_TRADE` = $250, a hard cap the broker enforces): Congress STANDARD scales with the disclosed amount (`CONGRESS_SIZE_BY_AMOUNT`: $100 / $175 for $50,001+ / $250 for $250,001+); HIGH $250; insiders $100, or `INSIDER_CLUSTER_NOTIONAL` $250 when 2+ insiders bought within `INSIDER_CLUSTER_WINDOW_DAYS`; executive branch `EXECUTIVE_NOTIONAL` $50.
   - HIGH (Congress only): $50,001+ disclosed and committee match or score 9+, OR any $15,001+ buy by a current party leader or full-committee chair (`LEADERS_ARE_HIGH_CONVICTION`; list from `house_clerk.load_leaders`, refreshed daily, fallback `data/congress_leaders.json`).
   - Congress buys need $15,001+ disclosed and a filing lag of <= 15 days (STANDARD) or <= 30 days (HIGH). A cluster of 3+ members buying the same ticker within 14 days lowers the minimum to $1,001 and makes the buy eligible even from non-target members.
   - Sells have no minimums: any sale of a mirrored ticker by the filer we mirrored exits the position, whatever its size or age.
   - Sleeves are separate budgets (`config.SLEEVE_CAPS`): Congress STANDARD and HIGH use `MAX_SLEEVE_EXPOSURE`; insider and executive-branch buys trade only in the INSIDER sleeve, capped at `INSIDER_SLEEVE_EXPOSURE` = $5,000 (since 2026-11-25; the existing insider positions moved there), so the sources never share money.
   - `MAX_SLEEVE_EXPOSURE` = $5,000.00 per Congress sleeve (STANDARD and HIGH each), measured as the sum of that sleeve's OPEN ledger positions. Raised from $2,000 on 2026-11-25 for a test and reviewed weekly; change it only in `src/config.py`.
   - `MARGIN_ALLOWED` = False. Buys are sized against non-marginable (cash) buying power.
3. **Market hours gating**: buys and exits are only sent when `broker.is_market_open()` is True. Otherwise they stay queued.
4. **PDT safeguard**: when equity < $25,000, a position opened today (ET) is never sold today.
5. **Execution mode**: paper trading by default. Live only when `ALPACA_PAPER=false`.
6. **AI cannot force trades**: the model's `should_trade` is ANDed with code rules (BUY only, not index/cash, score >= `MIN_CONFIDENCE_TO_TRADE`).
7. **No backfill**: disclosures older than `MAX_DISCLOSURE_AGE_DAYS` are logged as STALE and never traded. Approved buys expire after `PENDING_SIGNAL_TTL_DAYS`.
8. **AI cost cap**: at most `MAX_AI_EVALUATIONS_PER_RUN` evaluations per run; multi-member clusters first, then the largest disclosed amounts, then newest (`main._ai_priority`); the rest wait.
9. **Insider filters** (SEC Form 4): open-market purchases only, officers/directors only, no 10b5-1 plan trades, at least `INSIDER_MIN_PURCHASE_USD` per filing.
10. **Executive filters** (OGE 278-T): only `$1M+` buckets, and only the `MAX_EXECUTIVE_SIGNALS_PER_BATCH` largest buys per filing batch reach the AI.
11. **Exits follow the filer**: a later sale by the same Congress member or executive filer closes the mirrored position (POLITICIAN_SELL / EXECUTIVE_SELL). Insider sales do NOT (`EXIT_ON_INSIDER_SELL` = False since 2026-11-25): research finds they carry no signal, so insider positions ride to their holding period.

12. **Non-discretionary trades are not signals**: PTR descriptions matching `house_clerk.NON_DISCRETIONARY` (dividend reinvestment, automatic plans, mergers, spin-offs, vesting, gifts) are SKIPPED.
14. **One position per stock** (`MAX_POSITIONS_PER_TICKER`): when several filers buy the same ticker, the highest-scored signal is bought and the rest are SKIPPED.
15. **Congress-family bar** (`CONGRESS_FAMILY_MIN_AMOUNT_USD`): spouse/dependent trades of members outside `TARGET_MEMBERS` below $50,001 are SKIPPED before any AI call (also applied to rows already queued).
13. **Only mirror who we follow**: House trades for every member are recorded and shown; members outside `TARGET_MEMBERS` and the watchlist are `MONITORED` unless `CONGRESS_MIRROR_ALL_MEMBERS=true` (set in the GitHub workflow since 2026-11-25).
16. **Best-rated trades get the budget**: buys execute highest AI score first (ties: HIGH tier, larger amount, newer). When a sleeve is full, a buy scored `ROTATION_MIN_SCORE`+ sells that sleeve's weakest strictly-lower-scored position (`ROTATED_OUT`), never one opened today.
17. **Duplicate cleanup**: extra open positions in the same ticker and sleeve (from before rule 14) are exited as `DUPLICATE_TICKER`, keeping the highest-scored one.

## Data accuracy rules
- A source that cannot be read must never look like "no new trades". Every fetch failure goes into `ingestion.failures`; `poll_all_sources` records per-source health in `meta.source_health`, the journal logs it, and the dashboard shows a red banner.
- Congressional trades are deduplicated across sources and amendments by `database.dedupe_key` (member last name, ticker, trade date, action, amount).
- Parser changes must keep `tests/test_house_clerk.py` passing against the real PDFs in `tests/fixtures/`.

## Data sources
- `congress` House: **House Clerk (primary source)**: `{year}FD.zip` index + one PTR PDF per filing (`src/house_clerk.py`), every member, cached per DocID in `source_documents`. Scanned paper filings are `NO_TEXT` and listed for manual review.
- `congress` Senate: Tracefour `/v1/congress/{slug}` for Senate members of `TARGET_MEMBERS`. Senate eFD (the primary source) requires accepting its usage terms and is not used.
- Member names/party: `unitedstates/congress-legislators` (cached daily in `meta.legislators`).
- The sources below are Tracefour (CC BY 4.0; anonymous tier 60 requests/hour, `TRACEFOUR_API_KEY` raises it).
- `insider`: `/v1/filings?direction=P` (market-wide Form 4 open-market rows, grouped per filing) plus `/v1/clusters` as AI context; `direction=S` is fetched only for tickers we mirror from an insider.
- `executive`: every `/v1/trackers` entry with `archetype == "oge_278t"` (currently the President's trustee-managed accounts).
- `member_slug`/`member_name` identify the filer for all sources; `filer_type` says which. Ledger columns added later are migrated in `database.init_db`.

## Watchlist subset (`src/watchlist.py`, `data/watchlist.json`)
- Named people (Trump family, Lutnick family by default) are followed through their SEC Form 4s via `/v1/insiders/{slug}`; `query` entries resolve automatically once that person files. Their trades carry `watchlist_group`, skip the officer/director requirement and use `WATCHLIST_MIN_PURCHASE_USD`.
- Congress family = PTR rows whose `owner` is in `CONGRESS_FAMILY_OWNERS`. TARGET_MEMBERS are polled fully every run; every other member is swept at most every `CONGRESS_FULL_POLL_HOURS` (last sweep in `meta.congress_full_poll_at`), keeping only family trades and sales that exit a mirrored position.
- Only public disclosures exist for these people. Anyone without a filing obligation (adult children of members, private individuals) cannot be monitored; the dashboard shows them as "no SEC filings".

## Coding Standards & Conventions
- Python 3.11 in CI; code stays compatible with 3.9 (`from __future__ import annotations`) for local runs.
- Full type annotations; Pydantic v2 models for LLM output.
- Network calls use exponential backoff. HTTP 429 and 404 log a warning and fail gracefully without crashing the pipeline.
- Logging format: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`.
- Claude model: `claude-sonnet-5-5` (override with `ANTHROPIC_MODEL`), structured output via `client.beta.messages.parse(output_format=TradeEvaluation)` with server-side refusal fallbacks.
- Tracefour data is CC BY 4.0: anything that displays it must link to tracefour.com or the record's source link.

## Performance tracking (`src/performance.py`)
- Runs at the end of every pipeline run that scores or trades (never blocks trading if it fails). Needs Alpaca keys; market data is the free IEX feed, split/dividend-adjusted daily closes.
- Position returns use the broker's average fill price (entry and exit), never disclosure prices. Positions closed outside the bot (`MANUAL_CLOSE`) are priced from the daily close and flagged `exit_price_estimated`.
- Benchmark = the same dollars in SPY over the same dates (entry fill day -> exit day or today). Win rate counts closed positions only.
- `performance_snapshots` is rebuilt from daily closes every run, so missed runs don't distort the history.
- `signal_outcomes` scores every AI decision (approved and rejected) from the close on/after the decision date to up to `TIME_STOP_DAYS` later, to show whether the AI filter adds value.

## Commands
- Tests: `.venv/bin/python -m pytest -q`
- Pipeline: `python -m src.main` (`--no-trade`, `--ingest-only`)
- Dashboard: `python -m src.dashboard` then open http://127.0.0.1:8765
