"""
src/site_export.py
Builds the public, read-only copy of the dashboard as static files (run by Netlify on every push to main).

  python -m src.site_export            -> site/index.html + site/api/*.json

Uses only the committed ledger and the standard library (plus python-dotenv via config): no API keys, no
network. The Alpaca account panel is never exported, so balances stay private even in live mode.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src import config, database as db, performance
from src.summary import build_summary

logger = logging.getLogger("SITE")
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).parent / "static" / "dashboard.html"
FEED_LIMIT = 1000


def build_payloads() -> Dict[str, Any]:
    db.init_db()
    summary = build_summary(json.loads(db.get_meta("insider_clusters") or "[]"), {"view_only": True}, True)
    summary["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary["public_site"] = True
    return {
        "summary": summary,
        "filings": db.recent_filings(FEED_LIMIT),
        "positions": db.all_positions(),
        "performance": performance.summary(),
    }


def render_index() -> str:
    html = TEMPLATE.read_text()
    flag = ('<meta name="robots" content="noindex, nofollow">\n'
            '<script>window.STATIC_SITE = true;</script>\n</head>')
    return html.replace("</head>", flag, 1)


def export(out_dir: Path) -> Path:
    payloads = build_payloads()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "api").mkdir(parents=True)
    for name, data in payloads.items():
        (out_dir / "api" / f"{name}.json").write_text(json.dumps(data, default=str))
    (out_dir / "index.html").write_text(render_index())
    logger.info("Exported site to %s (%d filings, %d positions)", out_dir,
                len(payloads["filings"]), len(payloads["positions"]))
    return out_dir


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT)
    export(ROOT / "site")
