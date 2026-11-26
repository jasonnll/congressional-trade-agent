"""
src/house_clerk.py
House STOCK Act PTRs straight from the Clerk of the House (the primary source).

  Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
         -> {year}FD.xml rows: Prefix, Last, First, Suffix, FilingType ("P" = PTR), StateDst, Year, FilingDate, DocID
  PTR:   https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{DocID}.pdf

Electronically filed PTRs are text PDFs and are parsed here. Paper filings are scanned images with no text
layer; they are reported as unparseable (with their link) instead of being silently dropped.
"""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from pypdf import PdfReader

from src import config, database as db
from src.manual_filings import MANUAL_DIR, manual_transcription, rows_from_transcription  # noqa: F401

logger = logging.getLogger(__name__)

LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEE_MEMBERSHIP_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
COMMITTEES_URL = "https://unitedstates.github.io/congress-legislators/committees-current.json"
PARTY = {"Democrat": "D", "Republican": "R", "Independent": "I"}

# Non-discretionary transactions: real filings, but nobody decided to buy, so they are not signals.
NON_DISCRETIONARY = re.compile(
    r"dividend\s*re-?invest|\bDRIP\b|reinvest\w*\s+(?:of\s+)?dividend|automatic|auto-?invest|recurring|"
    r"merger|acquired\s+by|spin-?\s?off|stock\s+split|tender\s+offer|cash\s+proceeds|corporate\s+action|"
    r"exercise\s+of|vest(?:ed|ing)|gift(?:ed)?\b|inherit",
    re.I,
)

CLERK = "https://disclosures-clerk.house.gov/public_disc"
OWNER_CODES = {"SP": "Spouse", "JT": "Joint", "DC": "Dependent"}
TX_TYPES = {"P": "BUY", "S": "SELL", "S (partial)": "SELL", "S (Partial)": "SELL"}

# "... [ST] P 11/16/202611/21/2026 $1,001 - $15,000": the type follows the asset-type code, on the same line
# or the next; the two dates are often glued together and the amount may wrap onto a second line.
# The date pair is specific enough that the type letter may even be glued to the asset name at a page break.
ANCHOR = re.compile(
    r"(?P<type>S \([Pp]artial\)|P|S|E)\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s*(?P<ndate>\d{1,2}/\d{1,2}/\d{4})\s*"
    r"(?P<amount>(?:Spouse/DC\s+)?Over\s+\$[\d,]+|\$[\d,]+\s*-?\s*(?:\n\s*)?(?:\$[\d,]+)?)"
)
TICKER = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")
ASSET_CODE = re.compile(r"\[([A-Z]{2})\]")
META = re.compile(r"^\s*(F\s*S|S\s*O|D|L|C)\s*:", re.I)
OWNER_AT_START = re.compile(r"^\s*(SP|JT|DC)\s+")

_session = requests.Session()
_session.headers.update({"User-Agent": config.HTTP_USER_AGENT})


def _get(url: str, timeout: int = 60) -> Optional[bytes]:
    for attempt in range(config.HTTP_MAX_RETRIES):
        try:
            resp = _session.get(url, timeout=timeout)
        except requests.RequestException as e:
            logger.warning("Clerk request failed (%s), attempt %d: %s", url, attempt + 1, e)
            continue
        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 404:
            return None
        logger.warning("Clerk HTTP %d on %s", resp.status_code, url)
    raise ConnectionError(f"House Clerk unreachable: {url}")


def fetch_index(year: int) -> List[Dict[str, str]]:
    """All filings for a year. Raises ConnectionError if the Clerk cannot be reached (never returns a silent [])."""
    blob = _get(f"{CLERK}/financial-pdfs/{year}FD.zip")
    if blob is None:
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        root = ET.fromstring(zf.read(f"{year}FD.xml"))
    return [{c.tag: (c.text or "").strip() for c in m} for m in root]


def recent_ptrs(today: date, lookback_days: int) -> List[Dict[str, Any]]:
    years = {today.year}
    if (today - date(today.year, 1, 1)).days < lookback_days:
        years.add(today.year - 1)
    out = []
    for year in sorted(years):
        for r in fetch_index(year):
            if r.get("FilingType") != "P":
                continue
            try:
                filed = datetime.strptime(r["FilingDate"], "%m/%d/%Y").date()
            except (KeyError, ValueError):
                continue
            if (today - filed).days <= lookback_days:
                out.append({**r, "filed": filed.isoformat(), "year": year})
    return out


def pdf_url(doc: Dict[str, Any]) -> str:
    return f"{CLERK}/ptr-pdfs/{doc['year']}/{doc['DocID']}.pdf"


def extract_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    return text.replace("\x00", "")


def _iso(us_date: str) -> Optional[str]:
    try:
        return datetime.strptime(us_date, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _clean_amount(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.replace("\n", " ")).replace(" - ", " - ").strip(" -")


def _split_row_block(segment: str) -> Tuple[str, List[str]]:
    """Splits the text before an anchor into (metadata of the previous row, lines of this row's asset)."""
    lines = [ln for ln in segment.split("\n") if ln.strip()]
    tick_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if TICKER.search(lines[i]) or ASSET_CODE.search(lines[i]):
            tick_idx = i
            if TICKER.search(lines[i]):
                break
    if tick_idx is None:
        if lines and not META.match(lines[-1]):
            return "\n".join(lines[:-1]), lines[-1:]  # asset name cut off by a page break
        return "\n".join(lines), []
    start = tick_idx
    while start > 0 and not META.match(lines[start - 1]) and tick_idx - start < 3:
        if OWNER_AT_START.match(lines[start]):
            break
        start -= 1
    if META.match(lines[start]):
        start += 1
    return "\n".join(lines[:start]), lines[start:]


def _description(meta_text: str) -> str:
    m = re.search(r"(?:^|\n)\s*D\s*:\s*(.+?)(?=\n\s*(?:F\s*S|S\s*O|L|C)\s*:|\Z)", meta_text, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def parse_ptr_text(text: str) -> List[Dict[str, Any]]:
    """Transactions from one PTR's text layer. Rows without a ticker (bonds, private funds) are dropped."""
    start = re.search(r"\$200\?\s*\n", text)
    end = text.rfind("* For the complete list")
    body = text[start.end() if start else 0: end if end > 0 else len(text)]
    body = re.sub(r"Filing ID #\d+\s*\n?", "", body)
    body = re.sub(r"ID\s+Owner\s+Asset\s+Transaction\s*\nType\s*\nDate\s+Notification\s*\nDate\s*\nAmount\s+Cap\.\s*\nGains >\s*\n\$200\?\s*\n?", "", body)

    anchors = list(ANCHOR.finditer(body))
    rows: List[Dict[str, Any]] = []
    prev_end = 0
    for i, a in enumerate(anchors):
        prev_meta, asset_lines = _split_row_block(body[prev_end:a.start()])
        if rows and not rows[-1]["ticker"]:
            # A row split by a page break: the rest of its asset name (and ticker) opens the next segment.
            head = prev_meta.split("\n")
            spill = []
            while head and head[0].strip() and not META.match(head[0]):
                spill.append(head.pop(0))
            spill_text = " ".join(spill)
            if TICKER.search(spill_text):
                rows[-1]["ticker"] = TICKER.findall(spill_text)[-1]
                codes = ASSET_CODE.findall(spill_text)
                rows[-1]["asset_code"] = codes[-1] if codes else rows[-1]["asset_code"]
                rows[-1]["asset_name"] = re.sub(r"\s+", " ", f"{rows[-1]['asset_name']} {ASSET_CODE.sub('', spill_text)}").strip()
                prev_meta = "\n".join(head)
        if rows:
            rows[-1]["description"] = _description(prev_meta)
        asset_text = " ".join(asset_lines)
        owner_match = OWNER_AT_START.match(asset_text)
        tickers = TICKER.findall(asset_text)
        codes = ASSET_CODE.findall(asset_text)
        name = OWNER_AT_START.sub("", asset_text)
        name = ASSET_CODE.sub("", name).strip()
        rows.append({
            "owner": OWNER_CODES.get(owner_match.group(1), "Self") if owner_match else "Self",
            "asset_name": re.sub(r"\s+", " ", name),
            "ticker": tickers[-1] if tickers else None,
            "asset_code": codes[-1] if codes else None,
            "tx_code": a.group("type"),
            "action": TX_TYPES.get(a.group("type")),
            "transaction_date": _iso(a.group("tdate")),
            "notification_date": _iso(a.group("ndate")),
            "amount_range": _clean_amount(a.group("amount")),
            "description": "",
            "row": i,
        })
        prev_end = a.end()
    if rows:
        rows[-1]["description"] = _description(body[prev_end:])
    return rows


# --- Current legislators (party, canonical names) ---------------------------------------

def slugify(*parts: str) -> str:
    text = " ".join(p for p in parts if p).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text.replace("'", ""))
    return text.strip("-")


def load_legislators() -> Dict[str, Dict[str, Any]]:
    """{"house:HI01": {...}, "senate:RI:whitehouse": {...}}, refreshed at most daily, cached in the ledger."""
    cached = db.get_meta("legislators_v2")
    if cached:
        data = json.loads(cached)
        if datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"]) < timedelta(hours=24):
            return data["members"]
    try:
        raw = json.loads(_get(LEGISLATORS_URL) or b"[]")
    except (ConnectionError, ValueError) as e:
        logger.warning("Could not refresh legislators list: %s", e)
        return json.loads(cached)["members"] if cached else {}
    members: Dict[str, Dict[str, Any]] = {}
    for leg in raw:
        term = leg["terms"][-1]
        name = leg["name"]
        first = name.get("nickname") or name.get("first") or ""
        entry = {"slug": slugify(first, name["last"]), "last": slugify(name["last"]),
                 "name": name.get("official_full") or f"{first} {name['last']}",
                 "party": PARTY.get(term.get("party"), term.get("party")), "state": term["state"],
                 "district": term.get("district"), "chamber": "House" if term["type"] == "rep" else "Senate"}
        if term["type"] == "rep":
            members[f"house:{term['state']}{int(term.get('district') or 0):02d}"] = entry
        else:
            members[f"senate:{term['state']}:{slugify(name['last'])}"] = entry
    db.set_meta("legislators_v2", json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "members": members}))
    return members


def load_leaders() -> Dict[str, List[str]]:
    """{member_slug: [titles]} for current party leaders and full-committee chairs.

    Refreshed at most daily from unitedstates/congress-legislators and cached in the ledger; falls back to the
    committed snapshot (config.LEADERS_PATH) when the live data can't be read.
    """
    cached = db.get_meta("congress_leaders")
    if cached:
        data = json.loads(cached)
        if datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"]) < timedelta(hours=24):
            return data["leaders"]
    try:
        legislators = json.loads(_get(LEGISLATORS_URL) or b"[]")
        membership = json.loads(_get(COMMITTEE_MEMBERSHIP_URL) or b"{}")
        committees = {c["thomas_id"]: c["name"] for c in json.loads(_get(COMMITTEES_URL) or b"[]")}
        if not legislators or not membership:
            raise ValueError("empty legislators or committee data")
    except (ConnectionError, ValueError, KeyError) as e:
        logger.warning("Could not refresh congressional leaders: %s", e)
        if cached:
            return json.loads(cached)["leaders"]
        try:
            with open(config.LEADERS_PATH) as fh:
                return json.load(fh).get("leaders", {})
        except (OSError, ValueError):
            return {}
    today = date.today().isoformat()
    by_bioguide: Dict[str, str] = {}
    leaders: Dict[str, List[str]] = {}
    for leg in legislators:
        name = leg["name"]
        slug = slugify(name.get("nickname") or name.get("first") or "", name["last"])
        by_bioguide[leg["id"]["bioguide"]] = slug
        for role in leg.get("leadership_roles", []):
            if not role.get("end") or role["end"] >= today:
                leaders.setdefault(slug, []).append(role["title"])
    for code, members in membership.items():
        if len(code) != 4:  # subcommittees have longer codes
            continue
        for m in members:
            if m.get("title") in ("Chairman", "Chair") and m.get("bioguide") in by_bioguide:
                leaders.setdefault(by_bioguide[m["bioguide"]], []).append(f"Chair, {committees.get(code, code)}")
    db.set_meta("congress_leaders", json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(),
                                                "leaders": leaders}))
    return leaders


def load_committees() -> Dict[str, List[str]]:
    """{member_slug: ["House Committee on X", "...: Y Subcommittee (Chair)", ...]} for every current member.

    Refreshed at most daily from unitedstates/congress-legislators and cached in the ledger; falls back to the
    committed snapshot (config.COMMITTEES_PATH_SNAPSHOT) when the live data can't be read.
    """
    cached = db.get_meta("congress_committees")
    if cached:
        data = json.loads(cached)
        if datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"]) < timedelta(hours=24):
            return data["committees"]
    try:
        legislators = json.loads(_get(LEGISLATORS_URL) or b"[]")
        membership = json.loads(_get(COMMITTEE_MEMBERSHIP_URL) or b"{}")
        raw_committees = json.loads(_get(COMMITTEES_URL) or b"[]")
        if not legislators or not membership or not raw_committees:
            raise ValueError("empty legislators or committee data")
    except (ConnectionError, ValueError, KeyError) as e:
        logger.warning("Could not refresh committee assignments: %s", e)
        if cached:
            return json.loads(cached)["committees"]
        try:
            with open(config.COMMITTEES_PATH_SNAPSHOT) as fh:
                return json.load(fh).get("committees", {})
        except (OSError, ValueError):
            return {}
    names: Dict[str, str] = {}
    for c in raw_committees:
        names[c["thomas_id"]] = c["name"]
        for sub in c.get("subcommittees", []):
            names[c["thomas_id"] + sub["thomas_id"]] = f"{c['name']}: {sub['name']} Subcommittee"
    by_bioguide = {}
    for leg in legislators:
        name = leg["name"]
        by_bioguide[leg["id"]["bioguide"]] = slugify(name.get("nickname") or name.get("first") or "", name["last"])
    committees: Dict[str, List[str]] = {}
    for code, members in membership.items():
        for m in members:
            slug = by_bioguide.get(m.get("bioguide"))
            if slug:
                title = f" ({m['title']})" if m.get("title") else ""
                committees.setdefault(slug, []).append(names.get(code, code) + title)
    db.set_meta("congress_committees", json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(),
                                                   "committees": committees}))
    return committees


def chamber_of(slug: str, legislators: Dict[str, Dict[str, Any]]) -> Optional[str]:
    return next((m["chamber"] for m in legislators.values() if m["slug"] == slug), None)


def _resolve_member(doc: Dict[str, Any], legislators: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    sd = doc.get("StateDst") or ""
    leg = legislators.get(f"house:{sd[:2]}{int(sd[2:] or 0):02d}") if re.match(r"^[A-Z]{2}\d+$", sd) else None
    filed_last = slugify(doc.get("Last", ""))
    leg_last = leg.get("last") or slugify(leg["name"]).split("-")[-1] if leg else ""
    # "Kean" vs "Kean Jr.", "Taylor Greene" vs "Greene": match on either name containing the other's last word.
    if leg and (filed_last.split("-")[-1] in leg_last.split("-") or leg_last.split("-")[-1] in filed_last.split("-")):
        slug, name, party = leg["slug"], leg["name"], leg["party"]
    else:  # former member, vacancy change, or name mismatch: fall back to the filing's own name
        first = re.sub(r"[^A-Za-z\- ]", " ", doc.get("First", "")).split()
        slug = slugify(first[0] if first else "", doc.get("Last", ""))
        name, party = f"{doc.get('First', '')} {doc.get('Last', '')}".strip(), None
    targets = {t.split("-")[-1]: t for t in config.TARGET_MEMBERS}
    last = slug.split("-")[-1]
    if last in targets and targets[last][0] == slug[0]:
        slug = targets[last]  # keep the configured slug so positions and filters line up
    return {"slug": slug, "name": name, "party": party}


def _amount_low(amount: str) -> Optional[float]:
    m = re.search(r"\$([\d,]+)", amount)
    return float(m.group(1).replace(",", "")) if m else None


def normalize_house_rows(doc: Dict[str, Any], rows: List[Dict[str, Any]],
                         legislators: Dict[str, Dict[str, Any]], source: str = "house_clerk") -> List[Dict[str, Any]]:
    member = _resolve_member(doc, legislators)
    seen: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        if not r.get("ticker") or not r.get("action") or not r.get("transaction_date"):
            continue  # bonds, private funds, exchanges: nothing to mirror
        key = (r["owner"], r["ticker"], r["action"], r["transaction_date"], r["amount_range"])
        if key in seen:  # one trade split across sub-accounts
            seen[key]["signal_context"]["split_rows"] += 1
            continue
        seen[key] = {
            "trade_uid": f"house|{doc['DocID']}|{r['row']}",
            "filer_type": "congress",
            "filer_role": f"House {doc.get('StateDst', '')}".strip(),
            "member_slug": member["slug"],
            "member_name": member["name"],
            "chamber": "House",
            "party": member["party"],
            "committees": [],
            "ticker": r["ticker"].replace("-", "."),
            "action": r["action"],
            "amount_range": r["amount_range"],
            "amount_min": _amount_low(r["amount_range"]),
            "transaction_date": r["transaction_date"],
            "disclosure_date": doc["filed"],
            "owner": r["owner"],
            "asset_type": "Stock" if r.get("asset_code") == "ST" else (r.get("asset_code") or "Unknown"),
            "asset_description": r.get("asset_name"),
            "source_link": pdf_url(doc),
            "signal_context": {"source": source, "doc_id": doc["DocID"], "description": r.get("description") or "",
                               "tx_code": r.get("tx_code"), "split_rows": 1},
        }
    return list(seen.values())


def read_scanned_ptr(doc: Dict[str, Any], cached: Dict[str, Any]) -> bool:
    """Send a scanned PTR to Claude once (at most MAX_PDF_TRANSCRIPTION_ATTEMPTS tries). True if a call was made."""
    from src import pdf_reader

    attempts = json.loads(db.get_meta("pdf_read_attempts") or "{}")
    if attempts.get(doc["DocID"], 0) >= config.MAX_PDF_TRANSCRIPTION_ATTEMPTS or not config.ANTHROPIC_API_KEY:
        return False
    attempts[doc["DocID"]] = attempts.get(doc["DocID"], 0) + 1
    db.set_meta("pdf_read_attempts", json.dumps(attempts))
    pdf = _get(pdf_url(doc))
    result = pdf_reader.transcribe(pdf) if pdf else None
    if result is None:
        return True
    rows = rows_from_transcription(result["rows"])
    db.record_document({**cached, "status": "AI_READ", "row_count": len(rows), "ticker_rows": len(rows),
                        "rows_json": json.dumps(rows)})
    logger.info("Scanned PTR %s (%s) read by AI: %d stock rows. %s", doc["DocID"], cached.get("member_name"),
                len(rows), result["note"])
    return True


def poll_house(today: date) -> List[Dict[str, Any]]:
    """Every House PTR filed in the lookback window. New PDFs are downloaded once; parsed rows are cached."""
    legislators = load_legislators()
    filings: List[Dict[str, Any]] = []
    docs = recent_ptrs(today, config.HOUSE_LOOKBACK_DAYS)
    new_docs = 0
    ai_reads_left = config.MAX_PDF_TRANSCRIPTIONS_PER_RUN
    for doc in docs:
        cached = db.get_document(doc["DocID"])
        if cached is None:
            new_docs += 1
            record = {"doc_id": doc["DocID"], "source": "house_clerk", "filed_date": doc["filed"], "url": pdf_url(doc),
                      "member_name": f"{doc.get('First', '')} {doc.get('Last', '')}".strip(),
                      "state_district": doc.get("StateDst")}
            pdf = _get(pdf_url(doc))
            if pdf is None:
                db.record_document({**record, "status": "MISSING"})
                continue
            text = extract_text(pdf)
            if len(text.strip()) < 200:
                db.record_document({**record, "status": "NO_TEXT"})
            else:
                rows = parse_ptr_text(text)
                db.record_document({**record, "status": "PARSED", "row_count": len(rows),
                                    "ticker_rows": sum(1 for r in rows if r.get("ticker")),
                                    "rows_json": json.dumps(rows)})
            cached = db.get_document(doc["DocID"])
        if cached["status"] == "NO_TEXT" and not manual_transcription(doc["DocID"]) and ai_reads_left > 0:
            if read_scanned_ptr(doc, cached):
                ai_reads_left -= 1
                cached = db.get_document(doc["DocID"])
        if cached["status"] == "PARSED":
            filings.extend(normalize_house_rows(doc, json.loads(cached["rows_json"] or "[]"), legislators))
        elif cached["status"] in ("NO_TEXT", "AI_READ"):
            manual = manual_transcription(doc["DocID"])  # checked every run, so a transcription added later is used
            if manual:  # a person's transcription always wins over the AI read
                filings.extend(normalize_house_rows(doc, manual["rows"], legislators, source="manual_transcription"))
            elif cached["status"] == "AI_READ":
                filings.extend(normalize_house_rows(doc, json.loads(cached["rows_json"] or "[]"), legislators,
                                                    source="ai_transcription"))
    logger.info("House Clerk: %d PTRs in window (%d new), %d stock trades", len(docs), new_docs, len(filings))
    return filings
