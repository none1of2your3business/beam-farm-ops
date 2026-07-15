import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "beam_farm_ops.db"
c = sqlite3.connect(db)
tables = [
    t[0]
    for t in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
print("tables", len(tables))
for t in tables:
    n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    if n:
        print(f"{t}\t{n}")
