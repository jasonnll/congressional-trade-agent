"""
src/pdf_reader.py
Reads scanned (image-only) House PTRs with Claude, once per document, so their trades enter the pipeline
without a manual transcription. Output rows use the same shape as data/manual_filings/<DocID>.json, and go
through every normal rule (entry minimums, batch filter, AI scoring) like any other filing.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

import anthropic
from pydantic import BaseModel, Field

from src import config

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 30 * 1024 * 1024  # the API accepts up to 32 MB per document


class PtrRow(BaseModel):
    owner: str = Field(description="Owner code exactly as printed: SP, DC, JT, or Self when blank.")
    asset_name: str = Field(description="Asset name as printed.")
    ticker: Optional[str] = Field(description="Stock ticker as printed in parentheses, or null if none is shown.")
    asset_type: str = Field(description="Asset type code if printed (e.g. ST, OP, GS, MF), else 'ST' for a "
                                        "plain common stock, else 'Unknown'.")
    type: str = Field(description="Transaction type exactly: 'P', 'S', 'S (partial)', or 'E'.")
    transaction_date: str = Field(description="Transaction date as YYYY-MM-DD.")
    amount_range: str = Field(description="Amount bracket as printed, e.g. '$15,001 - $50,000', or 'Unknown'.")
    readable: bool = Field(description="False if any of type, date, ticker or amount had to be guessed.")


class PtrTranscription(BaseModel):
    rows: List[PtrRow]
    notes: str = Field(description="One or two sentences on anything unreadable or left out.")


PROMPT = """This is a scanned U.S. House Periodic Transaction Report (STOCK Act). Transcribe every transaction row.
Rules:
- Copy values exactly as printed. Never infer a ticker from a company name: if no ticker is printed, use null.
- Dates as YYYY-MM-DD. The transaction date is the first date column, not the notification date.
- If any of type, date, ticker or amount is illegible or guessed, still include the row but set readable=false.
- Do not add rows that are not on the form."""


def transcribe(pdf: bytes, client: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """{"rows": [...manual_filings row shape...], "note": str} for a scanned PTR, or None if it can't be read.

    Rows marked unreadable, without a ticker, or not plain stock are dropped: a wrong trade is worse than a
    missing one.
    """
    if len(pdf) > MAX_PDF_BYTES:
        logger.warning("Scanned PTR too large to send (%d bytes)", len(pdf))
        return None
    if client is None:
        if not config.ANTHROPIC_API_KEY:
            return None
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=3)
    try:
        response = client.beta.messages.parse(
            model=config.PDF_READER_MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf).decode()}},
                {"type": "text", "text": PROMPT},
            ]}],
            output_format=PtrTranscription,
        )
    except anthropic.APIError as e:
        logger.warning("Scanned PTR transcription failed: %s", e)
        return None
    from src.ai_analyst import record_usage

    record_usage(config.PDF_READER_MODEL, response)
    parsed = response.parsed_output
    if parsed is None:
        return None
    rows = [r.model_dump() for r in parsed.rows
            if r.readable and r.ticker and r.asset_type in ("ST", "Unknown") and r.type in ("P", "S", "S (partial)")]
    dropped = len(parsed.rows) - len(rows)
    note = (f"Read by {config.PDF_READER_MODEL} from the scanned PDF; {len(rows)} stock rows kept, {dropped} "
            f"dropped (unreadable, no ticker, or not common stock). {parsed.notes}")
    return {"rows": rows, "note": note}
