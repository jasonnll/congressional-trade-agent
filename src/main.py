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
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from src import broker, config, database as db, performance
from src.ai_analyst import AnalystAuthError, AnalystUnavailable, evaluate_filing_signal
from src.house_clerk import NON_DISCRETIONARY, load_leaders
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
    if filer_type == "congress":
        ctx = dict(ctx, congress_cluster=congress_cluster(f))
        f["signal_context"] = ctx  # recorded with the filing; the AI and dashboard read it
        gate = congress_buy_gate(f, ctx)
        if gate:
            return gate
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


def _amount_low(f: Dict[str, Any]) -> float:
    if f.get("amount_min") is not None:
        return float(f["amount_min"])
    m = re.search(r"\$([\d,]+)", str(f.get("amount_range") or ""))
    return float(m.group(1).replace(",", "")) if m else 0.0


def _ctx(f: Dict[str, Any]) -> Dict[str, Any]:
    ctx = f.get("signal_context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except ValueError:
            ctx = None
    return ctx if isinstance(ctx, dict) else {}


def _lag_days(f: Dict[str, Any]) -> int:
    """Filing lag: disclosure date minus trade date."""
    return (date.fromisoformat(f["disclosure_date"]) - date.fromisoformat(f["transaction_date"])).days


def _is_cluster(ctx: Dict[str, Any]) -> bool:
    return (ctx.get("congress_cluster") or {}).get("members", 0) >= config.CLUSTER_MIN_MEMBERS


def congress_cluster(f: Dict[str, Any]) -> Dict[str, Any]:
    """Members of Congress (this one included) who bought the same ticker within CLUSTER_WINDOW_DAYS."""
    buyers = {b["member_slug"]: b for b in db.congress_buyers(f["ticker"], f["transaction_date"],
                                                              config.CLUSTER_WINDOW_DAYS)}
    buyers.setdefault(f["member_slug"], {"member_slug": f["member_slug"], "member_name": f.get("member_name"),
                                         "party": f.get("party")})
    parties = {b.get("party") for b in buyers.values() if b.get("party")}
    return {"members": len(buyers), "bipartisan": {"D", "R"} <= parties,
            "names": sorted(str(b.get("member_name") or b["member_slug"]) for b in buyers.values())[:8]}


def family_bar_note(f: Dict[str, Any]) -> Optional[str]:
    """Why a Congress-family signal from a non-target member is too small to score, or None."""
    if (f.get("watchlist_group") == "Congress family" and f.get("member_slug") not in config.TARGET_MEMBERS
            and _amount_low(f) < config.CONGRESS_FAMILY_MIN_AMOUNT_USD and not _is_cluster(_ctx(f))):
        return f"below ${config.CONGRESS_FAMILY_MIN_AMOUNT_USD:,.0f} Congress-family minimum"
    return None


def congress_buy_gate(f: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[tuple]:
    """Entry minimums for a Congress buy, checked before any AI call. None means: send it to the AI.

    A cluster (CLUSTER_MIN_MEMBERS+ members buying the same ticker within CLUSTER_WINDOW_DAYS) lowers the dollar
    minimum to CLUSTER_MIN_AMOUNT_USD and makes the buy eligible even from a member we don't otherwise mirror.
    Sells never pass through here: any sale of a mirrored ticker exits regardless of size or age.
    """
    std, high = config.TIERS["STANDARD"], config.TIERS["HIGH"]
    cluster = _is_cluster(ctx)
    lag, amount = _lag_days(f), _amount_low(f)
    if lag > high["max_lag_days"]:
        return "SKIPPED", f"filing lag {lag}d exceeds the {high['max_lag_days']}-day limit"
    if not cluster:
        note = family_bar_note(f)
        if note:
            return "SKIPPED", note
        if (not config.CONGRESS_MIRROR_ALL_MEMBERS and not f.get("watchlist_group")
                and f["member_slug"] not in config.TARGET_MEMBERS):
            return "MONITORED", "not a mirrored member (add to TARGET_MEMBERS or set CONGRESS_MIRROR_ALL_MEMBERS)"
    minimum = config.CLUSTER_MIN_AMOUNT_USD if cluster else config.CONGRESS_MIN_AMOUNT_USD
    if amount < minimum:
        return "SKIPPED", f"below ${minimum:,.0f} Congress minimum"
    if lag > std["max_lag_days"] and amount < config.HIGH_CONVICTION_MIN_AMOUNT_USD and not is_leader(f):
        return "SKIPPED", (f"filing lag {lag}d: past {std['max_lag_days']} days only "
                           f"${config.HIGH_CONVICTION_MIN_AMOUNT_USD:,.0f}+ high-conviction buys qualify")
    return None


def is_leader(f: Dict[str, Any]) -> bool:
    """A current party leader or full-committee chair (config.LEADERS_ARE_HIGH_CONVICTION)."""
    return (config.LEADERS_ARE_HIGH_CONVICTION and (f.get("filer_type") or "congress") == "congress"
            and f.get("member_slug") in load_leaders())


def assign_tier(row: Dict[str, Any], committee_match: bool, score: int) -> tuple:
    """(tier, reason) for an AI-approved buy; tier None means it doesn't qualify for any tier."""
    if (row.get("filer_type") or "congress") != "congress":
        return "INSIDER", ""  # insiders and executive branch have their own sleeve and budget
    lag, amount = _lag_days(row), _amount_low(row)
    high = config.TIERS["HIGH"]
    if is_leader(row) and lag <= high["max_lag_days"] and amount >= config.CONGRESS_MIN_AMOUNT_USD:
        return "HIGH", ""
    if (amount >= config.HIGH_CONVICTION_MIN_AMOUNT_USD and lag <= high["max_lag_days"]
            and (committee_match or score >= config.HIGH_CONVICTION_MIN_SCORE)):
        return "HIGH", ""
    if lag <= config.TIERS["STANDARD"]["max_lag_days"]:
        return "STANDARD", ""
    return None, (f"filing lag {lag}d needs a high-conviction signal "
                  f"(${config.HIGH_CONVICTION_MIN_AMOUNT_USD:,.0f}+ with committee match or score "
                  f"{config.HIGH_CONVICTION_MIN_SCORE}+)")


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
            if (f.get("filer_type") == "insider" and not config.EXIT_ON_INSIDER_SELL
                    and db.get_open_positions_by_member(f["member_slug"], f["ticker"])):
                log.append(f"- *Insider sale noted, position kept*: {f['ticker']} ({f['member_name']}); "
                           f"insider sales carry no signal, the position rides to its holding period")
                continue
            reason = EXIT_REASONS.get(f.get("filer_type") or "congress", "POLITICIAN_SELL")
            for pos in db.get_open_positions_by_member(f["member_slug"], f["ticker"]):
                db.request_exit(pos["id"], reason)
                log.append(f"- **EXIT QUEUED ({reason})**: {f['ticker']} ({f['member_name']})")
    return counts


# --- Stage 2: AI evaluation ------------------------------------------------------------

def _ai_priority(row: Dict[str, Any]) -> tuple:
    """Sort key for the AI queue: the evaluation cap should spend its calls on the strongest-looking signals."""
    return (0 if _is_cluster(_ctx(row)) else 1, -_amount_low(row))

def evaluate_pending(today: date, log: List[str], client: Optional[Any] = None,
                     health: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    health = health if health is not None else {}
    committees = load_committee_overrides()
    evaluated = 0
    queue = []
    for row in db.get_filings_by_status("AWAITING_AI", "AI_RETRY"):
        if _age_days(row["disclosure_date"], today) > config.MAX_DISCLOSURE_AGE_DAYS:
            db.set_execution_status(row["trade_uid"], "STALE", "went stale before AI evaluation succeeded")
        elif family_bar_note(row):  # rows queued before the rule existed
            db.set_execution_status(row["trade_uid"], "SKIPPED", family_bar_note(row))
        else:
            queue.append(row)
    queue.sort(key=lambda r: (r["disclosure_date"], r["trade_uid"]), reverse=True)  # newest first...
    queue.sort(key=_ai_priority)  # ...within multi-member clusters first, then the largest disclosed amounts
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
        tier, tier_note = assign_tier(row, ev.committee_match, ev.confidence_score) if ev.should_trade else (None, "")
        should_trade = ev.should_trade and tier is not None
        db.update_ai_evaluation(row["trade_uid"], should_trade, ev.confidence_score, ev.rationale,
                                committee_match=ev.committee_match, signal_tier=tier)
        db.set_execution_status(row["trade_uid"], "PENDING" if should_trade else "REJECTED", tier_note)
        evaluated += 1
        log.append(f"- **EVALUATED**: {row['ticker']} ({row['member_name']}) | Score: {ev.confidence_score} "
                   f"| Should Trade: {should_trade}{f' | Tier: {tier}' if should_trade else ''}"
                   f"{f' ({tier_note})' if tier_note else ''}")
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
        limit = pos.get("time_stop_days") or config.TIME_STOP_DAYS
        if days_held >= limit and not pos["pending_exit_reason"]:
            db.request_exit(pos["id"], f"TIME_STOP_{limit}D")
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


def _buy_priority(row: Dict[str, Any]) -> tuple:
    """Highest AI score first; ties go to HIGH-tier signals, then the larger disclosed amount, then the newest."""
    return (-(row.get("confidence_score") or 0), 0 if row.get("signal_tier") == "HIGH" else 1,
            -_amount_low(row), -date.fromisoformat(row["disclosure_date"]).toordinal())


def insider_cluster(row: Dict[str, Any]) -> bool:
    """2+ distinct insiders bought this stock within INSIDER_CLUSTER_WINDOW_DAYS (Tracefour clusters or our ledger)."""
    cluster = _ctx(row).get("cluster") or {}
    if (cluster.get("insider_count") or 0) >= config.INSIDER_CLUSTER_MIN_INSIDERS:
        return True
    rows = db._rows(
        "SELECT COUNT(DISTINCT member_slug) AS n FROM processed_filings WHERE filer_type = 'insider' "
        "AND action = 'BUY' AND ticker = ? AND ABS(julianday(transaction_date) - julianday(?)) <= ?",
        (row["ticker"], row["transaction_date"], config.INSIDER_CLUSTER_WINDOW_DAYS))
    return bool(rows) and rows[0]["n"] >= config.INSIDER_CLUSTER_MIN_INSIDERS


def position_size(row: Dict[str, Any], tier: str) -> float:
    """Dollar size of a buy: bigger for stronger evidence, never above MAX_NOTIONAL_PER_TRADE."""
    if tier == "INSIDER":
        if row.get("filer_type") == "executive":
            size = config.EXECUTIVE_NOTIONAL
        else:
            size = config.INSIDER_CLUSTER_NOTIONAL if insider_cluster(row) else config.TIERS["INSIDER"]["notional"]
    elif tier == "STANDARD":
        amount = _amount_low(row)
        size = next(s for floor, s in config.CONGRESS_SIZE_BY_AMOUNT if amount >= floor)
    else:
        size = config.TIERS[tier]["notional"]
    return min(size, config.MAX_NOTIONAL_PER_TRADE)


def rotate_out(row: Dict[str, Any], tier: str, today: date, log: List[str]) -> bool:
    """Full sleeve: sell its weakest position to make room for a stronger signal. True when room was made.

    Only for signals scored ROTATION_MIN_SCORE+, only replacing a strictly lower score, and never a position
    opened today (no same-day round trip, which also keeps the PDT safeguard intact).
    """
    score = row.get("confidence_score") or 0
    if score < config.ROTATION_MIN_SCORE:
        return False
    for pos in db.get_open_positions_with_scores(tier):
        if pos["confidence_score"] >= score:
            return False  # weakest-first order: nothing weaker is left
        if pos["pending_exit_reason"] or _parse_ts(pos["opened_at"]).astimezone(ET).date() >= today:
            continue
        shared = db.count_open_positions_for_ticker(pos["ticker"]) > 1
        qty = pos["filled_qty"] if shared and pos["filled_qty"] else None
        if shared and not qty:
            continue
        res = broker.execute_sell_order(pos["ticker"], qty, pos["trade_uid"])
        if not res.ok:
            log.append(f"  - *Rotation sell failed*: {pos['ticker']} ({res.reason})")
            return False
        db.record_position_closed(pos["id"], exit_reason="ROTATED_OUT", exit_order_id=res.order_id)
        log.append(f"- **EXIT (ROTATED_OUT)**: {pos['ticker']} (score {pos['confidence_score']}) "
                   f"to make room for {row['ticker']} (score {score}) in the {tier} sleeve")
        return True
    return False


def queue_duplicate_exits(log: List[str]) -> None:
    """One position per stock per sleeve: extra copies (from before the rule existed) are exited, weakest first."""
    seen: Dict[tuple, int] = {}
    for pos in reversed(db.get_open_positions_with_scores()):  # strongest first, so the best copy is kept
        key = (pos["ticker"], pos.get("signal_tier") or "STANDARD")
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > config.MAX_POSITIONS_PER_TICKER and not pos["pending_exit_reason"]:
            db.request_exit(pos["id"], "DUPLICATE_TICKER")
            log.append(f"- **EXIT QUEUED (duplicate)**: {pos['ticker']} ({pos['member_slug']})")


def process_buys(today: date, log: List[str]) -> int:
    opened = 0
    # Strongest signals first, so a duplicate ticker keeps the highest-conviction filer.
    pending = sorted(db.get_filings_by_status("PENDING"), key=_buy_priority)
    for row in pending:
        if _age_days(row["disclosure_date"], today) > config.PENDING_SIGNAL_TTL_DAYS:
            db.set_execution_status(row["trade_uid"], "EXPIRED", "market/broker unavailable until signal expired")
            continue
        # Each tier is its own sleeve: its own $ cap and its own one-position-per-stock rule.
        tier = row.get("signal_tier") if row.get("signal_tier") in config.TIERS else "STANDARD"
        sizing = config.TIERS[tier]
        if db.count_open_positions_for_ticker(row["ticker"], tier) >= config.MAX_POSITIONS_PER_TICKER:
            db.set_execution_status(row["trade_uid"], "SKIPPED", f"{tier} sleeve already holds {row['ticker']}")
            log.append(f"  - *Buy skipped*: {row['ticker']} ({row['member_name']}): {tier} sleeve already holds it")
            continue
        size = position_size(row, tier)
        res = broker.execute_buy_order(row["ticker"], row["trade_uid"], db.get_open_exposure(tier),
                                       notional=size, max_exposure=config.SLEEVE_CAPS[tier])
        rotations = 0  # a bigger buy may need more than one weak position freed
        while (not res.ok and res.retryable and res.reason.startswith("exposure cap") and rotations < 3
               and rotate_out(row, tier, today, log)):
            rotations += 1
            res = broker.execute_buy_order(row["ticker"], row["trade_uid"], db.get_open_exposure(tier),
                                           notional=size, max_exposure=config.SLEEVE_CAPS[tier])
        if res.ok:
            db.record_position_opened(row["trade_uid"], row["member_slug"], row["ticker"],
                                      res.order_id, res.notional, res.filled_qty,
                                      signal_tier=tier, time_stop_days=sizing["time_stop_days"])
            opened += 1
            log.append(f"  - **EXECUTED BUY** ({tier} sleeve): ${res.notional:.2f} on {row['ticker']} "
                       f"({row['member_name']}), hold up to {sizing['time_stop_days']}d")
        elif res.retryable:
            db.set_execution_status(row["trade_uid"], "PENDING", res.reason)
            log.append(f"  - *Buy deferred* ({tier} sleeve): {row['ticker']} ({res.reason})")
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
        queue_duplicate_exits(log)
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
