"""
src/database.py
Database connection management, schema initialization, and state tracking helpers.

processed_filings.execution_status lifecycle:
  STALE | SKIPPED | SELL_SIGNAL                      -> terminal at ingest
  AWAITING_AI -> (AI_RETRY ->)* REJECTED | PENDING   -> after AI evaluation
  PENDING -> EXECUTED | EXPIRED | SKIPPED            -> after broker phase

member_slug / member_name identify the filer for every source (a Congress member, a corporate
insider "insider-<cik>", or an OGE tracker slug); filer_type says which.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_filings (
    trade_uid TEXT PRIMARY KEY,
    member_slug TEXT NOT NULL,
    member_name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    amount_range TEXT,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    ai_evaluated BOOLEAN NOT NULL DEFAULT 0,
    should_trade BOOLEAN DEFAULT 0,
    confidence_score INTEGER DEFAULT 0,
    ai_rationale TEXT,
    executed BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    chamber TEXT,
    party TEXT,
    owner TEXT,
    asset_type TEXT,
    asset_description TEXT,
    source_link TEXT,
    execution_status TEXT NOT NULL DEFAULT 'AWAITING_AI',
    execution_note TEXT,
    filer_type TEXT NOT NULL DEFAULT 'congress',
    filer_role TEXT,
    signal_context TEXT,
    watchlist_group TEXT,
    dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_filings_status ON processed_filings(execution_status);
CREATE INDEX IF NOT EXISTS ix_filings_disclosure ON processed_filings(disclosure_date);

CREATE TABLE IF NOT EXISTS active_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid TEXT UNIQUE NOT NULL,
    member_slug TEXT NOT NULL,
    ticker TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    notional_amount REAL NOT NULL,
    filled_qty REAL DEFAULT 0.0,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT CHECK(status IN ('OPEN', 'CLOSED')) DEFAULT 'OPEN',
    closed_at TIMESTAMP NULL,
    exit_reason TEXT NULL,
    exit_order_id TEXT NULL,
    pending_exit_reason TEXT NULL,
    FOREIGN KEY (trade_uid) REFERENCES processed_filings (trade_uid)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Daily value per sleeve (STANDARD, HIGH, and ALL) vs the same dollars in SPY on the same dates.
-- Rebuilt from fills and daily closes every run, so it can be dropped and recreated freely.
CREATE TABLE IF NOT EXISTS performance_snapshots (
    snapshot_date TEXT NOT NULL,
    sleeve TEXT NOT NULL DEFAULT 'ALL',
    invested REAL, value REAL, pnl REAL, spy_pnl REAL,
    open_positions INTEGER, closed_positions INTEGER,
    PRIMARY KEY (snapshot_date, sleeve)
);

-- Hypothetical outcome of every AI decision (approved AND rejected), to check whether the filter adds value.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    trade_uid TEXT PRIMARY KEY,
    decision TEXT NOT NULL,            -- APPROVED | REJECTED
    ticker TEXT NOT NULL,
    decided_date TEXT NOT NULL,
    ref_date TEXT, ref_price REAL, spy_ref REAL,
    last_date TEXT, last_price REAL, spy_last REAL,
    return_pct REAL, spy_return_pct REAL
);

-- Primary-source documents (House Clerk PTR PDFs). Parsed rows are cached so each PDF is downloaded once.
CREATE TABLE IF NOT EXISTS source_documents (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    member_name TEXT,
    state_district TEXT,
    filed_date TEXT,
    url TEXT,
    status TEXT NOT NULL,          -- PARSED | NO_TEXT (scanned paper filing) | MISSING
    row_count INTEGER DEFAULT 0,
    ticker_rows INTEGER DEFAULT 0,
    rows_json TEXT,
    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

FILING_FIELDS = (
    "trade_uid", "member_slug", "member_name", "ticker", "action", "amount_range",
    "transaction_date", "disclosure_date", "chamber", "party", "owner", "asset_type",
    "asset_description", "source_link", "filer_type", "filer_role", "signal_context", "watchlist_group",
)

# Columns added after the first release; older ledgers are upgraded in place.
MIGRATIONS = {
    "processed_filings": {
        "filer_type": "TEXT NOT NULL DEFAULT 'congress'",
        "filer_role": "TEXT",
        "signal_context": "TEXT",
        "watchlist_group": "TEXT",
        "dedupe_key": "TEXT",
        "committee_match": "INTEGER",
        "signal_tier": "TEXT",      # STANDARD | HIGH (Congress) | INSIDER (insider/executive), set on AI approval
        # Price of the security since the disclosed trade (refreshed each run from daily closes)
        "trade_price": "REAL",
        "latest_price": "REAL",
        "latest_price_date": "TEXT",
        "price_change_pct": "REAL",
        "spy_change_pct": "REAL",
    },
    "active_positions": {
        "signal_tier": "TEXT",
        "time_stop_days": "INTEGER",
        "entry_price": "REAL",       # broker average fill price
        "filled_at": "TEXT",
        "exit_price": "REAL",
        "exit_qty": "REAL",
        "exit_filled_at": "TEXT",
        "exit_price_estimated": "INTEGER DEFAULT 0",  # 1 when closed outside the bot and priced from the daily close
        "realized_pnl": "REAL",
        "last_price": "REAL",
        "last_price_date": "TEXT",
        "spy_entry": "REAL",
        "spy_exit": "REAL",
        "spy_last": "REAL",
    },
}


def dedupe_key(filing: Dict[str, Any]) -> Optional[str]:
    """Same congressional trade from two sources (Tracefour history vs Clerk) or an amended PTR -> same key."""
    if (filing.get("filer_type") or "congress") != "congress":
        return None
    last = str(filing.get("member_slug") or "").split("-")[-1]
    amount = "".join(str(filing.get("amount_range") or "").split())
    return "|".join([last, filing.get("ticker") or "", filing.get("transaction_date") or "",
                     filing.get("action") or "", amount])


@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    """Yields a connection that commits on success and always closes."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def init_db() -> None:
    with get_db_connection() as conn:
        # The pre-sleeve snapshot table had one row per date; it is derived data, so recreate it.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(performance_snapshots)")}
        if cols and "sleeve" not in cols:
            conn.execute("DROP TABLE performance_snapshots")
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    logger.info("Migrated %s: added column %s", table, name)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_filings_type ON processed_filings(filer_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_filings_dedupe ON processed_filings(dedupe_key)")
        # Positions opened before sleeves existed were $100 / 30-day buys: they belong to STANDARD.
        conn.execute("UPDATE active_positions SET signal_tier = 'STANDARD' WHERE signal_tier IS NULL")
        # Insider and executive signals moved to their own INSIDER sleeve (2026-11-25), positions included.
        conn.execute("UPDATE processed_filings SET signal_tier = 'INSIDER' "
                     "WHERE signal_tier IS NOT NULL AND signal_tier != 'INSIDER' "
                     "AND filer_type IN ('insider', 'executive')")
        conn.execute("UPDATE active_positions SET signal_tier = 'INSIDER' WHERE signal_tier != 'INSIDER' AND trade_uid IN "
                     "(SELECT trade_uid FROM processed_filings WHERE filer_type IN ('insider', 'executive'))")
        # Longer holding periods (2026-11-25): open positions get at least their sleeve's current hold.
        for tier, spec in config.TIERS.items():
            conn.execute(f"UPDATE active_positions SET time_stop_days = ? WHERE status = 'OPEN' "
                         f"AND {_SLEEVE} = ? AND COALESCE(time_stop_days, 0) < ?",
                         (spec["time_stop_days"], tier, spec["time_stop_days"]))
        if not config.EXIT_ON_INSIDER_SELL:  # insider sales no longer close positions
            conn.execute("UPDATE active_positions SET pending_exit_reason = NULL "
                         "WHERE status = 'OPEN' AND pending_exit_reason = 'INSIDER_SELL'")
        missing = conn.execute(
            "SELECT trade_uid, filer_type, member_slug, ticker, transaction_date, action, amount_range "
            "FROM processed_filings WHERE dedupe_key IS NULL AND filer_type = 'congress'").fetchall()
        for r in missing:
            conn.execute("UPDATE processed_filings SET dedupe_key = ? WHERE trade_uid = ?",
                         (dedupe_key(dict(r)), r["trade_uid"]))
    logger.info("Database initialized at %s", config.DB_PATH)


def dedupe_exists(key: Optional[str]) -> bool:
    return bool(key) and bool(_rows("SELECT 1 FROM processed_filings WHERE dedupe_key = ? LIMIT 1", (key,)))


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    rows = _rows("SELECT * FROM source_documents WHERE doc_id = ?", (doc_id,))
    return rows[0] if rows else None


def record_document(doc: Dict[str, Any]) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO source_documents
               (doc_id, source, member_name, state_district, filed_date, url, status, row_count, ticker_rows, rows_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc["doc_id"], doc["source"], doc.get("member_name"), doc.get("state_district"), doc.get("filed_date"),
             doc.get("url"), doc["status"], doc.get("row_count", 0), doc.get("ticker_rows", 0), doc.get("rows_json")),
        )


def documents_needing_review(limit: int = 50) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT doc_id, source, member_name, state_district, filed_date, url, status FROM source_documents
           WHERE status != 'PARSED' ORDER BY filed_date DESC LIMIT ?""", (limit,))


def document_stats() -> Dict[str, Any]:
    rows = _rows("""SELECT status, COUNT(*) AS docs, SUM(row_count) AS rows_, SUM(ticker_rows) AS ticker_rows,
                           MAX(filed_date) AS latest FROM source_documents GROUP BY status""")
    return {r["status"]: r for r in rows}


def get_meta(key: str) -> Optional[str]:
    rows = _rows("SELECT value FROM meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


def set_meta(key: str, value: str) -> None:
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# --- processed_filings ---------------------------------------------------------------

def is_filing_processed(trade_uid: str) -> bool:
    return bool(_rows("SELECT 1 FROM processed_filings WHERE trade_uid = ?", (trade_uid,)))


def record_filing(filing: Dict[str, Any], status: str, note: str = "") -> bool:
    """Inserts a filing once. Returns False if the trade_uid was already recorded (idempotency)."""
    values = [filing.get(f) for f in FILING_FIELDS]
    values[FILING_FIELDS.index("amount_range")] = filing.get("amount_range") or "Unknown"
    values[FILING_FIELDS.index("filer_type")] = filing.get("filer_type") or "congress"
    ctx = filing.get("signal_context")
    values[FILING_FIELDS.index("signal_context")] = json.dumps(ctx) if isinstance(ctx, dict) else ctx
    cols = ", ".join(FILING_FIELDS + ("execution_status", "execution_note", "dedupe_key"))
    marks = ", ".join("?" for _ in range(len(FILING_FIELDS) + 3))
    with get_db_connection() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO processed_filings ({cols}) VALUES ({marks})",
            (*values, status, note, dedupe_key(filing)),
        )
        return cur.rowcount == 1


def refresh_display_fields(filing: Dict[str, Any]) -> None:
    """Corrects names/party/role on an already-recorded filing (e.g. after a parser or roster fix).
    Never touches identity, status, or trading fields."""
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE processed_filings SET member_name = COALESCE(?, member_name), party = COALESCE(?, party),
                      filer_role = COALESCE(?, filer_role), asset_description = COALESCE(?, asset_description)
               WHERE trade_uid = ?""",
            (filing.get("member_name"), filing.get("party"), filing.get("filer_role"),
             filing.get("asset_description"), filing["trade_uid"]),
        )


def tag_watchlist(trade_uid: str, group: str) -> None:
    with get_db_connection() as conn:
        conn.execute("UPDATE processed_filings SET watchlist_group = ? WHERE trade_uid = ? AND watchlist_group IS NULL",
                     (group, trade_uid))


def get_filing(trade_uid: str) -> Optional[Dict[str, Any]]:
    rows = _rows("SELECT * FROM processed_filings WHERE trade_uid = ?", (trade_uid,))
    return rows[0] if rows else None


def get_filings_by_status(*statuses: str) -> List[Dict[str, Any]]:
    marks = ", ".join("?" for _ in statuses)
    return _rows(
        f"SELECT * FROM processed_filings WHERE execution_status IN ({marks}) ORDER BY disclosure_date, trade_uid",
        statuses,
    )


def update_ai_evaluation(trade_uid: str, should_trade: bool, confidence_score: int, rationale: str,
                         committee_match: Optional[bool] = None, signal_tier: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE processed_filings
               SET ai_evaluated = 1, should_trade = ?, confidence_score = ?, ai_rationale = ?,
                   committee_match = ?, signal_tier = ?
               WHERE trade_uid = ?""",
            (1 if should_trade else 0, confidence_score, rationale,
             None if committee_match is None else int(committee_match), signal_tier, trade_uid),
        )


def filings_to_price(since: str) -> List[Dict[str, Any]]:
    return _rows("SELECT trade_uid, ticker, transaction_date FROM processed_filings WHERE disclosure_date >= ?",
                 (since,))


def update_filing_prices(rows: List[Dict[str, Any]]) -> None:
    with get_db_connection() as conn:
        conn.executemany(
            """UPDATE processed_filings SET trade_price = :trade_price, latest_price = :latest_price,
                   latest_price_date = :latest_price_date, price_change_pct = :price_change_pct,
                   spy_change_pct = :spy_change_pct
               WHERE trade_uid = :trade_uid""",
            rows,
        )


def congress_buyers(ticker: str, transaction_date: str, window_days: int) -> List[Dict[str, Any]]:
    """Distinct members of Congress who disclosed buying `ticker` within `window_days` of `transaction_date`."""
    return _rows(
        """SELECT member_slug, MAX(member_name) AS member_name, MAX(party) AS party
           FROM processed_filings
           WHERE filer_type = 'congress' AND action = 'BUY' AND ticker = ?
             AND ABS(julianday(transaction_date) - julianday(?)) <= ?
           GROUP BY member_slug""",
        (ticker, transaction_date, window_days),
    )


def set_execution_status(trade_uid: str, status: str, note: str = "") -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE processed_filings SET execution_status = ?, execution_note = ? WHERE trade_uid = ?",
            (status, note, trade_uid),
        )


def recent_filings(limit: int = 200) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT * FROM processed_filings ORDER BY disclosure_date DESC, created_at DESC LIMIT ?", (limit,)
    )


def filing_stats() -> Dict[str, Any]:
    by_status = _rows("SELECT execution_status AS status, COUNT(*) AS n FROM processed_filings GROUP BY 1")
    by_member = _rows(
        """SELECT member_name, member_slug, filer_type, MAX(party) AS party, MAX(chamber) AS chamber,
                  MAX(filer_role) AS filer_role,
                  SUM(action = 'BUY') AS buys, SUM(action = 'SELL') AS sells,
                  SUM(execution_status = 'EXECUTED') AS mirrored
           FROM processed_filings GROUP BY member_slug ORDER BY buys + sells DESC LIMIT 15"""
    )
    by_type = _rows(
        """SELECT filer_type, COUNT(*) AS n, SUM(execution_status = 'EXECUTED') AS mirrored
           FROM processed_filings GROUP BY filer_type"""
    )
    top_tickers = _rows(
        """SELECT ticker, SUM(action = 'BUY') AS buys, SUM(action = 'SELL') AS sells
           FROM processed_filings WHERE disclosure_date >= date('now', '-180 day')
           GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 12"""
    )
    by_watchlist = _rows(
        """SELECT watchlist_group AS grp, COUNT(*) AS n, COUNT(DISTINCT member_slug) AS filers,
                  SUM(action = 'BUY') AS buys, SUM(action = 'SELL') AS sells,
                  SUM(execution_status = 'EXECUTED') AS mirrored, MAX(disclosure_date) AS last_disclosed
           FROM processed_filings WHERE watchlist_group IS NOT NULL GROUP BY 1 ORDER BY 1"""
    )
    return {"by_status": by_status, "by_member": by_member, "by_type": by_type, "top_tickers": top_tickers,
            "by_watchlist": by_watchlist}


# --- active_positions ----------------------------------------------------------------

def record_position_opened(trade_uid: str, member_slug: str, ticker: str, order_id: str,
                           notional: float, qty: float, signal_tier: str = "STANDARD",
                           time_stop_days: int = config.TIME_STOP_DAYS) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO active_positions
               (trade_uid, member_slug, ticker, entry_order_id, notional_amount, filled_qty, status,
                signal_tier, time_stop_days)
               VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
            (trade_uid, member_slug, ticker, order_id, notional, qty, signal_tier, time_stop_days),
        )
        conn.execute(
            "UPDATE processed_filings SET executed = 1, execution_status = 'EXECUTED', execution_note = ? "
            "WHERE trade_uid = ?",
            (f"order {order_id}", trade_uid),
        )


def update_position_fill(position_id: int, filled_qty: float) -> None:
    with get_db_connection() as conn:
        conn.execute("UPDATE active_positions SET filled_qty = ? WHERE id = ?", (filled_qty, position_id))


def get_open_positions_by_member(member_slug: str, ticker: str) -> List[Dict[str, Any]]:
    return _rows(
        "SELECT * FROM active_positions WHERE member_slug = ? AND ticker = ? AND status = 'OPEN'",
        (member_slug, ticker),
    )


def get_all_open_positions() -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM active_positions WHERE status = 'OPEN' ORDER BY opened_at")


_SLEEVE = "COALESCE(signal_tier, 'STANDARD')"


def count_open_positions_for_ticker(ticker: str, sleeve: Optional[str] = None) -> int:
    """Open positions in `ticker`: in one sleeve, or across the whole broker account when sleeve is None."""
    if sleeve:
        return len(_rows(f"SELECT 1 FROM active_positions WHERE ticker = ? AND status = 'OPEN' AND {_SLEEVE} = ?",
                         (ticker, sleeve)))
    return len(_rows("SELECT 1 FROM active_positions WHERE ticker = ? AND status = 'OPEN'", (ticker,)))


def get_open_positions_with_scores(sleeve: Optional[str] = None) -> List[Dict[str, Any]]:
    """Open positions (optionally one sleeve) with the AI score of the filing that opened them, weakest first."""
    where, params = (f"AND {_SLEEVE.replace('signal_tier', 'a.signal_tier')} = ?", (sleeve,)) if sleeve else ("", ())
    return _rows(
        f"""SELECT a.*, COALESCE(f.confidence_score, 0) AS confidence_score
            FROM active_positions a LEFT JOIN processed_filings f ON f.trade_uid = a.trade_uid
            WHERE a.status = 'OPEN' {where}
            ORDER BY COALESCE(f.confidence_score, 0) ASC, a.opened_at ASC""",
        params,
    )


def get_open_exposure(sleeve: Optional[str] = None) -> float:
    where, params = ("AND " + _SLEEVE + " = ?", (sleeve,)) if sleeve else ("", ())
    rows = _rows(f"SELECT COALESCE(SUM(notional_amount), 0) AS total FROM active_positions WHERE status = 'OPEN' {where}",
                 params)
    return float(rows[0]["total"])


def request_exit(position_id: int, reason: str) -> None:
    """Flags a position for exit; the broker phase executes it when the market and PDT rules allow."""
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE active_positions SET pending_exit_reason = ? "
            "WHERE id = ? AND status = 'OPEN' AND pending_exit_reason IS NULL",
            (reason, position_id),
        )


def record_position_closed(position_id: int, exit_reason: str, exit_order_id: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE active_positions
               SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP, exit_reason = ?, exit_order_id = ?,
                   pending_exit_reason = NULL
               WHERE id = ?""",
            (exit_reason, exit_order_id, position_id),
        )


def update_position(position_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_db_connection() as conn:
        conn.execute(f"UPDATE active_positions SET {cols} WHERE id = ?", (*fields.values(), position_id))


def positions_with_filings() -> List[Dict[str, Any]]:
    return _rows(
        """SELECT p.*, f.member_name, f.filer_type, f.watchlist_group, f.disclosure_date
           FROM active_positions p LEFT JOIN processed_filings f ON f.trade_uid = p.trade_uid ORDER BY p.opened_at"""
    )


def evaluated_filings_without_outcome() -> List[Dict[str, Any]]:
    return _rows(
        """SELECT f.trade_uid, f.ticker, f.should_trade, f.created_at FROM processed_filings f
           LEFT JOIN signal_outcomes o ON o.trade_uid = f.trade_uid
           WHERE f.ai_evaluated = 1 AND f.action = 'BUY' AND o.trade_uid IS NULL"""
    )


def upsert_outcome(row: Dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with get_db_connection() as conn:
        conn.execute(f"INSERT OR REPLACE INTO signal_outcomes ({cols}) VALUES ({marks})", tuple(row.values()))


def all_outcomes() -> List[Dict[str, Any]]:
    return _rows(
        """SELECT o.*, f.filer_type, f.watchlist_group, f.member_name, f.confidence_score
           FROM signal_outcomes o LEFT JOIN processed_filings f ON f.trade_uid = o.trade_uid"""
    )


def replace_snapshots(rows: List[Dict[str, Any]]) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM performance_snapshots")
        conn.executemany(
            """INSERT INTO performance_snapshots
               (snapshot_date, sleeve, invested, value, pnl, spy_pnl, open_positions, closed_positions)
               VALUES (:snapshot_date, :sleeve, :invested, :value, :pnl, :spy_pnl, :open_positions, :closed_positions)""",
            rows,
        )


def snapshots(sleeve: str = "ALL") -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM performance_snapshots WHERE sleeve = ? ORDER BY snapshot_date", (sleeve,))


def all_positions(limit: int = 200) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT p.*, f.member_name, f.filer_type FROM active_positions p
           LEFT JOIN processed_filings f ON f.trade_uid = p.trade_uid
           ORDER BY p.status = 'OPEN' DESC, p.opened_at DESC LIMIT ?""",
        (limit,),
    )
