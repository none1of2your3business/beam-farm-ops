"""Ensure auto machinery ops (e.g. corn planting when coverage is complete)."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.field_ledger import planting_coverage
from app.models import AppSettings, Field, FieldHybrid, FieldOperation, Hybrid
from app.operation_rates import (
    cost_for_key,
    op_type_for_rate,
    parse_rate_key,
    rate_by_key,
    rate_marker,
)


def _settings(db: Session) -> AppSettings | None:
    return db.scalar(select(AppSettings).limit(1))


def _hybrid_pairs(db: Session, field_id: int) -> list[tuple[Any, Any]]:
    links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    return [(h, db.get(Hybrid, h.hybrid_id)) for h in links]


def field_has_rate_op(db: Session, field_id: int, rate_key: str) -> bool:
    marker = rate_marker(rate_key)
    ops = list(db.scalars(select(FieldOperation).where(FieldOperation.field_id == field_id)))
    rate = rate_by_key(rate_key, _settings(db))
    for op in ops:
        if parse_rate_key(op.description) == rate_key:
            return True
        if op.description and marker in op.description:
            return True
        if rate and (op.op_type or "").strip().lower() == rate.label.lower():
            return True
    return False


def add_rate_operation(
    db: Session,
    field: Field,
    rate_key: str,
    *,
    op_date: Optional[date] = None,
    extra_note: str = "",
    billable: int = 0,
    settings: Any = None,
) -> Optional[FieldOperation]:
    settings = settings if settings is not None else _settings(db)
    rate = rate_by_key(rate_key, settings)
    if not rate:
        return None
    cost = cost_for_key(rate_key, field, settings)
    if cost <= 0:
        return None
    note = f"{rate_marker(rate_key)} ${rate.rate_per_ac:g}/ac × {float(field.acres_total or 0):g} ac"
    if (field.ownership_mode or "").strip() == "on_shares":
        note += " · operator pays 100% (landlord 0)"
    if extra_note:
        note += f" · {extra_note}"
    row = FieldOperation(
        field_id=field.id,
        op_date=op_date or date.today(),
        op_type=op_type_for_rate(rate),
        description=note,
        cost=cost,
        billable=billable,
    )
    db.add(row)
    return row


def ensure_corn_planting_cost(db: Session, field: Field) -> Optional[FieldOperation]:
    """If corn planting is complete and no plant op yet, post Corn planting @ catalog $/ac."""
    crop = (field.crop or "").strip().lower()
    if not crop.startswith("corn"):
        return None
    pairs = _hybrid_pairs(db, field.id)
    cov = planting_coverage(field, pairs)
    if cov.get("status") != "complete":
        return None
    if field_has_rate_op(db, field.id, "corn_plant"):
        return None
    return add_rate_operation(
        db,
        field,
        "corn_plant",
        extra_note="auto: planting complete",
    )


def ensure_all_completed_corn_planting_costs(db: Session, fields: list[Field]) -> int:
    """Backfill corn planting machinery ops for fields already marked complete."""
    n = 0
    for field in fields:
        if ensure_corn_planting_cost(db, field) is not None:
            n += 1
    return n
