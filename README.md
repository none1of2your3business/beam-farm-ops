# Beam Farm Ops

Cloud-ready farm operations app for corn and soybean farming. Track fields, ownership, grain, marketing, equipment, trials, and more — syncs across Mac and iPhone when hosted with a shared database.

Wishlist: [FEATURES.md](FEATURES.md). Panorama live API is **on hold** pending Precision Planting access.

## What works now

- Fields (detail with ops/$/acre, soil tests, rotation), parties, crop years, team roles, activity log
- Equipment + fuel + DIRTI $/acre + balance sheet + banker pack print + snapshots
- Crop trials, Insights + what-if + AI briefing download
- Grain bins (shares, transfers, moisture shrink, tickets, bin condition notes)
- Sales/risk: contracts, % sold, COP/breakeven, deadlines, insurance, HTA rolls, marketing targets
- Inputs & plans (hybrids, sprays with $/unit and $/ac, field plans), purchases (avg cost, assign, returns), invoices, settlements
- **Scan** — phone photo of receipts/statements, on-farm OCR, guided filing into purchases / invoices / tickets / contracts / archive
- Trucking rates/loads, sprayer fill calculator
- Master Upload (Cargill CSV + crop-acres Excel auto-import), photos, Excel backup export
- Panorama page (file upload live; API on hold)

## Run locally

```powershell
cd C:\Users\Driveshaft\Projects\beam-farm-ops
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

Or double-click **`run.bat`**. Open **http://127.0.0.1:8001** — `admin` / `farm2026`.

## Cross-platform sync (Windows + Mac + iPhone)

Follow **[DEPLOY.md](DEPLOY.md)** — Neon Postgres + Render HTTPS.

Short version:

1. Backup: `python scripts/backup_sqlite.py`
2. Create Neon DB → set `DATABASE_URL`
3. `python scripts/migrate_sqlite_to_postgres.py`
4. Deploy this repo to Render with the same `DATABASE_URL` + strong `SECRET_KEY`
5. Open the HTTPS URL everywhere (iPhone: Add to Home Screen)

Local SQLite stays in `data/beam_farm_ops.db` for offline work; production uses Neon.
