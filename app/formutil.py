"""Shared form parsing helpers for FastAPI Form lists and scalars."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def as_list(vals: Any) -> list:
    """Normalize Form(default=[]) / single scalar into a flat list."""
    if vals is None:
        return []
    if isinstance(vals, (str, int, float)):
        return [vals]
    return list(vals)


def parse_float(value: Any, default: float | None = 0.0) -> float | None:
    """Parse a form float; blank → default. Pass default=None to keep None on blank."""
    if value is None:
        return default
    s = str(value).strip().replace(",", "")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def parse_date(value: Any) -> date | None:
    """Parse YYYY-MM-DD (or common US variants); blank → None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
