"""Machinery operation rates ($/acre) for field ledger & P&L.

Defaults live here; AppSettings.operation_rates_json can override rate/label.
Operator pays 100% of these costs on share fields (landlord pays none).
Cost = rate × field.acres_total.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Optional


@dataclass(frozen=True)
class OpRate:
    key: str
    number: str  # "1", "2a", …
    label: str
    rate_per_ac: float
    crops: tuple[str, ...] = ()  # empty = any crop


OPERATION_RATES: tuple[OpRate, ...] = (
    OpRate("corn_plant", "1", "Corn planting", 20.0, ("Corn",)),
    OpRate("soy_plant_60", "2a", "Soy planting (60′ planter)", 18.0, ("Soybeans", "Soy")),
    OpRate("soy_plant_drill", "2b", "Soy planting (drill)", 10.0, ("Soybeans", "Soy")),
    OpRate("deep_till", "3", "Deep tillage (rip)", 6.0),
    OpRate("spring_till", "4", "Spring tillage (VT)", 15.0),
    OpRate("spray", "5", "Spraying (per pass)", 5.0),
    OpRate("soy_harvest", "6", "Soybean harvest + cart + haul", 18.0, ("Soybeans", "Soy")),
    OpRate("corn_harvest", "7", "Corn harvest + cart + haul", 15.0, ("Corn",)),
    OpRate("anhydrous", "8", "Anhydrous application", 25.0),
    OpRate("sidedress", "9", "Sidedressing", 9.0),
)

DEFAULT_KEYS = {r.key for r in OPERATION_RATES}

MARKER_PREFIX = "[op_rate:"


def _overrides_from_settings(settings: Any) -> dict[str, dict]:
    raw = getattr(settings, "operation_rates_json", None) if settings is not None else None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in data.items():
        if k not in DEFAULT_KEYS:
            continue
        if isinstance(v, (int, float)):
            out[k] = {"rate": float(v)}
        elif isinstance(v, dict):
            entry: dict = {}
            if "rate" in v and v["rate"] is not None:
                try:
                    entry["rate"] = float(v["rate"])
                except (TypeError, ValueError):
                    pass
            if v.get("label"):
                entry["label"] = str(v["label"]).strip()
            if entry:
                out[k] = entry
    return out


def effective_rates(settings: Any = None) -> list[OpRate]:
    """Catalog rates with any saved edits applied."""
    overrides = _overrides_from_settings(settings)
    out: list[OpRate] = []
    for r in OPERATION_RATES:
        o = overrides.get(r.key) or {}
        rate = float(o["rate"]) if "rate" in o else r.rate_per_ac
        label = o["label"] if "label" in o else r.label
        out.append(replace(r, rate_per_ac=rate, label=label))
    return out


def rate_by_key(key: str, settings: Any = None) -> Optional[OpRate]:
    for r in effective_rates(settings):
        if r.key == key:
            return r
    return None


def save_rate_overrides(settings: Any, updates: dict[str, dict]) -> str:
    """
    Merge rate/label edits into settings.operation_rates_json.
    updates: {key: {"rate": float, "label": str}}
    Returns JSON string to store.
    """
    current = _overrides_from_settings(settings)
    for key, patch in updates.items():
        if key not in DEFAULT_KEYS:
            continue
        entry = dict(current.get(key) or {})
        if "rate" in patch and patch["rate"] is not None:
            entry["rate"] = float(patch["rate"])
        if "label" in patch and patch["label"] is not None:
            label = str(patch["label"]).strip()
            if label:
                entry["label"] = label
        current[key] = entry
    # Drop overrides that match defaults exactly
    cleaned: dict[str, dict] = {}
    defaults = {r.key: r for r in OPERATION_RATES}
    for key, entry in current.items():
        base = defaults[key]
        keep: dict = {}
        if "rate" in entry and abs(float(entry["rate"]) - base.rate_per_ac) > 1e-9:
            keep["rate"] = float(entry["rate"])
        if entry.get("label") and entry["label"] != base.label:
            keep["label"] = entry["label"]
        if keep:
            cleaned[key] = keep
    return json.dumps(cleaned)


def rate_marker(key: str) -> str:
    return f"{MARKER_PREFIX}{key}]"


def parse_rate_key(description: str | None) -> Optional[str]:
    if not description:
        return None
    s = description.strip()
    if not s.startswith(MARKER_PREFIX):
        return None
    rest = s[len(MARKER_PREFIX) :]
    end = rest.find("]")
    if end <= 0:
        return None
    key = rest[:end].strip()
    return key if key in DEFAULT_KEYS else None


def is_operator_paid_op(op: Any) -> bool:
    """Machinery catalog ops are 100% operator cost (not billed to landlord)."""
    if parse_rate_key(getattr(op, "description", None)):
        return True
    return False


def machinery_acres(field: Any) -> float:
    """Full field acres — operator pays for the whole pass on share ground."""
    total = float(getattr(field, "acres_total", 0) or 0)
    if total > 0:
        return total
    return float(getattr(field, "acres_mine", 0) or 0)


def cost_for_key(key: str, field: Any, settings: Any = None) -> float:
    rate = rate_by_key(key, settings)
    if not rate:
        return 0.0
    ac = machinery_acres(field)
    if ac <= 0:
        return 0.0
    return round(float(rate.rate_per_ac) * ac, 2)


def display_name(rate: OpRate) -> str:
    return f"{rate.number} · {rate.label}"


def rates_for_crop(crop: str | None, settings: Any = None) -> list[OpRate]:
    return effective_rates(settings)


def op_type_for_rate(rate: OpRate) -> str:
    return rate.label


# Back-compat alias used by older imports
RATE_BY_KEY: dict[str, OpRate] = {r.key: r for r in OPERATION_RATES}
