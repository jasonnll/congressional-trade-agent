"""
src/setup_keys.py
Writes API keys into the project's .env with hidden input, so they never appear on screen or in chat.

  python -m src.setup_keys
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
FIELDS = [
    ("ALPACA_API_KEY", "Alpaca PAPER API key (starts with PK)"),
    ("ALPACA_SECRET_KEY", "Alpaca PAPER secret key"),
    ("ANTHROPIC_API_KEY", "Anthropic API key (starts with sk-ant-)"),
]


def set_value(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^#?\s*{key}=.*$", re.M)
    return pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"


def main() -> None:
    if not ENV.exists():
        shutil.copy(ROOT / ".env.example", ENV)
    text = ENV.read_text()
    print(f"Writing keys to {ENV}\nInput is hidden. Press Enter to keep a value that is already set.\n")
    for key, label in FIELDS:
        value = getpass.getpass(f"{label}: ").strip()
        if value:
            text = set_value(text, key, value)
    if re.search(r"^ALPACA_API_KEY=AK", text, re.M):
        print("\nThat Alpaca key starts with AK, which is a LIVE key. Not saved. Use your paper key (PK...).")
        return
    text = set_value(text, "ALPACA_PAPER", "true")
    ENV.write_text(text)
    os.chmod(ENV, 0o600)

    def looks_set(k: str) -> bool:
        m = re.search(rf"^{k}=(.*)$", text, re.M)
        return bool(m and m.group(1) and not m.group(1).startswith("your_"))

    print("\nSaved. " + ", ".join(f"{k}: {'set' if looks_set(k) else 'MISSING'}" for k, _ in FIELDS) + ", ALPACA_PAPER=true")


if __name__ == "__main__":
    main()
