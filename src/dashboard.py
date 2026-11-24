"""
src/dashboard.py
Local dashboard over the ledger and Alpaca account. Binds to 127.0.0.1 only.

  python -m src.dashboard   ->  http://127.0.0.1:8765

Two modes, decided by whether this machine has Alpaca keys:
  view-only (no keys; the bot runs on GitHub): data/ledger.db is the file GitHub commits, so the dashboard never
      writes to it. It serves a private, migrated copy (data/.dashboard/ledger.db, git-ignored) that is refreshed
      whenever data/ledger.db changes, e.g. after `git pull`. "Fetch latest filings" is disabled.
  local (keys present; the bot runs on this machine): reads and writes data/ledger.db directly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from src import broker, config, database as db, performance
from src.ingestion import fetch_insider_clusters
from src.main import run_pipeline

logger = logging.getLogger("DASHBOARD")
HTML_PATH = Path(__file__).parent / "static" / "dashboard.html"
PORT = int(os.getenv("DASHBOARD_PORT", "8765"))

_ingest_lock = threading.Lock()
_account_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
_cluster_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])

SOURCE_DB = Path(config.DB_PATH)
VIEW_DB = SOURCE_DB.parent / ".dashboard" / SOURCE_DB.name
_view_lock = threading.Lock()
_view_signature: Optional[Tuple[int, int, int]] = None
_local_ready = False


def view_only() -> bool:
    return not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)


def sync_ledger() -> None:
    """Points the db module at the right file before each request (see module docstring)."""
    global _view_signature, _local_ready
    if not view_only():
        if not _local_ready:
            config.DB_PATH = str(SOURCE_DB)
            db.init_db()
            _local_ready = True
        return
    if not SOURCE_DB.exists():
        config.DB_PATH = str(VIEW_DB)
        return
    st = SOURCE_DB.stat()
    signature = (st.st_ino, st.st_mtime_ns, st.st_size)
    if signature == _view_signature:
        return
    with _view_lock:
        if signature == _view_signature:
            return
        VIEW_DB.parent.mkdir(parents=True, exist_ok=True)
        tmp = VIEW_DB.with_suffix(".tmp")
        src = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)  # consistent snapshot even if git is mid-write
        finally:
            src.close()
            dst.close()
        os.replace(tmp, VIEW_DB)
        config.DB_PATH = str(VIEW_DB)
        db.init_db()  # migrations run on the private copy only
        _view_signature = signature
        logger.info("Dashboard view refreshed from %s", SOURCE_DB)


def insider_clusters() -> List[Dict[str, Any]]:
    global _cluster_cache
    ts, cached = _cluster_cache
    if time.time() - ts < 600:
        return cached
    try:
        rows = [dict(c, ticker=t) for t, c in fetch_insider_clusters().items()]
        rows.sort(key=lambda c: (not c.get("is_live"), -(c.get("insider_count") or 0)))
    except Exception as e:
        logger.warning("Could not fetch insider clusters: %s", e)
        rows = cached
    _cluster_cache = (time.time(), rows[:12])
    return rows[:12]


def account_info() -> Dict[str, Any]:
    global _account_cache
    ts, cached = _account_cache
    if time.time() - ts < 30 and cached:
        return cached
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        # Normal when the bot runs on GitHub: this machine only views the synced ledger.
        return {"view_only": True}
    try:
        info: Dict[str, Any] = broker.get_account_snapshot()
        info["market_open"] = broker.is_market_open()
    except broker.BrokerUnavailable as e:
        info = {"error": str(e)}
    except Exception as e:
        info = {"error": broker.describe_error(e)}
    _account_cache = (time.time(), info)
    return info


def summary() -> Dict[str, Any]:
    open_positions = db.get_all_open_positions()
    return {
        "mode": "PAPER" if config.ALPACA_PAPER else "LIVE",
        "view_only": view_only(),
        "model": config.ANTHROPIC_MODEL,
        "ai_configured": bool(config.ANTHROPIC_API_KEY),
        "tracked_members": config.TARGET_MEMBERS,
        "sources": {"congress": config.ENABLE_CONGRESS, "insider": config.ENABLE_INSIDERS,
                    "executive": config.ENABLE_EXECUTIVE},
        "clusters": insider_clusters() if config.ENABLE_INSIDERS else [],
        "watchlist": json.loads(db.get_meta("watchlist_status") or "[]"),
        "source_health": json.loads(db.get_meta("source_health") or "{}"),
        "documents": db.document_stats(),
        "needs_review": db.documents_needing_review(),
        "mirror_all_members": config.CONGRESS_MIRROR_ALL_MEMBERS,
        "tracefour_key": bool(config.TRACEFOUR_API_KEY),
        "congress_family_sweep_at": db.get_meta("congress_full_poll_at"),
        "open_positions": len(open_positions),
        "open_exposure": db.get_open_exposure(),
        "sleeve_exposure": {k: db.get_open_exposure(k) for k in config.TIERS},
        "pending_exits": sum(1 for p in open_positions if p["pending_exit_reason"]),
        "guardrails": {
            "max_notional_per_trade": config.MAX_NOTIONAL_PER_TRADE,
            "max_sleeve_exposure": config.MAX_SLEEVE_EXPOSURE,
            "margin_allowed": config.MARGIN_ALLOWED,
            "time_stop_days": config.TIME_STOP_DAYS,
            "pdt_equity_threshold": config.PDT_EQUITY_THRESHOLD,
            "min_confidence": config.MIN_CONFIDENCE_TO_TRADE,
            "max_disclosure_age_days": config.MAX_DISCLOSURE_AGE_DAYS,
            "pending_signal_ttl_days": config.PENDING_SIGNAL_TTL_DAYS,
            "max_ai_per_run": config.MAX_AI_EVALUATIONS_PER_RUN,
            "insider_min_purchase": config.INSIDER_MIN_PURCHASE_USD,
            "executive_min_value": config.EXECUTIVE_MIN_VALUE_LOW_USD,
            "executive_per_batch": config.MAX_EXECUTIVE_SIGNALS_PER_BATCH,
            "max_positions_per_ticker": config.MAX_POSITIONS_PER_TICKER,
            "tiers": config.TIERS,
            "high_min_amount": config.HIGH_CONVICTION_MIN_AMOUNT_USD,
            "high_min_score": config.HIGH_CONVICTION_MIN_SCORE,
            "congress_min": config.CONGRESS_MIN_AMOUNT_USD,
            "cluster_min_members": config.CLUSTER_MIN_MEMBERS,
            "cluster_window_days": config.CLUSTER_WINDOW_DAYS,
            "congress_family_min": config.CONGRESS_FAMILY_MIN_AMOUNT_USD,
        },
        "stats": db.filing_stats(),
        "account": account_info(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        routes = {
            "/api/summary": summary,
            "/api/filings": lambda: db.recent_filings(
                min(max(int((parse_qs(url.query).get("limit") or ["300"])[0]), 1), 2000)),
            "/api/positions": db.all_positions,
            "/api/performance": performance.summary,
        }
        if url.path not in routes:
            self._json({"error": "not found"}, 404)
            return
        try:
            sync_ledger()
            self._json(routes[url.path]())
        except Exception as e:  # always answer, so the page can show what went wrong
            logger.exception("Dashboard request %s failed", url.path)
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ingest":
            self._json({"error": "not found"}, 404)
            return
        if view_only():
            self._json({"error": "The bot runs on GitHub: run `git pull` for new data instead."}, 409)
            return
        if not _ingest_lock.acquire(blocking=False):
            self._json({"error": "ingestion already running"}, 409)
            return
        try:
            # The all-member Congress sweep takes ~8 minutes; it runs from the scheduled pipeline, not the button.
            self._json(run_pipeline(trade=False, use_ai=False, full_congress=False))
        except Exception as e:
            logger.exception("Ingestion from dashboard failed")
            self._json({"error": str(e)}, 500)
        finally:
            _ingest_lock.release()

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)


def serve(port: Optional[int] = None) -> None:
    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
    sync_ledger()
    server = ThreadingHTTPServer(("127.0.0.1", port or PORT), Handler)
    logger.info("Dashboard at http://127.0.0.1:%d", port or PORT)
    server.serve_forever()


if __name__ == "__main__":
    serve()
