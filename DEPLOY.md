# Cloud deploy (Windows + Mac + iPhone sync)

One hosted app + one Postgres database. Every device opens the same HTTPS URL.

## What I already prepared in this repo

- `Dockerfile`, `Procfile`, `runtime.txt`, `render.yaml`
- Postgres driver (`psycopg`) in `requirements.txt`
- Safer DB engine settings for Neon in `app/database.py`
- `scripts/backup_sqlite.py` — local DB backup
- `scripts/migrate_sqlite_to_postgres.py` — copy local data → Neon **without deleting** your SQLite file
- Startup still runs `create_all` + migrations + seed only if empty (won’t wipe existing data)

## You must do these account steps (I can’t log into Neon/Render for you)

### A) Backup local data first

In PowerShell from the project folder:

```powershell
.\.venv\Scripts\python.exe scripts\backup_sqlite.py
```

Also optional: in the app, Capture → Reports → download workbook.

### B) Create Neon Postgres (free)

1. Open https://console.neon.tech and sign up / sign in  
2. Create a project (e.g. `beam-farm-ops`)  
3. Open **Dashboard → Connection details**  
4. Copy the connection string (looks like `postgresql://...@....neon.tech/neondb?sslmode=require`)  
5. Keep that string private — paste only into env vars / your local PowerShell session, never into chat if you can avoid it  

### C) Load your farm data into Neon

```powershell
cd C:\Users\Driveshaft\Projects\beam-farm-ops
$env:DATABASE_URL = "postgresql://USER:PASS@HOST/neondb?sslmode=require"
.\.venv\Scripts\pip.exe install "psycopg[binary]==3.2.6"
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py
```

Expected: script prints each table’s row count, then `DONE. Target fields= 38 users= 1` (your numbers).

If it says **REFUSED: target already has fields**, that Neon DB isn’t empty — use a fresh branch/DB, or only use `--force` if you understand the risk.

### D) Put the app on Render (HTTPS for all devices)

1. Push this repo to GitHub (if not already)  
2. Open https://dashboard.render.com → **New → Blueprint** (or Web Service)  
3. Connect the `beam-farm-ops` repo  
4. Use:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Environment variables:
   - `DATABASE_URL` = **same Neon string** as above  
   - `SECRET_KEY` = a long random string (Render can auto-generate, or use the one we generated locally)  
6. Deploy → wait until green  

### E) Use it everywhere

- Windows / Mac: open the Render HTTPS URL  
- iPhone: Safari → same URL → Share → **Add to Home Screen**  
- Login: same users as local (after migrate) — default was `admin` / `farm2026` unless you changed it. Change that password after first login if it’s still the default.

## Day-to-day updates later

1. Improve the app locally  
2. `git push`  
3. Render redeploys automatically  
Same Neon DB stays — your data does **not** reset on deploy.

## Safety rules

- Never point production `DATABASE_URL` at a brand-new empty Neon DB after you’ve already migrated once  
- Keep local SQLite backups (and Neon’s built-in backups)  
- Don’t commit `.env` or connection strings  

## After you finish B + C

Tell me when Neon is created and either:

- paste that you finished migrate (field count), or  
- set `DATABASE_URL` in your environment and ask me to re-run the migrate script  

Then I can help verify the live URL / fix whatever deploy error shows up.
