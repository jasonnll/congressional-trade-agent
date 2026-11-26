"""
src/sectors.py
Sector of a stock, from the company's SEC SIC code (free: SEC EDGAR company_tickers.json + submissions API).
Cached per ticker in the ledger, so each company is looked up once. Used by the portfolio sector cap.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

import requests

from src import config, database as db

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# SEC asks automated clients to identify themselves.
SEC_HEADERS = {"User-Agent": "congressional-trade-agent research jason@nashvilleluxurylist.com"}

# (first SIC, last SIC, sector). Checked in order, so narrower ranges come first.
SIC_SECTORS = [
    (3720, 3729, "Aerospace & Defense"), (3760, 3769, "Aerospace & Defense"), (3480, 3489, "Aerospace & Defense"),
    (3812, 3812, "Aerospace & Defense"),
    (3570, 3579, "Technology"), (3600, 3699, "Technology"), (3670, 3679, "Technology"), (7370, 7379, "Technology"),
    (3820, 3829, "Technology"),
    (2830, 2836, "Health Care"), (3840, 3851, "Health Care"), (8000, 8099, "Health Care"), (8731, 8731, "Health Care"),
    (6500, 6599, "Real Estate"), (6798, 6798, "Real Estate"),
    (6000, 6799, "Financials"),
    (1300, 1399, "Energy"), (2900, 2999, "Energy"),
    (4900, 4999, "Utilities"),
    (4800, 4899, "Communication & Media"), (2700, 2799, "Communication & Media"), (7800, 7899, "Communication & Media"),
    (1000, 1499, "Materials"), (2400, 2699, "Materials"), (2800, 2899, "Materials"), (3000, 3399, "Materials"),
    (1500, 1799, "Industrials"), (3400, 3599, "Industrials"), (3700, 3799, "Industrials"), (3800, 3899, "Industrials"),
    (4000, 4799, "Industrials"), (8700, 8799, "Industrials"),
    (100, 999, "Consumer"), (2000, 2399, "Consumer"), (3900, 3999, "Consumer"), (5000, 5999, "Consumer"),
    (7000, 7299, "Consumer"), (7500, 7699, "Consumer"), (7900, 7999, "Consumer"),
]


def sector_of_sic(sic: Optional[int]) -> Optional[str]:
    if not sic:
        return None
    return next((name for lo, hi, name in SIC_SECTORS if lo <= sic <= hi), "Other")


_cik_cache: Dict[str, int] = {}
_sec_down = False  # set after the ticker list fails once, so a run doesn't retry it for every stock


def _ticker_ciks() -> Dict[str, int]:
    global _sec_down
    if _sec_down:
        raise requests.RequestException("SEC ticker list unavailable this run")
    if not _cik_cache:
        _sec_down = True
        resp = requests.get(TICKERS_URL, headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        for row in resp.json().values():
            _cik_cache[str(row["ticker"]).upper()] = int(row["cik_str"])
        _sec_down = False
    return _cik_cache


def sector(ticker: str) -> Optional[str]:
    """Sector name for `ticker`, or None when it can't be determined (never raises)."""
    ticker = ticker.upper()
    cached = db.get_meta(f"sector:{ticker}")
    if cached is not None:
        return json.loads(cached).get("sector")
    try:
        cik = _ticker_ciks().get(ticker) or _ticker_ciks().get(ticker.replace(".", "-"))
        if cik is None:
            db.set_meta(f"sector:{ticker}", json.dumps({"sector": None, "sic": None}))
            return None
        resp = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        sic = int(data["sic"]) if str(data.get("sic") or "").isdigit() else None
        name = sector_of_sic(sic)
        db.set_meta(f"sector:{ticker}", json.dumps({"sector": name, "sic": sic,
                                                    "sic_description": data.get("sicDescription")}))
        return name
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Sector lookup failed for %s: %s", ticker, e)
        return None  # not cached: try again next run
