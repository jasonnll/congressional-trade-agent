"""
src/main.py
Pipeline: ingest -> AI evaluation -> exits (politician sell, time stop) -> buys -> journal.

Usage:
  python -m src.main               full run (paper trading unless ALPACA_PAPER=false)
  python -m src.main --no-trade    ingest + AI scoring only, no broker calls
  python -m src.main --ingest-only ingest only, no AI calls and no broker calls
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from src import broker, config, database as db, performance
from src.ai_analyst import AnalystAuthError, AnalystUnavailable, evaluate_filing_signal
from src.house_clerk import NON_DISCRETIONARY
from src.ingestion import load_committee_overrides, poll_all_sources

logger = logging.getLogger("MAIN")
ET = ZoneInfo("America/New_York")


def _parse_ts(ts: str) -> datetime:
    """SQLite CURRENT_TIMESTAMP is UTC without an offset."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(iso_date: str, today: date) -> int:
    return (today - date.fromisoformat(iso_date)).days


# --- Stage 1: ingestion ---------------------------------------------------------------

def executive_shortlist(filings: List[Dict[str, Any]], today: date) -> set:
    """OGE batches carry hundreds of trades; only the largest few per batch are worth an AI call."""
    batches: Dict[tuple, List[Dict[str, Any]]] = {}
    for f in filings:
        if (f.get("filer_type") == "executive" and f["action"] == "BUY"
                and (f.get("amount_min") or 0) >= config.EXECUTIVE_MIN_VALUE_LOW_USD
                and _age_days(f["disclosure_date"], today) <= config.MAX_DISCLOSURE_AGE_DAYS):
            batches.setdefault((f["member_slug"], f["disclosure_date"]), []).append(f)
    allowed = set()
    for items in batches.values():
        items.sort(key=lambda f: (-(f.get("amount_min") or 0), f["trade_uid"]))
        allowed.update(f["trade_uid"] for f in items[: config.MAX_EXECUTIVE_SIGNALS_PER_BATCH])
    return allowed


def classify(f: Dict[str, Any], today: date, exec_allowed: set) -> tuple:
    age = _age_days(f["disclosure_date"], today)
    ctx = f.get("signal_context") or {}
    filer_type = f.get("filer_type") or "congress"
    if age > config.MAX_DISCLOSURE_AGE_DAYS:
        return "STALE", f"disclosed {age}d ago (> {config.MAX_DISCLOSURE_AGE_DAYS}d)"
    if f["action"] == "SELL":
        return "SELL_SIGNAL", ""
    if f.get("asset_type") and f["asset_type"] not in config.ALLOWED_ASSET_TYPES:
        return "SKIPPED", f"asset type {f['asset_type']} not mirrored"
    if f["ticker"] in config.INDEX_ETFS_AND_CASH:
        return "SKIPPED", "index ETF / cash equivalent"
    if len(f["ticker"]) == 5 and f["ticker"].endswith("X"):
        return "SKIPPED", "mutual fund (not mirrored)"
    description = ctx.get("description") or ""
    if description and NON_DISCRETIONARY.search(description):
        return "SKIPPED", f"non-discretionary: {description[:80]}"
    if (filer_type == "congress" and not config.CONGRESS_MIRROR_ALL_MEMBERS and not f.get("watchlist_group")
            and f["member_slug"] not in config.TARGET_MEMBERS):
        return "MONITORED", "not a mirrored member (add to TARGET_MEMBERS or set CONGRESS_MIRROR_ALL_MEMBERS)"
    if filer_type == "insider":
        watched = bool(f.get("watchlist_group"))
        minimum = config.WATCHLIST_MIN_PURCHASE_USD if watched else config.INSIDER_MIN_PURCHASE_USD
        if config.INSIDER_EXCLUDE_10B5_1 and ctx.get("is_10b5_1"):
            return "SKIPPED", "pre-scheduled 10b5-1 plan purchase"
        if (config.INSIDER_REQUIRE_OFFICER_OR_DIRECTOR and not watched
                and not (ctx.get("is_officer") or ctx.get("is_director"))):
            return "SKIPPED", "filer is not an officer or director"
        if (f.get("amount_min") or 0) < minimum:
            return "SKIPPED", f"below ${minimum:,.0f} {'watchlist' if watched else 'insider'} minimum"
    if filer_type == "executive":
        if (f.get("amount_min") or 0) < config.EXECUTIVE_MIN_VALUE_LOW_USD:
            return "SKIPPED", f"below ${config.EXECUTIVE_MIN_VALUE_LOW_USD:,.0f} executive minimum"
        if f["trade_uid"] not in exec_allowed:
            return "SKIPPED", f"outside the {config.MAX_EXECUTIVE_SIGNALS_PER_BATCH} largest trades in this batch"
    return "AWAITING_AI", ""


EXIT_REASONS = {"congress": "POLITICIAN_SELL", "insider": "INSIDER_SELL", "executive": "EXECUTIVE_SELL"}


def ingest(filings: List[Dict[str, Any]], today: date, log: List[str]) -> Dict[str, int]:
    counts = {"new": 0, "stale": 0, "sells": 0, "skipped": 0, "to_ai": 0, "monitored": 0, "duplicates": 0}
    exec_allowed = executive_shortlist(filings, today)
    for f in filings:
        if db.is_filing_processed(f["trade_uid"]):
            if f.get("watchlist_group"):
                db.tag_watchlist(f["trade_uid"], f["watchlist_group"])
            if str(f["trade_uid"]).startswith("house|"):
                db.refresh_display_fields(f)
            continue
        if db.dedupe_exists(db.dedupe_key(f)):
            counts["duplicates"] += 1  # already recorded from another source or an earlier (amended) PTR
            continue
        status, note = classify(f, today, exec_allowed)
        if not db.record_filing(f, status, note):
            continue
        counts["new"] += 1
        counts[{"STALE": "stale", "SELL_SIGNAL": "sells", "SKIPPED": "skipped", "AWAITING_AI": "to_ai",
                "MONITORED": "monitored"}[status]] += 1

        # Sells are honored regardless of age: if we still mirror this filer's position, flag it for exit.
        if f["action"] == "SELL":
            reason = EXIT_REASONS.get(f.get("filer_type") or "congress", "POLITICIAN_SELL")
            for pos in db.get_open_positions_by_member(f["member_slug"], f["ticker"]):
                db.request_exit(pos["id"], reason)
                log.append(f"- **EXIT QUEUED ({reason})**: {f['ticker']} ({f['member_name']})")
    return counts


# --- Stage 2: AI evaluation ------------------------------------------------------------

def evaluate_pending(today: date, log: List[str], client: Optional[Any] = None,
                     health: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    health = health if health is not None else {}
    committees = load_committee_overrides()
    evaluated = 0
    queue = []
    for row in db.get_filings_by_status("AWAITING_AI", "AI_RETRY"):
        if _age_days(row["disclosure_date"], today) > config.MAX_DISCLOSURE_AGE_DAYS:
            db.set_execution_status(row["trade_uid"], "STALE", "went stale before AI evaluation succeeded")
        else:
            queue.append(row)
    queue.sort(key=lambda r: (r["disclosure_date"], r["trade_uid"]), reverse=True)
    if len(queue) > config.MAX_AI_EVALUATIONS_PER_RUN:
        log.append(f"- AI cost cap: {len(queue) - config.MAX_AI_EVALUATIONS_PER_RUN} signal(s) wait for the next run")
    for row in queue[: config.MAX_AI_EVALUATIONS_PER_RUN]:
        is_congress = (row.get("filer_type") or "congress") == "congress"
        filing = dict(row, committees=committees.get(row["member_slug"], []) if is_congress else [])
        try:
            ev = evaluate_filing_signal(filing, client=client)
        except AnalystAuthError as e:
            # Every remaining call would fail the same way: stop, keep the queue for the next run.
            logger.error("AI evaluation stopped: %s", e)
            db.set_execution_status(row["trade_uid"], "AI_RETRY", str(e)[:300])
            health["anthropic"] = {"ok": False, "items": evaluated, "errors": [str(e)]}
            log.append(f"- AI evaluation stopped: {e}. {len(queue) - evaluated} signal(s) wait for the next run.")
            return evaluated
        except AnalystUnavailable as e:
            logger.warning("AI evaluation deferred for %s: %s", row["trade_uid"], e)
            db.set_execution_status(row["trade_uid"], "AI_RETRY", str(e)[:300])
            health.setdefault("anthropic", {"ok": False, "items": 0, "errors": []})["errors"].append(str(e)[:200])
            continue
        db.update_ai_evaluation(row["trade_uid"], ev.should_trade, ev.confidence_score, ev.rationale)
        db.set_execution_status(row["trade_uid"], "PENDING" if ev.should_trade else "REJECTED")
        evaluated += 1
        log.append(f"- **EVALUATED**: {row['ticker']} ({row['member_name']}) | Score: {ev.confidence_score} "
                   f"| Should Trade: {ev.should_trade}")
    if evaluated:
        health["anthropic"] = {"ok": True, "items": evaluated,
                               "errors": (health.get("anthropic") or {}).get("errors", [])[:5]}
    return evaluated


# --- Stage 3: broker -------------------------------------------------------------------

def reconcile_fills() -> None:
    for pos in db.get_all_open_positions():
        if not pos["filled_qty"]:
            try:
                qty = broker.get_filled_qty(pos["entry_order_id"])
            except Exception as e:
                logger.warning("Could not refresh fill for order %s: %s", pos["entry_order_id"], e)
                continue
            if qty:
                db.update_position_fill(pos["id"], qty)


def queue_time_stops(now: datetime, log: List[str]) -> None:
    for pos in db.get_all_open_positions():
        days_held = (now - _parse_ts(pos["opened_at"])).days
        if days_held >= config.TIME_STOP_DAYS and not pos["pending_exit_reason"]:
            db.request_exit(pos["id"], f"TIME_STOP_{config.TIME_STOP_DAYS}D")
            log.append(f"- **EXIT QUEUED (Time Stop)**: {pos['ticker']} after {days_held} days")


def process_exits(now: datetime, equity: float, log: List[str]) -> Dict[str, int]:
    counts = {"pol_exits": 0, "time_exits": 0}
    today_et = now.astimezone(ET).date()
    for pos in db.get_all_open_positions():
        reason = pos["pending_exit_reason"]
        if not reason:
            continue
        if equity < config.PDT_EQUITY_THRESHOLD and _parse_ts(pos["opened_at"]).astimezone(ET).date() == today_et:
            log.append(f"  - *Exit deferred (PDT safeguard, opened today)*: {pos['ticker']}")
            continue
        # Sell only this position's shares when other mirrored positions share the ticker.
        shared = db.count_open_positions_for_ticker(pos["ticker"]) > 1
        qty = pos["filled_qty"] if shared and pos["filled_qty"] else None
        if shared and not qty:
            log.append(f"  - *Exit deferred (fill qty unknown, ticker shared)*: {pos['ticker']}")
            continue
        res = broker.execute_sell_order(pos["ticker"], qty, pos["trade_uid"])
        if not res.ok:
            log.append(f"  - *Exit failed, will retry*: {pos['ticker']} ({res.reason})")
            continue
        db.record_position_closed(pos["id"], exit_reason=reason, exit_order_id=res.order_id)
        counts["time_exits" if reason.startswith("TIME_STOP") else "pol_exits"] += 1
        log.append(f"- **EXIT ({reason})**: {pos['ticker']} (Member: {pos['member_slug']})")
    return counts


def process_buys(today: date, log: List[str]) -> int:
    opened = 0
    for row in db.get_filings_by_status("PENDING"):
        if _age_days(row["disclosure_date"], today) > config.PENDING_SIGNAL_TTL_DAYS:
            db.set_execution_status(row["trade_uid"], "EXPIRED", "market/broker unavailable until signal expired")
            continue
        res = broker.execute_buy_order(row["ticker"], row["trade_uid"], db.get_open_exposure())
        if res.ok:
            db.record_position_opened(row["trade_uid"], row["member_slug"], row["ticker"],
                                      res.order_id, res.notional, res.filled_qty)
            opened += 1
            log.append(f"  - **EXECUTED BUY**: ${res.notional:.2f} on {row['ticker']} ({row['member_name']})")
        elif res.retryable:
            db.set_execution_status(row["trade_uid"], "PENDING", res.reason)
            log.append(f"  - *Buy deferred*: {row['ticker']} ({res.reason})")
        else:
            db.set_execution_status(row["trade_uid"], "SKIPPED", res.reason)
            log.append(f"  - *Buy skipped*: {row['ticker']} ({res.reason})")
    return opened


def broker_phase(now: datetime, stats: Dict[str, Any], log: List[str],
                 health: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    health = health if health is not None else {}
    try:
        market_open = broker.is_market_open()
        health["alpaca"] = {"ok": True, "items": 0, "errors": []}
        reconcile_fills()
        queue_time_stops(now, log)
        if not market_open:
            log.append("- Market closed: exits and approved buys stay queued for the next open-market run.")
            return
        equity = broker.get_account_snapshot()["equity"]
        stats.update(process_exits(now, equity, log))
        stats["opened"] = process_buys(now.astimezone(ET).date(), log)
        health["alpaca"]["items"] = stats["opened"]
    except broker.BrokerUnavailable as e:
        logger.warning("Broker phase skipped: %s", e)
        log.append(f"- Broker phase skipped: {e}. Approved buys and exits stay queued.")
        health["alpaca"] = {"ok": False, "items": 0, "errors": [str(e)]}


def record_service_health(services: Dict[str, Dict[str, Any]], now: datetime) -> None:
    """Adds Alpaca / Claude status to the same health record the data sources use (dashboard + journal)."""
    if not services:
        return
    health = json.loads(db.get_meta("source_health") or "{}")
    for name, h in services.items():
        previous = health.get(name) or {}
        health[name] = {**h, "checked_at": now.isoformat(),
                        "last_success": now.isoformat() if h["ok"] else previous.get("last_success")}
    db.set_meta("source_health", json.dumps(health))


# --- Journal ---------------------------------------------------------------------------

def generate_trade_journal(stats: Dict[str, Any], log: List[str], now: datetime) -> None:
    path = Path(config.JOURNAL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "PAPER" if config.ALPACA_PAPER else "LIVE"
    content = f"""# Trade Execution Journal - {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ({mode})

## Pipeline Summary Stats
- **Filings Fetched**: {stats['fetched']} (new: {stats['new']}, stale: {stats['stale']}, sells: {stats['sells']}, skipped: {stats['skipped']}, monitored only: {stats['monitored']}, cross-source duplicates: {stats['duplicates']})
- **AI Signals Evaluated**: {stats['evaluated']}
- **Positions Opened**: {stats['opened']}
- **Filer-Sell Exits**: {stats['pol_exits']}
- **Time-Stop Exits**: {stats['time_exits']}

## Operations Log
{chr(10).join(log) if log else 'No activity executed during cycle.'}
"""
    with open(path, "a") as fh:
        fh.write("\n" + content + "\n---\n")


def run_pipeline(trade: bool = True, use_ai: bool = True, now: Optional[datetime] = None,
                 filings: Optional[List[Dict[str, Any]]] = None, ai_client: Optional[Any] = None,
                 full_congress: bool = True) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    db.init_db()
    log: List[str] = []
    stats: Dict[str, Any] = {"fetched": 0, "new": 0, "stale": 0, "sells": 0, "skipped": 0, "to_ai": 0,
                             "monitored": 0, "duplicates": 0, "evaluated": 0, "opened": 0, "pol_exits": 0,
                             "time_exits": 0}

    polled = filings is None
    if polled:
        filings = poll_all_sources(today, db.get_all_open_positions(), now=now, full_congress=full_congress)
    stats["fetched"] = len(filings)
    stats.update(ingest(filings, today, log))

    services: Dict[str, Dict[str, Any]] = {}
    if use_ai:
        stats["evaluated"] = evaluate_pending(today, log, client=ai_client, health=services)
    if trade:
        broker_phase(now, stats, log, health=services)
    record_service_health(services, now)
    if polled or services:
        for name, h in json.loads(db.get_meta("source_health") or "{}").items():
            if h.get("checked_at") == now.isoformat() and not h.get("ok"):
                log.insert(0, f"- **SOURCE FAILED: {name}**: {'; '.join(h.get('errors') or [])[:300]}")
    if use_ai or trade:
        try:
            performance.update(now, log)
        except broker.BrokerUnavailable as e:
            log.append(f"- Performance not updated: {e}")
        except Exception as e:  # tracking must never block trading
            logger.warning("Performance update failed: %s", e)
            log.append(f"- Performance not updated: {e}")

    generate_trade_journal(stats, log, now)
    logger.info("Pipeline completed: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
    parser = argparse.ArgumentParser(description="Congressional trade mirror pipeline")
    parser.add_argument("--no-trade", action="store_true", help="ingest + AI scoring only")
    parser.add_argument("--ingest-only", action="store_true", help="ingest only (no AI, no trading)")
    args = parser.parse_args()
    run_pipeline(trade=not (args.no_trade or args.ingest_only), use_ai=not args.ingest_only)


if __name__ == "__main__":
    main()
