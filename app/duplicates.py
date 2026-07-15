"""Soft duplicate detection — warn before creating look-alike field records."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import (
    Field,
    FieldAssignment,
    FieldHybrid,
    FieldOperation,
    FieldPlan,
    FieldSprayMix,
    Hybrid,
    InputProduct,
    SoilTest,
    SprayMix,
)
from app.templating import templates


def _confirmed(value: str | None) -> bool:
    return (value or "").strip() in ("1", "true", "yes", "on")


def _fmt_date(when: Optional[date]) -> str:
    return when.isoformat() if when else "no date"


def _field_names(db: Session, field_ids: list[int]) -> dict[int, str]:
    if not field_ids:
        return {}
    rows = db.scalars(select(Field).where(Field.id.in_(field_ids))).all()
    return {f.id: f.name for f in rows}


def confirm_if_duplicates(
    request: Request,
    *,
    hits: list[str],
    confirm_duplicate: str,
    action: str,
    cancel_url: str,
    heading: str,
    form_pairs: list[tuple[str, str]],
    user: dict | None = None,
    farm_name: str = "Farm Ops",
    active: str = "",
    lede: str | None = None,
) -> HTMLResponse | None:
    """If hits exist and user has not confirmed, return the confirm page."""
    if not hits or _confirmed(confirm_duplicate):
        return None
    pairs = [(n, v) for n, v in form_pairs if n != "confirm_duplicate"]
    pairs.append(("confirm_duplicate", "1"))
    return templates.TemplateResponse(
        "confirm_duplicate.html",
        {
            "request": request,
            "user": user or request.session.get("user"),
            "farm_name": farm_name,
            "active": active,
            "heading": heading,
            "lede": lede
            or "This looks like a duplicate of something already on the field. Review before continuing.",
            "hits": hits,
            "action": action,
            "cancel_url": cancel_url,
            "form_pairs": pairs,
        },
    )


def find_spray_assign_dups(
    db: Session,
    field_ids: list[int],
    jobs: list[tuple[int, Optional[date]]],
) -> list[str]:
    if not field_ids or not jobs:
        return []
    mix_ids = {mid for mid, _ in jobs}
    mixes = {
        m.id: m.name
        for m in db.scalars(select(SprayMix).where(SprayMix.id.in_(mix_ids))).all()
    }
    names = _field_names(db, field_ids)
    hits: list[str] = []
    seen: set[tuple[int, int, Optional[date]]] = set()
    for fid in field_ids:
        for mid, when in jobs:
            key = (fid, mid, when)
            if key in seen:
                hits.append(
                    f"{names.get(fid, f'Field #{fid}')}: repeating {mixes.get(mid, 'mix')} "
                    f"({_fmt_date(when)}) in this same assign"
                )
                continue
            seen.add(key)
            q = select(FieldSprayMix).where(
                FieldSprayMix.field_id == fid,
                FieldSprayMix.spray_mix_id == mid,
            )
            q = q.where(FieldSprayMix.applied_date.is_(None) if when is None else FieldSprayMix.applied_date == when)
            if db.scalar(q.limit(1)):
                hits.append(
                    f"{names.get(fid, f'Field #{fid}')}: {mixes.get(mid, 'mix')} "
                    f"already assigned for {_fmt_date(when)}"
                )
    return hits


def find_hybrid_assign_dups(
    db: Session,
    field_ids: list[int],
    hybrid_id: int,
    when: Optional[date],
) -> list[str]:
    if not field_ids:
        return []
    hybrid = db.get(Hybrid, hybrid_id)
    hname = hybrid.name if hybrid else f"Hybrid #{hybrid_id}"
    names = _field_names(db, field_ids)
    hits: list[str] = []
    for fid in field_ids:
        q = select(FieldHybrid).where(
            FieldHybrid.field_id == fid,
            FieldHybrid.hybrid_id == hybrid_id,
        )
        q = q.where(FieldHybrid.applied_date.is_(None) if when is None else FieldHybrid.applied_date == when)
        if db.scalar(q.limit(1)):
            hits.append(
                f"{names.get(fid, f'Field #{fid}')}: {hname} already assigned for {_fmt_date(when)}"
            )
    return hits


def find_plan_dups(
    db: Session,
    field_ids: list[int],
    crop_year_id: int,
    plan_type: str,
    title: str,
    when: Optional[date],
) -> list[str]:
    if not field_ids or not title.strip():
        return []
    names = _field_names(db, field_ids)
    hits: list[str] = []
    title_s = title.strip()
    for fid in field_ids:
        q = select(FieldPlan).where(
            FieldPlan.field_id == fid,
            FieldPlan.crop_year_id == crop_year_id,
            FieldPlan.plan_type == plan_type,
            FieldPlan.title == title_s,
            FieldPlan.status != "done",
        )
        q = q.where(FieldPlan.target_date.is_(None) if when is None else FieldPlan.target_date == when)
        if db.scalar(q.limit(1)):
            hits.append(
                f"{names.get(fid, f'Field #{fid}')}: open {plan_type} plan “{title_s}” "
                f"already exists for {_fmt_date(when)}"
            )
    return hits


def find_assignment_dups(
    db: Session,
    field_id: int,
    product_id: int,
    when: date,
) -> list[str]:
    product = db.get(InputProduct, product_id)
    field = db.get(Field, field_id)
    pname = product.name if product else f"Product #{product_id}"
    fname = field.name if field else f"Field #{field_id}"
    existing = db.scalar(
        select(FieldAssignment)
        .where(
            FieldAssignment.field_id == field_id,
            FieldAssignment.product_id == product_id,
            FieldAssignment.assign_date == when,
        )
        .limit(1)
    )
    if not existing:
        return []
    return [
        f"{fname}: {pname} already assigned on {when.isoformat()}"
        f" ({existing.quantity:g} @ ${existing.unit_cost:,.2f})"
    ]


def find_operation_dups(
    db: Session,
    field_id: int,
    op_date: date,
    op_type: str,
    description: str | None,
) -> list[str]:
    field = db.get(Field, field_id)
    fname = field.name if field else f"Field #{field_id}"
    op_type_s = (op_type or "other").strip() or "other"
    desc_s = (description or "").strip()
    q = select(FieldOperation).where(
        FieldOperation.field_id == field_id,
        FieldOperation.op_date == op_date,
        FieldOperation.op_type == op_type_s,
    )
    rows = list(db.scalars(q.order_by(FieldOperation.id.desc()).limit(8)))
    if not rows:
        return []
    hits: list[str] = []
    for row in rows:
        row_desc = (row.description or "").strip()
        # Same type+date; if both have descriptions, require match (case-insensitive)
        if desc_s and row_desc and desc_s.casefold() != row_desc.casefold():
            continue
        bit = f"{fname}: {op_type_s} on {op_date.isoformat()}"
        if row_desc:
            bit += f" — “{row_desc}”"
        if row.cost:
            bit += f" (${row.cost:,.0f})"
        hits.append(bit)
    return hits


def find_soil_dups(db: Session, field_id: int, test_date: date) -> list[str]:
    field = db.get(Field, field_id)
    fname = field.name if field else f"Field #{field_id}"
    row = db.scalar(
        select(SoilTest)
        .where(SoilTest.field_id == field_id, SoilTest.test_date == test_date)
        .limit(1)
    )
    if not row:
        return []
    lab = f" ({row.lab})" if row.lab else ""
    return [f"{fname}: soil test already logged for {test_date.isoformat()}{lab}"]
