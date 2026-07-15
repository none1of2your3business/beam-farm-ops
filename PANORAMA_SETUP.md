# Beam Farm Ops — Precision Planting Panorama setup

## Status: LIVE API ON HOLD

Waiting for Precision Planting to approve partner API access.  
App code + `/panorama` UI stay in place. Use **file upload** until keys arrive.

When they say yes, continue from Step 1 below (or tell me and we’ll wire keys together).

---

## What I already built for you

In the app at **http://127.0.0.1:8001/panorama**:

- Leaf one-click Panorama connect API
- Authorize link button
- Refresh / pull operations + files
- File upload fallback (20|20 / Panorama exports)
- Sync log

Config checker:

```powershell
cd C:\Users\Driveshaft\Projects\beam-farm-ops
.\.venv\Scripts\Activate.ps1
python tools\test_panorama_config.py
```

## What only you can do (accounts / partner approval)

Live sync needs **two** outside accounts. I cannot create these for you.

### Step 1 — Leaf account (API bridge)

1. Go to [https://withleaf.io](https://withleaf.io) (or demo/signup: [get a demo](https://withleaf.io/account/get-a-demo)).
2. Register / ask Sales for an API developer account.
3. Once you can log in, get a token either way:

**Option A — paste token in `.env`**

```env
LEAF_API_TOKEN=paste_your_jwt_here
```

**Option B — store Leaf login (app can request a token)**

```env
LEAF_USERNAME=your-leaf-email@example.com
LEAF_PASSWORD=your-leaf-password
```

How to get a token manually (optional):

```powershell
curl -X POST "https://api.withleaf.io/api/authenticate" `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"YOUR_EMAIL\",\"password\":\"YOUR_PASSWORD\",\"rememberMe\":true}"
```

Copy `id_token` into `LEAF_API_TOKEN`.

### Step 2 — Precision Planting Panorama partner credentials

Contact Precision Planting and ask to **register as a Panorama API / data-sharing partner** for your farm software (Beam Farm Ops).

Ask them for:

- Client ID  
- Partner username (email)  
- Partner password  
- Access to Partner Portal / STAGE vs PRODUCTION  

Put them in `.env`:

```env
PANORAMA_CLIENT_ID=...
PANORAMA_PARTNER_USERNAME=...
PANORAMA_PARTNER_PASSWORD=...
PANORAMA_CLIENT_ENVIRONMENT=STAGE
```

Use `STAGE` until they say you’re cleared for `PRODUCTION`.

**Who to contact:** your Precision Planting dealer, or Precision Planting support / digital products team — ask specifically for “Panorama partner API credentials for third-party sync (Leaf).”

### Step 3 — Your Panorama Organization Code (farmer side)

1. Log into the **Panorama** app/website with your normal grower account: [https://panorama.ag](https://panorama.ag)
2. Find your **Organization Code** (in account / sharing / organization settings — wording varies).
3. Keep that code handy for the next step.

### Step 4 — Connect inside Beam Farm Ops

1. Make sure the farm app is running (`run.bat` or uvicorn on port 8001).
2. Copy `.env.example` to `.env` if you don’t have `.env` yet, fill in the keys from Steps 1–2.
3. Restart the app so it reloads `.env`.
4. Run `python tools\test_panorama_config.py` — it should say **Live ready: YES**.
5. Open **http://127.0.0.1:8001/panorama**
6. Enter Organization Code + your grower name/email → **Start one-click connect**
7. Click **Open Panorama authorize link** within **15 minutes** and approve sharing.
8. Click **Refresh / pull operations**

When status shows **active**, sync is working.

### Step 5 — Until partner keys arrive (use today)

On the same `/panorama` page:

1. Export or copy data/files from Gen3 20|20 / Panorama (USB or export).
2. **Upload file** on the Panorama page.
3. Files land in `data/panorama_uploads/` and show in the sync log.

That keeps you moving without waiting on partner approval.

## Checklist

- [ ] Leaf account approved  
- [ ] `LEAF_API_TOKEN` or `LEAF_USERNAME` / `LEAF_PASSWORD` in `.env`  
- [ ] Precision Planting partner Client ID + user + password  
- [ ] `PANORAMA_*` values in `.env`  
- [ ] `test_panorama_config.py` says Live ready: YES  
- [ ] Panorama Organization Code found  
- [ ] Connected + authorized in Portal  
- [ ] Refresh shows ACTIVE  

## When you have the keys

Paste them into chat (or just put them in `.env` and tell me “keys are in .env”) and I can finish the connect/refresh with you and tighten how operations map onto your fields.
