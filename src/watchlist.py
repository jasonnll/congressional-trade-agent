"""
src/watchlist.py
A subset of filers to follow closely because of their proximity to policy makers.

Only public disclosures are used:
  - named people who file SEC Form 4s (resolved to Tracefour insider slugs, or searched by name so
    someone who has never filed is picked up automatically if they start)
  - spouses and dependent children of Congress members (STOCK Act PTR `owner` field), across all members

People with no disclosure obligation (e.g. adult children of members, private individuals who are not
officers/directors/10% holders of a public company) cannot be monitored and are reported as "no filings".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

from src import config, database as db, ingestion
from src.ingestion import fetch_member_directory, fetch_member_trades_tracefour, get_json_with_retry, normalize_insider_rows

logger = logging.getLogger(__name__)

CONGRESS_FAMILY_GROUP = "Congress family"

DEFAULT_WATCHLIST: Dict[str, Any] = {
    "groups": [
        {
            "name": "Trump family",
            "people": [
                {"label": "Donald Trump Jr.", "slug": "trump-donald-j-jr"},
                {"label": "Eric Trump", "query": "trump eric"},
                {"label": "Jared Kushner", "slug": "kushner-jared-corey"},
            ],
        },
        {
            "name": "Lutnick family",
            "people": [
                {"label": "Brandon Lutnick", "slug": "lutnick-brandon"},
                {"label": "Kyle Lutnick", "slug": "lutnick-kyle"},
                {"label": "Howard Lutnick", "slug": "lutnick-howard-w"},
            ],
        },
    ],
    "congress_family": {"enabled": True, "all_members": True},
}


def load_watchlist() -> Dict[str, Any]:
    path = Path(config.WATCHLIST_PATH)
    if not path.exists():
        return DEFAULT_WATCHLIST
    try:
        data = json.loads(path.read_text())
        data.setdefault("groups", [])
        data.setdefault("congress_family", DEFAULT_WATCHLIST["congress_family"])
        return data
    except ValueError as e:
        logger.warning("Unreadable %s (%s); using built-in watchlist", path, e)
        return DEFAULT_WATCHLIST


def resolve_person(person: Dict[str, Any]) -> Optional[str]:
    if person.get("slug"):
        return person["slug"]
    query = (person.get("query") or "").strip().lower()
    if not query:
        return None
    payload = get_json_with_retry(f"{config.TRACEFOUR_API}/insiders?q={quote(query.split()[0])}&limit=50")
    ingestion._sleep(config.TRACEFOUR_DELAY_SECONDS)
    tokens = query.split()
    for row in (payload or {}).get("data") or []:
        name = (row.get("name") or "").lower()
        if all(t in name for t in tokens):
            return row.get("slug")
    return None


def poll_watchlist_insiders(clusters: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Form 4 trades (P/S) for every named watchlist person, plus a status row per person for the dashboard."""
    filings: List[Dict[str, Any]] = []
    status: List[Dict[str, Any]] = []
    for group in load_watchlist().get("groups", []):
        for person in group.get("people", []):
            label = person.get("label") or person.get("slug") or person.get("query")
            slug = resolve_person(person)
            entry = {"group": group["name"], "label": label, "slug": slug, "found": False, "tickers": [],
                     "last_seen": None, "cik": None}
            if slug:
                payload = get_json_with_retry(f"{config.TRACEFOUR_API}/insiders/{quote(slug)}")
                ingestion._sleep(config.TRACEFOUR_DELAY_SECONDS)
                data = (payload or {}).get("data") or {}
                identity = data.get("identity") or {}
                rows = data.get("transactions") or []
                entry.update(found=bool(identity), tickers=identity.get("tickers") or [],
                             last_seen=(identity.get("lastSeen") or "")[:10] or None,
                             cik=next((str(r.get("cik")) for r in rows if r.get("cik")), None))
                for f in normalize_insider_rows(rows, clusters):
                    f["watchlist_group"] = group["name"]
                    f["signal_context"]["watchlist_label"] = label
                    filings.append(f)
            status.append(entry)
    logger.info("Watchlist: %d people, %d open-market Form 4 signals", len(status), len(filings))
    return filings, status


def watchlist_insider_slugs(status: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """member_slug ('insider-<cik>') -> group, used to tag the same people when they appear in the market-wide feed."""
    return {f"insider-{s['cik']}": s["group"] for s in status if s.get("cik")}


def is_congress_family(filing: Dict[str, Any]) -> bool:
    return filing.get("filer_type", "congress") == "congress" and filing.get("owner") in config.CONGRESS_FAMILY_OWNERS


def congress_full_poll_due(now: datetime) -> bool:
    last = db.get_meta("congress_full_poll_at")
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(hours=config.CONGRESS_FULL_POLL_HOURS)


def poll_congress_family(now: datetime, open_positions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Senate spouse/dependent trades via Tracefour (House family trades come from the Clerk for every member).

    ~58 requests per sweep, so it needs a Tracefour API key: on the anonymous tier (60/hour) it would starve every
    other Tracefour source. The sweep is only marked done when every senator loaded.
    """
    cfg = load_watchlist().get("congress_family") or {}
    if not (cfg.get("enabled") and cfg.get("all_members")) or not congress_full_poll_due(now):
        return []
    if not config.TRACEFOUR_API_KEY:
        raise RuntimeError("Senate family sweep needs TRACEFOUR_API_KEY (anonymous tier is 60 requests/hour)")
    held: Set[Tuple[str, str]] = {(p["member_slug"], p["ticker"]) for p in open_positions}
    targets = set(config.TARGET_MEMBERS)
    start = len(ingestion.failures)
    out: List[Dict[str, Any]] = []
    senators = [m for m in fetch_member_directory()
                if m.get("slug") and m["slug"] not in targets and m.get("chamber") == "senate"]
    logger.info("Senate family sweep: %d senators", len(senators))
    for m in senators:
        for f in fetch_member_trades_tracefour(m["slug"]):
            # Keep family trades, plus any sale that would exit a position we mirror from this member.
            if is_congress_family(f) or (f["action"] == "SELL" and (f["member_slug"], f["ticker"]) in held):
                out.append(f)
    if len(ingestion.failures) == start:
        db.set_meta("congress_full_poll_at", now.astimezone(timezone.utc).isoformat())
    return out


def congress_family_enabled() -> bool:
    return bool((load_watchlist().get("congress_family") or {}).get("enabled"))


def tag_filings(filings: List[Dict[str, Any]], insider_groups: Dict[str, str]) -> None:
    family_on = congress_family_enabled()
    for f in filings:
        if f.get("watchlist_group"):
            continue
        if f.get("filer_type") == "insider" and f.get("member_slug") in insider_groups:
            f["watchlist_group"] = insider_groups[f["member_slug"]]
        elif family_on and is_congress_family(f):
            f["watchlist_group"] = CONGRESS_FAMILY_GROUP
