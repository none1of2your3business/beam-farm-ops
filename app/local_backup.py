"""Manual Save button: dated .db backups on Desktop → General files → Farm Ops Backups."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from app.database import DATA_DIR, engine

LIVE_DB = DATA_DIR / "beam_farm_ops.db"
BACKUP_DIR = Path.home() / "Desktop" / "General files" / "Farm Ops Backups"
KEEP = 5
# Match: Farm Ops Save - Jul 20 2026 - 2-41 PM.db
NAME_RE = re.compile(
    r"^Farm Ops Save - .+ - \d{1,2}-\d{2} (AM|PM)\.db$",
    re.IGNORECASE,
)


def backup_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def readable_save_name(when: datetime | None = None) -> str:
    """e.g. Farm Ops Save - Jul 20 2026 - 2-41 PM.db"""
    when = when or datetime.now()
    # %-I drops leading zero on macOS/Linux; Windows would need %#I — we're on Mac.
    try:
        stamp = when.strftime("%b %-d %Y - %-I-%M %p")
    except ValueError:
        stamp = when.strftime("%b %d %Y - %I-%M %p").replace(" 0", " ")
    return f"Farm Ops Save - {stamp}.db"


def list_saves() -> list[Path]:
    """Newest first."""
    d = backup_dir()
    files = [p for p in d.glob("Farm Ops Save - *.db") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def prune_old_saves(keep: int = KEEP) -> int:
    removed = 0
    for path in list_saves()[keep:]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def create_save() -> tuple[Path, int]:
    """Copy live DB to backup folder. Returns (path, pruned_count)."""
    if not LIVE_DB.exists():
        raise FileNotFoundError(f"No live database at {LIVE_DB}")
    dest_dir = backup_dir()
    name = readable_save_name()
    dest = dest_dir / name
    # Avoid clobber if two saves in the same minute
    if dest.exists():
        stem = dest.stem
        n = 2
        while True:
            candidate = dest_dir / f"{stem} ({n}).db"
            if not candidate.exists():
                dest = candidate
                break
            n += 1
    # Ensure SQLite has flushed; dispose connections before copy for consistency
    engine.dispose()
    shutil.copy2(LIVE_DB, dest)
    pruned = prune_old_saves()
    return dest, pruned


def restore_save(filename: str) -> Path:
    """Replace live DB with a named save from the backup folder."""
    # Security: only allow basename files inside backup dir
    safe = Path(filename).name
    if ".." in safe or "/" in filename or "\\" in filename:
        raise ValueError("Invalid backup file name")
    src = backup_dir() / safe
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Backup not found: {safe}")
    if not safe.startswith("Farm Ops Save -") or not safe.endswith(".db"):
        raise ValueError("Not a Farm Ops save file")

    # Safety copy of current live DB before overwrite
    safety = DATA_DIR / "beam_farm_ops_pre_restore.db"
    engine.dispose()
    if LIVE_DB.exists():
        shutil.copy2(LIVE_DB, safety)
    shutil.copy2(src, LIVE_DB)
    engine.dispose()
    return src
