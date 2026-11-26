"""
src/backtest.py
Replays every Congress buy in the ledger through the bot's rules and measures what mirroring it would have
returned against the same dollars in SPY over the same dates.

  python -m src.backtest            writes reports/backtest.md and reports/backtest.json

How a trade is simulated:
  entry   first daily close AFTER the disclosure date (the bot can only act once a filing is public)
  exit    the earlier of: the holding period, or the first close after the same member discloses a full,
          non-batch sale of the stock (the bot's exit rule). Trades still inside their hold are marked to the
          latest close and counted as open.
  return  exit close / entry close - 1 (split/dividend-adjusted daily closes, Alpaca IEX feed)

Limits, stated in the report: the AI score and committee match can't be replayed without re-running the model on
history, so the "current rules" set uses every deterministic rule but not the AI filter; leaders and chairs are
today's list applied to past trades; fills are daily closes, not real fills; no costs or taxes.
"""

from __future__ import annotations

import json
import logging
import statistics
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import config, database as db, performance
from src.house_clerk import NON_DISCRETIONARY, load_leaders

logger = logging.getLogger("BACKTEST")
REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"
Series = List[Tuple[str, float]]


def _ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ctx = json.loads(row.get("signal_context") or "{}")
    except (TypeError, ValueError):
        ctx = {}
    return ctx if isinstance(ctx, dict) else {}


def _amount(row: Dict[str, Any]) -> float:
    import re
    m = re.search(r"\$([\d,]+)", str(row.get("amount_range") or ""))
    return float(m.group(1).replace(",", "")) if m else 0.0


def _lag(row: Dict[str, Any]) -> int:
    return (date.fromisoformat(row["disclosure_date"]) - date.fromisoformat(row["transaction_date"])).days


def load_trades() -> List[Dict[str, Any]]:
    """Congress stock rows from the ledger, with batch sizes and sale types attached."""
    rows = db._rows(
        "SELECT trade_uid, member_slug, member_name, ticker, action, amount_range, transaction_date, disclosure_date, "
        "owner, asset_type, signal_context FROM processed_filings WHERE filer_type = 'congress' "
        "AND transaction_date IS NOT NULL AND disclosure_date IS NOT NULL")
    per_day: Dict[Tuple[str, str], int] = {}
    for r in rows:
        per_day[(r["member_slug"], r["transaction_date"])] = per_day.get((r["member_slug"], r["transaction_date"]), 0) + 1
    out = []
    for r in rows:
        if (r.get("asset_type") or "Stock") not in ("Stock", "ST") or not r.get("ticker"):
            continue
        if r["ticker"] in config.INDEX_ETFS_AND_CASH:
            continue
        ctx = _ctx(r)
        out.append({**r, "amount": _amount(r), "lag": _lag(r),
                    "batch": per_day[(r["member_slug"], r["transaction_date"])] >= config.BATCH_FILING_MIN_TRADES,
                    "partial": "partial" in str(ctx.get("tx_code") or "").lower(),
                    "non_discretionary": bool(NON_DISCRETIONARY.search(ctx.get("description") or ""))})
    return out


def _close_after(series: Series, day: str) -> Optional[Tuple[str, float]]:
    """First close strictly after `day`."""
    i = bisect_right(series, (day, float("inf")))
    return series[i] if i < len(series) else None


def _close_on_or_before(series: Series, day: str) -> Optional[Tuple[str, float]]:
    i = bisect_right(series, (day, float("inf"))) - 1
    return series[i] if i >= 0 else None


def simulate(buy: Dict[str, Any], hold_days: int, sales: Dict[Tuple[str, str], List[Dict[str, Any]]],
             closes: Dict[str, Series], follow_sales: bool = True) -> Optional[Dict[str, Any]]:
    px, spy = closes.get(buy["ticker"]), closes.get("SPY")
    if not px or not spy:
        return None
    entry = _close_after(px, buy["disclosure_date"])
    if not entry:
        return None
    spy_entry = _close_on_or_before(spy, entry[0])
    if not spy_entry:
        return None
    hold_end = (date.fromisoformat(entry[0]) + timedelta(days=hold_days)).isoformat()
    exit_day, reason = hold_end, "time"
    if follow_sales:
        for s in sales.get((buy["member_slug"], buy["ticker"]), []):
            if entry[0] <= s["disclosure_date"] < exit_day and not s["batch"] and not s["partial"]:
                exit_day, reason = s["disclosure_date"], "member_sale"
                break
    last_day = px[-1][0]
    if exit_day > last_day:
        exit_px, still_open = px[-1], True
    else:
        exit_px = _close_after(px, exit_day) if reason == "member_sale" else _close_on_or_before(px, exit_day)
        still_open = False
    spy_exit = _close_on_or_before(spy, exit_px[0]) if exit_px else None
    if not exit_px or not spy_exit or entry[1] <= 0 or spy_entry[1] <= 0:
        return None
    ret = exit_px[1] / entry[1] - 1
    spy_ret = spy_exit[1] / spy_entry[1] - 1
    return {"ticker": buy["ticker"], "member": buy["member_slug"], "entry_date": entry[0], "exit_date": exit_px[0],
            "return": ret, "spy_return": spy_ret, "excess": ret - spy_ret, "open": still_open, "exit": reason,
            "amount": buy["amount"], "lag": buy["lag"]}


def size_of(buy: Dict[str, Any], leaders: Dict[str, List[str]]) -> float:
    if buy["member_slug"] in leaders:
        return config.TIERS["HIGH"]["notional"]
    return next(s for floor, s in config.CONGRESS_SIZE_BY_AMOUNT if buy["amount"] >= floor)


def summarize(results: List[Dict[str, Any]], sizes: Optional[List[float]] = None) -> Dict[str, Any]:
    if not results:
        return {"trades": 0}
    excess = [r["excess"] for r in results]
    rets = [r["return"] for r in results]
    sizes = sizes or [1.0] * len(results)
    invested = sum(sizes)
    pnl = sum(s * r["return"] for s, r in zip(sizes, results))
    spy_pnl = sum(s * r["spy_return"] for s, r in zip(sizes, results))
    return {
        "trades": len(results),
        "open": sum(1 for r in results if r["open"]),
        "avg_return_pct": round(statistics.mean(rets) * 100, 2),
        "avg_spy_pct": round(statistics.mean(r["spy_return"] for r in results) * 100, 2),
        "avg_excess_pct": round(statistics.mean(excess) * 100, 2),
        "median_excess_pct": round(statistics.median(excess) * 100, 2),
        "win_rate_pct": round(100 * sum(1 for r in rets if r > 0) / len(rets)),
        "beat_spy_pct": round(100 * sum(1 for e in excess if e > 0) / len(excess)),
        "sized_return_pct": round(pnl / invested * 100, 2),
        "sized_spy_pct": round(spy_pnl / invested * 100, 2),
        "member_sale_exits": sum(1 for r in results if r["exit"] == "member_sale"),
    }


def current_rules(buy: Dict[str, Any], leaders: Dict[str, List[str]]) -> bool:
    """The deterministic entry rules (everything except the AI score and committee match)."""
    if buy["batch"] or buy["non_discretionary"] or buy["amount"] < config.CONGRESS_MIN_AMOUNT_USD:
        return False
    leader = buy["member_slug"] in leaders
    big = buy["amount"] >= config.HIGH_CONVICTION_MIN_AMOUNT_USD
    max_lag = config.TIERS["HIGH"]["max_lag_days"] if (leader or big) else config.TIERS["STANDARD"]["max_lag_days"]
    return buy["lag"] <= max_lag


def hold_for(buy: Dict[str, Any], leaders: Dict[str, List[str]]) -> int:
    leader_or_big = buy["member_slug"] in leaders or buy["amount"] >= config.HIGH_CONVICTION_MIN_AMOUNT_USD
    return config.TIERS["HIGH" if leader_or_big else "STANDARD"]["time_stop_days"]


def run(closes: Optional[Dict[str, Series]] = None, leaders: Optional[Dict[str, List[str]]] = None,
        write: bool = True) -> Dict[str, Any]:
    db.init_db()
    trades = load_trades()
    buys = [t for t in trades if t["action"] == "BUY"]
    sales: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for t in sorted((t for t in trades if t["action"] == "SELL"), key=lambda t: t["disclosure_date"]):
        sales.setdefault((t["member_slug"], t["ticker"]), []).append(t)
    leaders = leaders if leaders is not None else load_leaders()
    if closes is None:
        start = date.fromisoformat(min(t["disclosure_date"] for t in buys)) - timedelta(days=7) if buys else date.today()
        closes = performance._prices().daily_closes(sorted({t["ticker"] for t in buys} | {"SPY"}), start)

    def scenario(select: Callable[[Dict[str, Any]], bool], hold: Callable[[Dict[str, Any]], int],
                 sized: bool = False, follow_sales: bool = True) -> Dict[str, Any]:
        chosen = [b for b in buys if select(b)]
        results, sizes = [], []
        for b in chosen:
            r = simulate(b, hold(b), sales, closes, follow_sales)
            if r:
                results.append(r)
                sizes.append(size_of(b, leaders) if sized else 1.0)
        out = summarize(results, sizes if sized else None)
        out["candidates"] = len(chosen)
        return out

    rules = lambda b: current_rules(b, leaders)  # noqa: E731
    min_amt = lambda b: b["amount"] >= config.CONGRESS_MIN_AMOUNT_USD and not b["non_discretionary"]  # noqa: E731
    scenarios = {
        "Current rules (sized, rule-based holds)": scenario(rules, lambda b: hold_for(b, leaders), sized=True),
        "Every Congress stock buy, 90-day hold": scenario(lambda b: not b["non_discretionary"], lambda b: 90),
        "Hold 30 days (current entry rules)": scenario(rules, lambda b: 30),
        "Hold 60 days (current entry rules)": scenario(rules, lambda b: 60),
        "Hold 90 days (current entry rules)": scenario(rules, lambda b: 90),
        "Hold 180 days (current entry rules)": scenario(rules, lambda b: 180),
        "Hold 180 days, ignore member sales": scenario(rules, lambda b: 180, follow_sales=False),
        "Leaders & committee chairs only": scenario(lambda b: rules(b) and b["member_slug"] in leaders, lambda b: 180),
        "Rank-and-file members only": scenario(lambda b: rules(b) and b["member_slug"] not in leaders, lambda b: 90),
        "Batch-filing buys only (what the filter removes)": scenario(lambda b: min_amt(b) and b["batch"], lambda b: 90),
        "$15k-$50k disclosed": scenario(lambda b: rules(b) and b["amount"] < 50_001, lambda b: 90),
        "$50k-$250k disclosed": scenario(lambda b: rules(b) and 50_001 <= b["amount"] < 250_001, lambda b: 90),
        "$250k+ disclosed": scenario(lambda b: rules(b) and b["amount"] >= 250_001, lambda b: 90),
        "Filed within 15 days": scenario(lambda b: min_amt(b) and not b["batch"] and b["lag"] <= 15, lambda b: 90),
        "Filed 16-30 days after trade": scenario(lambda b: min_amt(b) and not b["batch"] and 16 <= b["lag"] <= 30,
                                                 lambda b: 90),
        "Filed 31+ days after trade": scenario(lambda b: min_amt(b) and not b["batch"] and b["lag"] > 30, lambda b: 90),
    }
    span = (min(t["transaction_date"] for t in buys), max(t["disclosure_date"] for t in buys)) if buys else (None, None)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "rules_version": config.RULES_VERSION,
              "congress_buys_in_ledger": len(buys), "tickers_priced": sum(1 for t in {b["ticker"] for b in buys}
                                                                         if t in closes),
              "period": span, "scenarios": scenarios}
    if write:
        REPORT_DIR.mkdir(exist_ok=True)
        (REPORT_DIR / "backtest.json").write_text(json.dumps(report, indent=1))
        (REPORT_DIR / "backtest.md").write_text(render_markdown(report))
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [f"# Backtest: mirroring Congress buys (rules {report['rules_version']})", "",
             f"Generated {report['generated_at'][:16]} UTC from {report['congress_buys_in_ledger']} Congress stock buys "
             f"in the ledger ({report['period'][0]} to {report['period'][1]}); {report['tickers_priced']} tickers had "
             "price data.", "",
             "Entry is the first close after the disclosure date; exit is the holding period or the member's next "
             "full, non-batch sale. Excess = return minus the same dollars in SPY over the same dates. Trades still "
             "inside their hold are marked to the latest close.", "",
             "**Not included:** the AI score and committee match (they'd need the model re-run on history), real "
             "fills, costs and taxes. Leaders/chairs are today's list applied to past trades.", "",
             "| Scenario | Trades (open) | Avg return | Avg SPY | Avg excess | Median excess | Win rate | "
             "Beat SPY | Sized return vs SPY |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, s in report["scenarios"].items():
        if not s.get("trades"):
            lines.append(f"| {name} | 0 | – | – | – | – | – | – | – |")
            continue
        lines.append(f"| {name} | {s['trades']} ({s['open']}) | {s['avg_return_pct']:+.2f}% | {s['avg_spy_pct']:+.2f}% | "
                     f"{s['avg_excess_pct']:+.2f} pts | {s['median_excess_pct']:+.2f} pts | {s['win_rate_pct']}% | "
                     f"{s['beat_spy_pct']}% | {s['sized_return_pct']:+.2f}% vs {s['sized_spy_pct']:+.2f}% |")
    lines += ["", "Small samples (under ~30 trades) are noise; read them as hints, not results."]
    return "\n".join(lines) + "\n"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
    report = run()
    print(render_markdown(report))


if __name__ == "__main__":
    main()
