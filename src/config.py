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

DB_PATH = os.getenv("DB_PATH", "data/ledger.db")
JOURNAL_PATH = os.getenv("JOURNAL_PATH", "logs/trade_journal.md")
COMMITTEES_PATH = os.getenv("COMMITTEES_PATH", "data/committees.json")
WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "data/watchlist.json")

DEFAULT_TARGET_MEMBERS = [
    "nancy-pelosi",
    "marjorie-taylor-greene",
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
TIME_STOP_DAYS = 30                 # default holding period (standard tier, insiders, executive)

# Two-tier sizing for Congress buys (assigned after the AI scores the signal).
#   STANDARD: $15,001+ disclosed, score 7+, filing lag <= 15 days -> 1x base, 30-day hold
#   HIGH:     $50,001+ disclosed AND (committee match OR score 9-10), filing lag <= 30 days -> 2.5x, 90-day hold
# Filing lag = disclosure date - transaction date.
BASE_NOTIONAL = 100.00
SLEEVE_LABELS = {"STANDARD": "Congress standard", "HIGH": "Congress high-conviction",
                 "INSIDER": "Insiders & executive"}
TIERS = {
    "STANDARD": {"notional": BASE_NOTIONAL, "max_lag_days": 15, "time_stop_days": TIME_STOP_DAYS},
    "HIGH": {"notional": 250.00, "max_lag_days": 30, "time_stop_days": 90},
    "INSIDER": {"notional": BASE_NOTIONAL, "max_lag_days": None, "time_stop_days": TIME_STOP_DAYS},
}
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
