"""Parse Precision Planting / Panorama Seasonal Inputs planting CSVs.

Typical Hybrid Totals export columns:
  Hybrid Totals: Product
  Hybrid Totals: Population (seeds/ac)
  Hybrid Totals: Total Units (units)
  Hybrid Totals: Area Covered (ac)

Field-level exports may also include Client / Farm / Field.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Optional


FIELD_KEYS = (
    "hybrid",
    "acres",
    "units",
    "population",
    "client",
    "farm",
    "field",
    "crop",
)

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "hybrid": (
        "hybrid totals: product",
        "hybrid totals product",
        "hybrid: hybrid",
        "product",
        "hybrid name",
        "hybrid/variety",
        "variety",
        "seed name",
        "seed product",
        # bare "hybrid" / "seed" only when the focused suffix is exactly that
        "hybrid",
        "seed",
    ),
    "acres": (
        "hybrid totals: area covered (ac)",
        "hybrid totals: area covered",
        "hybrid: acres (ac)",
        "hybrid: acres",
        "area covered (ac)",
        "area covered",
        "acres (ac)",
        "acres planted",
        "planted acres",
        "hybrid acres",
        "acres",
        "area",
    ),
    "units": (
        "hybrid totals: total units (units)",
        "hybrid totals: total units",
        "hybrid: total units (units)",
        "hybrid: total units",
        "total units (units)",
        "total units",
        "units used",
        "seed units",
        "units",
        "bags",
    ),
    "population": (
        "hybrid totals: population (seeds/ac)",
        "hybrid totals: population",
        "hybrid: population (seeds/ac)",
        "hybrid: population",
        "population (seeds/ac)",
        "population",
        "seeds/ac",
        "seeds per acre",
        "seeding rate",
        "target population",
    ),
    "client": (
        "client",
        "client name",
        "grower",
        "grower name",
        "customer",
        "organization",
        "org",
    ),
    "farm": (
        "farm",
        "farm name",
        "operation",
        "operation name",
    ),
    "field": (
        "field",
        "field name",
        "boundary",
        "boundary name",
        "land unit",
    ),
    "crop": (
        "crop",
        "crop type",
        "crop name",
    ),
}


def _norm_header(h: Any) -> str:
    s = str(h or "").strip().lower()
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _match_header(header: str) -> str | None:
    """Return the best field key for a CSV header (longest alias wins)."""
    h = _norm_header(header)
    if not h:
        return None

    # Prefer the part after "hybrid totals:" / "hybrid:" / "liquid totals:" etc.
    # so "Hybrid: Acres (ac)" matches acres, not the bare "hybrid" alias.
    focus = h
    for prefix in (
        "hybrid totals:",
        "hybrid totals",
        "liquid totals:",
        "liquid totals",
        "granular totals:",
        "granular totals",
        "hybrid:",
        "seeding:",
    ):
        if h.startswith(prefix):
            focus = h[len(prefix) :].strip(" :")
            break
    # Panorama sometimes emits ": Client" / ": Farm" / ": Field"
    if h.startswith(":"):
        focus = h.lstrip(": ").strip()

    best_key: str | None = None
    best_score = -1

    def consider(key: str, alias: str, hay: str, weight: int) -> None:
        nonlocal best_key, best_score
        if not alias or not hay:
            return
        # Bare "hybrid"/"seed" must be an exact focused label — otherwise
        # "Hybrid: Acres (ac)" would steal the acres column.
        if alias in ("hybrid", "seed") and hay != alias:
            return
        score = -1
        if hay == alias:
            score = 1000 + len(alias) + weight
        elif hay.endswith(alias):
            score = 800 + len(alias) + weight
        elif alias in hay and len(alias) >= 5:
            # Avoid tiny aliases like "unit" matching "units" elsewhere poorly;
            # require meaningful length for substring.
            score = 400 + len(alias) + weight
        if score > best_score:
            best_score = score
            best_key = key

    # 1) Match against focused suffix first (stronger weight)
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            consider(key, alias, focus, weight=50)

    # 2) Also allow full-header matches for Client/Field/Crop style columns
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            consider(key, alias, h, weight=0)

    # Liquid / granular product columns must not steal hybrid
    if h.startswith("liquid totals") or h.startswith("granular totals"):
        if best_key == "hybrid":
            return None
        if best_key in ("acres", "units", "population") and "hybrid totals" not in h:
            # Area/units on liquid/granular rows — ignore for planting hybrid import
            if "liquid" in h or "granular" in h:
                return None

    # Seeding:* metrics are not hybrid totals — ignore unless uniquely needed
    if h.startswith("seeding:") and best_key in ("acres", "units", "hybrid"):
        return None

    return best_key


def _f(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("%", "")
    if not s or s.lower() in ("—", "-", "n/a", "na", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _s(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip().strip('"')
    return s or None


def guess_crop_from_filename(filename: str | None) -> Optional[str]:
    name = (filename or "").lower()
    if "soy" in name:
        return "Soybeans"
    if "corn" in name:
        return "Corn"
    if "wheat" in name:
        return "Wheat"
    return None


def is_seasonal_inputs_headers(headers: list[str]) -> bool:
    norms = [_norm_header(h) for h in headers]
    joined = " | ".join(norms)
    if "hybrid totals" in joined:
        return True
    # Agronomic Activity export: "Hybrid: Hybrid", "Hybrid: Acres (ac)", ": Field"
    if "hybrid: hybrid" in joined or ("hybrid: acres" in joined and ": field" in joined):
        return True
    keys = {k for h in headers if (k := _match_header(h))}
    return "hybrid" in keys and ("acres" in keys or "units" in keys)


def map_headers(headers: list[str]) -> dict[str, int]:
    """Map logical field → column index (first match wins)."""
    out: dict[str, int] = {}
    for i, h in enumerate(headers):
        key = _match_header(h)
        if key and key not in out:
            out[key] = i
    return out


def parse_csv_bytes(content: bytes, *, filename: str | None = None) -> dict[str, Any]:
    """Parse a Seasonal Inputs / planting CSV into normalized proposal rows."""
    text = content.decode("utf-8-sig", errors="replace")
    # Prefer excel dialect; fall back to sniff
    try:
        reader = csv.reader(io.StringIO(text))
        rows_raw = list(reader)
    except csv.Error:
        return {
            "ok": False,
            "error": "Could not read CSV",
            "headers": [],
            "column_map": {},
            "rows": [],
            "crop": guess_crop_from_filename(filename),
            "has_field_column": False,
            "has_client_column": False,
        }

    if not rows_raw:
        return {
            "ok": False,
            "error": "Empty file",
            "headers": [],
            "column_map": {},
            "rows": [],
            "crop": guess_crop_from_filename(filename),
            "has_field_column": False,
            "has_client_column": False,
        }

    headers = [str(h or "").strip() for h in rows_raw[0]]
    column_map = map_headers(headers)
    crop_default = guess_crop_from_filename(filename)
    has_field = "field" in column_map
    has_client = "client" in column_map

    if "hybrid" not in column_map:
        return {
            "ok": False,
            "error": "No hybrid / product column found. Expected “Hybrid Totals: Product” (or Hybrid / Product).",
            "headers": headers,
            "column_map": column_map,
            "rows": [],
            "crop": crop_default,
            "has_field_column": has_field,
            "has_client_column": has_client,
        }

    proposals: list[dict[str, Any]] = []
    for line_no, raw in enumerate(rows_raw[1:], start=2):
        def cell(key: str) -> Any:
            idx = column_map.get(key)
            if idx is None or idx >= len(raw):
                return None
            return raw[idx]

        hybrid = _s(cell("hybrid"))
        if not hybrid:
            continue
        # Skip liquid/granular-only blanks that somehow land in hybrid col
        acres = _f(cell("acres"))
        units = _f(cell("units"))
        population = _f(cell("population"))
        if acres is None and units is None and population is None:
            # Still keep if it looks like a real hybrid name with zero coverage
            pass

        crop = _s(cell("crop")) or crop_default or "Corn"
        if crop.lower() in ("soy", "soys", "soybean"):
            crop = "Soybeans"
        elif crop.lower().startswith("corn"):
            crop = "Corn"

        client = _s(cell("client"))
        farm = _s(cell("farm"))
        field_name = _s(cell("field"))

        proposals.append(
            {
                "line": line_no,
                "hybrid_name": hybrid,
                "acres": round(acres, 4) if acres is not None else None,
                "units": round(units, 4) if units is not None else None,
                "population": round(population, 1) if population is not None else None,
                "client_name": client,
                "farm_name": farm,
                "field_name": field_name,
                "crop": crop,
                "include": True,
                "field_id": None,
                "field_match": None,
                "hybrid_id": None,
                "hybrid_match": None,
            }
        )

    return {
        "ok": True,
        "error": None,
        "headers": headers,
        "column_map": {k: headers[i] for k, i in column_map.items()},
        "rows": proposals,
        "crop": crop_default,
        "has_field_column": has_field,
        "has_client_column": has_client,
        "filename": filename,
        "is_seasonal_inputs": is_seasonal_inputs_headers(headers),
    }


def match_fields(rows: list[dict[str, Any]], fields: list[Any]) -> list[dict[str, Any]]:
    """Attach best-effort field_id / field_match from existing Field rows."""
    by_name: dict[str, Any] = {}
    for f in fields:
        key = (getattr(f, "name", None) or "").strip().lower()
        if key:
            by_name[key] = f

    out = []
    for row in rows:
        r = dict(row)
        raw = (r.get("field_name") or "").strip()
        if not raw:
            r["field_id"] = r.get("field_id")
            r["field_match"] = r.get("field_match") or "none"
            out.append(r)
            continue
        key = raw.lower()
        hit = by_name.get(key)
        if not hit:
            # loose: field name contained / contains
            for fname, f in by_name.items():
                if key in fname or fname in key:
                    hit = f
                    break
        if hit:
            r["field_id"] = hit.id
            r["field_match"] = "exact" if hit.name.strip().lower() == key else "fuzzy"
        else:
            r["field_id"] = None
            r["field_match"] = "missing"
        out.append(r)
    return out


def match_hybrids(rows: list[dict[str, Any]], hybrids: list[Any]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], Any] = {}
    for h in hybrids:
        name = (getattr(h, "name", None) or "").strip().lower()
        crop = (getattr(h, "crop", None) or "Corn").strip()
        if name:
            by_key[(crop, name)] = h

    out = []
    for row in rows:
        r = dict(row)
        name = (r.get("hybrid_name") or "").strip()
        crop = (r.get("crop") or "Corn").strip()
        hit = by_key.get((crop, name.lower())) if name else None
        if not hit:
            # case-insensitive any-crop fallback
            for (c, n), h in by_key.items():
                if n == name.lower():
                    hit = h
                    break
        if hit:
            r["hybrid_id"] = hit.id
            r["hybrid_match"] = "existing"
        else:
            r["hybrid_id"] = None
            r["hybrid_match"] = "new"
        out.append(r)
    return out


def dump_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def load_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    included = [r for r in rows if r.get("include")]
    return {
        "row_count": len(included),
        "hybrid_count": len({(r.get("crop"), (r.get("hybrid_name") or "").lower()) for r in included}),
        "total_acres": round(sum(float(r["acres"]) for r in included if r.get("acres") is not None), 2),
        "total_units": round(sum(float(r["units"]) for r in included if r.get("units") is not None), 2),
        "with_field": sum(1 for r in included if r.get("field_id") or r.get("field_name")),
        "missing_field": sum(
            1 for r in included if not r.get("field_id") and not (r.get("field_name") or "").strip()
        ),
    }


def read_file(path: Path) -> bytes:
    return path.read_bytes()
