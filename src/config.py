"""
src/config.py
Central runtime configuration and hardcoded safety constants.

Risk constants below are plain module constants on purpose: they are not read from
the environment and nothing the LLM returns can change them.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Credentials / environment ---------------------------------------------------
def _secret(name: str) -> str:
    """A key, or "" when unset or still the .env.example placeholder ("your_..._here")."""
    value = os.getenv(name, "").strip()
    return "" if value.startswith("your_") else value


ALPACA_API_KEY = _secret("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _secret("ALPACA_SECRET_KEY")
# Paper unless the value is exactly "false".
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"

ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# Two-step rating (2026-11-25): a cheaper model screens every signal first; only signals it scores
# SCREEN_PASS_SCORE+ go to ANTHROPIC_MODEL for the final score. Set ANTHROPIC_SCREEN_MODEL="" to turn it off.
SCREEN_MODEL = os.getenv("ANTHROPIC_SCREEN_MODEL", "claude-haiku-5-5").strip()
SCREEN_PASS_SCORE = 6   # one point under MIN_CONFIDENCE_TO_TRADE, so the cheaper model can't quietly veto a 7
# Scanned (image-only) House PTRs are read by Claude once each, when they arrive.
PDF_READER_MODEL = os.getenv("ANTHROPIC_PDF_MODEL", "claude-sonnet-5-5")
MAX_PDF_TRANSCRIPTIONS_PER_RUN = 3
MAX_PDF_TRANSCRIPTION_ATTEMPTS = 2

DB_PATH = os.getenv("DB_PATH", "data/ledger.db")
JOURNAL_PATH = os.getenv("JOURNAL_PATH", "logs/trade_journal.md")
COMMITTEES_PATH = os.getenv("COMMITTEES_PATH", "data/committees.json")
WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "data/watchlist.json")

# Every member is mirrored now (CONGRESS_MIRROR_ALL_MEMBERS); this list only gets the closest polling.
# Marjorie Taylor Greene left Congress in January 2026 and was removed on 2026-11-25.
DEFAULT_TARGET_MEMBERS = [
    "nancy-pelosi",
    "ro-khanna",
    "michael-mccaul",
    "sheldon-whitehouse",
]
TARGET_MEMBERS = [
    s.strip() for s in os.getenv("TARGET_MEMBERS", ",".join(DEFAULT_TARGET_MEMBERS)).split(",") if s.strip()
]


def _flag(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Which disclosure sources to ingest (data toggles, not risk controls).
ENABLE_CONGRESS = _flag("ENABLE_CONGRESS")
ENABLE_INSIDERS = _flag("ENABLE_INSIDERS")
ENABLE_EXECUTIVE = _flag("ENABLE_EXECUTIVE")

# Every House member's PTRs are ingested from the Clerk and shown; only these are eligible to be mirrored:
# TARGET_MEMBERS, watchlist groups (incl. Congress family), or everyone when this is true.
CONGRESS_MIRROR_ALL_MEMBERS = _flag("CONGRESS_MIRROR_ALL_MEMBERS", False)
HOUSE_LOOKBACK_DAYS = int(os.getenv("HOUSE_LOOKBACK_DAYS", "45"))

# Optional Tracefour key ("tf_..."). Anonymous tier is 60 requests/hour.
TRACEFOUR_API_KEY = _secret("TRACEFOUR_API_KEY")
MAX_RATE_LIMIT_WAIT_SECONDS = 60   # longer waits fail the source for this run instead of stalling it

# --- Hardcoded risk guardrails (non-modifiable) -----------------------------------
MAX_NOTIONAL_PER_TRADE = 250.00     # hard per-order cap; the broker refuses anything larger
# Each tier trades as its own sleeve (scenario) in the same paper account, with its own cap, so STANDARD and
# HIGH can be compared side by side. Every position is tagged with its sleeve (active_positions.signal_tier).
MAX_SLEEVE_EXPOSURE = 5000.00       # USD open mirror capital per sleeve (sum of that sleeve's open positions).
                                    # Raised from $2,000 on 2026-11-25 for a test; reviewed weekly.
# Corporate insiders (SEC Form 4) and the executive branch (OGE 278-T) trade in their own INSIDER sleeve with its
# own budget, so their fast, frequent filings can't use up the Congress sleeves' money (and each source gets a
# clean scorecard). Set 2026-11-25: the existing positions (almost all insider buys) and the $5,000 budget became
# the INSIDER sleeve; Congress starts fresh in STANDARD/HIGH. Reviewed weekly, separately from Congress.
INSIDER_SLEEVE_EXPOSURE = 5000.00
# When a sleeve is full, an approved buy scored at least this high replaces the sleeve's weakest open position
# (lower score; never one opened today, so no same-day round trip). Keeps the budget in the best-rated trades.
ROTATION_MIN_SCORE = 8
FEED_PRICE_LOOKBACK_DAYS = 120      # filings disclosed this recently get a "price change since trade" each run
MARGIN_ALLOWED = False              # orders are sized against non-marginable (cash) buying power
# Holding periods are long on purpose (2026-11-25): research on insider purchases finds about three quarters of
# the excess return arrives within six months, and the goal is long-term gains, not quick flips.
TIME_STOP_DAYS = 90                 # default holding period (Congress standard tier)

# Two-tier sizing for Congress buys (assigned after the AI scores the signal).
#   STANDARD: $15,001+ disclosed, score 7+, filing lag <= 45 days (15 until 2026-11-27) -> 1x base, 30-day hold
#   HIGH:     $50,001+ disclosed AND (committee match OR score 9-10), filing lag <= 45 days (30 until 2026-11-27) -> 2.5x, 90-day hold
# Filing lag = disclosure date - transaction date.
BASE_NOTIONAL = 100.00
SLEEVE_LABELS = {"STANDARD": "Congress standard", "HIGH": "Congress high-conviction",
                 "INSIDER": "Insiders & executive"}
TIERS = {
    "STANDARD": {"notional": BASE_NOTIONAL, "max_lag_days": 45, "time_stop_days": TIME_STOP_DAYS},
    "HIGH": {"notional": 250.00, "max_lag_days": 45, "time_stop_days": 180},
    "INSIDER": {"notional": BASE_NOTIONAL, "max_lag_days": None, "time_stop_days": 180},
}
# Position size by signal strength (all within MAX_NOTIONAL_PER_TRADE):
# Congress STANDARD buys scale with the disclosed amount (Quiver-style size weighting): (minimum disclosed, $ size).
CONGRESS_SIZE_BY_AMOUNT = [(250_001.00, 250.00), (50_001.00, 175.00), (0.00, BASE_NOTIONAL)]
# Party leaders (Speaker, floor leaders, whips, caucus/conference chairs) and full-committee chairs outperform
# rank-and-file members by a wide margin (Wei & Zhou, NBER w34524), so their $15,001+ buys are high-conviction.
LEADERS_ARE_HIGH_CONVICTION = True
LEADERS_PATH = os.getenv("LEADERS_PATH", "data/congress_leaders.json")
# Insider clusters (2+ distinct insiders buying the same stock within the window) roughly double the excess return.
INSIDER_CLUSTER_MIN_INSIDERS = 2
INSIDER_CLUSTER_WINDOW_DAYS = 14
INSIDER_CLUSTER_NOTIONAL = 250.00
# Executive branch (trustee-managed accounts): weakest evidence, smallest size until it proves itself.
EXECUTIVE_NOTIONAL = 50.00
# Insider sales carry no predictive signal (people sell for taxes, diversification, houses), so they do not close
# a mirrored insider position: it rides to its holding period. Congress and executive sales still exit.
EXIT_ON_INSIDER_SELL = False

# Batch filings (2026-11-25): a filer reporting this many trades on one trade date is an account manager
# rebalancing, not a conviction trade. Those buys are skipped and those sales don't close positions.
BATCH_FILING_MIN_TRADES = 15
# Partial sales ("S (partial)" on House PTRs) are trims or rebalances: positions are kept. Full sales still exit.
EXIT_ON_PARTIAL_SALE = False
# Committee jurisdiction (2026-11-25): every member's committee and subcommittee assignments go to the AI; a buy
# in a company under the member's committee jurisdiction (committee_match) is sized one step up.
COMMITTEES_PATH_SNAPSHOT = os.getenv("COMMITTEES_SNAPSHOT_PATH", "data/congress_committees.json")
# Liquidity floor, checked just before buying. Volume comes from Alpaca's free IEX feed, which sees only a few
# percent of consolidated volume, so $25,000/day on IEX is roughly $1M/day across all exchanges.
MIN_SHARE_PRICE = 2.00
MIN_IEX_DOLLAR_VOLUME = 25_000.00
LIQUIDITY_LOOKBACK_DAYS = 20
# Budget per sleeve (sum of its open positions). The two Congress sleeves share MAX_SLEEVE_EXPOSURE.
SLEEVE_CAPS = {"STANDARD": MAX_SLEEVE_EXPOSURE, "HIGH": MAX_SLEEVE_EXPOSURE, "INSIDER": INSIDER_SLEEVE_EXPOSURE}
HIGH_CONVICTION_MIN_AMOUNT_USD = 50_001.00
HIGH_CONVICTION_MIN_SCORE = 9
CONGRESS_MIN_AMOUNT_USD = 15_001.00         # buys in the $1,001-$15,000 bucket are not signals...
CLUSTER_MIN_MEMBERS = 3                     # ...unless this many members bought the same ticker
CLUSTER_WINDOW_DAYS = 14                    # within this many days of each other (by trade date)
CLUSTER_MIN_AMOUNT_USD = 1_001.00           # the minimum that applies to a cluster member's buy
PDT_EQUITY_THRESHOLD = 25000.00     # below this, no same-day buy->sell on a ticker
MIN_CONFIDENCE_TO_TRADE = 7         # AI score threshold, enforced in code after the LLM responds
MAX_DISCLOSURE_AGE_DAYS = 7         # older disclosures are logged but never traded (no backfill on first run)
PENDING_SIGNAL_TTL_DAYS = 5         # approved buys waiting for an open market expire after this
ALLOWED_ASSET_TYPES = {"Stock"}     # options, bonds, funds etc. are not mirrored
MAX_AI_EVALUATIONS_PER_RUN = 25     # cost cap; newest signals first, the rest wait for the next run
MAX_POSITIONS_PER_TICKER = 1        # a second filer buying the same stock doesn't double the exposure

# Corporate insiders (SEC Form 4, open-market only)
INSIDER_MIN_PURCHASE_USD = 100_000.00   # per filing, summed across its line items
INSIDER_LOOKBACK_DAYS = 3
INSIDER_EXCLUDE_10B5_1 = True           # pre-scheduled plan trades carry no timing information
INSIDER_REQUIRE_OFFICER_OR_DIRECTOR = True  # pure 10% holders (often funds) are not mirrored

# Executive branch (OGE Form 278-T, ranges only, batched, trustee-managed for the President)
EXECUTIVE_MIN_VALUE_LOW_USD = 1_000_001.00  # only the $1M+ buckets are considered
MAX_EXECUTIVE_SIGNALS_PER_BATCH = 10        # largest trades per filing batch go to the AI; the rest are skipped

# Watchlist (politically connected people with public Form 4s; Congress spouses/dependents)
WATCHLIST_MIN_PURCHASE_USD = 10_000.00      # lower bar than general insiders, still open-market and non-10b5-1
# Spouse/dependent trades of members outside TARGET_MEMBERS are mostly routine managed-account activity;
# only the $50,001+ disclosure buckets go to the AI (TARGET_MEMBERS' family trades are not affected).
CONGRESS_FAMILY_MIN_AMOUNT_USD = 50_001.00
CONGRESS_FAMILY_OWNERS = {"Spouse", "Dependent", "Dependent Child", "Child"}
CONGRESS_FULL_POLL_HOURS = 24               # all ~300 members are polled at most this often

INDEX_ETFS_AND_CASH = {
    "SPY", "VOO", "IVV", "QQQ", "DIA", "IWM", "VTI",
    "BIL", "SHV", "SGOV", "TLT", "IEF", "IEI", "SHY",
}

# --- Data source -------------------------------------------------------------------
TRACEFOUR_API = "https://tracefour.com/v1"
TRACEFOUR_BASE = f"{TRACEFOUR_API}/congress"
TRACEFOUR_DELAY_SECONDS = 1.5
HTTP_MAX_RETRIES = 4
HTTP_USER_AGENT = "congressional-trade-agent/1.0"

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# --- Rules version ---------------------------------------------------------------------
# Stamped on every AI decision and every position so results can be attributed to the rules that produced them.
# Bump it whenever a trading rule changes. Rules are frozen for 6-8 weeks from 2026-11-25 to measure them cleanly
# (the weekly budget review is the only planned change).
RULES_VERSION = "2026-11-27.1"  # filing lag 45 days for STANDARD (was 15) and HIGH (was 30): the STOCK Act deadline

# --- Senate and member lists -------------------------------------------------------------
# Every current senator's trades come from Tracefour, polled in rotation so the anonymous tier (60 requests/hour)
# still has room for the insider sources. With TRACEFOUR_API_KEY set, every senator is polled every run.
SENATE_POLL_PER_RUN_ANONYMOUS = 25
# Members who leave Congress (election, resignation) keep being monitored this long after they drop off the
# current-member list, so late filings and sales of positions we mirror from them are still caught.
OUTGOING_MEMBER_MONITOR_DAYS = 90

# --- Portfolio risk limits ------------------------------------------------------------------
# No sector may hold more than this share of the total budget (sum of SLEEVE_CAPS), across all sleeves.
# Sectors come from each company's SEC SIC code (src/sectors.py); an unknown sector never blocks a buy.
SECTOR_MAX_SHARE = 0.25
# Circuit breaker: new buys pause when the whole portfolio trails the same-dollar SPY benchmark by this many
# percentage points (with at least BREAKER_MIN_INVESTED deployed), and resume once it recovers to RESUME level.
# Exits and time stops keep running while paused.
BREAKER_PAUSE_EXCESS_PCT = -15.0
BREAKER_RESUME_EXCESS_PCT = -10.0
BREAKER_MIN_INVESTED = 2000.00

# --- Smarter time stops -----------------------------------------------------------------------
# At its holding period, a position is extended (instead of sold) when a newer buy of the same stock, by any
# Congress member or insider, was disclosed within SUPPORT_WINDOW_DAYS, up to MAX_HOLD_DAYS in total.
TIME_STOP_SUPPORT_WINDOW_DAYS = 60
TIME_STOP_EXTENSION_DAYS = 90
MAX_HOLD_DAYS = 365
