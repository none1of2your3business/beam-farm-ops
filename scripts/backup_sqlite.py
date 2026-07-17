"""Zip a timestamped backup of the local SQLite DB (+ optional Excel export reminder)."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "beam_farm_ops.db"
BACKUP_DIR = ROOT / "data" / "backups"


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"No database at {SRC}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"beam_farm_ops_{stamp}.db"
    shutil.copy2(SRC, dest)
    # Also keep a rolling "latest" copy
    latest = BACKUP_DIR / "beam_farm_ops_latest.db"
    shutil.copy2(SRC, latest)
    print("Backup written:", dest)
    print("Latest copy:", latest)
    print("Tip: also download Capture -> Reports workbook from the app for a human-readable backup.")


if __name__ == "__main__":
    main()
