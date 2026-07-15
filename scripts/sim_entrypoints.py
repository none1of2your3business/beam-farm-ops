"""
Stress-simulate authenticated GET/POST entry points against a temp DB copy.
Looks for 5xx, template UndefinedError traces, and exception bubbles.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC_DB = ROOT / "data" / "beam_farm_ops.db"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    if not SRC_DB.exists():
        print("No DB at", SRC_DB)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="beam-sim-"))
    sim_db = tmp / "beam_farm_ops.db"
    shutil.copy2(SRC_DB, sim_db)
    os.environ["DATABASE_URL"] = f"sqlite:///{sim_db.as_posix()}"
    # Force fresh engine on import
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from fastapi.testclient import TestClient
    from sqlalchemy import select, text

    from app.database import SessionLocal, engine
    from app.main import app
    from app.migrate import run_migrations
    from app.models import (
        CropYear,
        CropTrial,
        DocumentScan,
        Equipment,
        Field,
        GrainBin,
        GrainContract,
        ImportBatch,
        Invoice,
        Party,
        Settlement,
        User,
    )

    run_migrations()
    client = TestClient(app, raise_server_exceptions=False)
    findings: list[str] = []
    stats = Counter()

    def note(kind: str, msg: str):
        findings.append(f"{kind}: {msg}")
        stats[kind] += 1

    def looks_broken(resp, label: str) -> bool:
        stats["requests"] += 1
        body = resp.text or ""
        if resp.status_code >= 500:
            note("http5xx", f"{label} -> {resp.status_code}")
            # capture a short hint
            m = re.search(r"(UndefinedError|Error|Exception|Traceback)[^\n]{0,120}", body)
            if m:
                note("hint", m.group(0)[:160])
            return True
        if "Traceback (most recent call last)" in body or "jinja2.exceptions" in body:
            note("traceback", f"{label} -> {resp.status_code}")
            return True
        if "Internal Server Error" in body:
            note("ise", f"{label} -> {resp.status_code}")
            return True
        if resp.status_code == 422:
            note("validation", f"{label} -> 422")
            return False
        stats[f"status_{resp.status_code}"] += 1
        return False

    # Login
    r = client.post("/login", data={"username": "admin", "password": "farm2026"}, follow_redirects=False)
    if r.status_code not in (200, 303, 302):
        print("LOGIN FAILED", r.status_code)
        return 1
    # follow session
    client.post("/login", data={"username": "admin", "password": "farm2026"}, follow_redirects=True)

    db = SessionLocal()
    year = db.scalar(select(CropYear).where(CropYear.is_active == 1)) or db.scalar(select(CropYear).limit(1))
    fields = list(db.scalars(select(Field).limit(80)))
    bins = list(db.scalars(select(GrainBin).limit(40)))
    parties = list(db.scalars(select(Party).limit(40)))
    contracts = list(db.scalars(select(GrainContract).limit(40)))
    equipment = list(db.scalars(select(Equipment).limit(20)))
    settlements = list(db.scalars(select(Settlement).limit(20)))
    invoices = list(db.scalars(select(Invoice).limit(20)))
    scans = list(db.scalars(select(DocumentScan).limit(20)))
    trials = list(db.scalars(select(CropTrial).limit(20)))
    batches = list(db.scalars(select(ImportBatch).limit(10)))
    users = list(db.scalars(select(User).limit(20)))
    field_ids = [f.id for f in fields] or [1]
    bin_ids = [b.id for b in bins] or [1]
    party_ids = [p.id for p in parties] or [1]
    contract_ids = [c.id for c in contracts] or [1]
    equip_ids = [e.id for e in equipment] or [1]
    settle_ids = [s.id for s in settlements] or [1]
    invoice_ids = [i.id for i in invoices] or [1]
    scan_ids = [s.id for s in scans] or [1]
    trial_ids = [t.id for t in trials] or [1]
    batch_ids = [b.id for b in batches] or [1]
    user_ids = [u.id for u in users] or [1]
    db.close()

    # --- GET matrix ---
    get_paths = [
        "/",
        "/fields",
        "/fields?crop=Corn",
        "/fields?crop=Soybeans",
        "/fields?crop=None",
        "/fields?ownership=operated_by_me",
        "/fields?ownership=on_shares",
        "/fields?view=overview",
        "/fields/sheet",
        "/fields/sheet?crop=Corn",
        "/fields/new",
        "/parties",
        "/inputs",
        "/library",
        "/inputs/assign",
        "/purchases",
        "/inputs/upload",
        "/tools",
        "/risk",
        "/risk/contracts",
        "/risk/settings",
        "/risk/carry",
        "/bins",
        "/bins?view=overview",
        "/bins/ticket",
        "/bins/sheet",
        "/trucking",
        "/settlements",
        "/invoices",
        "/balance",
        "/balance/print",
        "/scan",
        "/upload",
        "/export",
        "/export/report?kind=fields",
        "/export/report?kind=bins",
        "/export/workbook",
        "/panorama",
        "/photos",
        "/insights",
        "/insights/ai-briefing",
        "/trials",
        "/equipment",
        "/equipment/new",
        "/settings",
        "/setting",
        "/team",
        "/activity",
        "/docs",
        "/openapi.json",
        "/logout",
    ]
    # re-login after logout check
    for path in get_paths:
        resp = client.get(path, follow_redirects=False)
        looks_broken(resp, f"GET {path}")
        if path == "/logout":
            client.post("/login", data={"username": "admin", "password": "farm2026"}, follow_redirects=True)

    # parameterized GETs
    for fid in field_ids[:25]:
        for path in (
            f"/fields/{fid}",
            f"/fields/{fid}/edit",
            f"/fields/{fid}/operations",
        ):
            looks_broken(client.get(path, follow_redirects=True), f"GET {path}")
    for eid in equip_ids[:10]:
        for path in (f"/equipment/{eid}", f"/equipment/{eid}/edit"):
            looks_broken(client.get(path, follow_redirects=True), f"GET {path}")
    for sid in settle_ids[:10]:
        looks_broken(client.get(f"/settlements/{sid}", follow_redirects=True), f"GET /settlements/{sid}")
    for tid in trial_ids[:10]:
        looks_broken(client.get(f"/trials/{tid}", follow_redirects=True), f"GET /trials/{tid}")
    for sid in scan_ids[:10]:
        looks_broken(client.get(f"/scan/{sid}", follow_redirects=True), f"GET /scan/{sid}")
    for bid in batch_ids[:5]:
        looks_broken(client.get(f"/inputs/upload/{bid}", follow_redirects=True), f"GET /inputs/upload/{bid}")
        looks_broken(client.get(f"/inputs/upload/{bid}/commit", follow_redirects=True), f"GET /inputs/upload/{bid}/commit")

    # bogus ids
    for path in (
        "/fields/999999",
        "/fields/999999/edit",
        "/fields/999999/operations",
        "/equipment/999999",
        "/settlements/999999",
        "/trials/999999",
        "/scan/999999",
        "/bins/999999/update",  # GET may 405
        "/inputs/upload/999999",
    ):
        looks_broken(client.get(path, follow_redirects=True), f"GET bogus {path}")

    # Crawl forms from HTML pages and submit mild variants
    crawl_pages = [
        "/",
        "/fields",
        "/fields/sheet",
        "/fields/new",
        f"/fields/{field_ids[0]}/edit",
        f"/fields/{field_ids[0]}/operations",
        "/parties",
        "/inputs",
        "/inputs/assign",
        "/purchases",
        "/tools",
        "/risk",
        "/risk/contracts",
        "/risk/settings",
        "/risk/carry",
        "/bins",
        "/bins/ticket",
        "/bins/sheet",
        "/trucking",
        "/settlements",
        "/invoices",
        "/balance",
        "/equipment",
        "/equipment/new",
        f"/equipment/{equip_ids[0]}" if equip_ids else "/equipment",
        "/settings",
        "/team",
        "/trials",
        "/scan",
        "/upload",
        "/panorama",
        "/photos",
    ]

    form_re = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
    action_re = re.compile(r'action=["\']([^"\']*)["\']', re.I)
    method_re = re.compile(r'method=["\']([^"\']*)["\']', re.I)
    input_re = re.compile(r"<(input|select|textarea)\b([^>]*)>", re.I)
    name_re = re.compile(r'\bname=["\']([^"\']+)["\']', re.I)
    type_re = re.compile(r'\btype=["\']([^"\']+)["\']', re.I)
    value_re = re.compile(r'\bvalue=["\']([^"\']*)["\']', re.I)
    option_re = re.compile(r"<option\b([^>]*)>", re.I)

    def extract_forms(html: str, page_url: str):
        out = []
        for m in form_re.finditer(html or ""):
            attrs, body = m.group(1), m.group(2)
            am = action_re.search(attrs)
            action = am.group(1) if am else page_url
            mm = method_re.search(attrs)
            method = (mm.group(1) if mm else "get").lower()
            fields_map: dict[str, list[str]] = {}
            for tag_m in input_re.finditer(body):
                tag, tattrs = tag_m.group(1).lower(), tag_m.group(2)
                nm = name_re.search(tattrs)
                if not nm:
                    continue
                name = nm.group(1)
                typ = (type_re.search(tattrs).group(1).lower() if type_re.search(tattrs) else "text")
                if typ in ("submit", "button", "image", "file"):
                    continue
                if typ == "checkbox":
                    continue
                val = value_re.search(tattrs).group(1) if value_re.search(tattrs) else ""
                if tag == "select":
                    # find first option value after this select is harder; use empty
                    opts = option_re.findall(body[tag_m.end() : tag_m.end() + 800])
                    picked = ""
                    for oa in opts:
                        ov = value_re.search(oa)
                        if ov and ov.group(1) != "":
                            picked = ov.group(1)
                            break
                    val = picked
                fields_map.setdefault(name, []).append(val)
            out.append((method, urljoin(page_url, action or page_url), fields_map))
        return out

    submitted = 0
    for page in crawl_pages:
        resp = client.get(page, follow_redirects=True)
        looks_broken(resp, f"CRAWL GET {page}")
        forms = extract_forms(resp.text, str(resp.url))
        for method, action, fmap in forms:
            path = urlparse(action).path or page
            if path.startswith("/docs") or path.startswith("/redoc"):
                continue
            # Variant A: as-is (existing values)
            data = {k: (v[0] if v else "") for k, v in fmap.items()}
            # list fields for multi
            multi = {k: v for k, v in fmap.items() if len(v) > 1}
            if multi:
                data = {k: v for k, v in fmap.items()}  # lists ok for testclient

            # avoid destructive team disable of admin on first pass — still run later with care
            if method == "get":
                r2 = client.get(path, params={k: (v if isinstance(v, str) else v[0]) for k, v in data.items()}, follow_redirects=True)
                looks_broken(r2, f"FORM GET {path} from {page}")
                submitted += 1
            else:
                # skip logout/login spam in crawl
                if path in ("/logout", "/login"):
                    continue
                r2 = client.post(path, data=data, follow_redirects=True)
                looks_broken(r2, f"FORM POST {path} from {page}")
                submitted += 1

            # Variant B: empty strings — only when form already had values (avoid pure 422 spam)
            if method == "post" and submitted < 500 and any(str(v).strip() for v in data.values() if not isinstance(v, list)):
                empty = {k: "" for k in fmap}
                for keep in ("field_id", "bin_id", "contract_id", "crop", "move_type", "year_id", "name", "title"):
                    if keep in data and data[keep] not in ("", None, []):
                        empty[keep] = data[keep]
                r3 = client.post(path, data=empty, follow_redirects=True)
                looks_broken(r3, f"FORM EMPTY {path} from {page}")
                submitted += 1

    # --- Targeted mutation storms ---
    mutations = []

    # year activate / add
    if year:
        mutations.append(("/years/activate", {"year_id": str(year.id)}))
    mutations.append(("/years/add", {"year": "2099", "label": "Sim 2099"}))

    # settings
    mutations.append(("/settings/farm", {"farm_name_value": "Beam Farm Sim"}))

    # fields save new + edit each field lightly
    mutations.append(
        (
            "/fields/save",
            {
                "name": "SIM Temp Field",
                "acres_total": "10",
                "acres_mine": "10",
                "crop": "Corn",
                "ownership_mode": "operated_by_me",
                "lease_type": "cash_rent",
                "rent_per_acre": "200",
                "expected_yield": "180",
                "notes": "sim",
            },
        )
    )
    for f in fields[:30]:
        mutations.append(
            (
                "/fields/save",
                {
                    "field_id": str(f.id),
                    "name": f.name,
                    "acres_total": str(f.acres_total or 0),
                    "acres_mine": str(f.acres_mine or 0),
                    "crop": f.crop or "None",
                    "ownership_mode": f.ownership_mode or "operated_by_me",
                    "lease_type": f.lease_type or "cash_rent",
                    "rent_per_acre": str(f.rent_per_acre or 0),
                    "expected_yield": str(f.expected_yield or ""),
                    "notes": f.notes or "",
                },
            )
        )
        mutations.append(
            (
                f"/fields/{f.id}/operation",
                {
                    "op_date": "2026-07-01",
                    "category": "other",
                    "description": "sim op",
                    "quantity": "1",
                    "unit": "ac",
                    "total_cost": "12.34",
                    "notes": "sim",
                },
            )
        )
        mutations.append(
            (
                f"/fields/{f.id}/yield",
                {"actual_yield": str((f.expected_yield or 50) + 1), "notes": "sim yield"},
            )
        )
        mutations.append(
            (
                f"/fields/{f.id}/soil",
                {"sample_date": "2026-01-15", "ph": "6.5", "om": "2.1", "notes": "sim soil"},
            )
        )

    # party
    mutations.append(
        ("/parties/save", {"name": "SIM Party", "party_type": "landlord", "phone": "", "email": "", "notes": ""})
    )
    for p in parties[:15]:
        mutations.append(
            (
                "/parties/save",
                {
                    "party_id": str(p.id),
                    "name": p.name,
                    "party_type": getattr(p, "party_type", None) or "landlord",
                    "phone": getattr(p, "phone", None) or "",
                    "email": getattr(p, "email", None) or "",
                    "notes": getattr(p, "notes", None) or "",
                },
            )
        )

    # library
    mutations.append(
        (
            "/library/hybrid",
            {
                "brand": "SIM",
                "name": "SIM Hybrid",
                "crop": "Corn",
                "trait_stack": "VT2P",
                "notes": "",
            },
        )
    )
    mutations.append(
        (
            "/library/spray",
            {
                "name": "SIM Spray",
                "product_type": "herbicide",
                "unit": "gal",
                "rate_per_acre": "1",
                "notes": "",
            },
        )
    )
    mutations.append(
        (
            "/library/plan",
            {
                "field_id": str(field_ids[0]),
                "plan_type": "planting",
                "title": "SIM Plan",
                "target_date": "2026-04-15",
                "estimated_cost_per_acre": "10",
                "details": "sim",
            },
        )
    )
    if fields:
        mutations.append(
            (
                "/library/assign-hybrid",
                {
                    "field_id": str(fields[0].id),
                    "hybrid_id": "1",
                    "rate_bags_per_ac": "0.3",
                    "notes": "sim",
                },
            )
        )
        mutations.append(
            (
                "/library/assign-spray",
                {
                    "field_id": str(fields[0].id),
                    "spray_id": "1",
                    "applied_date": "2026-06-01",
                    "rate": "1",
                    "notes": "sim",
                },
            )
        )

    # purchases
    mutations.append(("/purchases/product", {"name": "SIM Product", "unit": "gal", "category": "chem", "notes": ""}))
    mutations.append(
        (
            "/purchases/buy",
            {
                "product_id": "1",
                "purchase_date": "2026-03-01",
                "vendor": "SIM Vendor",
                "quantity": "10",
                "total_cost": "100",
                "notes": "",
            },
        )
    )
    mutations.append(
        (
            "/purchases/assign",
            {
                "product_id": "1",
                "field_id": str(field_ids[0]),
                "quantity": "1",
                "assign_date": "2026-05-01",
                "notes": "",
            },
        )
    )
    mutations.append(
        (
            "/purchases/return",
            {
                "product_id": "1",
                "quantity": "1",
                "return_date": "2026-05-02",
                "notes": "sim return",
            },
        )
    )

    # risk
    mutations.append(
        (
            "/risk/cop",
            {
                "corn_cost_per_ac": "650",
                "corn_price_assumption": "4.5",
                "soy_cost_per_ac": "400",
                "soy_price_assumption": "11",
            },
        )
    )
    mutations.append(
        (
            "/risk/insurance",
            {
                "crop": "Corn",
                "policy_type": "RP",
                "coverage_level": "85",
                "acres": "100",
                "premium": "5000",
                "guarantee_bu": "18000",
                "notes": "sim",
            },
        )
    )
    mutations.append(
        (
            "/risk/target",
            {
                "crop": "Corn",
                "target_pct": "50",
                "by_date": "2026-09-01",
                "price_floor": "4.25",
                "notes": "sim",
            },
        )
    )
    mutations.append(
        (
            "/risk/contract",
            {
                "crop": "Corn",
                "buyer": "SIM Buyer",
                "contract_type": "cash",
                "bushels": "5000",
                "price": "4.4",
                "delivery_start": "2026-10-01",
                "delivery_end": "2026-11-01",
                "status": "open",
                "notes": "sim",
            },
        )
    )
    for cid in contract_ids[:10]:
        mutations.append(
            (
                "/risk/event",
                {
                    "contract_id": str(cid),
                    "event_type": "note",
                    "event_date": "2026-07-01",
                    "bushels": "",
                    "price": "",
                    "futures_month": "",
                    "notes": "sim event",
                },
            )
        )
    mutations.append(("/risk/quotes", {}))
    mutations.append(("/risk/quotes/refresh", {"next": "/risk"}))
    mutations.append(
        (
            "/risk/carry",
            {
                "carry_interest_apr": "7.5",
                "carry_mark_mode": "assumption",
                "corn_futures": "",
                "soy_futures": "",
                "corn_local_basis": "-0.20",
                "soy_local_basis": "-0.35",
                "corn_storage_per_bu_mo": "0.03",
                "soy_storage_per_bu_mo": "0.04",
                "corn_shrink_per_bu_mo": "0.003",
                "soy_shrink_per_bu_mo": "0.004",
            },
        )
    )
    mutations.append(("/risk/stress", {"corn_price": "3.5", "soy_price": "9", "notes": "sim"}))

    # bins
    mutations.append(("/bins/create", {"name": "SIM Bin", "crop": "Corn", "capacity_bu": "40000"}))
    mutations.append(("/bins/carry-toggle", {"bins_show_carry": "1", "next": "/bins"}))
    mutations.append(("/bins/carry-toggle", {"bins_show_carry": "0", "next": "/bins"}))
    for bid in bin_ids[:15]:
        mutations.append(
            (
                f"/bins/{bid}/update",
                {
                    "name": next((b.name for b in bins if b.id == bid), f"Bin {bid}"),
                    "crop": "Corn",
                    "capacity_bu": "10000",
                    "on_hand_bu": "100",
                },
            )
        )
        mutations.append(
            (
                "/bins/condition",
                {
                    "bin_id": str(bid),
                    "note_date": "2026-07-01",
                    "moisture": "14",
                    "temperature": "60",
                    "notes": "sim",
                },
            )
        )
    mutations.append(
        (
            "/bins/move",
            {
                "move_type": "fill",
                "bin_id": str(bin_ids[0]),
                "to_bin_id": "",
                "field_id": str(field_ids[0]),
                "owner_name": "Me",
                "crop": "Corn",
                "wet_bu": "",
                "net_bu": "100",
                "moisture": "",
                "ticket_number": "SIM1",
                "destination": "",
                "contract_id": "",
                "notes": "sim fill",
                "move_date": "2026-07-01",
            },
        )
    )
    mutations.append(
        (
            "/bins/move",
            {
                "move_type": "transfer",
                "bin_id": str(bin_ids[0]),
                "to_bin_id": str(bin_ids[min(1, len(bin_ids) - 1)]),
                "field_id": "",
                "owner_name": "Me",
                "crop": "Corn",
                "wet_bu": "",
                "net_bu": "10",
                "moisture": "",
                "ticket_number": "",
                "destination": "",
                "contract_id": "",
                "notes": "sim xfer",
                "move_date": "2026-07-01",
            },
        )
    )
    mutations.append(
        (
            "/bins/ticket",
            {
                "crop": "Corn",
                "ticket_number": "T-SIM",
                "move_date": "2026-07-01",
                "destination": "Elevator",
                "wet_bu": "",
                "moisture": "",
                "net_bu": "50",
                "owner_name": "Me",
                "contract_id": "",
                "alloc_bin_id": str(bin_ids[0]),
                "alloc_bu": "50",
                "field_id": "",
                "field_bu": "",
                "notes": "sim ticket",
            },
        )
    )
    # ticket mismatch / empty source
    mutations.append(
        (
            "/bins/ticket",
            {
                "crop": "Corn",
                "ticket_number": "BAD",
                "move_date": "2026-07-01",
                "destination": "",
                "net_bu": "100",
                "owner_name": "Me",
                "contract_id": "",
                "alloc_bin_id": "",
                "alloc_bu": "",
                "field_id": "",
                "field_bu": "",
                "notes": "",
            },
        )
    )

    # trucking
    mutations.append(
        (
            "/trucking/rate",
            {
                "destination": "SIM Elevator",
                "hauler": "SIM Hauler",
                "rate_per_bu": "0.12",
                "rate_per_load": "",
                "miles": "20",
                "notes": "",
            },
        )
    )
    mutations.append(
        (
            "/trucking/load",
            {
                "load_date": "2026-07-01",
                "crop": "Corn",
                "bushels": "1000",
                "destination": "SIM Elevator",
                "hauler": "SIM",
                "rate_paid": "120",
                "ticket_number": "TR1",
                "notes": "",
            },
        )
    )

    # money
    mutations.append(("/settlements/create", {"title": "SIM Settle", "settlement_date": "2026-07-01", "party_id": str(party_ids[0]), "notes": ""}))
    for sid in settle_ids[:5]:
        mutations.append(
            (
                f"/settlements/{sid}/line",
                {
                    "description": "sim line",
                    "amount": "10",
                    "category": "other",
                },
            )
        )
        mutations.append((f"/settlements/{sid}/autofill-rent", {}))
    mutations.append(
        (
            "/invoices/create",
            {
                "invoice_date": "2026-07-01",
                "party_id": str(party_ids[0]),
                "description": "SIM custom work",
                "quantity": "1",
                "rate": "25",
            },
        )
    )
    for iid in invoice_ids[:5]:
        mutations.append((f"/invoices/{iid}/paid", {}))
    mutations.append(
        (
            "/balance/item",
            {
                "as_of_date": "2026-07-01",
                "side": "asset",
                "category": "cash",
                "label": "SIM Cash",
                "amount": "1000",
                "is_current": "1",
                "notes": "",
            },
        )
    )
    mutations.append(("/balance/snapshot", {"snapshot_date": "2026-07-01", "notes": "sim"}))

    # equipment
    mutations.append(
        (
            "/equipment/save",
            {
                "name": "SIM Tractor",
                "category": "tractor",
                "make": "SIM",
                "model": "X",
                "year": "2015",
                "notes": "",
            },
        )
    )
    for eid in equip_ids[:8]:
        mutations.append(
            (
                f"/equipment/{eid}/fuel",
                {"fuel_date": "2026-07-01", "gallons": "20", "total_cost": "70", "notes": ""},
            )
        )
        mutations.append(
            (
                f"/equipment/{eid}/maintenance",
                {
                    "service_date": "2026-07-01",
                    "description": "sim svc",
                    "total_cost": "50",
                    "notes": "",
                },
            )
        )
        mutations.append(
            (
                f"/equipment/{eid}/event",
                {
                    "event_date": "2026-07-01",
                    "event_type": "note",
                    "description": "sim event",
                    "amount": "",
                    "notes": "",
                },
            )
        )

    # tools
    mutations.append(
        (
            "/tools/sprayer",
            {
                "acres": "40",
                "rate_gpa": "15",
                "tank_gal": "500",
                "product_rate": "1",
                "product_unit": "qt/ac",
            },
        )
    )

    # trials
    mutations.append(
        (
            "/trials/create",
            {
                "name": "SIM Trial",
                "crop": "Corn",
                "field_id": str(field_ids[0]),
                "notes": "sim",
            },
        )
    )
    for tid in trial_ids[:5]:
        mutations.append(
            (
                f"/trials/{tid}/treatment",
                {"name": "SIM Treat", "notes": ""},
            )
        )
        mutations.append(
            (
                f"/trials/{tid}/result",
                {"treatment_id": "1", "yield_bu_ac": "200", "notes": ""},
            )
        )
        mutations.append((f"/trials/{tid}/note", {"note_date": "2026-07-01", "body": "sim note"}))
        mutations.append((f"/trials/{tid}/conclusion", {"conclusion": "sim conclusion"}))

    # team soft mutations
    for uid in user_ids[:5]:
        mutations.append((f"/team/{uid}/role", {"role": "owner"}))

    # scan discard safe-ish
    for sid in scan_ids[:3]:
        mutations.append((f"/scan/{sid}/discard", {}))

    # panorama / photos no-opish
    # panorama connect field names vary; skip wrong-shape posts
    mutations.append(("/panorama/refresh", {}))

    # sheet save (fields) — dump ids we have
    if fields:
        mutations.append(
            (
                "/fields/sheet/save",
                {
                    "field_id": [str(f.id) for f in fields[:20]],
                    "crop": [f.crop or "None" for f in fields[:20]],
                    "acres_total": [str(f.acres_total or 0) for f in fields[:20]],
                    "acres_mine": [str(f.acres_mine or 0) for f in fields[:20]],
                    "expected_yield": [str(f.expected_yield or "") for f in fields[:20]],
                    "rent_per_acre": [str(f.rent_per_acre or 0) for f in fields[:20]],
                    "ownership_mode": [f.ownership_mode or "operated_by_me" for f in fields[:20]],
                    "notes": [f.notes or "" for f in fields[:20]],
                },
            )
        )
    if bins:
        mutations.append(
            (
                "/bins/sheet/save",
                {
                    "bin_id": [str(b.id) for b in bins[:15]],
                    "name": [b.name for b in bins[:15]],
                    "crop": [b.crop or "Corn" for b in bins[:15]],
                    "capacity_bu": [str(b.capacity_bu or "") for b in bins[:15]],
                    "on_hand_bu": ["0" for _ in bins[:15]],
                    "notes": [getattr(b, "notes", None) or "" for b in bins[:15]],
                },
            )
        )

    # Extra chaos variants
    chaos_extra = [
        ("/fields/save", {"name": "Totals", "acres_total": "1", "acres_mine": "1", "crop": "Corn"}),
        ("/fields/save", {"name": "", "acres_total": "x", "acres_mine": "y", "crop": "Corn"}),
        ("/bins/create", {"name": "", "crop": "Corn", "capacity_bu": "abc"}),
        ("/bins/move", {"move_type": "delivery", "bin_id": "notint", "net_bu": "abc", "crop": "Corn", "owner_name": "Me", "move_date": "bad"}),
        ("/risk/contract", {"crop": "Corn", "bushels": "abc", "price": "xyz"}),
        ("/library/assign-hybrid", {"field_id": "999999", "hybrid_id": "999999"}),
        ("/library/assign-spray", {"field_id": "999999", "spray_id": "999999"}),
        ("/purchases/assign", {"product_id": "999999", "field_id": "999999", "quantity": "1"}),
        ("/tools/sprayer", {"acres": "-1", "rate_gpa": "0", "tank_gal": "0", "product_rate": "0"}),
        ("/settings/farm", {"farm_name_value": "Beam Farm"}),
        ("/trucking/rate", {"destination": "SIM Dest", "rate_per_bu": "0.1"}),
        ("/parties/save", {"name": "SIM Party 2", "party_type": "buyer"}),
    ]
    # Volume pass: repeat valid + chaos across many fields
    for i in range(40):
        f = fields[i % len(fields)] if fields else None
        if f:
            mutations.append(
                (
                    f"/fields/{f.id}/operation",
                    {
                        "op_date": f"2026-06-{(i % 28) + 1:02d}",
                        "category": "fuel",
                        "description": f"sim bulk {i}",
                        "quantity": "1",
                        "unit": "gal",
                        "total_cost": str(10 + i),
                        "notes": "",
                    },
                )
            )
            mutations.append(
                (
                    "/bins/condition",
                    {
                        "bin_id": str(bin_ids[i % len(bin_ids)]),
                        "note_date": "2026-07-01",
                        "moisture": str(13 + (i % 3)),
                        "temperature": str(50 + i % 20),
                        "notes": f"sim {i}",
                    },
                )
            )
    mutations.extend(chaos_extra * 25)

    for path, data in mutations:
        try:
            resp = client.post(path, data=data, follow_redirects=True)
            looks_broken(resp, f"MUT {path}")
            submitted += 1
        except Exception as e:
            note("exception", f"{path}: {e}")
            note("exception_tb", traceback.format_exc(limit=3))

    # Re-hit all main GETs after mutations (regression)
    for path in [
        "/",
        "/fields",
        "/fields/sheet",
        "/inputs",
        "/inputs/assign",
        "/risk",
        "/risk/contracts",
        "/risk/settings",
        "/risk/carry",
        "/bins",
        "/bins/ticket",
        "/bins/sheet",
        "/trucking",
        "/settlements",
        "/invoices",
        "/balance",
        "/equipment",
        "/settings",
        "/team",
        "/activity",
        "/purchases",
        "/tools",
        "/trials",
        "/scan",
        "/upload",
        "/export",
        "/photos",
        "/panorama",
        "/insights",
    ]:
        looks_broken(client.get(path, follow_redirects=True), f"POSTREG GET {path}")

    # Unique findings summary
    print("=== SIM SUMMARY ===")
    print("temp_db", sim_db)
    print("requests", stats["requests"])
    print("form/mutation submissions ~", submitted)
    for k, v in sorted(stats.items()):
        if k.startswith("status_") or k in ("requests", "http5xx", "traceback", "ise", "validation", "exception", "hint"):
            print(f"  {k}: {v}")

    # Dedup findings
    uniq = []
    seen = set()
    for f in findings:
        key = f.split(" -> ")[0] if " -> " in f else f
        # keep path-level uniqueness for 5xx
        if f.startswith("http5xx") or f.startswith("traceback") or f.startswith("exception") or f.startswith("ise"):
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        elif f.startswith("hint") and f not in seen:
            seen.add(f)
            uniq.append(f)

    print("=== FAILURES (unique) ===")
    if not uniq:
        print("(none)")
    else:
        for f in uniq[:200]:
            print(f)
        if len(uniq) > 200:
            print(f"... +{len(uniq)-200} more")

    # Persist full log
    log_path = ROOT / "data" / "sim_entrypoints_report.txt"
    log_path.write_text("\n".join(findings) + "\n\nSTATS\n" + "\n".join(f"{k}={v}" for k, v in sorted(stats.items())), encoding="utf-8")
    print("log", log_path)

    # Exit non-zero if hard failures
    hard = stats["http5xx"] + stats["traceback"] + stats["ise"] + stats["exception"]
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
