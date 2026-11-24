"""The public static site: what it contains, what it must never contain, and what it needs to build."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from src import config, database as db, site_export
from tests.conftest import make_filing

ROOT = Path(__file__).resolve().parent.parent


def test_export_writes_page_and_data(tmp_path):
    db.record_filing(make_filing(trade_uid="u1"), "STALE")
    db.set_meta("insider_clusters", json.dumps([{"ticker": "RMIX", "insider_count": 7}]))
    out = site_export.export(tmp_path / "site")
    html = (out / "index.html").read_text()
    assert "window.STATIC_SITE = true" in html and 'content="noindex, nofollow"' in html
    for name in ("summary", "filings", "positions", "performance"):
        assert (out / "api" / f"{name}.json").exists()
    summary = json.loads((out / "api" / "summary.json").read_text())
    assert summary["public_site"] and summary["view_only"] and summary["exported_at"]
    assert summary["clusters"][0]["ticker"] == "RMIX"
    assert [f["trade_uid"] for f in json.loads((out / "api" / "filings.json").read_text())] == ["u1"]


def test_export_never_contains_account_details_or_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY", "PKSECRETSECRETSECRET1")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-api03-secretsecretsecret")
    out = site_export.export(tmp_path / "site")
    summary = json.loads((out / "api" / "summary.json").read_text())
    assert summary["account"] == {"view_only": True}
    everything = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "SECRET" not in everything and "secretsecret" not in everything
    assert not re.search(r"sk-ant-|PK[A-Z0-9]{16}|tf_[A-Za-z0-9]{8}", everything)


def test_build_needs_no_trading_ai_pdf_or_http_libraries(tmp_path):
    """Netlify installs only python-dotenv; importing and running the export must not touch heavier packages."""
    script = (
        "import sys\n"
        "for m in ('alpaca', 'anthropic', 'pypdf', 'requests', 'httpx'):\n"
        "    sys.modules[m] = None  # any import of these now raises ImportError\n"
        "from pathlib import Path\n"
        "from src import site_export\n"
        f"site_export.export(Path({str(tmp_path / 'site')!r}))\n"
    )
    env = {"PATH": "/usr/bin:/bin", "DB_PATH": str(tmp_path / "ledger.db")}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    assert (tmp_path / "site" / "index.html").exists()
