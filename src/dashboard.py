"""
src/dashboard.py
Local read-only dashboard over the ledger and Alpaca account. Binds to 127.0.0.1 only.

  python -m src.dashboard   ->  http://127.0.0.1:8765

The only write action is "Fetch latest filings", which runs ingestion (no AI calls, no orders).
"""

from __future__ import annotations

import json
import logging
import os
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
        elif url.path == "/api/summary":
            self._json(summary())
        elif url.path == "/api/filings":
            limit = int((parse_qs(url.query).get("limit") or ["300"])[0])
            self._json(db.recent_filings(min(max(limit, 1), 2000)))
        elif url.path == "/api/positions":
            self._json(db.all_positions())
        elif url.path == "/api/performance":
            self._json(performance.summary())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ingest":
            self._json({"error": "not found"}, 404)
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
    db.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port or PORT), Handler)
    logger.info("Dashboard at http://127.0.0.1:%d", port or PORT)
    server.serve_forever()


if __name__ == "__main__":
    serve()
