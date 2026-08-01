#!/usr/bin/env python3.12
"""Daily farm DB backup → iCloud Drive, keep 30 days, delete older."""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "beam_farm_ops.db"
ICLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs"
BACKUP_DIR = ICLOUD / "Beam Farm Ops Backups"
KEEP_DAYS = 30


def main() -> int:
    if not SRC.exists():
        print(f"ERROR: no database at {SRC}", file=sys.stderr)
        return 1
    if not ICLOUD.exists():
        print(f"ERROR: iCloud Drive not found at {ICLOUD}", file=sys.stderr)
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"beam_farm_ops_{stamp}.db"
    shutil.copy2(SRC, dest)
    latest = BACKUP_DIR / "beam_farm_ops_latest.db"
    shutil.copy2(SRC, latest)

    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    for path in BACKUP_DIR.glob("beam_farm_ops_*.db"):
        if path.name == "beam_farm_ops_latest.db":
            continue
        # Parse timestamp from name beam_farm_ops_YYYYMMDD_HHMMSS.db
        try:
            ts = datetime.strptime(path.stem.replace("beam_farm_ops_", ""), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if ts < cutoff:
            path.unlink(missing_ok=True)
            removed += 1

    print(f"Backup written: {dest}")
    print(f"Latest copy: {latest}")
    print(f"Pruned {removed} backup(s) older than {KEEP_DAYS} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
