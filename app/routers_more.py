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
from app.flash import redirect_flash
from app.database import get_db
from app.duplicates import (
    confirm_if_duplicates,
    find_assignment_dups,
    find_hybrid_assign_dups,
    find_operation_dups,
    find_plan_dups,
    find_soil_dups,
    find_spray_assign_dups,
)
from app.field_ledger import build_field_ledger
from app.grain_utils import shrink_net_bu
from app import cme_quotes
from app.units import deplete_qty_from_rate
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
    SprayMixLine,
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


def _need_edit_fields(request: Request):
    """Fields module + write permission (blocks viewer)."""
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_fields(user.get("role")):
        return redirect_flash(request, "/fields", "View-only role — editing is disabled.", "warn")
    return user


def _f(value: str, default: float | None = 0.0) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return default
    try:
        out = float(value)
    except ValueError:
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / ±
        return default
    return out


def _d(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _field_insurance_budget(
    field: Field,
    db: Session,
    settings: AppSettings | None,
) -> tuple[float | None, float | None]:
    """Return (insurance_premium_share, budget_cop_ac) for build_field_ledger.

    Premium is split by field acres / policy acres when policy acres are set.
    When policy acres are missing, split across all same-crop fields in the year
    so the farm total never exceeds the premium.
    """
    ins_premium: float | None = None
    if field.crop_year_id and field.crop and field.crop not in ("None", ""):
        policies = list(
            db.scalars(
                select(CropInsurance).where(
                    CropInsurance.crop_year_id == field.crop_year_id,
                    CropInsurance.crop == field.crop,
                )
            )
        )
        if policies:
            field_ac = float(field.acres_mine or field.acres_total or 0)
            crop_fields = list(
                db.scalars(
                    select(Field).where(
                        Field.crop_year_id == field.crop_year_id,
                        Field.crop == field.crop,
                    )
                )
            )
            crop_ac_total = sum(float(f.acres_mine or f.acres_total or 0) for f in crop_fields) or 0.0
            share_total = 0.0
            for pol in policies:
                prem = float(pol.premium or 0)
                if prem <= 0:
                    continue
                pol_ac = float(pol.acres or 0)
                if pol_ac > 0 and field_ac > 0:
                    # Cap at 100% of premium if field acres exceed policy acres
                    share_total += prem * min(1.0, field_ac / pol_ac)
                elif crop_ac_total > 0 and field_ac > 0:
                    share_total += prem * (field_ac / crop_ac_total)
                elif len(crop_fields) == 1:
                    share_total += prem
            if share_total > 0:
                ins_premium = round(share_total, 2)

    budget_cop_ac: float | None = None
    if settings:
        crop_lower = (field.crop or "").strip().lower()
        if "corn" in crop_lower:
            v = getattr(settings, "corn_cost_per_ac", None)
            budget_cop_ac = float(v) if v else None
        elif "soy" in crop_lower:
            v = getattr(settings, "soy_cost_per_ac", None)
            budget_cop_ac = float(v) if v else None

    return ins_premium, budget_cop_ac


# Season milestones shown on Field Operations Center (done vs still needed).
_SEASON_MILESTONES = [
    ("Planting", ("planting", "plant")),
    ("Spraying", ("spraying", "spray")),
    ("Dry Fertilizer", ("dry fertilizer", "fertilizer")),
    ("Sidedress", ("sidedress", "side-dress", "side dress")),
    ("Lime", ("lime",)),
    ("Harvest", ("harvest",)),
]


def _op_matches_milestone(op_type: str, needles: tuple[str, ...]) -> bool:
    t = (op_type or "").strip().lower()
    return any(n in t for n in needles)


def _season_checklist(
    ops: list[FieldOperation],
    hybrids: list,
    sprays: list,
) -> list[dict]:
    """Mark common season passes done from logged ops / seed / spray links."""
    rows: list[dict] = []
    live_ops = [o for o in ops if not getattr(o, "voided", 0)]
    for label, needles in _SEASON_MILESTONES:
        matches = [o for o in live_ops if _op_matches_milestone(o.op_type or "", needles)]
        done = bool(matches)
        latest = None
        if matches:
            dated = [o.op_date for o in matches if o.op_date]
            latest = max(dated) if dated else None
        # Seed / spray assignments also count even without a typed op
        if not done and label == "Planting" and any(
            h for h, _ in hybrids if h and not getattr(h, "voided", 0)
        ):
            done = True
            dated = [h.applied_date for h, _ in hybrids if h and not getattr(h, "voided", 0) and h.applied_date]
            latest = max(dated) if dated else latest
        if not done and label == "Spraying" and any(
            s for s, _ in sprays if s and not getattr(s, "voided", 0)
        ):
            done = True
            dated = [s.applied_date for s, _ in sprays if s and not getattr(s, "voided", 0) and s.applied_date]
            latest = max(dated) if dated else latest
        rows.append(
            {
                "label": label,
                "done": done,
                "count": len(matches),
                "latest": latest.isoformat() if latest else None,
            }
        )
    return rows


# ---------- Field detail (ops center: costs, P&L, plans, soil) ----------
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
            joinedload(Field.crop_year),
        )
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)
    ops = list(
        db.scalars(
            select(FieldOperation)
            .where(FieldOperation.field_id == field_id)
            .order_by(FieldOperation.op_date.desc(), FieldOperation.id.desc())
        )
    )
    assigns = list(
        db.scalars(
            select(FieldAssignment)
            .where(FieldAssignment.field_id == field_id)
            .order_by(FieldAssignment.assign_date.desc(), FieldAssignment.id.desc())
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
    prior = list(
        db.scalars(
            select(Field)
            .join(CropYear, CropYear.id == Field.crop_year_id)
            .where(Field.name == field.name, Field.id != field.id)
            .options(joinedload(Field.crop_year))
            .order_by(CropYear.year.desc())
        )
    )

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

    ins_prem, budget_cop_ac = _field_insurance_budget(field, db, settings)
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
        insurance_premium=ins_prem,
        budget_cop_ac=budget_cop_ac,
    )

    open_plans = [p for p in plans if (p.status or "").lower() != "done"]
    done_plans = [p for p in plans if (p.status or "").lower() == "done"]
    acres = ledger["acres"] or 0
    planned_remaining = 0.0
    for p in open_plans:
        if p.estimated_cost_per_acre is not None and acres:
            planned_remaining += float(p.estimated_cost_per_acre) * acres
    planned_remaining = round(planned_remaining, 2)

    season_checklist = _season_checklist(ops, hybrids, sprays)
    done_milestones = sum(1 for m in season_checklist if m["done"])

    # Recent costed activity (exclude undated rent for the ops list — shown in breakdown)
    recent_events = [
        e
        for e in reversed(ledger["events"])
        if e.get("type") in ("op", "input", "spray", "hybrid")
    ][:12]

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
            "open_plans": open_plans,
            "done_plans": done_plans,
            "planned_remaining": planned_remaining,
            "hybrids": hybrids,
            "sprays": sprays,
            "soils": soils,
            "prior": prior,
            "ledger": ledger,
            "settings": settings,
            "season_checklist": season_checklist,
            "done_milestones": done_milestones,
            "recent_events": recent_events,
            "products_by_id": products_by_id,
            "today": date.today().isoformat(),
            "insurance_premium": ins_prem,
            "can_edit_fields": perms.can_edit_fields(user.get("role")),
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

    ins_prem, budget_cop_ac = _field_insurance_budget(field, db, settings)
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
        insurance_premium=ins_prem,
        budget_cop_ac=budget_cop_ac,
    )

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
        },
    )


@router.post("/fields/{field_id}/yield")
def field_yield(
    request: Request,
    field_id: int,
    expected_yield: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_edit_fields(request)
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
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)
    when = _d(op_date) or date.today()
    pairs = [
        ("op_date", when.isoformat()),
        ("op_type", op_type),
        ("description", description),
        ("cost", cost),
    ]
    if billable:
        pairs.append(("billable", billable))
    block = confirm_if_duplicates(
        request,
        hits=find_operation_dups(db, field_id, when, op_type, description),
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
            op_type=op_type.strip() or "other",
            description=description.strip() or None,
            cost=_f(cost) or 0,
            billable=1 if billable else 0,
        )
    )
    log_activity(db, user.get("username"), "field_operation", f"{field.name}: {op_type}")
    db.commit()
    return RedirectResponse(f"/fields/{field_id}", status_code=303)



OP_WIZARD_TYPES = [
    ("planting", "Planting", "Seed / hybrid pass"),
    ("spraying", "Spraying", "Chem / tank mix"),
    ("dry_fertilizer", "Dry Fertilizer", "Dry or bulk fert"),
    ("sidedress", "Sidedress", "In-season N"),
    ("harvest", "Harvest", "Yield & destination"),
    ("lime", "Lime", "Ag lime / pH"),
    ("custom", "Add New", "Custom operation type"),
]

_OP_KIND_LABELS = {
    "planting": "Planting",
    "spraying": "Spraying",
    "dry_fertilizer": "Dry Fertilizer",
    "sidedress": "Sidedress",
    "harvest": "Harvest",
    "lime": "Lime",
}


@router.get("/fields/{field_id}/add-operation", response_class=HTMLResponse)
def field_add_operation_wizard(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)
    year = _year(db)
    year_id = year.id if year else field.crop_year_id
    hybrids = list(
        db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year_id).order_by(Hybrid.name))
    )
    if field.crop and field.crop not in ("None", ""):
        cropped = [h for h in hybrids if (h.crop or "").lower() == field.crop.lower()]
        if cropped:
            hybrids = cropped
    sprays = list(
        db.scalars(
            select(SprayMix).where(SprayMix.crop_year_id == year_id).order_by(SprayMix.name)
        )
    )
    fert_products = list(
        db.scalars(
            select(InputProduct)
            .where(InputProduct.category.in_(("fertilizer", "other", "chemical")))
            .order_by(InputProduct.name)
        )
    )
    default_acres = field.acres_mine or field.acres_total or 0
    preselect_kind = request.query_params.get("kind", "")
    return templates.TemplateResponse(
        "field_add_operation.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "op_types": OP_WIZARD_TYPES,
            "hybrids": hybrids,
            "sprays": sprays,
            "fert_products": fert_products,
            "today": date.today().isoformat(),
            "default_acres": default_acres,
            "preselect_kind": preselect_kind,
        },
    )


@router.post("/fields/{field_id}/add-operation")
async def field_add_operation(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/fields", status_code=303)

    form = await request.form()

    def g(key: str, default: str = "") -> str:
        v = form.get(key)
        if v is None:
            return default
        return str(v)

    op_kind = g("op_kind").strip()
    op_date = g("op_date")
    description = g("description").strip()
    cost_raw = g("cost").strip()
    billable = g("billable")
    operator = g("operator").strip()
    acres = _f(g("acres"), field.acres_mine or field.acres_total or 0) or 0
    wizard_url = f"/fields/{field_id}/add-operation"
    foc_url = f"/fields/{field_id}"
    confirm_duplicate = g("confirm_duplicate")

    if op_kind not in _OP_KIND_LABELS and op_kind != "custom":
        return redirect_flash(request, wizard_url, "Choose an operation type.", "warn")
    # --- hybrid units gate: warn if cost_per_unit set but no way to calculate $ ---

    when = _d(op_date)
    if not when:
        return redirect_flash(request, wizard_url, "Date is required.", "warn")

    custom_type = g("custom_type").strip()
    if op_kind == "custom":
        if not custom_type:
            return redirect_flash(request, wizard_url, "Custom type name is required.", "warn")
        op_type_label = custom_type
    else:
        op_type_label = _OP_KIND_LABELS[op_kind]

    desc_parts: list[str] = []
    if description:
        desc_parts.append(description)
    if operator:
        desc_parts.append(f"Operator: {operator}")
    if acres and acres != (field.acres_mine or field.acres_total or 0):
        desc_parts.append(f"Treated {acres:g} ac")

    # Application / custom-hire only. Product $ lives on hybrid / mix / inventory assign
    # so the field ledger does not double-count (Granular-style split).
    hire_cost = _f(cost_raw, None) if cost_raw else 0.0
    if hire_cost is None:
        hire_cost = 0.0

    plant_lines: list[tuple[Hybrid, str, float | None]] = []
    mix: SprayMix | None = None
    timing = ""
    weather_temp = ""
    weather_wind = ""
    product: InputProduct | None = None
    qty = 0.0
    pname = ""

    if op_kind == "planting":
        plant_ids = [str(v) for v in form.getlist("plant_hybrid_id")]
        plant_rates = [str(v) for v in form.getlist("plant_rate")]
        plant_units = [str(v) for v in form.getlist("plant_units")]
        # Backward-compat single fields from older form / confirm pages
        if not plant_ids and g("hybrid_id").strip():
            plant_ids = [g("hybrid_id").strip()]
            plant_rates = [g("hybrid_rate").strip()]
            plant_units = [g("hybrid_units").strip()]
        for i, raw_id in enumerate(plant_ids):
            raw_id = (raw_id or "").strip()
            if not raw_id:
                continue
            try:
                hid = int(raw_id)
            except ValueError:
                continue
            hybrid = db.get(Hybrid, hid) if hid else None
            if not hybrid:
                continue
            rate = (plant_rates[i] if i < len(plant_rates) else "").strip()
            units_raw = (plant_units[i] if i < len(plant_units) else "").strip()
            units = _f(units_raw, None) if units_raw else None
            plant_lines.append((hybrid, rate, units))
            label = f"{hybrid.brand} {hybrid.name}".strip() if hybrid.brand else hybrid.name
            bit = label
            if rate:
                bit += f" @ {rate}"
            if units is not None:
                ul = hybrid.unit_label or "units"
                bit += f" · {units:g} {ul}"
            desc_parts.append(bit)
        if not plant_lines:
            # Allow planting with notes only
            pass

    elif op_kind == "spraying":
        mix_raw = g("spray_mix_id").strip()
        timing = g("timing_label").strip()
        weather_temp = g("weather_temp").strip()
        weather_wind = g("weather_wind").strip()
        if mix_raw:
            try:
                mid = int(mix_raw)
            except ValueError:
                mid = 0
            mix = db.get(SprayMix, mid) if mid else None
            if mix:
                bit = f"Mix: {mix.name}"
                if timing:
                    bit += f" ({timing})"
                desc_parts.append(bit)
        weather_bits = []
        if weather_temp:
            weather_bits.append(f"{weather_temp}°F")
        if weather_wind:
            weather_bits.append(f"wind {weather_wind}")
        if weather_bits:
            desc_parts.append("Weather: " + ", ".join(weather_bits))

    elif op_kind in ("dry_fertilizer", "sidedress", "lime"):
        suffix = {"dry_fertilizer": "", "sidedress": "_sd", "lime": "_lime"}[op_kind]
        prod_raw = g(f"product_id{suffix}").strip()
        qty_raw = g(f"quantity{suffix}").strip()
        pname = g(f"product_name{suffix}").strip()
        qty = _f(qty_raw, 0) or 0
        if prod_raw and qty > 0:
            try:
                pid = int(prod_raw)
            except ValueError:
                pid = 0
            product = db.get(InputProduct, pid) if pid else None
            if product:
                unit = product.unit or ""
                desc_parts.append(f"{product.name}: {qty:g} {unit}".strip())
        if pname:
            desc_parts.append(pname)

    elif op_kind == "harvest":
        yld = g("yield_bu").strip()
        moisture = g("moisture").strip()
        dest = g("destination").strip()
        if yld:
            desc_parts.append(f"Yield {yld} bu/ac")
        if moisture:
            desc_parts.append(f"Moisture {moisture}%")
        if dest:
            desc_parts.append(f"Dest: {dest}")

    full_desc = " · ".join(desc_parts) if desc_parts else None

    hits: list[str] = []
    hits.extend(find_operation_dups(db, field_id, when, op_type_label, full_desc))
    for hybrid, _rate, _units in plant_lines:
        hits.extend(find_hybrid_assign_dups(db, [field_id], hybrid.id, when))
    if mix:
        hits.extend(find_spray_assign_dups(db, [field_id], [(mix.id, when)]))
    if product and qty > 0:
        hits.extend(find_assignment_dups(db, field_id, product.id, when))

    form_pairs = [(k, str(v)) for k, v in form.multi_items() if k != "confirm_duplicate"]
    block = confirm_if_duplicates(
        request,
        hits=hits,
        confirm_duplicate=confirm_duplicate,
        action=wizard_url,
        cancel_url=wizard_url,
        heading="Possible duplicate field operation",
        form_pairs=form_pairs,
        user=user,
        farm_name=_farm(db),
        active="fields",
        lede="This looks similar to something already on the field. Review before saving again.",
    )
    if block:
        return block

    if op_kind == "harvest":
        yld = g("yield_bu").strip()
        update_ey = g("update_expected_yield").strip()
        yf = _f(yld, None) if yld else None
        if update_ey == "1" and yf is not None:
            field.expected_yield = yf

    # Hybrid units gate: if hybrid has cost_per_unit but no units_applied and no cost_per_acre,
    # the ledger will show $0 — warn and redirect back unless user confirmed.
    if op_kind == "planting" and not confirm_duplicate:
        missing_gate = []
        for h, _rate, u in plant_lines:
            if h.cost_per_unit and u is None and not h.cost_per_acre:
                lbl = f"{h.brand} {h.name}".strip() if h.brand else h.name
                missing_gate.append(lbl)
        if missing_gate:
            return redirect_flash(
                request,
                wizard_url,
                f"Hybrid(s) {', '.join(missing_gate)} have $/unit cost but no units entered "
                "and no $/ac fallback — enter units (bags/units applied) so seed cost is captured. "
                "Or set $/ac on the hybrid in the library.",
                "warn",
            )

    for hybrid, rate, units in plant_lines:
        db.add(
            FieldHybrid(
                field_id=field_id,
                hybrid_id=hybrid.id,
                rate=rate or None,
                units_applied=units,
                treated_acres=acres if acres else None,
                applied_date=when,
            )
        )
    if mix:
        db.add(
            FieldSprayMix(
                field_id=field_id,
                spray_mix_id=mix.id,
                timing_label=timing or None,
                applied_date=when,
                treated_acres=acres if acres else None,
                weather_temp=weather_temp or None,
                weather_wind=weather_wind or None,
            )
        )
        # Deplete inventory for each product line in the spray mix
        if acres and acres > 0:
            mix_lines = list(
                db.scalars(
                    select(SprayMixLine).where(SprayMixLine.spray_mix_id == mix.id)
                )
            )
            for ml in mix_lines:
                rate_val = float(ml.rate or 0)
                if rate_val <= 0:
                    continue
                # Resolve product: prefer product_id, then name match
                inv_prod: InputProduct | None = None
                if ml.product_id:
                    inv_prod = db.get(InputProduct, ml.product_id)
                if inv_prod is None and ml.product_name:
                    inv_prod = db.scalar(
                        select(InputProduct).where(InputProduct.name == ml.product_name)
                    )
                if inv_prod is None:
                    continue
                # Rate is per-acre — convert into inventory unit when possible.
                # Cost stays on mix.cost_per_acre (avoid double-counting in ledger).
                depleted_qty = deplete_qty_from_rate(
                    rate_val,
                    acres,
                    rate_unit=getattr(ml, "rate_unit", None),
                    inventory_unit=getattr(inv_prod, "unit", None),
                )
                if depleted_qty <= 0:
                    continue
                inv_prod.on_hand = max(0.0, (inv_prod.on_hand or 0) - depleted_qty)
                db.add(
                    FieldAssignment(
                        product_id=inv_prod.id,
                        field_id=field_id,
                        assign_date=when,
                        quantity=depleted_qty,
                        unit_cost=0.0,
                        notes=f"Auto-depleted from spray mix: {mix.name} (inventory only; $ on mix CPA)",
                    )
                )

    if product and qty > 0:
        product.on_hand = max(0, (product.on_hand or 0) - qty)
        db.add(
            FieldAssignment(
                product_id=product.id,
                field_id=field_id,
                assign_date=when,
                quantity=qty,
                unit_cost=product.avg_unit_cost or 0,
            )
        )

    db.add(
        FieldOperation(
            field_id=field_id,
            op_date=when,
            op_type=op_type_label,
            description=full_desc,
            cost=float(hire_cost or 0),
            billable=1 if billable else 0,
        )
    )
    log_activity(
        db, user.get("username"), "field_add_operation", f"{field.name}: {op_type_label}"
    )
    # Auto-complete open work orders / plans that match this op kind
    completed_wo = 0
    open_plans = list(
        db.scalars(
            select(FieldPlan).where(
                FieldPlan.field_id == field_id,
                FieldPlan.status != "done",
            )
        )
    )
    kind_aliases = {
        "planting": {"planting", "plant"},
        "spraying": {"spraying", "spray"},
        "dry_fertilizer": {"dry_fertilizer", "fertilizer", "fert"},
        "sidedress": {"sidedress"},
        "lime": {"lime"},
        "harvest": {"harvest"},
    }
    match_keys = kind_aliases.get(op_kind, {op_kind}) if op_kind != "custom" else set()
    for p in open_plans:
        keys = {(p.op_kind or "").lower(), (p.plan_type or "").lower()} - {""}
        if keys & match_keys:
            p.status = "done"
            p.completed_date = when
            completed_wo += 1
    db.commit()
    msg = f"Saved {op_type_label} on {field.name} ({when.isoformat()})."
    if completed_wo:
        msg += f" Closed {completed_wo} matching work order{'s' if completed_wo != 1 else ''}."
    return redirect_flash(
        request,
        foc_url,
        msg,
    )


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
def field_plan_done(
    request: Request,
    field_id: int,
    plan_id: int = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    plan = db.get(FieldPlan, plan_id)
    if plan and plan.field_id == field_id:
        plan.status = "done"
        plan.completed_date = date.today()
        db.commit()
    dest = (next or "").strip()
    if dest.startswith("/") and not dest.startswith("//"):
        return RedirectResponse(dest, status_code=303)
    return RedirectResponse(f"/fields/{field_id}", status_code=303)


# ---------- Void endpoints ----------
@router.post("/fields/{field_id}/void/op/{op_id}")
def void_op(request: Request, field_id: int, op_id: int, db: Session = Depends(get_db)):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    op = db.get(FieldOperation, op_id)
    if op and op.field_id == field_id and not op.voided:
        op.voided = 1
        log_activity(db, user.get("username"), "void_op", f"op#{op_id} on field#{field_id}")
        db.commit()
    return redirect_flash(request, f"/fields/{field_id}", "Operation voided.", "warn")


@router.post("/fields/{field_id}/void/assign/{assign_id}")
def void_assign(request: Request, field_id: int, assign_id: int, db: Session = Depends(get_db)):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    a = db.get(FieldAssignment, assign_id)
    if a and a.field_id == field_id and not a.voided:
        a.voided = 1
        # Restore on_hand
        prod = db.get(InputProduct, a.product_id)
        if prod:
            prod.on_hand = (prod.on_hand or 0) + float(a.quantity or 0)
        log_activity(db, user.get("username"), "void_assign", f"assign#{assign_id} on field#{field_id}")
        db.commit()
    return redirect_flash(request, f"/fields/{field_id}", "Input assignment voided — inventory restored.", "warn")


@router.post("/fields/{field_id}/void/spray/{link_id}")
def void_spray(request: Request, field_id: int, link_id: int, db: Session = Depends(get_db)):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    link = db.get(FieldSprayMix, link_id)
    if link and link.field_id == field_id and not link.voided:
        link.voided = 1
        mix = db.get(SprayMix, link.spray_mix_id)
        mix_name = mix.name if mix else ""
        # Cascade: void inventory-only auto-assigns from this spray and restore on_hand
        auto_assigns = list(
            db.scalars(
                select(FieldAssignment).where(
                    FieldAssignment.field_id == field_id,
                    FieldAssignment.voided == 0,
                    FieldAssignment.notes.isnot(None),
                )
            )
        )
        for a in auto_assigns:
            note = a.notes or ""
            if "Auto-depleted from spray mix" not in note:
                continue
            if mix_name and mix_name not in note:
                continue
            if link.applied_date and a.assign_date and a.assign_date != link.applied_date:
                continue
            a.voided = 1
            prod = db.get(InputProduct, a.product_id)
            if prod:
                prod.on_hand = (prod.on_hand or 0) + float(a.quantity or 0)
        log_activity(db, user.get("username"), "void_spray", f"spray_link#{link_id} on field#{field_id}")
        db.commit()
    return redirect_flash(request, f"/fields/{field_id}", "Spray application voided (inventory restored).", "warn")


@router.post("/fields/{field_id}/void/hybrid/{link_id}")
def void_hybrid(request: Request, field_id: int, link_id: int, db: Session = Depends(get_db)):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    link = db.get(FieldHybrid, link_id)
    if link and link.field_id == field_id and not link.voided:
        link.voided = 1
        log_activity(db, user.get("username"), "void_hybrid", f"hybrid_link#{link_id} on field#{field_id}")
        db.commit()
    return redirect_flash(request, f"/fields/{field_id}", "Hybrid planting record voided.", "warn")


# ---------- Work orders ----------
@router.get("/work-orders", response_class=HTMLResponse)
def work_orders_list(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    plans: list[FieldPlan] = []
    fields_by_id: dict[int, Field] = {}
    if year:
        plans = list(
            db.scalars(
                select(FieldPlan)
                .where(
                    FieldPlan.crop_year_id == year.id,
                    FieldPlan.status != "done",
                )
                .order_by(FieldPlan.priority.desc(), FieldPlan.target_date.asc(), FieldPlan.id.asc())
            )
        )
        fids = {p.field_id for p in plans}
        if fids:
            for f in db.scalars(select(Field).where(Field.id.in_(fids))):
                fields_by_id[f.id] = f
    year_fields: list[Field] = []
    if year:
        year_fields = list(
            db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))
        )
    return templates.TemplateResponse(
        "work_orders.html",
        {
            "request": request,
            "user": user,
            "active": "work_orders",
            "farm_name": _farm(db),
            "year": year,
            "plans": plans,
            "fields_by_id": fields_by_id,
            "year_fields": year_fields,
            "today": date.today().isoformat(),
            "can_edit_fields": perms.can_edit_fields(user.get("role")),
        },
    )


@router.post("/work-orders/create")
def work_orders_create(
    request: Request,
    field_id: str = Form(...),
    title: str = Form(...),
    plan_type: str = Form("work_order"),
    op_kind: str = Form(""),
    assigned_to: str = Form(""),
    priority: str = Form("normal"),
    target_date: str = Form(""),
    details: str = Form(""),
    estimated_cost_per_acre: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_edit_fields(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year or not field_id.isdigit():
        return redirect_flash(request, "/work-orders", "Invalid field or year.", "warn")
    fid = int(field_id)
    field = db.get(Field, fid)
    if not field:
        return redirect_flash(request, "/work-orders", "Field not found.", "warn")
    if not title.strip():
        return redirect_flash(request, "/work-orders", "Title is required.", "warn")
    kind = (op_kind.strip() or "").lower()
    ptype = (plan_type.strip() or "work_order").lower()
    plan_to_kind = {
        "spray": "spraying",
        "spraying": "spraying",
        "planting": "planting",
        "plant": "planting",
        "fertilizer": "dry_fertilizer",
        "dry_fertilizer": "dry_fertilizer",
        "sidedress": "sidedress",
        "lime": "lime",
        "harvest": "harvest",
    }
    if not kind:
        kind = plan_to_kind.get(ptype)
    else:
        kind = plan_to_kind.get(kind, kind)
    plan = FieldPlan(
        field_id=fid,
        crop_year_id=year.id,
        plan_type=(plan_type.strip() or "work_order"),
        op_kind=kind,
        title=title.strip(),
        assigned_to=assigned_to.strip() or None,
        priority=(priority.strip() or "normal"),
        target_date=_d(target_date),
        details=details.strip() or None,
        estimated_cost_per_acre=_f(estimated_cost_per_acre, None) if estimated_cost_per_acre.strip() else None,
        status="planned",
    )
    db.add(plan)
    log_activity(db, user.get("username"), "work_order_create", f"{field.name}: {plan.title}")
    db.commit()
    return redirect_flash(request, "/work-orders", f"Work order created for {field.name}.")


# ---------- Landlord statement ----------
@router.get("/fields/{field_id}/landlord-statement", response_class=HTMLResponse)
def field_landlord_statement(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = _need(request, "fields")
    if isinstance(user, RedirectResponse):
        return user
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(
            joinedload(Field.party),
            joinedload(Field.shares).joinedload(FieldShare.party),
            joinedload(Field.crop_year),
        )
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)

    ops = list(db.scalars(select(FieldOperation).where(FieldOperation.field_id == field_id)))
    assigns = list(db.scalars(select(FieldAssignment).where(FieldAssignment.field_id == field_id)))
    hybrid_links = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field_id)))
    hybrids = [(h, db.get(Hybrid, h.hybrid_id)) for h in hybrid_links]
    spray_links = list(db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id == field_id)))
    sprays = [(s, db.get(SprayMix, s.spray_mix_id)) for s in spray_links]
    plans = list(db.scalars(select(FieldPlan).where(FieldPlan.field_id == field_id)))

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

    ins_prem, budget_cop_ac = _field_insurance_budget(field, db, settings)
    ledger = build_field_ledger(
        field,
        ops=ops,
        assigns=assigns,
        products_by_id=products_by_id,
        spray_links=sprays,
        hybrid_links=hybrids,
        plans=plans,
        soils=[],
        settings=settings,
        board=board,
        contracts=contracts,
        insurance_premium=ins_prem,
        budget_cop_ac=budget_cop_ac,
    )

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

    # Crop-share / landlord entitlement snapshot
    my_pct = 100.0
    landlord_rows = []
    for s in shares:
        pct = float(s.share_pct or 0)
        if getattr(s, "is_me", 0):
            my_pct = pct
            continue
        landlord_rows.append(s)
    landlord_pct = max(0.0, sum(float(s.share_pct or 0) for s in landlord_rows))
    total_ac = float(field.acres_total or field.acres_mine or 0)
    ey = float(field.expected_yield or 0) if field.expected_yield is not None else None
    total_bu = round(total_ac * ey, 1) if ey is not None and total_ac else None
    landlord_bu = round(total_bu * landlord_pct / 100.0, 1) if total_bu is not None and landlord_pct else None
    my_bu = round(total_bu * my_pct / 100.0, 1) if total_bu is not None else None
    mark = (ledger.get("marks") or {}).get("futures") or (ledger.get("marks") or {}).get("contracted")
    landlord_grain_value = round(landlord_bu * float(mark), 2) if landlord_bu is not None and mark else None
    # Cash rent owed to landlords (full field rent when cash lease; 0 on pure crop share)
    rent_pa = float(field.rent_per_acre or 0)
    cash_rent_total = round(total_ac * rent_pa, 2) if rent_pa and total_ac else 0.0
    # Typical crop-share: landlord does not pay operator inputs — show operator COP as note only
    crop_share = {
        "my_pct": my_pct,
        "landlord_pct": landlord_pct,
        "total_ac": total_ac,
        "total_bu": total_bu,
        "my_bu": my_bu,
        "landlord_bu": landlord_bu,
        "mark": mark,
        "landlord_grain_value": landlord_grain_value,
        "cash_rent_total": cash_rent_total,
        "is_crop_share": (field.lease_type or "").lower() in ("crop_share", "crop share")
        or (field.ownership_mode or "").lower() == "on_shares",
        "landlord_rows": landlord_rows,
    }

    return templates.TemplateResponse(
        "field_landlord_statement.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": _farm(db),
            "field": field,
            "shares": shares,
            "ledger": ledger,
            "crop_share": crop_share,
            "today": date.today().isoformat(),
        },
    )


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
        # For landlord Cash Rent/Property Taxes: rent owed is acres * rent (use total acres typically)
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
    db.add(
        TruckingRate(
            destination=destination.strip(),
            hauler=hauler.strip() or None,
            rate_per_bu=_f(rate_per_bu, None) if rate_per_bu.strip() else None,
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
    db.add(
        TruckLoad(
            load_date=_d(load_date) or date.today(),
            crop=crop,
            destination=destination.strip(),
            hauler=hauler.strip() or None,
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
        out = float(value)
    except ValueError:
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / ±
        return default
    return out


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
