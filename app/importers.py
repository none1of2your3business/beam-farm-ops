"""Import helpers for Master Upload (fields Excel + Cargill-ish CSV)."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CropYear, Field, GrainContract, GrainMovement


def _num(value, default=0.0):
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return default


def _parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def infer_ownership(acres_total: float, acres_mine: float) -> str:
    if acres_mine <= 0 and acres_total > 0:
        return "custom_work"
    if acres_total > 0 and abs(acres_mine - acres_total) > 0.05:
        return "on_shares"
    return "operated_by_me"


# Spreadsheet footer / rollup labels that must never become Field rows.
_SUMMARY_FIELD_NAMES = frozenset(
    {
        "total",
        "totals",
        "grand total",
        "grandtotal",
        "sum",
        "summary",
        "subtotal",
        "sub total",
        "all fields",
    }
)


def is_summary_field_name(name: str | None) -> bool:
    """True for Excel rollup rows like 'Totals' — not real fields."""
    if name is None:
        return False
    text = str(name).strip().lower()
    if not text:
        return False
    compact = " ".join(text.split())
    if compact in _SUMMARY_FIELD_NAMES:
        return True
    return compact.replace(" ", "") in {s.replace(" ", "") for s in _SUMMARY_FIELD_NAMES}


def import_fields_excel(db: Session, path: Path, year: CropYear) -> tuple[int, int, str]:
    wb = load_workbook(path, data_only=True)
    sheet = None
    for name in wb.sheetnames:
        if name.lower().replace(" ", "") in ("cropacres", "fields", "acres"):
            sheet = wb[name]
            break
    if sheet is None:
        sheet = wb[wb.sheetnames[0]]
    created = updated = skipped = 0
    # detect header row
    start = 1
    for i, row in enumerate(sheet.iter_rows(min_row=1, max_row=5, values_only=True), start=1):
        cells = [str(c).lower() if c is not None else "" for c in row[:6]]
        if any("field" in c for c in cells):
            start = i + 1
            break
    for row in sheet.iter_rows(min_row=start, values_only=True):
        name = row[0] if row else None
        if not name or str(name).strip() in ("", "Field", "Table 1"):
            continue
        name = str(name).strip()
        if is_summary_field_name(name):
            skipped += 1
            continue
        acres_total = _num(row[1] if len(row) > 1 else 0)
        acres_mine = _num(row[2] if len(row) > 2 else acres_total)
        crop = str(row[3] if len(row) > 3 else "None").strip() or "None"
        if crop.lower() in ("bean", "beans", "soy", "soybean"):
            crop = "Soybeans"
        elif crop.lower() == "corn":
            crop = "Corn"
        rent = _num(row[8] if len(row) > 8 else (row[4] if len(row) > 4 else 0))
        existing = db.scalar(select(Field).where(Field.crop_year_id == year.id, Field.name == name))
        if existing:
            field = existing
            updated += 1
        else:
            field = Field(crop_year_id=year.id, name=name)
            db.add(field)
            created += 1
        field.acres_total = acres_total
        field.acres_mine = acres_mine
        field.crop = crop
        field.rent_per_acre = rent
        field.ownership_mode = infer_ownership(acres_total, acres_mine)
    note = f"Fields: {created} created, {updated} updated from {sheet.title}"
    if skipped:
        note += f" ({skipped} total/summary row(s) skipped)"
    return created, updated, note


def _norm_header(h: str) -> str:
    return "".join(ch for ch in (h or "").lower() if ch.isalnum())


# Common Cargill / elevator export column aliases
CARGILL_MAP = {
    "buyer": ["buyer", "elevator", "location", "facility"],
    "crop": ["crop", "commodity", "product"],
    "bushels": ["bushels", "quantity", "qty", "netbu", "netbushels", "bu"],
    "price": ["price", "cashprice", "netprice", "unitprice"],
    "ticket": ["ticket", "ticketnumber", "ticketno", "scaleticket"],
    "date": ["date", "deliverydate", "settledate", "loaddate"],
    "contract": ["contract", "contractnumber", "contractno", "contractid"],
    "moisture": ["moisture", "moist", "m"],
    "type": ["type", "contracttype", "saletype"],
}


def _pick(row: dict, key: str):
    aliases = CARGILL_MAP.get(key, [key])
    for a in aliases:
        for rk, rv in row.items():
            if _norm_header(rk) == a:
                return rv
    return None


def import_cargill_csv(db: Session, content: bytes, year: CropYear) -> tuple[int, int, str]:
    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return 0, 0, "CSV has no header row"
    contracts_made = 0
    tickets = 0
    seen_tickets: set[str] = set()
    for raw in reader:
        row = {k: v for k, v in raw.items() if k is not None}
        ticket = str(_pick(row, "ticket") or "").strip()
        if ticket and ticket in seen_tickets:
            continue
        if ticket:
            seen_tickets.add(ticket)
        crop_raw = str(_pick(row, "crop") or "Corn").strip()
        crop = "Soybeans" if "soy" in crop_raw.lower() else ("Corn" if "corn" in crop_raw.lower() else crop_raw)
        bu = _num(_pick(row, "bushels"))
        price = _num(_pick(row, "price"), None)
        buyer = str(_pick(row, "buyer") or "Cargill").strip()
        ctype = str(_pick(row, "type") or "cash").strip().lower() or "cash"
        move_date = _parse_date(_pick(row, "date")) or date.today()
        moist = _num(_pick(row, "moisture"), None)
        if bu <= 0:
            continue
        # create/open a simple cash contract bucket for this buyer+crop+price
        contract = None
        contract_no = str(_pick(row, "contract") or "").strip()
        if contract_no:
            contract = db.scalar(
                select(GrainContract).where(
                    GrainContract.crop_year_id == year.id,
                    GrainContract.notes == f"import:{contract_no}",
                )
            )
        if not contract:
            contract = GrainContract(
                crop_year_id=year.id,
                crop=crop,
                contract_type=ctype if ctype in ("cash", "forward", "hta", "basis", "dp") else "cash",
                buyer=buyer,
                bushels=bu,
                delivered_bu=bu,
                cash_price=price,
                notes=f"import:{contract_no}" if contract_no else "cargill_import",
                status="open",
            )
            db.add(contract)
            db.flush()
            contracts_made += 1
        else:
            contract.bushels = (contract.bushels or 0) + bu
            contract.delivered_bu = (contract.delivered_bu or 0) + bu
        db.add(
            GrainMovement(
                contract_id=contract.id,
                move_date=move_date,
                move_type="elevator",
                crop=crop,
                owner_name="Me",
                wet_bu=bu if moist else None,
                moisture=moist,
                net_bu=bu,
                ticket_number=ticket or None,
                destination=buyer,
                notes="Cargill/CSV import",
            )
        )
        tickets += 1
    return contracts_made, tickets, f"Cargill import: {contracts_made} contracts, {tickets} tickets (dup tickets skipped)"
