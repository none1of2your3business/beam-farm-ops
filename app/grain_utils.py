"""Grain helpers: moisture shrink, etc."""

from __future__ import annotations


def base_moisture(crop: str) -> float:
    c = (crop or "").lower()
    if "soy" in c:
        return 13.0
    return 15.0


def shrink_net_bu(wet_bu: float, moisture: float | None, crop: str = "Corn") -> float:
    """Convert wet bushels to net (dry) using standard (100-M)/(100-base) factor."""
    if wet_bu is None:
        return 0.0
    if moisture is None:
        return round(float(wet_bu), 2)
    base = base_moisture(crop)
    if moisture <= base:
        return round(float(wet_bu), 2)
    factor = (100.0 - moisture) / (100.0 - base)
    return round(float(wet_bu) * factor, 2)
