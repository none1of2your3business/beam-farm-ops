"""Guided planting/season-report CSV import (Precision Planting condensed style)."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Field, FieldHybrid, FieldOperation, Hybrid

FIELD_KEYS = (
    "field_name",
    "farm_name",
    "planting_dates",
    "acres",
    "hybrids",
    "total_units",
    "population",
    "crop",
    "client",
)

FIELD_LABELS = {
    "field_name": "Field name",
    "farm_name": "Farm / alternate name",
    "planting_dates": "Planting date(s)",
    "acres": "Acres planted",
    "hybrids": "Hybrids / varieties",
    "total_units": "Total units applied",
    "population": "Population (seeds/ac)",
    "crop": "Crop",
    "client": "Client / grower",
}

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "field_name": (
        "field name",
        "field",
        "fieldname",
        "name",
        "grower field",
    ),
    "farm_name": (
        "farm",
        "farm name",
        "farmname",
        "location",
        "site",
    ),
    "planting_dates": (
        "planting dates",
        "planting date",
        "plant date",
        "plant dates",
        "date planted",
        "dates planted",
        "planted",
        "op date",
        "date",
    ),
    "acres": (
        "acres planted",
        "acres planted ac",
        "planted acres",
        "planted ac",
        "acres",
        "area",
    ),
    "hybrids": (
        "hybrids",
        "hybrid",
        "varieties",
        "variety",
        "seed",
        "seeds",
        "product",
        "products",
    ),
    "total_units": (
        "total units",
        "total units units",
        "units applied",
        "seed units",
        "total bags",
        "units",
        "bags",
    ),
    "population": (
        "population seeds/ac",
        "population seeds ac",
        "population",
        "seeds/ac",
        "seeds ac",
        "seeding rate",
        "pop",
    ),
    "crop": ("crop", "crop type", "commodity"),
    "client": ("client", "grower", "operator", "customer"),
}


def _norm_header(h: Any) -> str:
    s = str(h or "").strip().lower()
    # strip PP-style prefixes like "72: Field Name"
    if ":" in s:
        s = s.split(":", 1)[-1]
    s = s.replace("_", " ").replace("-", " ").replace("(", " ").replace(")", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _match_header(header: str) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    # Score aliases: prefer longer / more specific matches.
    best_key: str | None = None
    best_score = 0
    for key, aliases in HEADER_ALIASES.items():
        for a in aliases:
            score = 0
            if h == a:
                score = 100 + len(a)
            elif h.startswith(a + " ") or h.endswith(" " + a) or f" {a} " in f" {h} ":
                score = 60 + len(a)
            elif len(a) >= 4 and a in h:
                score = 30 + len(a)
            else:
                continue
            # Prefer planting/field specifics over generic leftovers
            if key in ("field_name", "planting_dates", "hybrids", "total_units", "population"):
                score += 5
            if score > best_score:
                best_score = score
                best_key = key
    return best_key


def guess_column_map(headers: list[str]) -> dict[str, int | None]:
    mapping: dict[str, int | None] = {k: None for k in FIELD_KEYS}
    # First pass: collect all candidates with scores per header
    claimed: set[int] = set()
    ranked: list[tuple[int, int, str, int]] = []  # (-score, idx, key, alias_len)
    for idx, header in enumerate(headers):
        h = _norm_header(header)
        if not h:
            continue
        for key, aliases in HEADER_ALIASES.items():
            for a in aliases:
                score = 0
                if h == a:
                    score = 100 + len(a)
                elif h.startswith(a + " ") or h.endswith(" " + a) or f" {a} " in f" {h} ":
                    score = 60 + len(a)
                elif len(a) >= 5 and a in h:
                    score = 30 + len(a)
                else:
                    continue
                if key in ("field_name", "planting_dates", "hybrids", "total_units", "population", "acres"):
                    score += 2
                ranked.append((-score, idx, key, len(a)))
    ranked.sort()
    for _neg, idx, key, _alen in ranked:
        if mapping.get(key) is not None:
            continue
        if idx in claimed:
            continue
        mapping[key] = idx
        claimed.add(idx)
    return mapping


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"—", "-", "n/a", "na", "none"}:
        return None
    s = s.replace(",", "").replace("$", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_dates(value: Any) -> list[date]:
    if value is None:
        return []
    if isinstance(value, datetime):
        return [value.date()]
    if isinstance(value, date):
        return [value]
    text = str(value).strip()
    if not text:
        return []
    # Split on ; for multi-pass reports, strip extra quotes
    parts = re.split(r"\s*;\s*", text)
    out: list[date] = []
    for part in parts:
        p = part.strip().strip('"').strip("'").strip()
        if not p:
            continue
        # ISO datetime
        try:
            if "T" in p:
                out.append(datetime.fromisoformat(p.replace("Z", "+00:00")).date())
                continue
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                out.append(datetime.strptime(p[:10] if fmt == "%Y-%m-%d" and len(p) >= 10 else p, fmt).date())
                break
            except ValueError:
                continue
    # unique preserve order
    seen: set[date] = set()
    unique: list[date] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def parse_list_cell(value: Any) -> list[str]:
    """Parse PP-style multi values: \"\"a\"\"; \"\"b\"\"  or a; b."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    # Remove outer wrapping quotes often left by weird CSV exports
    parts = re.split(r"\s*;\s*", text)
    out: list[str] = []
    for part in parts:
        p = part.strip()
        # strip repeated quotes
        while len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            p = p[1:-1].strip()
        p = p.strip('"').strip("'").strip()
        if p:
            out.append(p)
    return out


def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_field(name: str, farm: str, fields: list[Field]) -> Field | None:
    candidates = [c for c in (name, farm) if (c or "").strip()]
    if not candidates or not fields:
        return None
    # exact (casefold)
    for c in candidates:
        cl = c.strip().casefold()
        for f in fields:
            if (f.name or "").strip().casefold() == cl:
                return f
    # normalized equality
    for c in candidates:
        cn = _norm_name(c)
        for f in fields:
            if _norm_name(f.name) == cn:
                return f
    # containment either way (prefer longest match)
    best: Field | None = None
    best_score = 0
    for c in candidates:
        cn = _norm_name(c)
        if len(cn) < 2:
            continue
        for f in fields:
            fn = _norm_name(f.name)
            if not fn:
                continue
            if cn in fn or fn in cn:
                score = min(len(cn), len(fn))
                if score > best_score:
                    best = f
                    best_score = score
            # token overlap (e.g. "Hurley rd (5390)" vs "Hurley Rd")
            ct = set(cn.split())
            ft = set(fn.split())
            if not ct or not ft:
                continue
            overlap = len(ct & ft)
            if overlap >= 2 or (overlap == 1 and len(ct) == 1):
                score = overlap * 10 + min(len(cn), len(fn))
                if score > best_score:
                    best = f
                    best_score = score
    return best


def parse_csv_bytes(content: bytes) -> dict[str, Any]:
    text = content.decode("utf-8-sig", errors="ignore")
    # sniffer-friendly
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    matrix = [list(row) for row in reader]
    rows: list[list[Any]] = []
    for row in matrix:
        cells = [("" if c is None else c) for c in row]
        if any(str(c).strip() for c in cells):
            rows.append(cells)
    if not rows:
        return {"headers": [], "rows": [], "header_row": 0}

    best_i = 0
    best_score = -1
    for i, row in enumerate(rows[:12]):
        non_empty = sum(1 for c in row if str(c).strip())
        if non_empty < 2:
            continue
        score = non_empty
        for c in row:
            if _match_header(str(c)):
                score += 4
        if score > best_score:
            best_score = score
            best_i = i

    headers = [str(c).strip() if str(c).strip() else f"Col{j+1}" for j, c in enumerate(rows[best_i])]
    # unique labels
    seen: dict[str, int] = {}
    uniq = []
    for h in headers:
        base = h
        n = seen.get(base, 0)
        seen[base] = n + 1
        uniq.append(base if n == 0 else f"{base} ({n+1})")
    data = rows[best_i + 1 :]
    # pad/truncate to header width
    width = len(uniq)
    normalized = []
    for row in data:
        r = list(row) + [""] * max(0, width - len(row))
        normalized.append(r[:width])
    return {"headers": uniq, "rows": normalized, "header_row": best_i}


def dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def load_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def build_payload(filename: str, path: str, content: bytes, crop_guess: str = "Corn") -> dict[str, Any]:
    parsed = parse_csv_bytes(content)
    headers = parsed["headers"]
    cmap = guess_column_map(headers)
    if cmap.get("crop") is None and crop_guess:
        # soft default later at row build
        pass
    return {
        "step": "map",
        "original_filename": filename,
        "stored_path": path,
        "source_kind": "planting_csv",
        "headers": headers,
        "rows": parsed["rows"],
        "header_row": parsed["header_row"],
        "column_map": cmap,
        "crop_default": crop_guess,
        "proposals": [],
        "result": None,
    }


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def match_hybrid(name: str, hybrids: list[Hybrid], crop: str | None = None) -> Hybrid | None:
    """Best-effort match a CSV hybrid/variety name to library Hybrids."""
    raw = (name or "").strip()
    if not raw or not hybrids:
        return None
    want = raw.casefold()
    want_n = _norm_name(raw)
    crop_l = (crop or "").strip().casefold()

    def crop_ok(h: Hybrid) -> bool:
        if not crop_l or crop_l in ("none", ""):
            return True
        hc = (h.crop or "").strip().casefold()
        return (not hc) or hc == crop_l or hc in ("none", "")

    # 1) exact name (prefer same crop)
    exact = [h for h in hybrids if (h.name or "").strip().casefold() == want and crop_ok(h)]
    if exact:
        return exact[0]
    exact_any = [h for h in hybrids if (h.name or "").strip().casefold() == want]
    if exact_any:
        return exact_any[0]

    # 2) brand + name combined
    for h in hybrids:
        label = f"{h.brand or ''} {h.name or ''}".strip().casefold()
        if label == want and crop_ok(h):
            return h

    # 3) normalized equality
    for h in hybrids:
        if _norm_name(h.name or "") == want_n and crop_ok(h):
            return h
        if _norm_name(f"{h.brand or ''} {h.name or ''}") == want_n and crop_ok(h):
            return h

    # 4) containment either way (min length 3)
    if len(want_n) >= 3:
        best = None
        best_score = 0
        for h in hybrids:
            if not crop_ok(h):
                continue
            hn = _norm_name(h.name or "")
            label_n = _norm_name(f"{h.brand or ''} {h.name or ''}")
            for cand in (hn, label_n):
                if not cand:
                    continue
                if want_n == cand:
                    return h
                if want_n in cand or cand in want_n:
                    score = min(len(want_n), len(cand))
                    if score > best_score:
                        best = h
                        best_score = score
        if best and best_score >= 3:
            return best
    return None


def hybrid_catalog(hybrids: list[Hybrid]) -> list[dict[str, Any]]:
    out = []
    seen_ids: set[int] = set()
    for h in hybrids:
        if h.id in seen_ids:
            continue
        seen_ids.add(h.id)
        name = (h.name or f"#{h.id}").strip()
        brand = (h.brand or "").strip()
        if brand and not name.casefold().startswith(brand.casefold()):
            label = f"{brand} {name}".strip()
        else:
            label = name
        if h.crop:
            label = f"{label} ({h.crop})"
        out.append({"id": h.id, "label": label, "name": h.name, "crop": h.crop or ""})
    # de-dupe identical labels keep first
    out.sort(key=lambda x: (x["label"] or "").casefold())
    return out


def build_proposals(
    payload: dict[str, Any],
    fields: list[Field],
    hybrids: list[Hybrid] | None = None,
) -> list[dict[str, Any]]:
    headers = payload.get("headers") or []
    rows = payload.get("rows") or []
    cmap = payload.get("column_map") or {}
    crop_default = payload.get("crop_default") or "Corn"
    hybrids = hybrids or []

    def idx(key: str) -> int | None:
        v = cmap.get(key)
        if v is None or v == "" or int(v) < 0:
            return None
        return int(v)

    proposals: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        field_name = str(_cell(row, idx("field_name")) or "").strip()
        farm_name = str(_cell(row, idx("farm_name")) or "").strip()
        if not field_name and not farm_name:
            continue
        dates = _parse_dates(_cell(row, idx("planting_dates")))
        acres = _to_float(_cell(row, idx("acres")))
        hybrid_names = parse_list_cell(_cell(row, idx("hybrids")))
        total_units = _to_float(_cell(row, idx("total_units")))
        population = _to_float(_cell(row, idx("population")))
        crop_raw = str(_cell(row, idx("crop")) or "").strip() or crop_default
        crop = crop_raw
        if crop.lower() in ("bean", "beans", "soy", "soybean", "soybeans"):
            crop = "Soybeans"
        elif crop.lower().startswith("corn"):
            crop = "Corn"
        client = str(_cell(row, idx("client")) or "").strip()

        matched = match_field(field_name, farm_name, fields)
        matched_crop = None
        if matched and (matched.crop or "").strip() and (matched.crop or "") not in ("None",):
            matched_crop = matched.crop
            crop = matched.crop
        rate_label = None
        if population is not None:
            rate_label = f"{population:.0f} seeds/ac"

        units_each: list[float | None] = []
        if hybrid_names:
            if total_units is not None and len(hybrid_names) == 1:
                units_each = [round(total_units, 2)]
            elif total_units is not None and len(hybrid_names) > 1:
                each = round(total_units / len(hybrid_names), 2)
                units_each = [each] * len(hybrid_names)
            else:
                units_each = [None] * len(hybrid_names)

        hybrid_lines = []
        for hi, hname in enumerate(hybrid_names):
            mh = match_hybrid(hname, hybrids, crop)
            hybrid_lines.append(
                {
                    "name": hname,
                    "rate": rate_label,
                    "units": units_each[hi] if hi < len(units_each) else None,
                    "matched_hybrid_id": mh.id if mh else None,
                    "matched_hybrid_name": (
                        f"{mh.brand} {mh.name}".strip() if mh and mh.brand else (mh.name if mh else None)
                    ),
                    # "match" uses library hybrid; "new" creates from CSV name
                    "resolve": "match" if mh else "new",
                    "selected_hybrid_id": mh.id if mh else None,
                }
            )

        proposals.append(
            {
                "id": i,
                "include": True,
                "create_field": matched is None,
                "update_field_crop": False,
                "field_name": field_name or farm_name,
                "farm_name": farm_name,
                "matched_field_id": matched.id if matched else None,
                "matched_field_name": matched.name if matched else None,
                "matched_field_crop": matched_crop,
                "op_date": dates[0].isoformat() if dates else None,
                "all_dates": [d.isoformat() for d in dates],
                "acres": round(acres, 4) if acres is not None else None,
                "crop": crop,
                "client": client,
                "total_units": round(total_units, 2) if total_units is not None else None,
                "population": round(population, 2) if population is not None else None,
                "hybrids": hybrid_lines,
            }
        )
    return proposals


def commit_proposals(
    db: Session,
    *,
    crop_year_id: int,
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == crop_year_id)))
    fields_by_id = {f.id: f for f in fields}
    hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == crop_year_id)))
    hybrids_by_id = {h.id: h for h in hybrids}

    def create_hybrid(name: str, crop: str, details: dict[str, Any] | None = None) -> Hybrid:
        details = details or {}
        n = (details.get("name") or name or "").strip() or name.strip()
        brand = (details.get("brand") or "").strip() or None
        maturity = (details.get("maturity") or "").strip() or None
        traits = (details.get("traits") or "").strip() or None
        notes = (details.get("notes") or "").strip() or None
        unit_label = (details.get("unit_label") or "unit").strip() or "unit"
        cpu = details.get("cost_per_unit")
        cpa = details.get("cost_per_acre")
        try:
            cpu_f = float(cpu) if cpu is not None and str(cpu).strip() != "" else None
        except (TypeError, ValueError):
            cpu_f = None
        try:
            cpa_f = float(cpa) if cpa is not None and str(cpa).strip() != "" else None
        except (TypeError, ValueError):
            cpa_f = None
        h = Hybrid(
            crop_year_id=crop_year_id,
            crop=crop or "Corn",
            brand=brand,
            name=n,
            maturity=maturity,
            traits=traits,
            notes=notes,
            unit_label=unit_label,
            cost_per_unit=cpu_f,
            cost_per_acre=cpa_f,
        )
        db.add(h)
        db.flush()
        hybrids.append(h)
        hybrids_by_id[h.id] = h
        return h

    def resolve_hybrid(line: dict[str, Any], crop: str) -> Hybrid | None:
        resolve = (line.get("resolve") or "new").strip()
        csv_name = (line.get("name") or "").strip()
        details = line.get("details") if isinstance(line.get("details"), dict) else {}
        create_name = (details.get("name") or csv_name or "").strip()
        if resolve == "match" or resolve == "pick":
            sid = line.get("selected_hybrid_id")
            if sid:
                try:
                    hid = int(sid)
                except (TypeError, ValueError):
                    hid = 0
                h = hybrids_by_id.get(hid)
                if h:
                    return h
            mh = match_hybrid(csv_name, hybrids, crop)
            if mh:
                return mh
        if not create_name and not csv_name:
            return None
        # Prefer reusing an exact-name hybrid created earlier in this import
        lookup = create_name or csv_name
        existing = match_hybrid(lookup, hybrids, crop)
        if existing and (existing.name or "").casefold() == lookup.casefold():
            # If user filled details now on a later row, enrich empty fields
            if details.get("detail_now"):
                if not existing.brand and details.get("brand"):
                    existing.brand = str(details["brand"]).strip() or None
                if not existing.maturity and details.get("maturity"):
                    existing.maturity = str(details["maturity"]).strip() or None
                if not existing.traits and details.get("traits"):
                    existing.traits = str(details["traits"]).strip() or None
                if existing.cost_per_unit is None and details.get("cost_per_unit") not in (None, ""):
                    try:
                        existing.cost_per_unit = float(details["cost_per_unit"])
                    except (TypeError, ValueError):
                        pass
                if existing.cost_per_acre is None and details.get("cost_per_acre") not in (None, ""):
                    try:
                        existing.cost_per_acre = float(details["cost_per_acre"])
                    except (TypeError, ValueError):
                        pass
            return existing
        return create_hybrid(lookup, crop if crop != "None" else "Corn", details)

    ops = 0
    hybrid_links = 0
    hybrids_created = 0
    fields_created = 0
    fields_crop_updated = 0
    skipped = 0

    for row in proposals:
        if not row.get("include"):
            skipped += 1
            continue
        field: Field | None = None
        fid = row.get("matched_field_id")
        if fid:
            field = fields_by_id.get(int(fid))
        crop = (row.get("crop") or "Corn").strip() or "Corn"
        if crop.lower() in ("bean", "beans", "soy", "soybean", "soybeans"):
            crop = "Soybeans"
        elif crop.lower().startswith("corn"):
            crop = "Corn"
        elif crop not in ("Corn", "Soybeans"):
            crop = "None"

        if field is None and row.get("create_field"):
            name = (row.get("field_name") or row.get("farm_name") or "").strip()
            if not name:
                skipped += 1
                continue
            field = Field(
                crop_year_id=crop_year_id,
                name=name,
                crop=crop,
                acres_total=float(row["acres"]) if row.get("acres") is not None else 0,
                acres_mine=float(row["acres"]) if row.get("acres") is not None else 0,
                ownership_mode="operated_by_me",
            )
            db.add(field)
            db.flush()
            fields_by_id[field.id] = field
            fields.append(field)
            fields_created += 1
        if field is None:
            skipped += 1
            continue

        if row.get("update_field_crop") and crop in ("Corn", "Soybeans", "None"):
            if (field.crop or "") != crop:
                field.crop = crop
                fields_crop_updated += 1

        when = None
        if row.get("op_date"):
            try:
                when = date.fromisoformat(str(row["op_date"])[:10])
            except ValueError:
                when = None

        desc_bits: list[str] = []
        desc_bits.append(f"Crop {crop}")
        if row.get("acres") is not None:
            desc_bits.append(f"Planted {row['acres']:g} ac")
        if row.get("total_units") is not None:
            desc_bits.append(f"{row['total_units']:g} units")
        if row.get("population") is not None:
            desc_bits.append(f"{row['population']:.0f} seeds/ac")
        if row.get("client"):
            desc_bits.append(f"Client: {row['client']}")
        if row.get("all_dates") and len(row["all_dates"]) > 1:
            desc_bits.append("Dates: " + ", ".join(row["all_dates"]))

        hybrid_bits = []
        before_ids = set(hybrids_by_id.keys())
        for line in row.get("hybrids") or []:
            hybrid = resolve_hybrid(line, crop if crop != "None" else (field.crop or "Corn"))
            if not hybrid:
                continue
            if hybrid.id not in before_ids:
                hybrids_created += 1
                before_ids.add(hybrid.id)
            units = line.get("units")
            try:
                units_f = float(units) if units is not None and str(units).strip() != "" else None
            except (TypeError, ValueError):
                units_f = None
            rate = (line.get("rate") or "").strip() or None
            db.add(
                FieldHybrid(
                    field_id=field.id,
                    hybrid_id=hybrid.id,
                    rate=rate,
                    units_applied=units_f,
                    applied_date=when,
                )
            )
            hybrid_links += 1
            label = f"{hybrid.brand} {hybrid.name}".strip() if hybrid.brand else hybrid.name
            csv_name = (line.get("name") or "").strip()
            bit = label
            if csv_name and csv_name.casefold() != (hybrid.name or "").casefold():
                bit += f" (from {csv_name})"
            if rate:
                bit += f" @ {rate}"
            if units_f is not None:
                bit += f" · {units_f:g} units"
            hybrid_bits.append(bit)

        if hybrid_bits:
            desc_bits.append("Hybrids: " + "; ".join(hybrid_bits))

        db.add(
            FieldOperation(
                field_id=field.id,
                op_date=when,
                op_type="Planting",
                description=" · ".join(desc_bits) if desc_bits else "Imported planting",
                cost=0,
                billable=0,
            )
        )
        ops += 1

    return {
        "operations": ops,
        "hybrid_links": hybrid_links,
        "hybrids_created": hybrids_created,
        "fields_created": fields_created,
        "fields_crop_updated": fields_crop_updated,
        "skipped": skipped,
    }
