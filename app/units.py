"""Ag rate / inventory unit conversions for spray deplete and tank mixes."""

from __future__ import annotations

from typing import Optional

# Convert a rate amount into gallons (volume) or pounds (weight) for inventory.
# Rates are typically "per acre"; inventory products use gal / qt / pt / oz / lb.

_VOLUME_TO_GAL = {
    "gal": 1.0,
    "gallon": 1.0,
    "gallons": 1.0,
    "qt": 0.25,
    "quart": 0.25,
    "quarts": 0.25,
    "pt": 0.125,
    "pint": 0.125,
    "pints": 0.125,
    "fl oz": 1.0 / 128.0,
    "floz": 1.0 / 128.0,
    "oz": 1.0 / 128.0,  # assume fluid oz when converting to gal inventory
    "ounce": 1.0 / 128.0,
    "ounces": 1.0 / 128.0,
    "ml": 1.0 / 3785.411784,
    "l": 1.0 / 3.785411784,
    "liter": 1.0 / 3.785411784,
    "litre": 1.0 / 3.785411784,
}

_WEIGHT_TO_LB = {
    "lb": 1.0,
    "lbs": 1.0,
    "pound": 1.0,
    "pounds": 1.0,
    "oz": 1.0 / 16.0,  # weight oz when inventory is lb
    "ounce": 1.0 / 16.0,
    "ounces": 1.0 / 16.0,
    "kg": 2.2046226218,
    "g": 0.0022046226218,
}


def _norm(unit: Optional[str]) -> str:
    return (unit or "").strip().lower().replace("/ac", "").replace(" per acre", "").strip()


def parse_rate_unit(rate_unit: Optional[str]) -> str:
    """Return the amount unit from a rate string like 'oz/ac' or 'qt/ac'."""
    u = (rate_unit or "").strip().lower()
    if not u:
        return ""
    if "/" in u:
        u = u.split("/", 1)[0].strip()
    u = u.replace("per acre", "").replace("per ac", "").strip()
    return u


def convert_amount(
    amount: float,
    from_unit: Optional[str],
    to_unit: Optional[str],
) -> Optional[float]:
    """
    Convert amount between volume or weight units.
    Returns None if units are incompatible or unknown (caller should fall back).
    """
    if amount is None:
        return None
    src = _norm(from_unit)
    dst = _norm(to_unit)
    if not src or not dst:
        return float(amount)  # no conversion possible — pass through
    if src == dst:
        return float(amount)

    # Volume path
    if src in _VOLUME_TO_GAL and dst in _VOLUME_TO_GAL:
        gallons = float(amount) * _VOLUME_TO_GAL[src]
        return gallons / _VOLUME_TO_GAL[dst]

    # Weight path (prefer weight oz over fluid when dst is lb)
    if src in _WEIGHT_TO_LB and dst in _WEIGHT_TO_LB:
        # Disambiguate bare "oz": if either side is clearly weight, treat as weight
        pounds = float(amount) * _WEIGHT_TO_LB[src]
        return pounds / _WEIGHT_TO_LB[dst]

    # Cross: can't convert volume ↔ weight without density
    return None


def deplete_qty_from_rate(
    rate: float,
    acres: float,
    *,
    rate_unit: Optional[str] = None,
    inventory_unit: Optional[str] = None,
) -> float:
    """
    rate × acres, converted into the inventory product's unit when possible.
    Falls back to raw rate×acres if conversion is unknown.
    """
    raw = float(rate or 0) * float(acres or 0)
    if raw <= 0:
        return 0.0
    from_u = parse_rate_unit(rate_unit)
    to_u = _norm(inventory_unit)
    if not from_u or not to_u:
        return raw
    converted = convert_amount(raw, from_u, to_u)
    if converted is None:
        return raw
    return converted
