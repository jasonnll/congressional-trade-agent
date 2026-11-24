"""
src/summary.py
The dashboard's summary payload, built from the ledger only (standard library + config), so the local dashboard
and the public static site show exactly the same thing. Callers supply what needs network or keys.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from src import config, database as db
from src.manual_filings import manual_transcription


def build_summary(clusters: List[Dict[str, Any]], account: Dict[str, Any], view_only: bool) -> Dict[str, Any]:
    open_positions = db.get_all_open_positions()
    return {
        "mode": "PAPER" if config.ALPACA_PAPER else "LIVE",
        "view_only": view_only,
        "model": config.ANTHROPIC_MODEL,
        "ai_configured": bool(config.ANTHROPIC_API_KEY),
        "tracked_members": config.TARGET_MEMBERS,
        "sources": {"congress": config.ENABLE_CONGRESS, "insider": config.ENABLE_INSIDERS,
                    "executive": config.ENABLE_EXECUTIVE},
        "clusters": clusters,
        "watchlist": json.loads(db.get_meta("watchlist_status") or "[]"),
        "source_health": json.loads(db.get_meta("source_health") or "{}"),
        "documents": db.document_stats(),
        "needs_review": [dict(d, transcribed=len((manual_transcription(d["doc_id"]) or {}).get("rows") or []))
                         for d in db.documents_needing_review()],
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
        "account": account,
    }
