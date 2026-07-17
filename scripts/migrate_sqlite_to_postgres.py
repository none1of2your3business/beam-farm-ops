"""Copy local SQLite farm data into a cloud Postgres DATABASE_URL.

Safety:
  - Refuses if the target already has fields (unless --force)
  - Never deletes local SQLite
  - Creates schema on target first via SQLAlchemy models

Usage (PowerShell):
  $env:DATABASE_URL = "postgresql://...neon.tech/neondb?sslmode=require"
  .\\.venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py

  # Or:
  .\\.venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py --url "postgresql://..."
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


def _normalize_pg_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Beam Farm Ops SQLite → Postgres")
    parser.add_argument(
        "--sqlite",
        default=str(ROOT / "data" / "beam_farm_ops.db"),
        help="Path to source SQLite file",
    )
    parser.add_argument("--url", default="", help="Target Postgres URL (else DATABASE_URL env)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow copy even if target already has field rows (dangerous)",
    )
    args = parser.parse_args()

    src_path = Path(args.sqlite)
    if not src_path.exists():
        print("ERROR: SQLite file not found:", src_path)
        return 2

    target_url = _normalize_pg_url(args.url or os.getenv("DATABASE_URL", ""))
    if not target_url or target_url.startswith("sqlite"):
        print("ERROR: Set a Postgres DATABASE_URL (Neon) via --url or env DATABASE_URL")
        return 2

    # Import models only after path fix
    from app import models  # noqa: F401
    from app.models import Base

    src = create_engine(f"sqlite:///{src_path.as_posix()}", connect_args={"check_same_thread": False})
    dst = create_engine(target_url, pool_pre_ping=True)

    print("Source:", src_path)
    print("Target dialect:", dst.dialect.name)

    # Create empty schema on Postgres
    Base.metadata.create_all(bind=dst)

    src_insp = inspect(src)
    dst_insp = inspect(dst)
    src_tables = [t for t in src_insp.get_table_names() if not t.startswith("sqlite_")]

    # Safety gate
    with dst.connect() as conn:
        if "fields" in dst_insp.get_table_names():
            n = conn.execute(text("SELECT COUNT(*) FROM fields")).scalar() or 0
            if n and not args.force:
                print(f"REFUSED: target already has {n} fields. Pass --force only if you mean to add duplicates.")
                return 3

    # Parent-ish tables first, then the rest (FK-friendly order)
    preferred = [
        "users",
        "crop_years",
        "app_settings",
        "parties",
        "fields",
        "field_shares",
        "hybrids",
        "spray_mixes",
        "spray_mix_lines",
        "input_products",
        "grain_bins",
        "bin_shares",
        "grain_contracts",
        "equipment",
        "crop_trials",
        "trial_treatments",
    ]
    ordered = [t for t in preferred if t in src_tables]
    ordered += [t for t in sorted(src_tables) if t not in ordered]

    SrcSession = sessionmaker(bind=src)
    DstSession = sessionmaker(bind=dst)
    src_db = SrcSession()
    dst_db = DstSession()

    copied = {}
    replica_mode = False
    try:
        # Disable FK checks on Postgres during bulk load (may be restricted on some hosts)
        if dst.dialect.name == "postgresql":
            try:
                dst_db.execute(text("SET session_replication_role = replica"))
                replica_mode = True
            except Exception as exc:  # noqa: BLE001
                dst_db.rollback()  # clear aborted transaction on Neon
                print("Note: could not disable FK checks — using ordered inserts")
                print(" ", str(exc).split("\n")[0][:160])

        for table_name in ordered:
            table = Base.metadata.tables.get(table_name)
            if table is None:
                print("skip (no model):", table_name)
                continue
            rows = src_db.execute(table.select()).mappings().all()
            if not rows:
                copied[table_name] = 0
                continue
            cols = [c.name for c in table.columns]
            payload = []
            for row in rows:
                item = {k: row[k] for k in cols if k in row}
                payload.append(item)
            # chunk inserts
            chunk = 200
            for i in range(0, len(payload), chunk):
                dst_db.execute(table.insert(), payload[i : i + chunk])
            copied[table_name] = len(payload)
            print(f"  {table_name}: {len(payload)}")

        if dst.dialect.name == "postgresql":
            # Reset sequences so new inserts don't collide with copied IDs
            for table_name in ordered:
                table = Base.metadata.tables.get(table_name)
                if table is None or "id" not in table.c:
                    continue
                try:
                    dst_db.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {table_name}), 1))"
                        )
                    )
                except Exception:
                    pass
            if replica_mode:
                dst_db.execute(text("SET session_replication_role = DEFAULT"))

        dst_db.commit()
    except Exception:
        dst_db.rollback()
        raise
    finally:
        src_db.close()
        dst_db.close()

    # Verify
    with dst.connect() as conn:
        fields_n = conn.execute(text("SELECT COUNT(*) FROM fields")).scalar()
        users_n = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
    print("DONE. Target fields=", fields_n, "users=", users_n)
    print("Tables copied:", sum(1 for v in copied.values() if v), "with rows;", "total rows", sum(copied.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
