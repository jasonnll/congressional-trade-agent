"""
src/ingestion.py
Fetches public trade disclosures from the Tracefour JSON API and normalizes them into one shape.

Sources (all Tracefour, CC BY 4.0 - anything that displays the data must link back to Tracefour):
  congress   GET /v1/congress/{slug}   STOCK Act PTRs. {"data": {"member": {...}, "trades": [...]}}
  insider    GET /v1/filings           SEC Form 4 open-market line items (P/S only), market-wide
             GET /v1/clusters          tickers several insiders are buying at once (signal context)
  executive  GET /v1/trackers          trackers with archetype "oge_278t" (OGE Form 278-T filers)
             GET /v1/trackers/{slug}   data.ogeTransactions, data.filingHistory
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from src import config

logger = logging.getLogger(__name__)

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_sleep: Callable[[float], None] = time.sleep
_session = requests.Session()
_session.headers.update({"User-Agent": config.HTTP_USER_AGENT, "Accept": "application/json"})
if config.TRACEFOUR_API_KEY:
    _session.headers["Authorization"] = f"Bearer {config.TRACEFOUR_API_KEY}"

# Every request that ends without data (other than a 404) is recorded here, so callers can tell
# "the source had nothing new" apart from "the source could not be read".
failures: List[str] = []
key_warnings: List[str] = []   # the API key was rejected but data still came through anonymously
_rate_limited_until = 0.0


def _fail(url: str, reason: str) -> None:
    failures.append(f"{reason} ({url.replace(config.TRACEFOUR_API, '')})")
    logger.warning("Fetch failed: %s: %s", url, reason)


def get_json_with_retry(url: str) -> Optional[Dict[str, Any]]:
    """GET with exponential backoff on 429/5xx/network errors. Returns None on failure and records it in `failures`."""
    global _rate_limited_until
    if time.time() < _rate_limited_until:
        _fail(url, "rate limited")
        return None
    for attempt in range(config.HTTP_MAX_RETRIES):
        backoff = 2 ** attempt
        try:
            resp = _session.get(url, timeout=15)
        except requests.RequestException as e:
            logger.warning("Network error on %s (attempt %d): %s", url, attempt + 1, e)
            _sleep(backoff)
            continue
        if resp.status_code == 404:
            logger.warning("Not found (404): %s", url)
            return None
        if resp.status_code in (401, 403) and "Authorization" in _session.headers:
            # A bad key must not block data that the anonymous tier would serve.
            del _session.headers["Authorization"]
            key_warnings.append(f"TRACEFOUR_API_KEY rejected (HTTP {resp.status_code}); using the anonymous "
                                f"tier (60 requests/hour). Re-enter the key without quotes or spaces.")
            logger.error(key_warnings[-1])
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else backoff
            if resp.status_code == 429:
                try:
                    body = resp.json()
                    data = body.get("data") if isinstance(body, dict) else None
                    wait = max(wait, float((data or {}).get("retryAfterSec") or 0))
                except (ValueError, TypeError, AttributeError):
                    pass
                # One short wait is fine; a second 429 means the hourly quota is spent, so stop asking this run.
                if wait > config.MAX_RATE_LIMIT_WAIT_SECONDS or attempt > 0:
                    _rate_limited_until = time.time() + max(wait, config.MAX_RATE_LIMIT_WAIT_SECONDS)
                    hint = "" if config.TRACEFOUR_API_KEY else "; set TRACEFOUR_API_KEY for a higher limit"
                    _fail(url, f"rate limited for {wait:.0f}s{hint}")
                    return None
            logger.warning("HTTP %d on %s, retrying in %.0fs", resp.status_code, url, wait)
            _sleep(wait)
            continue
        if resp.status_code >= 400:
            _fail(url, f"HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError:
            _fail(url, "non-JSON response")
            return None
    _fail(url, f"gave up after {config.HTTP_MAX_RETRIES} attempts")
    return None


def load_committee_overrides() -> Dict[str, List[str]]:
    """Optional data/committees.json: {"member-slug": ["Committee A", ...]}. Tracefour has no committee data."""
    path = Path(config.COMMITTEES_PATH)
    if not path.exists():
        return {}
    try:
        return {k: list(v) for k, v in json.loads(path.read_text()).items()}
    except (ValueError, AttributeError) as e:
        logger.warning("Ignoring unreadable %s: %s", path, e)
        return {}


def normalize_action(raw_type: Optional[str]) -> Optional[str]:
    t = (raw_type or "").strip().lower()
    if t.startswith("purchase") or t == "buy":
        return "BUY"
    if t.startswith("sale") or t == "sell":
        return "SELL"
    return None


def normalize_trade(raw: Dict[str, Any], member_slug: str, member: Dict[str, Any],
                    committees: List[str]) -> Optional[Dict[str, Any]]:
    ticker = (raw.get("ticker") or "").upper().strip()
    action = normalize_action(raw.get("type"))
    tx_date = raw.get("transactionDate") or ""
    disc_date = raw.get("disclosureDate") or ""
    if not TICKER_RE.match(ticker) or not action or not DATE_RE.match(tx_date) or not DATE_RE.match(disc_date):
        return None
    amount = raw.get("amountLabel") or "Unknown"
    chamber = (raw.get("chamber") or member.get("chamber") or "").title() or None
    district = raw.get("district") or member.get("district")
    return {
        # filingId already encodes member|ticker|date|type|amount|owner|asset, so it is unique per line item.
        "trade_uid": raw.get("filingId") or f"{member_slug}|{ticker}|{tx_date}|{action}|{amount}",
        "filer_type": "congress",
        "filer_role": " ".join(p for p in (chamber, district) if p) or None,
        "member_slug": member_slug,
        "member_name": raw.get("memberName") or member.get("memberName") or member_slug.replace("-", " ").title(),
        "chamber": chamber,
        "party": raw.get("party") or member.get("party"),
        "committees": committees,
        "ticker": ticker,
        "action": action,
        "amount_range": amount,
        "amount_min": raw.get("amountMin"),
        "transaction_date": tx_date,
        "disclosure_date": disc_date,
        "owner": raw.get("owner"),
        "asset_type": raw.get("assetType"),
        "asset_description": raw.get("assetDescription"),
        "source_link": raw.get("sourceLink"),
    }


def fetch_member_trades_tracefour(member_slug: str, committees: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    payload = get_json_with_retry(f"{config.TRACEFOUR_BASE}/{member_slug}")
    _sleep(config.TRACEFOUR_DELAY_SECONDS)
    if not payload:
        return []
    data = payload.get("data") or {}
    member = data.get("member") or {}
    trades = []
    for raw in data.get("trades") or []:
        t = normalize_trade(raw, member_slug, member, committees or [])
        if t:
            trades.append(t)
    logger.info("Fetched %d usable trades for %s", len(trades), member_slug)
    return trades


def poll_all_target_members(members: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    committees = load_committee_overrides()
    all_filings: List[Dict[str, Any]] = []
    for slug in members or config.TARGET_MEMBERS:
        logger.info("Polling ingestion data for member: %s", slug)
        all_filings.extend(fetch_member_trades_tracefour(slug, committees.get(slug)))
    return all_filings


def fetch_member_directory() -> List[Dict[str, Any]]:
    """All members Tracefour tracks: [{slug, name, chamber, district, url}]."""
    payload = get_json_with_retry(config.TRACEFOUR_BASE)
    return (payload or {}).get("data") or []


# --- Corporate insiders (SEC Form 4) ----------------------------------------------------

def _get_list(path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    payload = get_json_with_retry(f"{config.TRACEFOUR_API}/{path}" + (f"?{query}" if query else ""))
    _sleep(config.TRACEFOUR_DELAY_SECONDS)
    data = (payload or {}).get("data")
    return data if isinstance(data, list) else []


def fetch_insider_clusters() -> Dict[str, Dict[str, Any]]:
    """Buy clusters keyed by ticker: several distinct insiders buying the same stock in a short window."""
    clusters = {}
    for c in _get_list("clusters"):
        ticker = (c.get("issuerTicker") or "").upper()
        if c.get("direction") == "buy" and TICKER_RE.match(ticker):
            clusters[ticker] = {
                "insider_count": c.get("insiderCount"),
                "conviction": c.get("conviction"),
                "total_value": c.get("totalValue"),
                "seniority_mix": c.get("seniorityMix"),
                "is_live": c.get("isLive"),
                "first_filed": (c.get("firstFiledAt") or "")[:10],
                "last_filed": (c.get("lastFiledAt") or "")[:10],
            }
    return clusters


def insider_role(row: Dict[str, Any]) -> str:
    roles = []
    if row.get("ownerTitle"):
        roles.append(row["ownerTitle"].strip())
    elif row.get("isOfficer"):
        roles.append("Officer")
    if row.get("isDirector"):
        roles.append("Director")
    if row.get("isTenPercentOwner"):
        roles.append("10% Owner")
    return ", ".join(roles) or (row.get("otherText") or "Other")


def _accession(filing_id: str) -> str:
    """Tracefour line-item ids are '<accession>-<n>'; accession numbers themselves contain two dashes."""
    return filing_id.rsplit("-", 1)[0] if filing_id.count("-") >= 3 else filing_id


def normalize_insider_rows(rows: Iterable[Dict[str, Any]],
                           clusters: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Collapses Form 4 line items into one signal per (filing, owner, direction)."""
    groups: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        code = r.get("transactionCode")
        ticker = (r.get("issuerTicker") or "").upper().strip()
        filed = (r.get("filedAt") or "")[:10]
        if code not in ("P", "S") or not TICKER_RE.match(ticker) or not DATE_RE.match(filed):
            continue
        key = (_accession(r.get("filingId") or ""), str(r.get("cik") or r.get("ownerName")), code, ticker)
        groups.setdefault(key, []).append(r)

    out = []
    for (accession, owner_id, code, ticker), items in groups.items():
        items.sort(key=lambda i: i.get("filingId") or "")
        first, last = items[0], items[-1]
        value = sum(float(i.get("totalValue") or 0) for i in items)
        shares = sum(float(i.get("shares") or 0) for i in items)
        owned_after = float(last.get("sharesOwnedAfter") or 0)
        before = owned_after - shares if code == "P" else owned_after + shares
        tx_dates = sorted(d for d in (i.get("transactionDate") for i in items) if d and DATE_RE.match(d))
        if not tx_dates:
            continue
        out.append({
            "trade_uid": f"form4|{accession}|{owner_id}|{code}",
            "filer_type": "insider",
            "filer_role": insider_role(first),
            "member_slug": f"insider-{owner_id}",
            "member_name": (first.get("ownerName") or "Unknown insider").strip(),
            "chamber": None,
            "party": None,
            "committees": [],
            "ticker": ticker,
            "action": "BUY" if code == "P" else "SELL",
            "amount_range": f"${value:,.0f}",
            "amount_min": value,
            "transaction_date": tx_dates[0],
            "disclosure_date": (first.get("filedAt") or "")[:10],
            "owner": "Indirect" if first.get("ownershipType") == "I" else "Direct",
            "asset_type": "Stock",
            "asset_description": first.get("issuerName"),
            "source_link": first.get("rawFilingUrl") or first.get("url"),
            "signal_context": {
                "shares": shares,
                "avg_price": round(value / shares, 4) if shares else None,
                "total_value": round(value, 2),
                "shares_owned_after": owned_after,
                "position_change_pct": round(shares / before * 100, 1) if before > 0 else None,
                "is_10b5_1": any(bool(i.get("is10b5_1")) for i in items),
                "is_officer": any(bool(i.get("isOfficer")) for i in items),
                "is_director": any(bool(i.get("isDirector")) for i in items),
                "is_ten_percent_owner": any(bool(i.get("isTenPercentOwner")) for i in items),
                "line_items": len(items),
                "cluster": (clusters or {}).get(ticker) if code == "P" else None,
            },
        })
    return out


def poll_insider_buys(today: date, clusters: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    since = (today - timedelta(days=config.INSIDER_LOOKBACK_DAYS)).isoformat()
    # The API's min_value is per line item; the per-filing threshold is applied after grouping.
    rows = _get_list("filings", {"direction": "P", "since": since, "min_value": 10000, "limit": 500})
    if len(rows) >= 500:
        logger.warning("Insider feed hit the 500-row limit; some filings since %s may be missing", since)
    signals = normalize_insider_rows(rows, clusters)
    logger.info("Fetched %d insider purchase filings since %s", len(signals), since)
    return signals


def poll_insider_sells(open_positions: Iterable[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    """Sales by insiders we currently mirror, so their exits can be followed."""
    held = {p["ticker"] for p in open_positions if str(p.get("member_slug", "")).startswith("insider-")}
    out: List[Dict[str, Any]] = []
    since = (today - timedelta(days=60)).isoformat()
    for ticker in sorted(held):
        out.extend(normalize_insider_rows(_get_list("filings", {"direction": "S", "ticker": ticker,
                                                                "since": since, "limit": 500})))
    return out


# --- Executive branch (OGE Form 278-T) ------------------------------------------------

def normalize_oge_tracker(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    identity = data.get("identity") or {}
    slug = identity.get("slug") or "oge-filer"
    sources = {h.get("filedAt"): h.get("sourceUrl") for h in data.get("filingHistory") or []}
    seen: Dict[str, Dict[str, Any]] = {}
    for tx in data.get("ogeTransactions") or []:
        ticker = (tx.get("ticker") or "").upper().strip()
        action = normalize_action(tx.get("transactionType"))
        traded, filed = tx.get("tradedDate") or "", tx.get("filedDate") or ""
        if not TICKER_RE.match(ticker) or not action or not DATE_RE.match(traded) or not DATE_RE.match(filed):
            continue
        uid = f"oge|{slug}|{ticker}|{traded}|{action}|{int(tx.get('valueLow') or 0)}"
        if uid in seen:  # same bucket, same day, other trust account: one signal, counted
            seen[uid]["signal_context"]["duplicate_lines"] += 1
            continue
        seen[uid] = {
            "trade_uid": uid,
            "filer_type": "executive",
            "filer_role": identity.get("role"),
            "member_slug": slug,
            "member_name": identity.get("displayName") or slug,
            "chamber": None,
            "party": None,
            "committees": [],
            "ticker": ticker,
            "action": action,
            "amount_range": tx.get("valueLabel") or "Unknown",
            "amount_min": float(tx.get("valueLow") or 0),
            "transaction_date": traded,
            "disclosure_date": filed,
            "owner": "Trustee-managed" if "trustee" in (identity.get("role") or "").lower() else None,
            "asset_type": "Stock",
            "asset_description": tx.get("issuerName"),
            "source_link": sources.get(filed) or f"https://tracefour.com/trackers/{slug}",
            "signal_context": {"value_low": tx.get("valueLow"), "value_high": tx.get("valueHigh"),
                               "duplicate_lines": 1},
        }
    return list(seen.values())


def poll_executive_filers() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tracker in _get_list("trackers"):
        if tracker.get("archetype") != "oge_278t" or not tracker.get("slug"):
            continue
        payload = get_json_with_retry(f"{config.TRACEFOUR_API}/trackers/{tracker['slug']}")
        _sleep(config.TRACEFOUR_DELAY_SECONDS)
        filings = normalize_oge_tracker((payload or {}).get("data") or {})
        logger.info("Fetched %d executive-branch trades for %s", len(filings), tracker["slug"])
        out.extend(filings)
    return out


# --- All sources ----------------------------------------------------------------------

def poll_all_sources(today: date, open_positions: Iterable[Dict[str, Any]] = (), now: Optional[datetime] = None,
                     full_congress: bool = True) -> List[Dict[str, Any]]:
    from src import database as db, house_clerk, watchlist  # these import this module

    now = now or datetime.now(timezone.utc)
    key_warnings.clear()
    if config.TRACEFOUR_API_KEY:  # give the key a fresh chance each run (a long-lived dashboard reuses this module)
        _session.headers["Authorization"] = f"Bearer {config.TRACEFOUR_API_KEY}"
    open_positions = list(open_positions)
    filings: List[Dict[str, Any]] = []
    insider_groups: Dict[str, str] = {}
    previous = json.loads(db.get_meta("source_health") or "{}")
    health: Dict[str, Dict[str, Any]] = {}

    def run(name: str, fn: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        start = len(failures)
        try:
            items = fn()
            errors = failures[start:]
        except Exception as e:  # a broken source must never take the others down with it
            logger.exception("Source %s failed", name)
            items, errors = [], failures[start:] + [f"{type(e).__name__}: {e}"]
        ok = not errors
        health[name] = {"ok": ok, "items": len(items), "errors": errors[:5], "checked_at": now.isoformat(),
                        "last_success": now.isoformat() if ok else (previous.get(name) or {}).get("last_success")}
        return items

    if config.ENABLE_CONGRESS:
        filings.extend(run("house_clerk", lambda: house_clerk.poll_house(today)))
        legislators = house_clerk.load_legislators()
        others = [s for s in config.TARGET_MEMBERS if house_clerk.chamber_of(s, legislators) != "House"]
        if others:
            filings.extend(run("senate_targets", lambda: poll_all_target_members(others)))
        if full_congress and config.TRACEFOUR_API_KEY:
            filings.extend(run("senate_family", lambda: watchlist.poll_congress_family(now, open_positions)))
        elif full_congress and watchlist.congress_family_enabled():
            health["senate_family"] = {"ok": True, "skipped": "needs TRACEFOUR_API_KEY (60 requests/hour anonymous)",
                                       "items": 0, "errors": [], "checked_at": now.isoformat(), "last_success": None}
    if config.ENABLE_INSIDERS:
        clusters: Dict[str, Dict[str, Any]] = {}

        def _clusters() -> List[Dict[str, Any]]:
            clusters.update(fetch_insider_clusters())
            ranked = sorted((dict(c, ticker=t) for t, c in clusters.items()),
                            key=lambda c: (not c.get("is_live"), -(c.get("insider_count") or 0)))
            db.set_meta("insider_clusters", json.dumps(ranked[:12]))  # the public site reads this snapshot
            return list(clusters.values())

        run("insider_clusters", _clusters)

        def _watchlist() -> List[Dict[str, Any]]:
            nonlocal insider_groups
            wl_filings, wl_status = watchlist.poll_watchlist_insiders(clusters)
            db.set_meta("watchlist_status", json.dumps(wl_status))
            insider_groups = watchlist.watchlist_insider_slugs(wl_status)
            return wl_filings

        filings.extend(run("watchlist", _watchlist))
        filings.extend(run("insider_form4", lambda: poll_insider_buys(today, clusters)
                           + poll_insider_sells(open_positions, today)))
    if config.ENABLE_EXECUTIVE:
        filings.extend(run("executive_oge", poll_executive_filers))
    watchlist.tag_filings(filings, insider_groups)
    if config.TRACEFOUR_API_KEY:
        health["tracefour_key"] = {"ok": not key_warnings, "items": 0, "errors": key_warnings[:1],
                                   "checked_at": now.isoformat(),
                                   "last_success": now.isoformat() if not key_warnings
                                   else (previous.get("tracefour_key") or {}).get("last_success")}
    db.set_meta("source_health", json.dumps({**previous, **health}))
    return filings
