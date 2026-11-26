"""
src/manual_filings.py
Manual transcriptions of scanned (paper) House PTRs: data/manual_filings/<DocID>.json.
Standard library only, so the public site build can read them too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

MANUAL_DIR = Path(__file__).resolve().parent.parent / "data" / "manual_filings"
MANUAL_TYPES = {"P": "BUY", "S": "SELL", "S (partial)": "SELL"}
OWNER_CODES = {"SP": "Spouse", "JT": "Joint", "DC": "Dependent"}


def manual_transcription(doc_id: str) -> Optional[Dict[str, Any]]:
    """Rows typed in for a scanned paper PTR, in house_clerk.parse_ptr_text's row shape; None if there is no file."""
    path = MANUAL_DIR / f"{doc_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return {"rows": rows_from_transcription(data.get("rows") or []), "note": data.get("source_note", "")}


def rows_from_transcription(raw_rows: list) -> list:
    """Transcribed rows (manual file or AI read) -> house_clerk.parse_ptr_text's row shape."""
    rows = []
    for i, r in enumerate(raw_rows):
        action = MANUAL_TYPES.get(r.get("type", ""))
        if not action or not r.get("ticker") or not r.get("transaction_date"):
            continue
        rows.append({"owner": OWNER_CODES.get(r.get("owner", ""), "Self"), "asset_name": r.get("asset_name"),
                     "ticker": r["ticker"].upper(), "asset_code": "ST", "tx_code": r["type"], "action": action,
                     "transaction_date": r["transaction_date"], "notification_date": None,
                     "amount_range": r.get("amount_range") or "Unknown", "description": "", "row": i})
    return rows
