"""Field detail, settlements, trucking, tools, export, photos, activity."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.activity import log_activity
from app.database import get_db
from app.duplicates import (
    confirm_if_duplicates,
    find_operation_dups,
    find_plan_dups,
    find_soil_dups,
)
from app.field_ledger import (
    apply_planting_fill,
    build_field_ledger,
    hybrid_link_cost,
    planting_coverage,
    seeds_per_unit,
)
from app.op_cost_ensure import ensure_corn_planting_cost
from app.operation_rates import (
    cost_for_key,
    display_name,
    effective_rates,
    is_operator_paid_op,
    op_type_for_rate,
    rate_by_key,
    rate_marker,
)
from app.grain_utils import shrink_net_bu
from app import cme_quotes
from app.models import (
    ActivityLog,
    AppSettings,
    Attachment,
    BalanceSheetItem,
    BalanceSnapshot,
    BinConditionNote,
    ContractEvent,
    CropInsurance,
    CropYear,
    DocumentScan,
    Equipment,
    EquipmentFuel,
    Field,
    FieldAssignment,
    FieldHybrid,
    FieldOperation,
    FieldPlan,
    FieldShare,
    FieldSprayMix,
    GrainBin,
    BinShare,
    GrainContract,
    Hybrid,
    ImportBatch,
    ImportMappingTemplate,
    InputProduct,
    InputPurchase,
    Invoice,
    InvoiceLine,
    Party,
    Settlement,
    SettlementLine,
    SoilTest,
    SprayMix,
    TruckLoad,
    TruckingRate,
    MarketingTarget,
)
from app import permissions as perms
from app import receipt_ocr
from app.templating import templates

router = APIRouter()
ROOT = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = ROOT / "data" / "uploads"
PHOTO_ROOT = ROOT / "data" / "photos"


def _user(request: Request):
    return request.session.get("user")


def _farm(db: Session) -> str:
    s = db.scalar(select(AppSettings).limit(1))
    return s.farm_name if s else "Beam Farm"


def _year(db: Session) -> CropYear | None:
    s = db.scalar(select(AppSettings).limit(1))
    if s and s.active_crop_year_id:
        y = db.get(CropYear, s.active_crop_year_id)
        if y:
            return y
    return db.scalar(select(CropYear).where(CropYear.is_active == 1).limit(1))


def _settings(db: Session) -> AppSettings:
    s = db.scalar(select(AppSettings).limit(1))
    if not s:
        s = AppSettings(farm_name="Beam Farm")
        db.add(s)
        db.flush()
    return s


def _need(request: Request, module: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not perms.can_access(user.get("role"), module):
        return perms.deny()
    return user


def _f(value: str, default: float | None = 0.0) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _d(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ---------- Field detail (ops, costs, soil, plans) ----------
@router.get("/fields/{field_id}", response_class=HTMLResponse)
def field_detail(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(
            joinedload(Field.party),
            joinedload(Field.shares).joinedload(FieldShare.party),
        )
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)
    ops = list(
        db.scalars(
            select(FieldOperation)
            .where(FieldOperation.field_id == field_id)
            .order_by(FieldOperation.op_date.desc())
        )
    )
    assigns = list(
        db.scalars(
            select(FieldAssignment).where(FieldAssignment.field_id == field_id).order_by(FieldAssignment.id.desc())
        )
    )
    plans = list(db.scalars(select(FieldPlan).where(FieldPlan.field_id == field_id).order_by(FieldPlan.id.desc())))
    hybrid_links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    hybrids = [(h, db.get(Hybrid, h.hybrid_id)) for h in hybrid_links]
    spray_links = list(db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id == field_id)))
    sprays = [(s, db.get(SprayMix, s.spray_mix_id)) for s in spray_links]
    soils = list(
        db.scalars(select(SoilTest).where(SoilTest.field_id == field_id).order_by(SoilTest.test_date.desc()))
    )
    # rotation: same field name in other years
    prior = list(
        db.scalars(
            select(Field)
            .join(CropYear, CropYear.id == Field.crop_year_id)
            .where(Field.name == field.name, Field.id != field.id)
            .options(joinedload(Field.crop_year))
            .order_by(CropYear.year.desc())
        )
    )
    op_cost = sum(o.cost or 0 for o in ops)
    assign_cost = sum((a.quantity or 0) * (a.unit_cost or 0) for a in assigns)
    rent = field.rent_my_share
    total_cost = op_cost + assign_cost + rent
    acres = field.acres_mine or field.acres_total or 0
    cost_ac = round(total_cost / acres, 2) if acres else None
    shares = list(field.shares or [])
    if not shares and (field.my_share_pct is not None or field.ownership_mode == "on_shares"):
        shares = [
            FieldShare(
                field_id=field.id,
                partner_name="Me",
                share_pct=field.my_share_pct if field.my_share_pct is not None else 100.0,
                is_me=1,
                sort_order=0,
            )
        ]
        if field.party_id:
            other = max(0.0, 100.0 - float(field.my_share_pct or 0))
            shares.append(
                FieldShare(
                    field_id=field.id,
                    party_id=field.party_id,
                    partner_name=field.party.name if field.party else "Partner",
                    share_pct=other,
                    is_me=0,
                    sort_order=1,
                )
            )
    return templates.TemplateResponse(
        "field_detail.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "shares": shares,
            "ops": ops,
            "assigns": assigns,
            "plans": plans,
            "hybrids": hybrids,
            "sprays": sprays,
            "soils": soils,
            "prior": prior,
            "op_cost": op_cost,
            "assign_cost": assign_cost,
            "rent": rent,
            "total_cost": total_cost,
            "cost_ac": cost_ac,
            "today": date.today().isoformat(),
        },
    )


@router.get("/fields/{field_id}/operations", response_class=HTMLResponse)
def field_operations(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(joinedload(Field.party))
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)

    ops = list(
        db.scalars(
            select(FieldOperation)
            .where(FieldOperation.field_id == field_id)
            .order_by(FieldOperation.op_date.asc(), FieldOperation.id.asc())
        )
    )
    assigns = list(
        db.scalars(
            select(FieldAssignment)
            .where(FieldAssignment.field_id == field_id)
            .order_by(FieldAssignment.assign_date.asc(), FieldAssignment.id.asc())
        )
    )
    plans = list(db.scalars(select(FieldPlan).where(FieldPlan.field_id == field_id)))
    hybrid_links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    hybrids = [(h, db.get(Hybrid, h.hybrid_id)) for h in hybrid_links]
    spray_links = list(db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id == field_id)))
    sprays = [(s, db.get(SprayMix, s.spray_mix_id)) for s in spray_links]
    soils = list(db.scalars(select(SoilTest).where(SoilTest.field_id == field_id)))

    product_ids = [a.product_id for a in assigns if a.product_id]
    products_by_id: dict[int, InputProduct] = {}
    if product_ids:
        for p in db.scalars(select(InputProduct).where(InputProduct.id.in_(product_ids))):
            products_by_id[p.id] = p

    settings = db.scalar(select(AppSettings).limit(1))
    board = cme_quotes.board_from_settings(settings)
    contracts: list[GrainContract] = []
    if field.crop_year_id:
        contracts = list(
            db.scalars(
                select(GrainContract).where(
                    GrainContract.crop_year_id == field.crop_year_id,
                    GrainContract.crop == field.crop,
                )
            )
        )

    ledger = build_field_ledger(
        field,
        ops=ops,
        assigns=assigns,
        products_by_id=products_by_id,
        spray_links=sprays,
        hybrid_links=hybrids,
        plans=plans,
        soils=soils,
        settings=settings,
        board=board,
        contracts=contracts,
    )

    # Auto-post corn planting machinery cost when planting is already complete
    created = ensure_corn_planting_cost(db, field)
    if created is not None:
        db.commit()
        ops = list(
            db.scalars(
                select(FieldOperation)
                .where(FieldOperation.field_id == field_id)
                .order_by(FieldOperation.op_date.asc(), FieldOperation.id.asc())
            )
        )
        ledger = build_field_ledger(
            field,
            ops=ops,
            assigns=assigns,
            products_by_id=products_by_id,
            spray_links=sprays,
            hybrid_links=hybrids,
            plans=plans,
            soils=soils,
            settings=settings,
            board=board,
            contracts=contracts,
        )

    acres_basis = float(field.acres_total or 0) or float(field.acres_mine or 0)
    rate_options = [
        {
            "key": r.key,
            "number": r.number,
            "label": display_name(r),
            "rate": r.rate_per_ac,
            "est_cost": round(r.rate_per_ac * acres_basis, 2) if acres_basis else 0.0,
        }
        for r in effective_rates(settings)
    ]

    return templates.TemplateResponse(
        "field_operations.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "ledger": ledger,
            "settings": settings,
            "today": date.today().isoformat(),
            "op_rates": rate_options,
            "acres_basis": acres_basis,
            "planting_cost_added": bool(created),
        },
    )


@router.get("/fields/{field_id}/planting/complete", response_class=HTMLResponse)
def field_planting_complete(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field).where(Field.id == field_id).options(joinedload(Field.party))
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)

    links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    hybrid_pairs = [(h, db.get(Hybrid, h.hybrid_id)) for h in links]
    coverage = planting_coverage(field, hybrid_pairs)

    year_id = field.crop_year_id
    crop = (field.crop or "").strip()
    hybrids_q = select(Hybrid).where(Hybrid.crop_year_id == year_id).order_by(Hybrid.name)
    catalog = list(db.scalars(hybrids_q))
    if crop and crop not in ("None", ""):
        same = [h for h in catalog if (h.crop or "") == crop]
        other = [h for h in catalog if (h.crop or "") != crop]
        catalog = same + other

    return templates.TemplateResponse(
        "field_planting_complete.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "coverage": coverage,
            "hybrids": catalog,
            "hybrid_spu": {h.id: seeds_per_unit(h.crop, h.brand) for h in catalog},
            "field_crop_spu": seeds_per_unit(field.crop, None),
            "error": request.query_params.get("err"),
            "msg": request.query_params.get("msg"),
        },
    )


@router.post("/fields/{field_id}/planting/complete")
def field_planting_complete_save(
    request: Request,
    field_id: int,
    mode: str = Form(...),
    acres: str = Form(""),
    units: str = Form(""),
    source_link_id: str = Form(""),
    hybrid_id: str = Form(""),
    rate: str = Form(""),
    population: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)

    links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    hybrid_pairs = [(h, db.get(Hybrid, h.hybrid_id)) for h in links]
    coverage = planting_coverage(field, hybrid_pairs)

    try:
        acres_f = float(str(acres).replace(",", "").strip() or "0")
    except ValueError:
        acres_f = 0.0
    if acres_f <= 0:
        acres_f = float(coverage.get("remaining_acres") or 0)

    units_f = None
    raw_units = (units or "").strip()
    if raw_units:
        try:
            units_f = float(raw_units.replace(",", ""))
        except ValueError:
            units_f = None

    pop = None
    raw_pop = (population or "").strip()
    if raw_pop:
        try:
            pop = float(raw_pop.replace(",", ""))
        except ValueError:
            pop = None

    sid = int(source_link_id) if str(source_link_id).strip().isdigit() else None
    hid = int(hybrid_id) if str(hybrid_id).strip().isdigit() else None

    result = apply_planting_fill(
        db,
        field,
        mode=mode,
        acres=acres_f,
        units=units_f,
        source_link_id=sid,
        hybrid_id=hid,
        rate=rate.strip() or None,
        population=pop,
        notes=notes.strip() or None,
    )
    if not result.get("ok"):
        from urllib.parse import quote

        err = quote(str(result.get("error") or "Could not save"), safe="")
        return RedirectResponse(
            f"/fields/{field_id}/planting/complete?err={err}",
            status_code=303,
        )

    log_activity(
        db,
        user.get("username"),
        "planting_fill",
        f"{field.name}: +{result['acres_added']:g} ac / +{result.get('units_added', 0):g} u · {result['hybrid_name']} ({result['mode']})",
    )
    db.commit()

    from urllib.parse import quote

    # Re-check coverage for redirect message
    links2 = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    pairs2 = [(h, db.get(Hybrid, h.hybrid_id)) for h in links2]
    cov2 = planting_coverage(field, pairs2)
    if cov2["status"] == "complete":
        field2 = db.get(Field, field_id)
        if field2 and ensure_corn_planting_cost(db, field2) is not None:
            db.commit()
        return RedirectResponse(
            f"/fields?msg=planting_complete&field={field_id}",
            status_code=303,
        )
    return RedirectResponse(
        f"/fields/{field_id}/planting/complete?msg="
        + quote(f"Added {result['acres_added']:g} ac. Still {cov2['remaining_acres']:g} ac short."),
        status_code=303,
    )


@router.post("/fields/{field_id}/yield")
def field_yield(
    request: Request,
    field_id: int,
    expected_yield: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)
    field.expected_yield = _f(expected_yield, None) if expected_yield.strip() else None
    log_activity(db, user.get("username"), "field_yield", f"{field.name}: {field.expected_yield}")
    db.commit()
    next_url = request.query_params.get("next") or f"/fields/{field_id}"
    if next_url.startswith("/"):
        return RedirectResponse(next_url, status_code=303)
    return RedirectResponse(f"/fields/{field_id}", status_code=303)


@router.post("/fields/{field_id}/operation")
def field_operation(
    request: Request,
    field_id: int,
    op_date: str = Form(""),
    op_type: str = Form("other"),
    description: str = Form(""),
    cost: str = Form("0"),
    billable: str = Form(""),
    op_rate_key: str = Form(""),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)

    rate_key = (op_rate_key or "").strip()
    settings = db.scalar(select(AppSettings).limit(1))
    rate = rate_by_key(rate_key, settings) if rate_key else None
    when = _d(op_date) or date.today()

    if rate:
        final_type = op_type_for_rate(rate)
        final_cost = cost_for_key(rate.key, field, settings)
        # Allow manual override if user typed a different cost
        override = _f(cost)
        if cost.strip() and override is not None and abs(override - final_cost) > 0.009:
            final_cost = override
        desc_bits = [rate_marker(rate.key), f"${rate.rate_per_ac:g}/ac × {float(field.acres_total or 0):g} ac"]
        if (field.ownership_mode or "").strip() == "on_shares":
            desc_bits.append("operator pays 100% (landlord 0)")
        if description.strip():
            desc_bits.append(description.strip())
        final_desc = " · ".join(desc_bits)
        # Machinery catalog: never bill landlord by default
        final_billable = 1 if billable else 0
        if (field.ownership_mode or "").strip() == "on_shares":
            final_billable = 0
    else:
        final_type = op_type.strip() or "other"
        final_cost = _f(cost) or 0
        final_desc = description.strip() or None
        final_billable = 1 if billable else 0

    pairs = [
        ("op_date", when.isoformat()),
        ("op_type", final_type),
        ("description", final_desc or ""),
        ("cost", str(final_cost)),
        ("op_rate_key", rate_key),
    ]
    if final_billable:
        pairs.append(("billable", "1"))
    block = confirm_if_duplicates(
        request,
        hits=find_operation_dups(db, field_id, when, final_type, final_desc or ""),
        confirm_duplicate=confirm_duplicate,
        action=f"/fields/{field_id}/operation",
        cancel_url=f"/fields/{field_id}/operations",
        heading="Possible duplicate field operation",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="fields",
    )
    if block:
        return block
    db.add(
        FieldOperation(
            field_id=field_id,
            op_date=when,
            op_type=final_type,
            description=final_desc,
            cost=final_cost,
            billable=final_billable,
        )
    )
    log_activity(db, user.get("username"), "field_operation", f"{field.name}: {final_type}")
    db.commit()
    return RedirectResponse(f"/fields/{field_id}/operations", status_code=303)


@router.post("/fields/{field_id}/operations/{op_id}/delete")
def field_operation_delete(
    request: Request,
    field_id: int,
    op_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    op = db.get(FieldOperation, op_id)
    if op and op.field_id == field_id:
        label = f"{op.op_type} ${op.cost}"
        db.delete(op)
        log_activity(db, user.get("username"), "field_operation_delete", label)
        db.commit()
    return RedirectResponse(f"/fields/{field_id}/operations?deleted=1", status_code=303)


@router.post("/fields/{field_id}/assignments/{assign_id}/delete")
def field_assignment_delete(
    request: Request,
    field_id: int,
    assign_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    row = db.get(FieldAssignment, assign_id)
    if row and row.field_id == field_id:
        db.delete(row)
        log_activity(db, user.get("username"), "field_assignment_delete", f"#{assign_id}")
        db.commit()
    return RedirectResponse(f"/fields/{field_id}/operations?deleted=1", status_code=303)


@router.post("/fields/{field_id}/sprays/{link_id}/delete")
def field_spray_delete(
    request: Request,
    field_id: int,
    link_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    row = db.get(FieldSprayMix, link_id)
    if row and row.field_id == field_id:
        db.delete(row)
        log_activity(db, user.get("username"), "field_spray_delete", f"#{link_id}")
        db.commit()
    return RedirectResponse(f"/fields/{field_id}/operations?deleted=1", status_code=303)


@router.post("/fields/{field_id}/hybrids/{link_id}/delete")
def field_hybrid_delete(
    request: Request,
    field_id: int,
    link_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    row = db.get(FieldHybrid, link_id)
    if row and row.field_id == field_id:
        db.delete(row)
        log_activity(db, user.get("username"), "field_hybrid_delete", f"#{link_id}")
        db.commit()
    return RedirectResponse(f"/fields/{field_id}/operations?deleted=1", status_code=303)


def _field_bill_partners(db: Session, field: Field) -> list[dict]:
    """All non-Me partners who can be billed on this field."""
    mode = (field.ownership_mode or "").strip()
    shares = list(field.shares or [])
    if not shares:
        shares = list(
            db.scalars(
                select(FieldShare)
                .where(FieldShare.field_id == field.id)
                .options(joinedload(FieldShare.party))
            ).unique()
        )
    out: list[dict] = []
    if mode == "custom_work":
        party = field.party or (db.get(Party, field.party_id) if field.party_id else None)
        if party:
            out.append({"party": party, "pct": 100.0, "label": party.name})
        return out

    for s in shares:
        if s.is_me:
            continue
        party = s.party or (db.get(Party, s.party_id) if s.party_id else None)
        pct = float(s.share_pct or 0)
        if pct <= 0:
            continue
        if party:
            out.append({"party": party, "pct": pct, "label": party.name})
        else:
            out.append({"party": None, "pct": pct, "label": s.display_name, "needs_party": True})

    if not out and (field.party_id or field.party):
        party = field.party or db.get(Party, field.party_id)
        pct = max(0.0, 100.0 - float(field.my_share_pct or 50))
        if party and pct > 0:
            out.append({"party": party, "pct": pct, "label": party.name})
    return out


def _field_bill_partner(db: Session, field: Field) -> tuple[Party | None, float, str | None]:
    """
    Default partner to invoice and their %.
    on_shares → largest non-Me share (not Me).
    custom_work → 100% to the field party.
    """
    mode = (field.ownership_mode or "").strip()
    if mode == "operated_by_me":
        return None, 0.0, "This field is operated by you — there’s no partner to invoice."

    partners = _field_bill_partners(db, field)
    if not partners:
        return None, 0.0, "Set share partners on this field before invoicing."
    # Prefer largest share with a real party
    usable = [p for p in partners if p.get("party")]
    if not usable:
        return None, partners[0]["pct"], f"Add “{partners[0]['label']}” as a party so they can be invoiced."
    best = max(usable, key=lambda p: float(p["pct"]))
    return best["party"], float(best["pct"]), None


def _field_acres(field: Field) -> float:
    return float(getattr(field, "acres_mine", 0) or getattr(field, "acres_total", 0) or 0)


def _field_billable_items(db: Session, field: Field, partner_pct: float) -> list[dict]:
    """
    Billable ledger lines for a field invoice: planting/seed, sprays, inputs, ops.
    Costs match the field ledger; partner share is their % of each line.
    """
    acres = _field_acres(field)
    share_frac = float(partner_pct or 0) / 100.0
    items: list[dict] = []

    hybrid_links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field.id)))
    for link in hybrid_links:
        hybrid = db.get(Hybrid, link.hybrid_id)
        amt, detail = hybrid_link_cost(link, hybrid, acres)
        title = (hybrid.name if hybrid else f"Hybrid #{link.hybrid_id}") or "Seed"
        if hybrid and hybrid.brand:
            title = f"{title} ({hybrid.brand})"
        cost = float(amt) if amt is not None else None
        share = round(cost * share_frac, 2) if cost is not None else None
        items.append(
            {
                "kind": "hybrid",
                "id": link.id,
                "input_name": "hybrid_id",
                "date": link.applied_date,
                "group": "Planting / seed",
                "type_label": "Seed",
                "title": title,
                "detail": detail or "",
                "cost": cost,
                "share": share,
                "invoice_id": link.invoice_id,
                "selectable": bool(cost and cost > 0 and not link.invoice_id),
                "default_checked": bool(cost and cost > 0 and not link.invoice_id),
            }
        )

    spray_links = list(db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id == field.id)))
    for link in spray_links:
        mix = db.get(SprayMix, link.spray_mix_id)
        cpa = getattr(mix, "cost_per_acre", None) if mix else None
        try:
            cpa_f = float(cpa) if cpa is not None else None
        except (TypeError, ValueError):
            cpa_f = None
        cost = round(cpa_f * acres, 2) if cpa_f is not None and acres else None
        share = round(cost * share_frac, 2) if cost is not None else None
        timing = link.timing_label or (mix.timing if mix else None)
        detail_bits = []
        if timing:
            detail_bits.append(str(timing))
        if cpa_f is not None and acres:
            detail_bits.append(f"${cpa_f:.2f}/ac × {acres:g} ac")
        items.append(
            {
                "kind": "spray",
                "id": link.id,
                "input_name": "spray_id",
                "date": link.applied_date,
                "group": "Sprays",
                "type_label": "Spray",
                "title": (mix.name if mix else f"Spray #{link.spray_mix_id}"),
                "detail": " · ".join(detail_bits),
                "cost": cost,
                "share": share,
                "invoice_id": link.invoice_id,
                "selectable": bool(cost and cost > 0 and not link.invoice_id),
                "default_checked": bool(cost and cost > 0 and not link.invoice_id),
            }
        )

    assigns = list(db.scalars(select(FieldAssignment).where(FieldAssignment.field_id == field.id)))
    for a in assigns:
        prod = db.get(InputProduct, a.product_id)
        qty = float(a.quantity or 0)
        unit = float(a.unit_cost or 0)
        cost = round(qty * unit, 2)
        share = round(cost * share_frac, 2) if cost > 0 else None
        unit_label = (prod.unit if prod else "") or "units"
        items.append(
            {
                "kind": "assign",
                "id": a.id,
                "input_name": "assign_id",
                "date": a.assign_date,
                "group": "Inputs",
                "type_label": "Input",
                "title": (prod.name if prod else f"Product #{a.product_id}"),
                "detail": f"{qty:g} {unit_label} @ ${unit:.4g}",
                "cost": cost if cost > 0 else None,
                "share": share,
                "invoice_id": a.invoice_id,
                "selectable": bool(cost > 0 and not a.invoice_id),
                "default_checked": bool(cost > 0 and not a.invoice_id),
            }
        )

    ops = list(
        db.scalars(
            select(FieldOperation)
            .where(FieldOperation.field_id == field.id)
            .order_by(FieldOperation.op_date.desc(), FieldOperation.id.desc())
        )
    )
    for op in ops:
        cost = float(op.cost or 0)
        operator_paid = is_operator_paid_op(op) or (
            (field.ownership_mode or "").strip() == "on_shares" and not op.billable
        )
        # Share fields: machinery ops are 100% operator — do not bill landlord
        if operator_paid and (field.ownership_mode or "").strip() == "on_shares":
            share = None
            selectable = False
            default_checked = False
            detail = (op.description or "") + (" · operator-only (not billed)" if op.description else "operator-only (not billed)")
        else:
            share = round(cost * share_frac, 2) if cost > 0 else None
            selectable = bool(cost > 0 and not op.invoice_id)
            default_checked = bool(
                cost > 0
                and not op.invoice_id
                and (op.billable or (field.ownership_mode or "") == "custom_work")
            )
            detail = op.description or ""
        items.append(
            {
                "kind": "op",
                "id": op.id,
                "input_name": "op_id",
                "date": op.op_date,
                "group": "Operations",
                "type_label": op.op_type or "Op",
                "title": op.op_type or "Operation",
                "detail": detail,
                "cost": cost if cost > 0 else None,
                "share": share,
                "invoice_id": op.invoice_id,
                "selectable": selectable and not operator_paid,
                "default_checked": default_checked,
            }
        )

    # Stable display: group order, then date desc
    group_order = {"Planting / seed": 0, "Sprays": 1, "Inputs": 2, "Operations": 3}
    items.sort(
        key=lambda it: (
            group_order.get(it["group"], 9),
            -(it["date"].toordinal() if it.get("date") else 0),
            it["id"],
        )
    )
    return items


@router.get("/fields/{field_id}/invoice", response_class=HTMLResponse)
def field_invoice_page(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(
            joinedload(Field.party),
            joinedload(Field.shares).joinedload(FieldShare.party),
        )
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)

    partners = _field_bill_partners(db, field)
    party, partner_pct, err = _field_bill_partner(db, field)
    # Optional preselect from query
    pre_pid = request.query_params.get("party_id")
    if pre_pid and pre_pid.isdigit():
        for p in partners:
            if p.get("party") and p["party"].id == int(pre_pid):
                party = p["party"]
                partner_pct = float(p["pct"])
                err = None
                break

    items = _field_billable_items(db, field, partner_pct or 0) if partner_pct else []
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it["group"], []).append(it)
    selectable = sum(1 for it in items if it.get("selectable"))
    already = sum(1 for it in items if it.get("invoice_id"))
    open_share = round(
        sum(float(it["share"] or 0) for it in items if it.get("selectable") and it.get("share")),
        2,
    )

    return templates.TemplateResponse(
        "field_invoice.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "party": party,
            "partner_pct": partner_pct,
            "partners": partners,
            "bill_error": err,
            "items": items,
            "groups": groups,
            "selectable_count": selectable,
            "already_count": already,
            "open_share_total": open_share,
            "field_acres": _field_acres(field),
            "today": date.today().isoformat(),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/fields/{field_id}/invoice")
async def field_invoice_create(
    request: Request,
    field_id: int,
    invoice_date: str = Form(""),
    due_date: str = Form(""),
    notes: str = Form(""),
    bill_party_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(
            joinedload(Field.party),
            joinedload(Field.shares).joinedload(FieldShare.party),
        )
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)

    partners = _field_bill_partners(db, field)
    party, partner_pct, err = _field_bill_partner(db, field)
    if bill_party_id.strip().isdigit():
        pid = int(bill_party_id)
        for p in partners:
            if p.get("party") and p["party"].id == pid:
                party = p["party"]
                partner_pct = float(p["pct"])
                err = None
                break

    if err or not party or partner_pct <= 0:
        return RedirectResponse(
            f"/fields/{field_id}/invoice?error=partner",
            status_code=303,
        )

    form = await request.form()
    op_ids = {int(x) for x in form.getlist("op_id") if str(x).isdigit()}
    hybrid_ids = {int(x) for x in form.getlist("hybrid_id") if str(x).isdigit()}
    spray_ids = {int(x) for x in form.getlist("spray_id") if str(x).isdigit()}
    assign_ids = {int(x) for x in form.getlist("assign_id") if str(x).isdigit()}
    # Custom extra lines
    custom_names = [str(x) for x in form.getlist("custom_name")]
    custom_qtys = [str(x) for x in form.getlist("custom_qty")]
    custom_rates = [str(x) for x in form.getlist("custom_rate")]

    share_frac = float(partner_pct) / 100.0
    acres = _field_acres(field)
    lines: list[dict] = []
    mark_ops: list[FieldOperation] = []
    mark_hybrids: list[FieldHybrid] = []
    mark_sprays: list[FieldSprayMix] = []
    mark_assigns: list[FieldAssignment] = []

    for oid in op_ids:
        op = db.get(FieldOperation, oid)
        if not op or op.field_id != field_id or op.invoice_id:
            continue
        # Machinery catalog / operator-paid ops stay on your P&L — never invoice landlord
        if is_operator_paid_op(op):
            continue
        amt = round(float(op.cost or 0) * share_frac, 2)
        if amt <= 0:
            continue
        mark_ops.append(op)
        desc = f"{op.op_date} · {op.op_type}"
        if op.description:
            desc += f" — {op.description}"
        desc += f" ({partner_pct:g}% share)"
        lines.append(
            {
                "description": desc[:255],
                "quantity": 1.0,
                "rate": amt,
                "op": op,
                "log_op": False,
            }
        )

    for hid in hybrid_ids:
        link = db.get(FieldHybrid, hid)
        if not link or link.field_id != field_id or link.invoice_id:
            continue
        hybrid = db.get(Hybrid, link.hybrid_id)
        cost, detail = hybrid_link_cost(link, hybrid, acres)
        if cost is None or float(cost) <= 0:
            continue
        amt = round(float(cost) * share_frac, 2)
        if amt <= 0:
            continue
        mark_hybrids.append(link)
        title = (hybrid.name if hybrid else "Seed") or "Seed"
        if hybrid and hybrid.brand:
            title = f"{title} ({hybrid.brand})"
        desc = f"Planting · {title}"
        if detail:
            desc += f" — {detail}"
        desc += f" ({partner_pct:g}% share)"
        lines.append(
            {
                "description": desc[:255],
                "quantity": 1.0,
                "rate": amt,
                "op": None,
                "log_op": False,
            }
        )

    for sid in spray_ids:
        link = db.get(FieldSprayMix, sid)
        if not link or link.field_id != field_id or link.invoice_id:
            continue
        mix = db.get(SprayMix, link.spray_mix_id)
        cpa = getattr(mix, "cost_per_acre", None) if mix else None
        try:
            cpa_f = float(cpa) if cpa is not None else None
        except (TypeError, ValueError):
            cpa_f = None
        if cpa_f is None or acres <= 0:
            continue
        cost = round(cpa_f * acres, 2)
        amt = round(cost * share_frac, 2)
        if amt <= 0:
            continue
        mark_sprays.append(link)
        title = mix.name if mix else "Spray"
        timing = link.timing_label or (mix.timing if mix else None)
        desc = f"Spray · {title}"
        if timing:
            desc += f" — {timing}"
        desc += f" · ${cpa_f:.2f}/ac ({partner_pct:g}% share)"
        lines.append(
            {
                "description": desc[:255],
                "quantity": 1.0,
                "rate": amt,
                "op": None,
                "log_op": False,
            }
        )

    for aid in assign_ids:
        row = db.get(FieldAssignment, aid)
        if not row or row.field_id != field_id or row.invoice_id:
            continue
        cost = round(float(row.quantity or 0) * float(row.unit_cost or 0), 2)
        amt = round(cost * share_frac, 2)
        if amt <= 0:
            continue
        mark_assigns.append(row)
        prod = db.get(InputProduct, row.product_id)
        pname = prod.name if prod else f"Product #{row.product_id}"
        desc = f"Input · {pname} ({partner_pct:g}% share)"
        lines.append(
            {
                "description": desc[:255],
                "quantity": 1.0,
                "rate": amt,
                "op": None,
                "log_op": False,
            }
        )

    for name, qty_s, rate_s in zip(custom_names, custom_qtys, custom_rates):
        name = (name or "").strip()
        if not name:
            continue
        qty = _f(qty_s) or 1.0
        rate = _f(rate_s) or 0.0
        if qty <= 0:
            continue
        lines.append(
            {
                "description": name[:255],
                "quantity": qty,
                "rate": rate,
                "op": None,
                "log_op": True,
            }
        )

    if not lines:
        return RedirectResponse(
            f"/fields/{field_id}/invoice?error=empty",
            status_code=303,
        )

    total = round(sum(float(L["quantity"]) * float(L["rate"]) for L in lines), 2)
    inv = Invoice(
        party_id=party.id,
        field_id=field.id,
        invoice_date=_d(invoice_date) or date.today(),
        due_date=_d(due_date),
        status="unpaid",
        notes=(notes.strip() or None)
        or f"{field.name} · {party.name} billed at {partner_pct:g}%",
        total=total,
    )
    db.add(inv)
    db.flush()

    invoiced_labels: list[str] = []
    for L in lines:
        op_row = L["op"]
        if L["log_op"]:
            # Log custom invoice item as a field operation so the ledger shows it
            op_row = FieldOperation(
                field_id=field.id,
                op_date=inv.invoice_date,
                op_type="invoice item",
                description=L["description"],
                cost=round(float(L["quantity"]) * float(L["rate"]), 2),
                billable=1,
                invoice_id=inv.id,
            )
            db.add(op_row)
            db.flush()
        line = InvoiceLine(
            invoice_id=inv.id,
            description=L["description"],
            quantity=float(L["quantity"]),
            rate=float(L["rate"]),
            field_id=field.id,
            field_operation_id=op_row.id if op_row else None,
        )
        db.add(line)
        if op_row and not L["log_op"]:
            op_row.invoice_id = inv.id
            op_row.billable = 1
            invoiced_labels.append(f"{op_row.op_date} {op_row.op_type}")
        elif L["log_op"]:
            invoiced_labels.append(L["description"])

    for link in mark_hybrids:
        link.invoice_id = inv.id
        hybrid = db.get(Hybrid, link.hybrid_id)
        invoiced_labels.append(f"Seed {hybrid.name if hybrid else link.id}")
    for link in mark_sprays:
        link.invoice_id = inv.id
        mix = db.get(SprayMix, link.spray_mix_id)
        invoiced_labels.append(f"Spray {mix.name if mix else link.id}")
    for row in mark_assigns:
        row.invoice_id = inv.id
        prod = db.get(InputProduct, row.product_id)
        invoiced_labels.append(f"Input {prod.name if prod else row.id}")

    log_activity(
        db,
        user.get("username"),
        "field_invoice",
        f"#{inv.id} {field.name} → {party.name} ${total:.2f}",
    )
    db.commit()
    return RedirectResponse(f"/invoices/{inv.id}?created=1", status_code=303)


@router.post("/fields/{field_id}/soil")
def field_soil(
    request: Request,
    field_id: int,
    test_date: str = Form(""),
    lab: str = Form(""),
    ph: str = Form(""),
    p: str = Form(""),
    k: str = Form(""),
    om: str = Form(""),
    notes: str = Form(""),
    recommendations: str = Form(""),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    if not db.get(Field, field_id):
        return RedirectResponse("/fields", status_code=303)
    when = _d(test_date) or date.today()
    pairs = [
        ("test_date", when.isoformat()),
        ("lab", lab),
        ("ph", ph),
        ("p", p),
        ("k", k),
        ("om", om),
        ("notes", notes),
        ("recommendations", recommendations),
    ]
    hits = find_soil_dups(db, field_id, when)
    year = _year(db)
    if recommendations.strip() and year:
        hits.extend(
            find_plan_dups(
                db,
                [field_id],
                year.id,
                "fertilizer",
                "From soil test",
                when,
            )
        )
    block = confirm_if_duplicates(
        request,
        hits=hits,
        confirm_duplicate=confirm_duplicate,
        action=f"/fields/{field_id}/soil",
        cancel_url=f"/fields/{field_id}",
        heading="Possible duplicate soil test",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="fields",
    )
    if block:
        return block
    db.add(
        SoilTest(
            field_id=field_id,
            test_date=when,
            lab=lab.strip() or None,
            ph=_f(ph, None) if ph.strip() else None,
            p=_f(p, None) if p.strip() else None,
            k=_f(k, None) if k.strip() else None,
            om=_f(om, None) if om.strip() else None,
            notes=notes.strip() or None,
            recommendations=recommendations.strip() or None,
        )
    )
    if recommendations.strip() and year:
        db.add(
            FieldPlan(
                field_id=field_id,
                crop_year_id=year.id,
                plan_type="fertilizer",
                title="From soil test",
                details=recommendations.strip(),
                status="planned",
                target_date=when,
            )
        )
    db.commit()
    return RedirectResponse(f"/fields/{field_id}", status_code=303)


@router.post("/fields/{field_id}/plan-done")
def field_plan_done(request: Request, field_id: int, plan_id: int = Form(...), db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    plan = db.get(FieldPlan, plan_id)
    if plan and plan.field_id == field_id:
        plan.status = "done"
        plan.completed_date = date.today()
        db.commit()
    return RedirectResponse(f"/fields/{field_id}", status_code=303)


# ---------- Settlements ----------
@router.get("/settlements", response_class=HTMLResponse)
def settlements_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    settlements = []
    if year:
        settlements = list(
            db.scalars(
                select(Settlement)
                .where(Settlement.crop_year_id == year.id)
                .order_by(Settlement.id.desc())
            )
        )
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "settlements.html",
        {
            "request": request,
            "user": user,
            "active": "settlements",
            "farm_name": _farm(db),
            "year": year,
            "settlements": settlements,
            "parties": parties,
            "fields": fields,
            "today": date.today().isoformat(),
        },
    )


@router.post("/settlements/create")
def settlements_create(
    request: Request,
    title: str = Form(...),
    party_id: str = Form(""),
    settlement_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/settlements", status_code=303)
    s = Settlement(
        crop_year_id=year.id,
        party_id=int(party_id) if party_id.isdigit() else None,
        title=title.strip(),
        settlement_date=_d(settlement_date) or date.today(),
        notes=notes.strip() or None,
        status="draft",
    )
    db.add(s)
    log_activity(db, user.get("username"), "settlement_create", title.strip())
    db.commit()
    return RedirectResponse(f"/settlements/{s.id}", status_code=303)


@router.get("/settlements/{settlement_id}", response_class=HTMLResponse)
def settlement_detail(request: Request, settlement_id: int, db: Session = Depends(get_db)):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    s = db.get(Settlement, settlement_id)
    if not s:
        return RedirectResponse("/settlements", status_code=303)
    lines = list(db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == s.id)))
    party = db.get(Party, s.party_id) if s.party_id else None
    year = _year(db)
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "settlement_detail.html",
        {
            "request": request,
            "user": user,
            "active": "settlements",
            "farm_name": _farm(db),
            "settlement": s,
            "lines": lines,
            "party": party,
            "fields": fields,
            "print": request.query_params.get("print") == "1",
        },
    )


@router.post("/settlements/{settlement_id}/line")
def settlement_line(
    request: Request,
    settlement_id: int,
    description: str = Form(...),
    amount: str = Form("0"),
    field_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    s = db.get(Settlement, settlement_id)
    if not s:
        return RedirectResponse("/settlements", status_code=303)
    amt = _f(amount) or 0
    db.add(
        SettlementLine(
            settlement_id=s.id,
            field_id=int(field_id) if field_id.isdigit() else None,
            description=description.strip(),
            amount=amt,
        )
    )
    s.total_due = (s.total_due or 0) + amt
    db.commit()
    return RedirectResponse(f"/settlements/{settlement_id}", status_code=303)


@router.post("/settlements/{settlement_id}/autofill-rent")
def settlement_autofill_rent(request: Request, settlement_id: int, db: Session = Depends(get_db)):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    s = db.get(Settlement, settlement_id)
    if not s or not s.party_id:
        return RedirectResponse("/settlements", status_code=303)
    fields = list(
        db.scalars(
            select(Field).where(Field.crop_year_id == s.crop_year_id, Field.party_id == s.party_id)
        )
    )
    for f in fields:
        amt = f.rent_my_share if f.ownership_mode != "custom_work" else (f.acres_total or 0) * (f.rent_per_acre or 0)
        # For landlord cash rent: rent owed is acres * rent (use total acres typically)
        amt = round((f.acres_total or 0) * (f.rent_per_acre or 0), 2)
        if amt <= 0:
            continue
        db.add(
            SettlementLine(
                settlement_id=s.id,
                field_id=f.id,
                description=f"Rent — {f.name} ({f.acres_total} ac × ${f.rent_per_acre})",
                amount=amt,
            )
        )
        s.total_due = (s.total_due or 0) + amt
    db.commit()
    return RedirectResponse(f"/settlements/{settlement_id}", status_code=303)


@router.post("/settlements/{settlement_id}/finalize")
def settlement_finalize(request: Request, settlement_id: int, db: Session = Depends(get_db)):
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    s = db.get(Settlement, settlement_id)
    if s:
        s.status = "final"
        db.commit()
    return RedirectResponse(f"/settlements/{settlement_id}?print=1", status_code=303)


# ---------- Trucking ----------
@router.get("/trucking", response_class=HTMLResponse)
def trucking_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "trucking")
    if isinstance(user, RedirectResponse):
        return user
    rates = list(db.scalars(select(TruckingRate).order_by(TruckingRate.destination)))
    loads = list(
        db.scalars(
            select(TruckLoad).order_by(TruckLoad.crop, TruckLoad.load_date.desc()).limit(100)
        )
    )
    return templates.TemplateResponse(
        "trucking.html",
        {
            "request": request,
            "user": user,
            "active": "trucking",
            "farm_name": _farm(db),
            "rates": rates,
            "loads": loads,
            "today": date.today().isoformat(),
        },
    )


@router.post("/trucking/rate")
def trucking_rate(
    request: Request,
    destination: str = Form(...),
    hauler: str = Form(""),
    rate_per_bu: str = Form(""),
    rate_per_load: str = Form(""),
    miles: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trucking")
    if isinstance(user, RedirectResponse):
        return user
    from app import lookups as lu

    dest = destination.strip()
    trucker = hauler.strip() or None
    rate = _f(rate_per_bu, None) if rate_per_bu.strip() else None
    if dest:
        lu.ensure(db, lu.DESTINATION, dest)
    if trucker:
        lu.ensure(db, lu.HAULER, trucker)
    if rate is not None:
        lu.ensure_freight(db, rate)
    db.add(
        TruckingRate(
            destination=dest,
            hauler=trucker,
            rate_per_bu=rate,
            rate_per_load=_f(rate_per_load, None) if rate_per_load.strip() else None,
            miles=_f(miles, None) if miles.strip() else None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/trucking", status_code=303)


@router.post("/trucking/load")
def trucking_load(
    request: Request,
    load_date: str = Form(""),
    crop: str = Form("Corn"),
    destination: str = Form(...),
    hauler: str = Form(""),
    bushels: str = Form("0"),
    rate_paid: str = Form(""),
    ticket_number: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trucking")
    if isinstance(user, RedirectResponse):
        return user
    from app import lookups as lu

    dest = destination.strip()
    trucker = hauler.strip() or None
    if dest:
        lu.ensure(db, lu.DESTINATION, dest)
    if trucker:
        lu.ensure(db, lu.HAULER, trucker)
    if crop:
        lu.ensure(db, lu.CROP, crop)
    db.add(
        TruckLoad(
            load_date=_d(load_date) or date.today(),
            crop=crop,
            destination=dest,
            hauler=trucker,
            bushels=_f(bushels) or 0,
            rate_paid=_f(rate_paid, None) if rate_paid.strip() else None,
            ticket_number=ticket_number.strip() or None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/trucking", status_code=303)


# ---------- Tools: sprayer fill ----------
@router.get("/tools", response_class=HTMLResponse)
def tools_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "tools")
    if isinstance(user, RedirectResponse):
        return user
    settings = _settings(db)
    year = _year(db)
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "tools.html",
        {
            "request": request,
            "user": user,
            "active": "tools",
            "farm_name": _farm(db),
            "settings": settings,
            "fields": fields,
            "result": None,
        },
    )


@router.post("/tools/sprayer", response_class=HTMLResponse)
def tools_sprayer(
    request: Request,
    tank_gal: str = Form("1000"),
    gpa: str = Form("15"),
    acres: str = Form(""),
    field_id: str = Form(""),
    product_rate: str = Form(""),
    product_unit: str = Form("oz/ac"),
    save_defaults: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "tools")
    if isinstance(user, RedirectResponse):
        return user
    settings = _settings(db)
    tank = _f(tank_gal) or 1000
    gpa_v = _f(gpa) or 15
    if save_defaults:
        settings.sprayer_tank_gal = tank
        settings.sprayer_gpa = gpa_v
        db.commit()
    acres_v = _f(acres, None)
    if field_id.isdigit() and (not acres_v or acres_v <= 0):
        f = db.get(Field, int(field_id))
        if f:
            acres_v = f.acres_total or f.acres_mine or 0
    acres_v = acres_v or 0
    acres_per_fill = round(tank / gpa_v, 2) if gpa_v else 0
    fills = round(acres_v / acres_per_fill, 2) if acres_per_fill else 0
    rate = _f(product_rate, None)
    product_per_fill = round(rate * acres_per_fill, 2) if rate is not None and acres_per_fill else None
    product_total = round(rate * acres_v, 2) if rate is not None and acres_v else None
    year = _year(db)
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "tools.html",
        {
            "request": request,
            "user": user,
            "active": "tools",
            "farm_name": _farm(db),
            "settings": settings,
            "fields": fields,
            "result": {
                "tank": tank,
                "gpa": gpa_v,
                "acres": acres_v,
                "acres_per_fill": acres_per_fill,
                "fills": fills,
                "product_unit": product_unit,
                "product_per_fill": product_per_fill,
                "product_total": product_total,
            },
        },
    )


# ---------- Photos / attachments ----------
@router.get("/photos", response_class=HTMLResponse)
def photos_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    items = list(db.scalars(select(Attachment).order_by(Attachment.id.desc()).limit(80)))
    pending = list(
        db.scalars(
            select(DocumentScan)
            .where(DocumentScan.status == "pending")
            .order_by(DocumentScan.id.desc())
            .limit(20)
        )
    )
    return templates.TemplateResponse(
        "photos.html",
        {
            "request": request,
            "user": user,
            "active": "upload",
            "farm_name": _farm(db),
            "items": items,
            "pending_scans": pending,
        },
    )


@router.post("/photos/upload")
async def photos_upload(
    request: Request,
    entity_type: str = Form("ticket"),
    entity_id: str = Form(""),
    caption: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    content = await file.read()
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (file.filename or "photo.bin"))
    name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path = PHOTO_ROOT / name
    path.write_bytes(content)
    db.add(
        Attachment(
            entity_type=entity_type.strip() or "other",
            entity_id=int(entity_id) if entity_id.isdigit() else None,
            filename=file.filename or name,
            stored_path=str(path.relative_to(ROOT)),
            caption=caption.strip() or None,
        )
    )
    log_activity(db, user.get("username"), "photo_upload", file.filename or name)
    db.commit()
    return RedirectResponse("/photos", status_code=303)


@router.get("/photos/file/{attachment_id}")
def photos_file(request: Request, attachment_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    att = db.get(Attachment, attachment_id)
    if not att:
        return RedirectResponse("/photos", status_code=303)
    path = ROOT / att.stored_path
    if not path.exists():
        return RedirectResponse("/photos", status_code=303)
    media = "application/octet-stream"
    lower = path.suffix.lower()
    if lower in (".jpg", ".jpeg"):
        media = "image/jpeg"
    elif lower == ".png":
        media = "image/png"
    elif lower == ".webp":
        media = "image/webp"
    elif lower == ".gif":
        media = "image/gif"
    elif lower == ".pdf":
        media = "application/pdf"
    elif lower in (".heic", ".heif"):
        media = "image/heic"
    return Response(
        path.read_bytes(),
        media_type=media,
        headers={"Content-Disposition": f'inline; filename="{att.filename}"'},
    )


def _f(value: str, default: float | None = 0.0) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _d(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _scan_suggested(scan: DocumentScan) -> dict:
    try:
        return json.loads(scan.suggested_json or "{}")
    except json.JSONDecodeError:
        return {}


@router.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    pending = list(
        db.scalars(
            select(DocumentScan)
            .where(DocumentScan.status == "pending")
            .order_by(DocumentScan.id.desc())
            .limit(15)
        )
    )
    return templates.TemplateResponse(
        "scan.html",
        {
            "request": request,
            "user": user,
            "active": "scan",
            "farm_name": _farm(db),
            "pending": pending,
            "msg": request.query_params.get("msg"),
        },
    )


@router.post("/scan")
async def scan_upload(
    request: Request,
    doc_kind: str = Form("receipt"),
    caption: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    content = await file.read()
    if not content:
        return RedirectResponse("/scan?msg=Empty+file", status_code=303)

    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    raw_name = file.filename or "scan.jpg"
    # iPhone often sends image.jpg / image.jpeg from camera
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw_name)
    if "." not in safe:
        safe += ".jpg"
    name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path = PHOTO_ROOT / name
    path.write_bytes(content)

    kind = (doc_kind or "receipt").strip().lower()
    if kind not in ("receipt", "statement", "ticket", "contract", "other"):
        kind = "receipt"

    att = Attachment(
        entity_type="scan",
        entity_id=None,
        filename=raw_name,
        stored_path=str(path.relative_to(ROOT)),
        caption=caption.strip() or None,
    )
    db.add(att)
    db.flush()

    ocr = receipt_ocr.run_ocr(content)
    suggested = ocr.get("suggested") or {}
    if ocr.get("ocr_note"):
        suggested["ocr_note"] = ocr["ocr_note"]

    scan = DocumentScan(
        attachment_id=att.id,
        status="pending",
        doc_kind=kind,
        ocr_text=ocr.get("ocr_text") or "",
        suggested_json=json.dumps(suggested),
        notes=caption.strip() or None,
    )
    db.add(scan)
    log_activity(db, user.get("username"), "scan_upload", raw_name)
    db.commit()
    return RedirectResponse(f"/scan/{scan.id}", status_code=303)


@router.get("/scan/{scan_id}", response_class=HTMLResponse)
def scan_review(request: Request, scan_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    scan = db.get(DocumentScan, scan_id)
    if not scan:
        return RedirectResponse("/scan", status_code=303)
    att = db.get(Attachment, scan.attachment_id)
    products = list(db.scalars(select(InputProduct).order_by(InputProduct.name)))
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    contracts = list(db.scalars(select(GrainContract).order_by(GrainContract.id.desc()).limit(40)))
    suggested = _scan_suggested(scan)
    return templates.TemplateResponse(
        "scan_review.html",
        {
            "request": request,
            "user": user,
            "active": "scan",
            "farm_name": _farm(db),
            "scan": scan,
            "att": att,
            "suggested": suggested,
            "products": products,
            "parties": parties,
            "contracts": contracts,
            "today": date.today().isoformat(),
            "msg": request.query_params.get("msg"),
        },
    )


@router.post("/scan/{scan_id}/discard")
def scan_discard(request: Request, scan_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    scan = db.get(DocumentScan, scan_id)
    if scan and scan.status == "pending":
        scan.status = "discarded"
        db.commit()
    return RedirectResponse("/scan", status_code=303)


@router.post("/scan/{scan_id}/file")
def scan_file(
    request: Request,
    scan_id: int,
    destination: str = Form(...),
    doc_kind: str = Form("receipt"),
    ocr_text: str = Form(""),
    vendor: str = Form(""),
    doc_date: str = Form(""),
    total_cost: str = Form(""),
    quantity: str = Form("1"),
    product_id: str = Form(""),
    new_product_name: str = Form(""),
    product_category: str = Form("other"),
    product_unit: str = Form("gal"),
    party_id: str = Form(""),
    invoice_description: str = Form(""),
    link_entity_id: str = Form(""),
    contract_id: str = Form(""),
    notes: str = Form(""),
    caption: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    scan = db.get(DocumentScan, scan_id)
    if not scan or scan.status != "pending":
        return RedirectResponse("/scan", status_code=303)
    att = db.get(Attachment, scan.attachment_id)
    if not att:
        return RedirectResponse("/scan", status_code=303)

    dest = (destination or "").strip().lower()
    scan.ocr_text = ocr_text
    scan.doc_kind = (doc_kind or scan.doc_kind).strip().lower() or "receipt"
    scan.notes = notes.strip() or None
    scan.destination = dest
    if caption.strip():
        att.caption = caption.strip()

    dt = _d(doc_date) or date.today()
    total = _f(total_cost, default=None)
    qty = _f(quantity) or 1.0
    linked_type = None
    linked_id = None

    if dest == "purchase":
        product = None
        if product_id.isdigit():
            product = db.get(InputProduct, int(product_id))
        if not product and new_product_name.strip():
            pname = new_product_name.strip()
            product = db.scalar(select(InputProduct).where(InputProduct.name == pname))
            if not product:
                product = InputProduct(
                    name=pname,
                    category=product_category.strip() or "other",
                    unit=product_unit.strip() or "gal",
                )
                db.add(product)
                db.flush()
        if not product:
            return RedirectResponse(f"/scan/{scan_id}?msg=Pick+or+create+a+product", status_code=303)
        cost = total or 0.0
        old_qty = product.on_hand or 0
        old_cost = (product.avg_unit_cost or 0) * old_qty
        new_qty = old_qty + qty
        product.on_hand = new_qty
        if new_qty > 0:
            product.avg_unit_cost = (old_cost + cost) / new_qty
        purchase = InputPurchase(
            product_id=product.id,
            purchase_date=dt,
            vendor=vendor.strip() or None,
            quantity=qty,
            total_cost=cost,
            notes=(notes.strip() or None) or (f"From scan #{scan.id}"),
        )
        db.add(purchase)
        db.flush()
        linked_type, linked_id = "purchase", purchase.id
        att.entity_type = "receipt"
        att.entity_id = purchase.id

    elif dest == "invoice":
        desc = (invoice_description or vendor or "Scanned invoice").strip()
        rate = total if total is not None else 0.0
        inv = Invoice(
            party_id=int(party_id) if party_id.isdigit() else None,
            invoice_date=dt,
            status="unpaid",
            notes=notes.strip() or f"From scan #{scan.id}",
            total=qty * rate,
        )
        db.add(inv)
        db.flush()
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                description=desc[:255],
                quantity=qty,
                rate=rate,
            )
        )
        linked_type, linked_id = "invoice", inv.id
        att.entity_type = "receipt"
        att.entity_id = inv.id

    elif dest == "ticket":
        eid = int(link_entity_id) if link_entity_id.isdigit() else None
        att.entity_type = "ticket"
        att.entity_id = eid
        linked_type, linked_id = "ticket", eid

    elif dest == "contract":
        eid = None
        if contract_id.isdigit():
            eid = int(contract_id)
        elif link_entity_id.isdigit():
            eid = int(link_entity_id)
        att.entity_type = "contract"
        att.entity_id = eid
        linked_type, linked_id = "contract", eid

    elif dest == "archive":
        att.entity_type = scan.doc_kind if scan.doc_kind in ("receipt", "statement", "other") else "other"
        att.entity_id = None
        linked_type, linked_id = "archive", None

    else:
        return RedirectResponse(f"/scan/{scan_id}?msg=Choose+where+this+goes", status_code=303)

    scan.linked_entity_type = linked_type
    scan.linked_entity_id = linked_id
    scan.status = "filed"
    scan.suggested_json = json.dumps(
        {
            **_scan_suggested(scan),
            "vendor": vendor.strip() or None,
            "date": dt.isoformat(),
            "total": total,
            "quantity": qty,
        }
    )
    log_activity(db, user.get("username"), "scan_file", f"scan#{scan.id} → {dest}")
    db.commit()
    if dest == "purchase":
        return RedirectResponse("/purchases", status_code=303)
    if dest == "invoice":
        return RedirectResponse("/invoices", status_code=303)
    return RedirectResponse("/photos", status_code=303)


# ---------- Activity log ----------
@router.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "activity")
    if isinstance(user, RedirectResponse):
        return user
    rows = list(db.scalars(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(200)))
    return templates.TemplateResponse(
        "activity.html",
        {
            "request": request,
            "user": user,
            "active": "activity",
            "farm_name": _farm(db),
            "rows": rows,
        },
    )


# ---------- Excel / PDF reports ----------
@router.get("/export")
def export_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app.reports import REPORT_MODULES

    return templates.TemplateResponse(
        "export.html",
        {
            "request": request,
            "user": user,
            "active": "export",
            "farm_name": _farm(db),
            "modules": REPORT_MODULES,
            "year": _year(db),
        },
    )


@router.get("/export/workbook")
def export_workbook(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app.reports import build_excel

    year = _year(db)
    buf = build_excel(db, year, "all", _farm(db))
    fname = f"beam_farm_backup_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/report")
def export_report(
    request: Request,
    module: str = "fields",
    fmt: str = "xlsx",
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app.reports import REPORT_MODULES, build_excel, build_pdf

    allowed = {k for k, _ in REPORT_MODULES}
    if module not in allowed:
        module = "fields"
    fmt = (fmt or "xlsx").lower()
    year = _year(db)
    farm = _farm(db)
    label = next((t for k, t in REPORT_MODULES if k == module), module)
    slug = label.replace(" ", "_").replace("/", "-")[:40]
    day = date.today().isoformat()

    if fmt == "pdf":
        if module == "all":
            # PDF of every module as sections
            buf = build_pdf(db, year, "all", farm)
        else:
            buf = build_pdf(db, year, module, farm)
        fname = f"beam_{slug}_{day}.pdf"
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    buf = build_excel(db, year, module, farm)
    fname = f"beam_{slug}_{day}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------- Balance snapshot + print banker pack ----------
@router.post("/balance/snapshot")
def balance_snapshot(
    request: Request,
    as_of_date: str = Form(""),
    label: str = Form(""),
    assets_total: str = Form("0"),
    liabilities_total: str = Form("0"),
    equity: str = Form("0"),
    working_capital: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _need(request, "balance")
    if isinstance(user, RedirectResponse):
        return user
    as_of = _d(as_of_date) or date.today()
    db.add(
        BalanceSnapshot(
            as_of_date=as_of,
            label=label.strip() or f"Snapshot {as_of.isoformat()}",
            assets_total=_f(assets_total) or 0,
            liabilities_total=_f(liabilities_total) or 0,
            equity=_f(equity) or 0,
            working_capital=_f(working_capital) or 0,
        )
    )
    log_activity(db, user.get("username"), "balance_snapshot", str(as_of))
    db.commit()
    return RedirectResponse(f"/balance?as_of={as_of.isoformat()}", status_code=303)


@router.get("/balance/print", response_class=HTMLResponse)
def balance_print(request: Request, as_of: str = "", db: Session = Depends(get_db)):
    user = _need(request, "balance")
    if isinstance(user, RedirectResponse):
        return user
    as_of_date = _d(as_of) or date.today()
    items = list(db.scalars(select(BalanceSheetItem).where(BalanceSheetItem.as_of_date == as_of_date)))
    assets = [i for i in items if i.side == "asset"]
    liabilities = [i for i in items if i.side == "liability"]
    equip = list(db.scalars(select(Equipment).where(Equipment.status == "active")))
    equip_market = sum(e.market_value or 0 for e in equip)
    equip_loans = sum(e.loan_balance or 0 for e in equip if e.finance_status == "loan")
    grain_bu = sum(s.bushels or 0 for s in db.scalars(select(BinShare)))
    assets_total = sum(a.amount for a in assets) + equip_market
    liabilities_total = sum(l.amount for l in liabilities) + equip_loans
    current_assets = sum(a.amount for a in assets if a.is_current) + 0  # grain manual
    current_liab = sum(l.amount for l in liabilities if l.is_current)
    equity = assets_total - liabilities_total
    working = current_assets - current_liab
    snapshots = list(db.scalars(select(BalanceSnapshot).order_by(BalanceSnapshot.id.desc()).limit(10)))
    return templates.TemplateResponse(
        "balance_print.html",
        {
            "request": request,
            "user": user,
            "farm_name": _farm(db),
            "as_of_date": as_of_date,
            "assets": assets,
            "liabilities": liabilities,
            "equip": equip,
            "equip_market": equip_market,
            "equip_loans": equip_loans,
            "grain_bu": grain_bu,
            "assets_total": assets_total,
            "liabilities_total": liabilities_total,
            "equity": equity,
            "working_capital": working,
            "snapshots": snapshots,
        },
    )


# ---------- Equipment fuel ----------
@router.post("/equipment/{equip_id}/fuel")
def equipment_fuel(
    request: Request,
    equip_id: int,
    fill_date: str = Form(""),
    gallons: str = Form("0"),
    cost: str = Form("0"),
    hours_at_fill: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not db.get(Equipment, equip_id):
        return RedirectResponse("/equipment", status_code=303)
    db.add(
        EquipmentFuel(
            equipment_id=equip_id,
            fill_date=_d(fill_date) or date.today(),
            gallons=_f(gallons) or 0,
            cost=_f(cost) or 0,
            hours_at_fill=_f(hours_at_fill, None) if hours_at_fill.strip() else None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(f"/equipment/{equip_id}", status_code=303)


# ---------- Mapping templates list ----------
@router.post("/upload/save-mapping")
def save_mapping(
    request: Request,
    name: str = Form(...),
    import_type: str = Form("generic"),
    mapping_json: str = Form("{}"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    try:
        json.loads(mapping_json or "{}")
    except json.JSONDecodeError:
        mapping_json = "{}"
    db.add(
        ImportMappingTemplate(
            name=name.strip(),
            import_type=import_type,
            mapping_json=mapping_json or "{}",
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/upload", status_code=303)
