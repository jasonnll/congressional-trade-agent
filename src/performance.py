"""
src/performance.py
How the mirror strategy is doing, measured against simply holding SPY over the same dates.

Every number comes from broker fills and daily closing prices (Alpaca market data, IEX feed, split- and
dividend-adjusted), never from the disclosures themselves:

  position return    entry = broker average fill; current = latest close, or the exit fill once closed
  SPY comparison     the same dollars in SPY from the entry date to the exit date (or today)
  daily history      rebuilt from daily closes every run, so gaps between runs don't distort the curve
  AI filter check    every evaluated buy, approved or rejected: close on/after the decision date ->
                     close up to TIME_STOP_DAYS later. If rejected signals do as well, the filter isn't helping.
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from src import broker, config, database as db

logger = logging.getLogger(__name__)

BENCHMARK = "SPY"
Series = List[Tuple[str, float]]  # [(YYYY-MM-DD, close)] ascending


class PriceSource(Protocol):
    def daily_closes(self, symbols: List[str], start: date) -> Dict[str, Series]: ...


class AlpacaPrices:
    def __init__(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
            raise broker.BrokerUnavailable("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set")
        self.client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)

    def daily_closes(self, symbols: List[str], start: date) -> Dict[str, Series]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        out: Dict[str, Series] = {}
        for i in range(0, len(symbols), 50):
            req = StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day,
                                   start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                                   feed=DataFeed.IEX, adjustment=Adjustment.ALL)
            try:
                bars = self.client.get_stock_bars(req).data
            except Exception as e:
                msg = broker.describe_error(e)
                if "rejected the API keys" in msg:
                    raise broker.BrokerUnavailable(msg) from e
                logger.warning("Price lookup failed for %s: %s", symbols[i:i + 50], msg)
                continue
            for sym, rows in bars.items():
                out[sym] = [(b.timestamp.date().isoformat(), float(b.close)) for b in rows]
        return out


_price_source: Optional[PriceSource] = None


def set_price_source(source: Optional[PriceSource]) -> None:
    """Test hook."""
    global _price_source
    _price_source = source


def _prices() -> PriceSource:
    return _price_source or AlpacaPrices()


def close_on_or_after(series: Series, day: str) -> Optional[Tuple[str, float]]:
    i = bisect_left([d for d, _ in series], day)
    return series[i] if i < len(series) else None


def close_on_or_before(series: Series, day: str) -> Optional[Tuple[str, float]]:
    i = bisect_right([d for d, _ in series], day) - 1
    return series[i] if i >= 0 else None


def _day(ts: Optional[str]) -> Optional[str]:
    return str(ts)[:10] if ts else None


# --- 1. fills -------------------------------------------------------------------------

def reconcile_fills(log: List[str]) -> None:
    for p in db.positions_with_filings():
        try:
            if p["entry_price"] is None and p["entry_order_id"]:
                fill = broker.get_order_fill(p["entry_order_id"])
                if fill:
                    db.update_position(p["id"], entry_price=fill["price"], filled_qty=fill["qty"],
                                       filled_at=fill["filled_at"] or p["opened_at"])
            if (p["status"] == "CLOSED" and p["exit_price"] is None and p["exit_order_id"]
                    and p["exit_order_id"] != "MANUAL_CLOSE"):
                fill = broker.get_order_fill(p["exit_order_id"])
                if fill:
                    db.update_position(p["id"], exit_price=fill["price"], exit_qty=fill["qty"],
                                       exit_filled_at=fill["filled_at"] or p["closed_at"])
        except broker.BrokerUnavailable:
            raise
        except Exception as e:
            logger.warning("Fill lookup failed for position %s: %s", p["id"], e)


# --- 2. pricing -----------------------------------------------------------------------

def _value_on(p: Dict[str, Any], day: str, closes: Dict[str, Series]) -> Optional[float]:
    """Mirror position value at the close of `day` (exit proceeds once it has closed)."""
    qty = p["filled_qty"] or 0
    exit_day = _day(p["exit_filled_at"] or p["closed_at"]) if p["status"] == "CLOSED" else None
    if exit_day and day >= exit_day and p["exit_price"] is not None:
        return (p["exit_qty"] or qty) * p["exit_price"]
    c = close_on_or_before(closes.get(p["ticker"], []), day)
    return qty * c[1] if c else None


def _spy_value_on(p: Dict[str, Any], day: str, spy: Series) -> Optional[float]:
    entry = close_on_or_after(spy, _day(p["filled_at"] or p["opened_at"]))
    if not entry:
        return None
    exit_day = _day(p["exit_filled_at"] or p["closed_at"]) if p["status"] == "CLOSED" else None
    end = close_on_or_before(spy, min(day, exit_day) if exit_day else day)
    return p["notional_amount"] * end[1] / entry[1] if end else None


def price_positions(today: date, closes: Dict[str, Series]) -> None:
    spy = closes.get(BENCHMARK, [])
    t = today.isoformat()
    for p in db.positions_with_filings():
        if p["entry_price"] is None:
            continue
        fields: Dict[str, Any] = {}
        series = closes.get(p["ticker"], [])
        last = close_on_or_before(series, t)
        if last:
            fields.update(last_price=last[1], last_price_date=last[0])
        entry_spy = close_on_or_after(spy, _day(p["filled_at"] or p["opened_at"]))
        if entry_spy:
            fields["spy_entry"] = entry_spy[1]
        if p["status"] == "CLOSED":
            exit_day = _day(p["exit_filled_at"] or p["closed_at"])
            if p["exit_price"] is None and p["exit_order_id"] == "MANUAL_CLOSE":
                c = close_on_or_before(series, exit_day)  # closed outside the bot: best available estimate
                if c:
                    fields.update(exit_price=c[1], exit_qty=p["filled_qty"], exit_price_estimated=1)
                    p = {**p, "exit_price": c[1], "exit_qty": p["filled_qty"]}
            spy_exit = close_on_or_before(spy, exit_day)
            if spy_exit:
                fields["spy_exit"] = spy_exit[1]
            if p["exit_price"] is not None:
                fields["realized_pnl"] = round(((p["exit_qty"] or p["filled_qty"]) * p["exit_price"]) - p["notional_amount"], 4)
        last_spy = close_on_or_before(spy, t)
        if last_spy:
            fields["spy_last"] = last_spy[1]
        db.update_position(p["id"], **fields)


def _sleeve(p: Dict[str, Any]) -> str:
    return p.get("signal_tier") if p.get("signal_tier") in config.TIERS else "STANDARD"


def rebuild_history(today: date, closes: Dict[str, Series]) -> List[Dict[str, Any]]:
    """Daily rows for each sleeve (STANDARD, HIGH) and for ALL, from the first fill to today."""
    spy = closes.get(BENCHMARK, [])
    positions = [p for p in db.positions_with_filings() if p["entry_price"] is not None]
    if not positions or not spy:
        db.replace_snapshots([])
        return []
    first = min(_day(p["filled_at"] or p["opened_at"]) for p in positions)
    rows = []
    for day, _ in spy:
        if day < first or day > today.isoformat():
            continue
        acc = {s: {"invested": 0.0, "value": 0.0, "spy": 0.0, "open": 0, "closed": 0}
               for s in ("ALL", *config.TIERS)}
        for p in positions:
            if _day(p["filled_at"] or p["opened_at"]) > day:
                continue
            v, s = _value_on(p, day, closes), _spy_value_on(p, day, spy)
            if v is None or s is None:
                continue
            exit_day = _day(p["exit_filled_at"] or p["closed_at"]) if p["status"] == "CLOSED" else None
            closed = bool(exit_day and day >= exit_day)
            for key in ("ALL", _sleeve(p)):
                a = acc[key]
                a["invested"] += p["notional_amount"]
                a["value"] += v
                a["spy"] += s
                a["closed" if closed else "open"] += 1
        for key, a in acc.items():
            if key != "ALL" and not a["invested"]:
                continue  # a sleeve starts its history at its own first fill
            rows.append({"snapshot_date": day, "sleeve": key, "invested": round(a["invested"], 2),
                         "value": round(a["value"], 2), "pnl": round(a["value"] - a["invested"], 2),
                         "spy_pnl": round(a["spy"] - a["invested"], 2),
                         "open_positions": a["open"], "closed_positions": a["closed"]})
    db.replace_snapshots(rows)
    return rows


# --- 3. AI filter check -----------------------------------------------------------------

def record_new_decisions() -> None:
    for f in db.evaluated_filings_without_outcome():
        db.upsert_outcome({"trade_uid": f["trade_uid"], "decision": "APPROVED" if f["should_trade"] else "REJECTED",
                           "ticker": f["ticker"], "decided_date": _day(f["created_at"])})


def score_outcomes(today: date, closes: Dict[str, Series]) -> None:
    spy = closes.get(BENCHMARK, [])
    for o in db.all_outcomes():
        series = closes.get(o["ticker"], [])
        ref, spy_ref = close_on_or_after(series, o["decided_date"]), close_on_or_after(spy, o["decided_date"])
        if not ref or not spy_ref:
            continue
        horizon = (date.fromisoformat(ref[0]) + timedelta(days=config.TIME_STOP_DAYS)).isoformat()
        end_day = min(horizon, today.isoformat())
        last, spy_last = close_on_or_before(series, end_day), close_on_or_before(spy, end_day)
        if not last or not spy_last or last[0] <= ref[0]:
            continue
        db.upsert_outcome({
            "trade_uid": o["trade_uid"], "decision": o["decision"], "ticker": o["ticker"],
            "decided_date": o["decided_date"], "ref_date": ref[0], "ref_price": ref[1], "spy_ref": spy_ref[1],
            "last_date": last[0], "last_price": last[1], "spy_last": spy_last[1],
            "return_pct": round((last[1] / ref[1] - 1) * 100, 3),
            "spy_return_pct": round((spy_last[1] / spy_ref[1] - 1) * 100, 3),
        })


# --- entry point --------------------------------------------------------------------------

def update(now: datetime, log: Optional[List[str]] = None) -> Dict[str, Any]:
    """Refreshes fills, prices, daily history and AI-decision outcomes. Needs Alpaca keys (market data is free)."""
    log = log if log is not None else []
    today = now.date()
    reconcile_fills(log)
    record_new_decisions()
    positions = db.positions_with_filings()
    outcomes = db.all_outcomes()
    symbols = sorted({p["ticker"] for p in positions} | {o["ticker"] for o in outcomes} | {BENCHMARK})
    starts = [_day(p["opened_at"]) for p in positions] + [o["decided_date"] for o in outcomes]
    start = date.fromisoformat(min(starts)) - timedelta(days=7) if starts else today - timedelta(days=7)
    closes = _prices().daily_closes(symbols, start)
    if BENCHMARK not in closes:
        raise RuntimeError("No SPY prices returned; performance not updated")
    price_positions(today, closes)
    rebuild_history(today, closes)
    score_outcomes(today, closes)
    s = summary()
    for key, t in s["sleeves"].items():
        if t["positions"]:
            log.append(f"- **Performance ({t['label']})**: {t['positions']} positions, "
                       f"${t['open_exposure']:,.0f} / ${t['cap']:,.0f} open; P&L ${t['pnl']:+,.2f} "
                       f"({t['pnl_pct']:+.2f}%) vs SPY ${t['spy_pnl']:+,.2f} ({t['spy_pnl_pct']:+.2f}%); "
                       f"closed win rate {t['win_rate'] if t['win_rate'] is not None else '–'}%")
    return s


# --- read model for the dashboard and journal ----------------------------------------------

def _position_metrics(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if p["entry_price"] is None:
        return None
    closed = p["status"] == "CLOSED" and p["exit_price"] is not None
    current = p["exit_price"] if closed else p["last_price"]
    spy_end = (p["spy_exit"] if closed else p["spy_last"])
    if current is None:
        return None
    ret = (current / p["entry_price"] - 1) * 100
    spy_ret = (spy_end / p["spy_entry"] - 1) * 100 if spy_end and p["spy_entry"] else None
    pnl = p["notional_amount"] * ret / 100
    return {"return_pct": round(ret, 2), "spy_return_pct": round(spy_ret, 2) if spy_ret is not None else None,
            "excess_pct": round(ret - spy_ret, 2) if spy_ret is not None else None, "pnl": round(pnl, 2),
            "spy_pnl": round(p["notional_amount"] * spy_ret / 100, 2) if spy_ret is not None else None,
            "closed": closed, "estimated": bool(p.get("exit_price_estimated"))}


def _group_stats(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(items)
    closed = [m for m in items if m["closed"]]
    with_spy = [m for m in items if m["excess_pct"] is not None]
    return {
        "positions": len(items), "closed": len(closed),
        "pnl": round(sum(m["pnl"] for m in items), 2),
        "spy_pnl": round(sum(m["spy_pnl"] or 0 for m in items), 2),
        "avg_return_pct": round(sum(m["return_pct"] for m in items) / len(items), 2) if items else None,
        "avg_excess_pct": round(sum(m["excess_pct"] for m in with_spy) / len(with_spy), 2) if with_spy else None,
        "win_rate": round(100 * sum(1 for m in closed if m["return_pct"] > 0) / len(closed)) if closed else None,
        "beat_spy_rate": round(100 * sum(1 for m in with_spy if m["excess_pct"] > 0) / len(with_spy)) if with_spy else None,
    }


SOURCE_LABELS = {"congress": "Congress", "insider": "Corporate insiders", "executive": "Executive branch"}


def _totals(metrics: List[Dict[str, Any]], raw: List[Dict[str, Any]]) -> Dict[str, Any]:
    t = _group_stats(metrics)
    invested = sum(p["notional_amount"] for p in raw if p["entry_price"] is not None)
    t.update(invested=round(invested, 2), excess=round(t["pnl"] - t["spy_pnl"], 2),
             pnl_pct=round(t["pnl"] / invested * 100, 2) if invested else None,
             spy_pnl_pct=round(t["spy_pnl"] / invested * 100, 2) if invested else None,
             unfilled=sum(1 for p in raw if p["entry_price"] is None))
    return t


def summary() -> Dict[str, Any]:
    raw = db.positions_with_filings()
    positions = []
    for p in raw:
        m = _position_metrics(p)
        if m:
            label = p["watchlist_group"] or SOURCE_LABELS.get(p["filer_type"] or "congress", p["filer_type"])
            positions.append({**m, "id": p["id"], "ticker": p["ticker"], "source": label, "sleeve": _sleeve(p),
                              "filer": p["member_name"] or p["member_slug"]})
    totals = _totals(positions, raw)

    sleeves = {}
    for key, tier in config.TIERS.items():
        mine = [m for m in positions if m["sleeve"] == key]
        mine_raw = [p for p in raw if _sleeve(p) == key]
        held = [p for p in mine_raw if p["status"] == "OPEN"]
        sleeves[key] = {**_totals(mine, mine_raw), "label": config.SLEEVE_LABELS.get(key, key),
                        "open_positions": len(held), "open_exposure": round(sum(p["notional_amount"] for p in held), 2),
                        "cap": config.MAX_SLEEVE_EXPOSURE, "size": tier["notional"],
                        "hold_days": tier["time_stop_days"], "history": db.snapshots(key)}

    def grouped(key: str) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in positions:
            groups.setdefault(m[key], []).append(m)
        rows = [{"name": k, **_group_stats(v)} for k, v in groups.items()]
        return sorted(rows, key=lambda r: -r["positions"])

    ai: Dict[str, Dict[str, Any]] = {}
    for decision in ("APPROVED", "REJECTED"):
        scored = [o for o in db.all_outcomes() if o["decision"] == decision and o["return_pct"] is not None]
        n = len(scored)
        ai[decision] = {
            "signals": sum(1 for o in db.all_outcomes() if o["decision"] == decision), "scored": n,
            "avg_return_pct": round(sum(o["return_pct"] for o in scored) / n, 2) if n else None,
            "avg_excess_pct": round(sum(o["return_pct"] - o["spy_return_pct"] for o in scored) / n, 2) if n else None,
            "beat_spy_rate": round(100 * sum(1 for o in scored if o["return_pct"] > o["spy_return_pct"]) / n) if n else None,
        }
    return {"totals": totals, "by_source": grouped("source"), "by_filer": grouped("filer")[:10],
            "ai_filter": ai, "history": db.snapshots(), "positions": {p["id"]: p for p in positions},
            "sleeves": sleeves,
            "horizon_days": config.TIME_STOP_DAYS}
