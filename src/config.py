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
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# Paper unless the value is exactly "false".
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
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
TRACEFOUR_API_KEY = os.getenv("TRACEFOUR_API_KEY", "")
MAX_RATE_LIMIT_WAIT_SECONDS = 60   # longer waits fail the source for this run instead of stalling it

# --- Hardcoded risk guardrails (non-modifiable) -----------------------------------
MAX_NOTIONAL_PER_TRADE = 100.00     # USD per buy signal
MAX_PORTFOLIO_EXPOSURE = 2000.00    # USD total open mirror capital (sum of open ledger positions)
MARGIN_ALLOWED = False              # orders are sized against non-marginable (cash) buying power
TIME_STOP_DAYS = 30                 # max holding period
PDT_EQUITY_THRESHOLD = 25000.00     # below this, no same-day buy->sell on a ticker
MIN_CONFIDENCE_TO_TRADE = 7         # AI score threshold, enforced in code after the LLM responds
MAX_DISCLOSURE_AGE_DAYS = 7         # older disclosures are logged but never traded (no backfill on first run)
PENDING_SIGNAL_TTL_DAYS = 5         # approved buys waiting for an open market expire after this
ALLOWED_ASSET_TYPES = {"Stock"}     # options, bonds, funds etc. are not mirrored
MAX_AI_EVALUATIONS_PER_RUN = 25     # cost cap; newest signals first, the rest wait for the next run

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
