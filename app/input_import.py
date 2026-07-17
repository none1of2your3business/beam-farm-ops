"""Guided Inputs upload: extract Excel/CSV/PDF/images, guess type, build review rows."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

FIELD_KEYS = (
    "product",
    "unit",
    "price",
    "rate",
    "rate_unit",
    "mix",
    "timing",
    "crop",
    "cost_per_acre",
)

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "product": (
        "product",
        "product name",
        "chemical",
        "chemical name",
        "material",
        "item",
        "name",
        "herbicide",
        "fungicide",
        "insecticide",
        "description",
    ),
    "unit": ("unit", "uom", "buy unit", "package", "pack", "size", "$ unit"),
    "price": (
        "price",
        "cost",
        "unit cost",
        "cost/unit",
        "cost per unit",
        "$/unit",
        "$/gal",
        "$/gallon",
        "$/lb",
        "$/qt",
        "price/gal",
        "price/unit",
        "unit price",
        "net price",
    ),
    "rate": ("rate", "use rate", "app rate", "oz/ac", "pt/ac", "qt/ac", "lb/ac", "gal/ac", "fl oz/ac"),
    "rate_unit": ("rate unit", "rate uom", "application unit"),
    "mix": ("mix", "mix name", "program", "tank mix", "recipe", "treatment", "program name"),
    "timing": ("timing", "stage", "application", "when", "pass"),
    "crop": ("crop", "crop type"),
    "cost_per_acre": ("$/ac", "$/acre", "cost/acre", "cost per acre", "per acre", "cpa"),
}

TYPE_LABELS = {
    "chem_prices": "Chemical price list",
    "mix_program": "Mix / tank program",
    "both": "Both (prices + mixes)",
    "unknown": "Not sure — I will guide it",
}


def _norm_header(h: Any) -> str:
    s = str(h or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _match_header(header: str) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    for key, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if h == a or h.startswith(a + " ") or a in h:
                return key
    # bare $/x often means price
    if h.startswith("$/") or "price" in h or h.endswith(" cost"):
        return "price"
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in {"—", "-", "n/a", "na", "None"}:
        return None
    s = s.replace("$", "").replace(",", "").replace("%", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _guess_unit_from_header(header: str) -> str | None:
    h = _norm_header(header)
    for u in ("gal", "gallon", "qt", "pt", "oz", "lb", "jug", "case", "bag", "unit"):
        if u in h:
            if u == "gallon":
                return "gal"
            return u
    return None


def guess_column_map(headers: list[str]) -> dict[str, int | None]:
    mapping: dict[str, int | None] = {k: None for k in FIELD_KEYS}
    for idx, header in enumerate(headers):
        key = _match_header(header)
        if key and mapping.get(key) is None:
            mapping[key] = idx
    return mapping


def guess_doc_type(headers: list[str], sample_rows: list[list[Any]] | None = None) -> str:
    mapped = guess_column_map(headers)
    has_product = mapped["product"] is not None
    has_price = mapped["price"] is not None
    has_mix = mapped["mix"] is not None
    has_rate = mapped["rate"] is not None
    joined = " ".join(_norm_header(h) for h in headers)
    if has_mix or "program" in joined or "tank" in joined:
        if has_price:
            return "both"
        return "mix_program"
    if has_rate and has_product:
        return "both" if has_price else "mix_program"
    if has_product and has_price:
        return "chem_prices"
    # OCR / weak sheets
    if sample_rows:
        textish = " ".join(str(c) for row in sample_rows[:8] for c in row).lower()
        if "mix" in textish or "program" in textish:
            return "mix_program"
        if "$" in textish or "price" in textish:
            return "chem_prices"
    return "unknown"


def _sheet_from_matrix(name: str, matrix: list[list[Any]]) -> dict[str, Any]:
    # Trim trailing empty rows/cols
    rows = []
    for row in matrix:
        cells = [("" if c is None else c) for c in row]
        if any(str(c).strip() for c in cells):
            rows.append(cells)
    if not rows:
        return {"name": name, "headers": [], "rows": [], "header_row": 0}

    # Pick header row: first row with >=2 non-empty and preferably alias matches
    best_i = 0
    best_score = -1
    for i, row in enumerate(rows[:12]):
        non_empty = sum(1 for c in row if str(c).strip())
        if non_empty < 2:
            continue
        score = non_empty
        for c in row:
            if _match_header(c):
                score += 3
        if score > best_score:
            best_score = score
            best_i = i

    headers = [str(c).strip() if c is not None else f"Col{j+1}" for j, c in enumerate(rows[best_i])]
    # Ensure unique header labels for display
    seen: dict[str, int] = {}
    uniq = []
    for h in headers:
        base = h or "Column"
        n = seen.get(base, 0)
        seen[base] = n + 1
        uniq.append(base if n == 0 else f"{base} ({n+1})")
    data_rows = rows[best_i + 1 :]
    # Pad short rows
    width = len(uniq)
    padded = []
    for r in data_rows:
        rr = list(r) + [""] * max(0, width - len(r))
        padded.append(rr[:width])
    return {"name": name, "headers": uniq, "rows": padded, "header_row": best_i}


def extract_excel(path: Path) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    sheets = []
    for ws in wb.worksheets:
        matrix: list[list[Any]] = []
        for row in ws.iter_rows(values_only=True):
            matrix.append(list(row))
            if len(matrix) >= 500:
                break
        sheets.append(_sheet_from_matrix(ws.title, matrix))
    wb.close()
    return {"source_kind": "excel", "sheets": sheets, "ocr_text": "", "ocr_note": ""}


def extract_csv(content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    matrix = [list(r) for r in reader]
    sheet = _sheet_from_matrix("Sheet1", matrix)
    return {"source_kind": "csv", "sheets": [sheet], "ocr_text": "", "ocr_note": ""}


def extract_ocr_bytes(content: bytes, filename: str) -> dict[str, Any]:
    from app import receipt_ocr

    text, note = receipt_ocr.extract_text(content)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # Heuristic: turn OCR lines into a loose 2–3 column table
    matrix: list[list[Any]] = [["Product / line", "Numbers", "Raw"]]
    for ln in lines[:200]:
        nums = re.findall(r"\$?\d[\d,]*\.?\d*", ln)
        name = re.sub(r"\$?\d[\d,]*\.?\d*", " ", ln)
        name = re.sub(r"\s+", " ", name).strip(" -–|")
        matrix.append([name or ln, " ".join(nums), ln])
    sheet = _sheet_from_matrix("OCR", matrix)
    kind = "pdf" if filename.lower().endswith(".pdf") else "image"
    return {
        "source_kind": kind,
        "sheets": [sheet],
        "ocr_text": text or "",
        "ocr_note": note or ("PDF opened as image OCR" if kind == "pdf" else ""),
    }


def extract_file(path: Path, content: bytes | None = None) -> dict[str, Any]:
    name = path.name.lower()
    data = content if content is not None else path.read_bytes()
    if name.endswith((".xlsx", ".xlsm")):
        return extract_excel(path)
    if name.endswith(".xls"):
        # openpyxl doesn't read legacy xls — try csv-ish fallback messaging
        return {
            "source_kind": "excel",
            "sheets": [_sheet_from_matrix("Sheet1", [])],
            "ocr_text": "",
            "ocr_note": "Legacy .xls not supported — save as .xlsx or CSV and re-upload.",
        }
    if name.endswith((".csv", ".txt")):
        return extract_csv(data)
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".pdf")):
        # PDF: try render first page via PIL if possible; else OCR bytes directly
        if name.endswith(".pdf"):
            try:
                # Best-effort: treat bytes as image through RapidOCR may fail; store text note
                return extract_ocr_bytes(data, path.name)
            except Exception as exc:  # noqa: BLE001
                return {
                    "source_kind": "pdf",
                    "sheets": [_sheet_from_matrix("PDF", [["Product / line", "Numbers", "Raw"]])],
                    "ocr_text": "",
                    "ocr_note": f"Could not OCR PDF ({exc}). Paste or type lines on review.",
                }
        return extract_ocr_bytes(data, path.name)
    # Unknown — try CSV decode then OCR
    try:
        return extract_csv(data)
    except Exception:
        return extract_ocr_bytes(data, path.name)


def build_payload_from_extract(
    filename: str,
    stored_path: str,
    extract: dict[str, Any],
    products: list[Any] | None = None,
    mixes: list[Any] | None = None,
) -> dict[str, Any]:
    sheets = extract.get("sheets") or []
    sheet_index = 0
    sheet = sheets[sheet_index] if sheets else {"headers": [], "rows": []}
    headers = list(sheet.get("headers") or [])
    rows_matrix = list(sheet.get("rows") or [])
    column_map = guess_column_map(headers)
    guessed = guess_doc_type(headers, rows_matrix)
    payload = {
        "step": "type",
        "guessed_type": guessed,
        "confirmed_type": None,
        "source_kind": extract.get("source_kind") or "unknown",
        "stored_path": stored_path,
        "original_filename": filename,
        "sheets": [
            {
                "name": s.get("name"),
                "headers": s.get("headers") or [],
                "row_count": len(s.get("rows") or []),
            }
            for s in sheets
        ],
        # Keep active sheet data for mapping (truncate huge sheets)
        "active_sheet": {
            "name": sheet.get("name"),
            "headers": headers,
            "rows": rows_matrix[:400],
        },
        "sheet_index": sheet_index,
        "ocr_text": extract.get("ocr_text") or "",
        "ocr_note": extract.get("ocr_note") or "",
        "column_map": column_map,
        "rows": [],
        "commit_summary": None,
    }
    payload["rows"] = build_row_proposals(payload, products or [], mixes or [])
    return payload


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _find_product(name: str, products: list[Any]) -> Any | None:
    want = name.strip().lower()
    if not want:
        return None
    for p in products:
        if (p.name or "").strip().lower() == want:
            return p
    for p in products:
        pn = (p.name or "").strip().lower()
        if want in pn or pn in want:
            return p
    return None


def _find_mix(name: str, mixes: list[Any]) -> Any | None:
    want = name.strip().lower()
    if not want:
        return None
    for m in mixes:
        if (m.name or "").strip().lower() == want:
            return m
    return None


def build_row_proposals(
    payload: dict[str, Any],
    products: list[Any],
    mixes: list[Any],
) -> list[dict[str, Any]]:
    sheet = payload.get("active_sheet") or {}
    headers = list(sheet.get("headers") or [])
    matrix = list(sheet.get("rows") or [])
    cmap = dict(payload.get("column_map") or guess_column_map(headers))
    doc_type = payload.get("confirmed_type") or payload.get("guessed_type") or "unknown"
    proposals: list[dict[str, Any]] = []

    # Infer default unit from price column header
    price_idx = cmap.get("price")
    default_unit = "gal"
    if price_idx is not None and price_idx < len(headers):
        default_unit = _guess_unit_from_header(headers[price_idx]) or default_unit

    for i, row in enumerate(matrix):
        product = str(_cell(row, cmap.get("product")) or "").strip()
        unit = str(_cell(row, cmap.get("unit")) or "").strip() or default_unit
        price = _to_float(_cell(row, cmap.get("price")))
        rate = _to_float(_cell(row, cmap.get("rate")))
        rate_unit = str(_cell(row, cmap.get("rate_unit")) or "").strip() or None
        if rate is not None and not rate_unit and price_idx is not None:
            # try parse unit from rate header
            ri = cmap.get("rate")
            if ri is not None and ri < len(headers):
                rh = _norm_header(headers[ri])
                for candidate in ("oz/ac", "pt/ac", "qt/ac", "gal/ac", "lb/ac", "fl oz/ac"):
                    if candidate in rh:
                        rate_unit = candidate
                        break
        mix_name = str(_cell(row, cmap.get("mix")) or "").strip()
        timing = str(_cell(row, cmap.get("timing")) or "").strip()
        crop = str(_cell(row, cmap.get("crop")) or "").strip()
        cpa = _to_float(_cell(row, cmap.get("cost_per_acre")))

        if not product and not mix_name:
            continue
        if not product and mix_name:
            # mix header-only row
            product = ""

        match_p = _find_product(product, products) if product else None
        match_m = _find_mix(mix_name, mixes) if mix_name else None

        include = bool(product or mix_name)
        action = "skip"
        if doc_type in ("chem_prices", "both", "unknown") and product and price is not None:
            action = "update_product" if match_p else "create_product"
        if doc_type in ("mix_program", "both") and (mix_name or product):
            if not mix_name and product:
                mix_name = "Imported mix"
            if product:
                action = "mix_line"
            elif mix_name:
                action = "mix_header"

        # For both: prefer mix_line when rate present, else product
        if doc_type == "both" and product and price is not None and rate is None:
            action = "update_product" if match_p else "create_product"
        if doc_type == "both" and product and (rate is not None or mix_name):
            action = "mix_line"

        confidence = "low"
        if match_p or match_m:
            confidence = "high"
        elif product and (price is not None or rate is not None):
            confidence = "med"

        proposals.append(
            {
                "id": i,
                "include": include and action != "skip",
                "action": action,
                "product_name": product,
                "unit": unit[:40] if unit else "gal",
                "cost_per_unit": price,
                "rate": rate,
                "rate_unit": rate_unit,
                "cost_per_acre": cpa,
                "mix_name": mix_name,
                "timing": timing,
                "crop": crop,
                "match_product_id": match_p.id if match_p else None,
                "match_product_name": match_p.name if match_p else None,
                "match_mix_id": match_m.id if match_m else None,
                "match_mix_name": match_m.name if match_m else None,
                "confidence": confidence,
            }
        )
    return proposals


def rebuild_proposals_after_map(
    payload: dict[str, Any],
    products: list[Any],
    mixes: list[Any],
) -> dict[str, Any]:
    payload = dict(payload)
    payload["rows"] = build_row_proposals(payload, products, mixes)
    return payload


def apply_sheet_switch(payload: dict[str, Any], sheet_index: int, full_sheets_data: list[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(payload)
    if sheet_index < 0 or sheet_index >= len(full_sheets_data):
        return payload
    sheet = full_sheets_data[sheet_index]
    payload["sheet_index"] = sheet_index
    payload["active_sheet"] = {
        "name": sheet.get("name"),
        "headers": list(sheet.get("headers") or []),
        "rows": list(sheet.get("rows") or [])[:400],
    }
    payload["column_map"] = guess_column_map(payload["active_sheet"]["headers"])
    payload["guessed_type"] = guess_doc_type(
        payload["active_sheet"]["headers"],
        payload["active_sheet"]["rows"],
    )
    return payload


def load_payload(batch: Any) -> dict[str, Any]:
    try:
        return json.loads(batch.payload_json or "{}")
    except json.JSONDecodeError:
        return {}


def dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def line_cost_per_acre(rate: float | None, cost_per_unit: float | None, cost_per_acre: float | None) -> float | None:
    if cost_per_acre is not None:
        return cost_per_acre
    if rate is not None and cost_per_unit is not None:
        return round(rate * cost_per_unit, 4)
    return None


def commit_rows(
    db: Any,
    payload: dict[str, Any],
    year: Any,
    *,
    InputProduct: Any,
    InputPurchase: Any,
    SprayMix: Any,
    SprayMixLine: Any,
) -> dict[str, Any]:
    """Write included rows. Mutates DB session (caller commits)."""
    from datetime import date
    from sqlalchemy import select

    doc_type = payload.get("confirmed_type") or payload.get("guessed_type") or "unknown"
    products_by_name = {
        (p.name or "").strip().lower(): p
        for p in db.scalars(select(InputProduct)).all()
    }
    mixes_by_name: dict[str, Any] = {}
    if year:
        for m in db.scalars(select(SprayMix).where(SprayMix.crop_year_id == year.id)).all():
            mixes_by_name[(m.name or "").strip().lower()] = m

    created_products = 0
    updated_products = 0
    mixes_touched: set[int] = set()
    lines_written = 0
    skipped = 0

    # Group mix lines by mix name to rebuild lines when importing a full program
    mix_line_buckets: dict[str, list[dict[str, Any]]] = {}

    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        if not row.get("include"):
            skipped += 1
            continue
        action = row.get("action") or "skip"
        pname = (row.get("product_name") or "").strip()
        unit = (row.get("unit") or "gal").strip() or "gal"
        price = row.get("cost_per_unit")
        if isinstance(price, str):
            price = _to_float(price)

        if action in ("create_product", "update_product") or (
            doc_type in ("chem_prices", "both", "unknown") and action == "mix_line" and price is not None and pname
        ):
            if pname and price is not None:
                key = pname.lower()
                prod = products_by_name.get(key)
                if prod is None:
                    # try id match
                    mid = row.get("match_product_id")
                    if mid:
                        prod = db.get(InputProduct, int(mid))
                if prod is None:
                    prod = InputProduct(
                        name=pname[:160],
                        category="chemical",
                        unit=unit[:40],
                        avg_unit_cost=float(price),
                        on_hand=0.0,
                    )
                    db.add(prod)
                    db.flush()
                    products_by_name[key] = prod
                    created_products += 1
                else:
                    prod.avg_unit_cost = float(price)
                    if unit:
                        prod.unit = unit[:40]
                    if (prod.category or "other") == "other":
                        prod.category = "chemical"
                    updated_products += 1
                # Audit purchase stub so history shows the import
                if year:
                    db.add(
                        InputPurchase(
                            product_id=prod.id,
                            crop_year_id=year.id,
                            purchase_date=date.today(),
                            vendor="Import",
                            quantity=0.0,
                            total_cost=0.0,
                            notes=f"Guided import price ${float(price):.4g}/{unit}",
                        )
                    )

        if action in ("mix_line", "mix_header") or (
            doc_type in ("mix_program", "both") and pname
        ):
            mix_name = (row.get("mix_name") or "").strip() or "Imported mix"
            mix_line_buckets.setdefault(mix_name, []).append(row)

    if year:
        for mix_name, lines in mix_line_buckets.items():
            key = mix_name.lower()
            mix = mixes_by_name.get(key)
            timing = next(((r.get("timing") or "").strip() for r in lines if (r.get("timing") or "").strip()), None)
            crop = next(((r.get("crop") or "").strip() for r in lines if (r.get("crop") or "").strip()), None)
            if mix is None:
                mix = SprayMix(
                    crop_year_id=year.id,
                    name=mix_name[:160],
                    timing=timing,
                    crop=crop,
                    notes="Created from guided import",
                )
                db.add(mix)
                db.flush()
                mixes_by_name[key] = mix
            else:
                if timing:
                    mix.timing = timing
                if crop:
                    mix.crop = crop

            # Replace lines for this import batch's included products
            existing = list(
                db.scalars(select(SprayMixLine).where(SprayMixLine.spray_mix_id == mix.id))
            )
            # Upsert by product_name within mix
            by_line = {(ln.product_name or "").strip().lower(): ln for ln in existing}
            summaries: list[str] = []
            total_cpa = 0.0
            has_cpa = False
            sort_i = 0
            for r in lines:
                if not r.get("include"):
                    continue
                if (r.get("action") or "") == "mix_header" and not (r.get("product_name") or "").strip():
                    continue
                pname = (r.get("product_name") or "").strip()
                if not pname:
                    continue
                rate = r.get("rate")
                if isinstance(rate, str):
                    rate = _to_float(rate)
                cpu = r.get("cost_per_unit")
                if isinstance(cpu, str):
                    cpu = _to_float(cpu)
                # Fall back to product avg cost
                if cpu is None:
                    prod = products_by_name.get(pname.lower())
                    if prod and prod.avg_unit_cost:
                        cpu = float(prod.avg_unit_cost)
                cpa = line_cost_per_acre(rate, cpu, _to_float(r.get("cost_per_acre")))
                ru = (r.get("rate_unit") or None)
                ul = (r.get("unit") or None)
                line = by_line.get(pname.lower())
                if line is None:
                    line = SprayMixLine(
                        spray_mix_id=mix.id,
                        product_name=pname[:160],
                        sort_order=sort_i,
                    )
                    db.add(line)
                    by_line[pname.lower()] = line
                line.rate = rate
                line.rate_unit = ru
                line.unit_label = ul
                line.cost_per_unit = cpu
                line.cost_per_acre = cpa
                line.sort_order = sort_i
                sort_i += 1
                lines_written += 1
                bit = pname
                if rate is not None:
                    bit += f" {rate:g}"
                    if ru:
                        bit += f" {ru}"
                if cpa is not None:
                    bit += f" ${cpa:g}/ac"
                    total_cpa += cpa
                    has_cpa = True
                summaries.append(bit)
            mix.products_json = "; ".join(summaries) if summaries else mix.products_json
            if has_cpa:
                mix.cost_per_acre = round(total_cpa, 4)
            mixes_touched.add(mix.id)

    return {
        "created_products": created_products,
        "updated_products": updated_products,
        "mixes_updated": len(mixes_touched),
        "lines_written": lines_written,
        "skipped": skipped,
    }
