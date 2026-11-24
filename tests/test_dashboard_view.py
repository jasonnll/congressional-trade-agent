"""In view-only mode the dashboard must never modify the git-tracked ledger, and must survive it being replaced."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from src import config, dashboard


def _old_format_ledger(path: Path) -> None:
    """A ledger as GitHub might have committed it before the newest columns existed."""
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE active_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_uid TEXT UNIQUE NOT NULL,
            member_slug TEXT NOT NULL, ticker TEXT NOT NULL, entry_order_id TEXT NOT NULL, notional_amount REAL NOT NULL,
            filled_qty REAL DEFAULT 0.0, opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'OPEN',
            closed_at TIMESTAMP NULL, exit_reason TEXT NULL, exit_order_id TEXT NULL, pending_exit_reason TEXT NULL)""")
        conn.execute("CREATE TABLE performance_snapshots (snapshot_date TEXT PRIMARY KEY, invested REAL)")
        conn.execute("INSERT INTO active_positions (trade_uid, member_slug, ticker, entry_order_id, notional_amount) "
                     "VALUES ('u1', 'nancy-pelosi', 'NVDA', 'o1', 100)")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def view(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "ledger.db"
    _old_format_ledger(source)
    monkeypatch.setattr(config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(dashboard, "SOURCE_DB", source)
    monkeypatch.setattr(dashboard, "VIEW_DB", repo / ".dashboard" / "ledger.db")
    monkeypatch.setattr(dashboard, "_view_signature", None)
    monkeypatch.setattr(dashboard, "insider_clusters", lambda: [])
    return source


def test_view_only_never_modifies_the_tracked_ledger(view):
    before = _sha(view)
    dashboard.sync_ledger()
    assert _sha(view) == before
    assert Path(config.DB_PATH) == dashboard.VIEW_DB
    assert dashboard.summary()["sleeve_exposure"]["STANDARD"] == 100  # migrated copy answers new-format queries


def test_view_refreshes_when_the_ledger_is_replaced(view):
    dashboard.sync_ledger()
    replacement = view.with_name("incoming.db")
    _old_format_ledger(replacement)
    with sqlite3.connect(replacement) as conn:
        conn.execute("INSERT INTO active_positions (trade_uid, member_slug, ticker, entry_order_id, notional_amount) "
                     "VALUES ('u2', 'ro-khanna', 'AMD', 'o2', 100)")
    os.replace(replacement, view)  # what `git pull` does
    dashboard.sync_ledger()
    assert dashboard.summary()["open_positions"] == 2


def test_api_errors_return_json_instead_of_dropping_the_connection(view, monkeypatch):
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(dashboard, "summary", boom)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/summary", timeout=5)
        assert err.value.code == 500
        assert "database is locked" in json.loads(err.value.read())["error"]
    finally:
        server.shutdown()


def test_fetch_button_is_refused_in_view_only_mode(view):
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/ingest", method="POST", data=b"")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 409
    finally:
        server.shutdown()
