# Beam Farm Ops — Feature wishlist (living)

Keep adding Musts anytime. This file is the source of truth across chats.

Priorities: **Must** = required for the product · **Should** = soon after · **Later** = important but not first.

## Must — Foundation (building now)

- [x] Login / sessions
- [x] Crop year switcher + farm name settings
- [x] Fields: acres, crop, rent, ownership modes (mine / shares / custom work)
- [x] Parties (landlord, partner, customer, buyer)
- [x] Dashboard acre / rent rollups
- [x] Excel import of crop acres (CLI + Master Upload UI)
- [ ] Cloud deploy (Neon Postgres + Railway/Render) for Mac/iPhone sync — do when ready to host

## Must — Core domain

### Plans & libraries
- [x] Planting / fertilizer / spray plans on fields
- [x] Hybrids/varieties library → assign to fields
- [x] Spray mixes library + upload → assign to fields
- [x] Sprayer fill calculator

### Purchases & field costs
- [x] Large input purchases → on-hand pool → assign to fields
- [x] Weighted average cost across purchases
- [x] Returns of unused product to pool
- [x] Field-level operations + expenses + $/acre rollups

### Custom work & settlements
- [x] Invoices for custom work + paid/unpaid
- [x] Landlord/partner year-end settlements + printable report
- [x] Lease/share terms on fields (cash / flex / crop share)

### Grain
- [x] Bins, fills, deliveries (subtract), split ownership of grain in bin
- [x] Bin-to-bin transfers
- [x] Expected production + harvest by field → bin or elevator
- [x] Scale tickets, settlements, moisture shrink
- [x] Photo uploads for tickets/contracts

### Sales / risk tracker
- [x] Contracts: cash, forward, HTA, basis, DP, min price, accumulator, custom
- [x] % sold, bu left on contract, apply deliveries to contracts
- [x] Partial pricing + HTA rolls
- [x] Breakeven / COP on risk screen
- [x] Delivery deadlines / due soon
- [x] Crop insurance basics

### Master Upload
- [x] Universal import hub (CSV/Excel/PDF/photo)
- [x] Cargill export recipe → contracts + deliveries
- [x] Column mapping + saved templates + duplicate detection
- [x] Panorama/20|20 file upload fallback

### Integrations
- [ ] **Panorama live API — ON HOLD** (waiting on Precision Planting partner access decision)
- [x] Panorama page + Leaf one-click code + file upload fallback (ready when keys arrive)
- [ ] FieldView — Later
- [ ] Cargill live API — not planned; use Master Upload

### Trucking
- [x] Rates table + load log

### Equipment — Must (accepted)

- [x] Equipment registry (tractor, planter, sprayer, combine, truck, etc.): make, model, year, serial, hours/acres, notes, photos
- [x] **Acquisition events:** buy, trade-in, gift/inherit; record date, price, seller, trade allowance
- [x] **Disposition events:** sell, trade away, scrap; record date, proceeds
- [x] **Ownership status per unit:** owned free & clear · owned with loan/payments · leased · rented/short-term
- [x] Loan/lease terms when applicable: payment amount, frequency, remaining balance, end date, lender/lessor
- [x] **Maintenance & repair log** per machine (date, description, cost, hours/odometer, vendor)
- [x] Other annual ownership costs: insurance, taxes, housing/storage (DIRTI-style)
- [x] Operating costs optional: fuel tied to machine when useful
- [x] **Cost per acre** views (per machine + fleet)
- [x] Market value vs book/cost basis (banker view defaults to market)
- [ ] Link equipment to custom-work invoices later (bill machine time)

### Balance sheet (banker pack) — Must (accepted)

- [x] **Balance sheet as-of date** with Assets / Liabilities / Equity
- [x] Assets: cash, grain inventory (from bins), receivables, prepaid inputs, equipment (market), land (manual), other
- [x] Liabilities: operating notes, equipment loans, land loans, payables, deferred crop income, other
- [x] Equity = Assets − Liabilities; working capital = current assets − current liabilities
- [x] Pull live where we can (equipment market values, grain bu, open invoices); allow manual overrides
- [x] Printable / PDF **banker pack** (balance sheet + equipment schedule + grain position summary)
- [x] Snapshot history (save a dated balance sheet for year-over-year)

### Team access (agronomist & accountant) — Must (accepted)

- [x] Invite additional users with roles (Owner, Agronomist, Accountant, Viewer)
- [x] Revoke / change role anytime
- [x] Audit-friendly: who changed what (basic activity log)
- [ ] When cloud-hosted: each person uses their own login on Mac/iPhone

### Crop trials — Must (accepted)

- [x] Attach a **crop trial** to a field + crop year
- [x] Trial metadata: name, crop, question/hypothesis, factor tested, design notes (strips/reps), start/end dates, status
- [x] **Treatments** with optional rep count / strip IDs
- [x] **Lots of notes** over the season dated and tagged to trial or treatment
- [x] **Results** per treatment/rep: yield bu/ac, moisture, test weight, notes; optional grain price → revenue/acre and delta vs control
- [x] Trial summary: winner, yield gap, $/acre gap, caveats
- [x] Photo attachments on notes/results (via Photos / Master Upload)
- [x] Multi-year trial history on a field (rotation + trials by year)

### Analysis & AI insights — Must (accepted)

- [x] **Insights** hub: trial comparisons, field $/acre and yield standouts, marketing position hooks as data grows
- [x] Simple **what-if / breakeven** helpers (price × yield − cost)
- [x] **AI briefing export**: one-click pack of farm/trial/field data (Markdown) to paste into ChatGPT/Claude/Cursor
- [ ] Optional later: in-app AI (API key)
- [x] Guardrails: AI suggestions are advisory; you and your agronomist decide

## Should

- [x] Soil tests → fert plans
- [x] Crop rotation history
- [x] Bin moisture/temp notes
- [x] Full Excel backup/export
- [x] Marketing targets (sell X% by date/price)

## Later / optional

- [ ] Live futures/basis feeds
- [ ] John Deere Ops Center
- [ ] Satellite / rainfall layers
- [ ] Full QuickBooks replacement (prefer export)
- [ ] Offline PWA
- [ ] In-app AI chat with live farm DB (after briefing export proves useful)
- [ ] FieldView
- [ ] Link equipment hours to custom-work invoices
