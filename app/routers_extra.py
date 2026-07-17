"""Additional farm modules: trials, grain, marketing, plans, purchases, panorama, insights."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
import json
import re

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.flash import redirect_flash
from app.duplicates import (
    confirm_if_duplicates,
    find_assignment_dups,
    find_hybrid_assign_dups,
    find_plan_dups,
    find_spray_assign_dups,
)
from app.insights import build_ai_briefing, treatment_summary
from app.grain_utils import shrink_net_bu
from app.activity import log_activity
from app.importers import is_summary_field_name
from app import cme_quotes
from app import marketing_analytics as mkt
from app.models import (
    AppSettings,
    CropTrial,
    CropYear,
    Field,
    FieldAssignment,
    FieldHybrid,
    FieldOperation,
    FieldPlan,
    FieldSprayMix,
    GrainBin,
    BinShare,
    GrainContract,
    GrainMovement,
    Hybrid,
    ImportBatch,
    InputProduct,
    InputPurchase,
    Invoice,
    InvoiceLine,
    PanoramaConnection,
    PanoramaSyncLog,
    Party,
    ProductionEstimate,
    ProductReturn,
    SprayMix,
    SprayMixLine,
    TrialNote,
    TrialResult,
    TrialTreatment,
    CropInsurance,
    ContractEvent,
    MarketingTarget,
    BinConditionNote,
)
from app import panorama as panorama_api
from app import permissions as perms
from app.templating import templates

router = APIRouter()

CONTRACT_TYPES = [
    "cash",
    "forward",
    "hta",
    "basis",
    "dp",
    "minimum_price",
    "accumulator",
    "custom",
]


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


# ---------- Trials ----------
@router.get("/trials", response_class=HTMLResponse)
def trials_list(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    trials = []
    if year:
        trials = list(
            db.scalars(
                select(CropTrial)
                .where(CropTrial.crop_year_id == year.id)
                .options(joinedload(CropTrial.field), joinedload(CropTrial.treatments))
                .order_by(CropTrial.id.desc())
            ).unique()
        )
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "trials.html",
        {
            "request": request,
            "user": user,
            "active": "trials",
            "farm_name": _farm(db),
            "year": year,
            "trials": trials,
            "fields": fields,
            "today": date.today().isoformat(),
        },
    )


@router.post("/trials/create")
def trials_create(
    request: Request,
    field_id: int = Form(...),
    name: str = Form(...),
    crop: str = Form("Corn"),
    question: str = Form(""),
    factor_tested: str = Form(""),
    design_notes: str = Form(""),
    grain_price: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    field = db.get(Field, field_id)
    if not year or not field:
        return RedirectResponse("/trials", status_code=303)
    trial = CropTrial(
        field_id=field.id,
        crop_year_id=year.id,
        name=name.strip(),
        crop=crop,
        question=question.strip() or None,
        factor_tested=factor_tested.strip() or None,
        design_notes=design_notes.strip() or None,
        status="active",
        start_date=date.today(),
        grain_price=_f(grain_price, None) if grain_price.strip() else None,
    )
    db.add(trial)
    db.commit()
    return RedirectResponse(f"/trials/{trial.id}", status_code=303)


@router.get("/trials/{trial_id}", response_class=HTMLResponse)
def trial_detail(request: Request, trial_id: int, db: Session = Depends(get_db)):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    trial = db.scalar(
        select(CropTrial)
        .where(CropTrial.id == trial_id)
        .options(
            joinedload(CropTrial.field),
            joinedload(CropTrial.treatments).joinedload(TrialTreatment.results),
            joinedload(CropTrial.notes),
        )
    )
    if not trial:
        return RedirectResponse("/trials", status_code=303)
    return templates.TemplateResponse(
        "trial_detail.html",
        {
            "request": request,
            "user": user,
            "active": "trials",
            "farm_name": _farm(db),
            "trial": trial,
            "summary": treatment_summary(trial),
            "today": date.today().isoformat(),
        },
    )


@router.post("/trials/{trial_id}/treatment")
def trial_add_treatment(
    request: Request,
    trial_id: int,
    name: str = Form(...),
    is_control: str = Form("0"),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    db.add(
        TrialTreatment(
            trial_id=trial_id,
            name=name.strip(),
            is_control=1 if is_control == "1" else 0,
            description=description.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


@router.post("/trials/{trial_id}/note")
def trial_add_note(
    request: Request,
    trial_id: int,
    note_date: str = Form(...),
    title: str = Form(""),
    body: str = Form(...),
    treatment_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    db.add(
        TrialNote(
            trial_id=trial_id,
            treatment_id=int(treatment_id) if treatment_id.isdigit() else None,
            note_date=_d(note_date) or date.today(),
            title=title.strip() or None,
            body=body.strip(),
        )
    )
    db.commit()
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


@router.post("/trials/{trial_id}/result")
def trial_add_result(
    request: Request,
    trial_id: int,
    treatment_id: int = Form(...),
    rep_label: str = Form(""),
    harvest_date: str = Form(""),
    yield_bu_ac: str = Form(""),
    moisture: str = Form(""),
    test_weight: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    db.add(
        TrialResult(
            treatment_id=treatment_id,
            rep_label=rep_label.strip() or None,
            harvest_date=_d(harvest_date),
            yield_bu_ac=_f(yield_bu_ac, None) if yield_bu_ac.strip() else None,
            moisture=_f(moisture, None) if moisture.strip() else None,
            test_weight=_f(test_weight, None) if test_weight.strip() else None,
            notes=notes.strip() or None,
        )
    )
    trial = db.get(CropTrial, trial_id)
    if trial:
        trial.status = "harvested"
    db.commit()
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


@router.post("/trials/{trial_id}/conclusion")
def trial_conclusion(
    request: Request,
    trial_id: int,
    conclusion: str = Form(""),
    status: str = Form("analyzed"),
    grain_price: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "trials")
    if isinstance(user, RedirectResponse):
        return user
    trial = db.get(CropTrial, trial_id)
    if trial:
        trial.conclusion = conclusion.strip() or None
        trial.status = status
        if grain_price.strip():
            trial.grain_price = _f(grain_price, None)
        db.commit()
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


# ---------- Insights ----------
@router.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "insights")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    fields = []
    trials = []
    if year:
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)))
        trials = list(
            db.scalars(
                select(CropTrial)
                .where(CropTrial.crop_year_id == year.id)
                .options(
                    joinedload(CropTrial.field),
                    joinedload(CropTrial.treatments).joinedload(TrialTreatment.results),
                    joinedload(CropTrial.notes),
                )
            ).unique()
        )
    trial_cards = [{"trial": t, "summary": treatment_summary(t)} for t in trials]
    return templates.TemplateResponse(
        "insights.html",
        {
            "request": request,
            "user": user,
            "active": "insights",
            "farm_name": _farm(db),
            "year": year,
            "fields": fields,
            "trial_cards": trial_cards,
        },
    )


@router.get("/insights/ai-briefing", response_class=PlainTextResponse)
def ai_briefing(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "insights")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id))) if year else []
    trials = []
    if year:
        trials = list(
            db.scalars(
                select(CropTrial)
                .where(CropTrial.crop_year_id == year.id)
                .options(
                    joinedload(CropTrial.field),
                    joinedload(CropTrial.treatments).joinedload(TrialTreatment.results),
                    joinedload(CropTrial.notes),
                )
            ).unique()
        )
    text = build_ai_briefing(
        _farm(db),
        str(year.year) if year else "n/a",
        fields,
        trials,
    )
    return PlainTextResponse(text)


# ---------- Panorama ----------
@router.get("/panorama", response_class=HTMLResponse)
def panorama_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    conn = db.scalar(select(PanoramaConnection).limit(1))
    logs = list(db.scalars(select(PanoramaSyncLog).order_by(PanoramaSyncLog.id.desc()).limit(20)))
    status = panorama_api.config_status()
    return templates.TemplateResponse(
        "panorama.html",
        {
            "request": request,
            "user": user,
            "active": "panorama",
            "farm_name": _farm(db),
            "conn": conn,
            "logs": logs,
            "live_ready": status["live_ready"],
            "config": status,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/panorama/connect")
def panorama_connect(
    request: Request,
    organization_code: str = Form(...),
    grower_name: str = Form("Beam Farm"),
    grower_email: str = Form("farm@example.com"),
    db: Session = Depends(get_db),
):
    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    conn = db.scalar(select(PanoramaConnection).limit(1))
    if not conn:
        conn = PanoramaConnection()
        db.add(conn)
    try:
        if not conn.leaf_user_id:
            created = panorama_api.create_leaf_user(grower_name.strip(), grower_email.strip())
            conn.leaf_user_id = str(created.get("id") or created.get("uuid") or created)
        result = panorama_api.start_panorama_one_click(
            conn.leaf_user_id, organization_code.strip()
        )
        conn.organization_code = organization_code.strip()
        conn.sign_in_url = result.get("signInUrl") or result.get("sign_in_url")
        conn.status = "pending_auth"
        conn.last_error = None
        db.add(
            PanoramaSyncLog(
                action="one_click_started",
                detail=f"org={organization_code.strip()} leafUser={conn.leaf_user_id}",
                ok=1,
            )
        )
        db.commit()
        msg = "Connection started. Open the Panorama authorize link within 15 minutes."
        return RedirectResponse(f"/panorama?msg={msg}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        conn.status = "error"
        conn.last_error = str(exc)
        db.add(PanoramaSyncLog(action="connect_error", detail=str(exc), ok=0))
        db.commit()
        return RedirectResponse(f"/panorama?err={str(exc)[:180]}", status_code=303)


@router.post("/panorama/refresh")
def panorama_refresh(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    conn = db.scalar(select(PanoramaConnection).limit(1))
    if not conn or not conn.leaf_user_id:
        return RedirectResponse("/panorama?err=Connect+first", status_code=303)
    try:
        creds = panorama_api.get_panorama_credentials(conn.leaf_user_id)
        status = str(creds.get("status") or "").upper()
        if status == "ACTIVE":
            conn.status = "active"
            ops = panorama_api.list_operations(conn.leaf_user_id)
            files = panorama_api.list_files(conn.leaf_user_id)
            conn.last_sync_at = datetime.utcnow()
            conn.last_error = None
            detail = f"status=ACTIVE operations={len(ops)} files={len(files)}"
            db.add(PanoramaSyncLog(action="refresh", detail=detail, ok=1))
            # Create field ops stubs from operation summaries when possible
            for op in ops[:50]:
                name = str(op.get("type") or op.get("operationType") or "Panorama op")
                started = op.get("startTime") or op.get("startDate") or ""
                db.add(
                    FieldOperation(
                        field_id=_year_field_fallback(db),
                        op_date=date.today(),
                        op_type=f"panorama:{name}"[:80],
                        description=str(op)[:500],
                        cost=0,
                        billable=0,
                    )
                )
            db.commit()
            return RedirectResponse("/panorama?msg=Sync+refreshed", status_code=303)
        conn.status = "pending_auth" if status else conn.status
        db.add(PanoramaSyncLog(action="refresh", detail=f"status={status or creds}", ok=1))
        db.commit()
        return RedirectResponse(f"/panorama?msg=Credential+status:+{status or 'unknown'}", status_code=303)
    except Exception as exc:  # noqa: BLE001
        conn.status = "error"
        conn.last_error = str(exc)
        db.add(PanoramaSyncLog(action="refresh_error", detail=str(exc), ok=0))
        db.commit()
        return RedirectResponse(f"/panorama?err={str(exc)[:180]}", status_code=303)


def _year_field_fallback(db: Session) -> int:
    year = _year(db)
    if not year:
        raise RuntimeError("No crop year")
    field = db.scalar(select(Field).where(Field.crop_year_id == year.id).limit(1))
    if not field:
        field = Field(crop_year_id=year.id, name="Panorama Import", crop="Corn", acres_mine=0)
        db.add(field)
        db.flush()
    return field.id


@router.post("/panorama/upload")
async def panorama_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    content = await file.read()
    path = panorama_api.save_uploaded_file(file.filename or "panorama.bin", content)
    conn = db.scalar(select(PanoramaConnection).limit(1))
    if not conn:
        conn = PanoramaConnection(status="file_only")
        db.add(conn)
    else:
        if conn.status == "not_configured":
            conn.status = "file_only"
    db.add(
        PanoramaSyncLog(
            action="file_upload",
            detail=f"Saved {path.name} ({len(content)} bytes)",
            ok=1,
        )
    )
    db.add(
        ImportBatch(
            filename=path.name,
            import_type="panorama_file",
            status="stored",
            row_count=0,
            notes="Stored for processing; live Leaf sync preferred when credentials available.",
        )
    )
    db.commit()
    return RedirectResponse("/panorama?msg=File+uploaded", status_code=303)


# ---------- Grain bins ----------
def _bin_cards(db: Session):
    bins = list(db.scalars(select(GrainBin).order_by(GrainBin.crop, GrainBin.name)))
    shares = list(db.scalars(select(BinShare)))
    by_bin: dict[int, list] = {}
    for s in shares:
        by_bin.setdefault(s.bin_id, []).append(s)
    bin_cards = []
    for b in bins:
        share_list = by_bin.get(b.id, [])
        total = round(sum((s.bushels or 0) for s in share_list), 1)
        me_share = next((s for s in share_list if (s.owner_name or "").lower() == "me"), None)
        if me_share is None and share_list:
            me_share = share_list[0]
        me_bu = (me_share.bushels if me_share else 0) or 0
        cap = b.capacity_bu
        pct = None
        if cap and cap > 0:
            pct = round(100.0 * total / cap, 1)
        fill = min(100.0, max(0.0, pct if pct is not None else 0.0))
        over = bool(cap and total > cap)
        bin_cards.append(
            {
                "bin": b,
                "shares": share_list,
                "total_bu": total,
                "me_bu": me_bu,
                "capacity": cap,
                "pct": pct,
                "fill": fill if pct is not None else 0.0,
                "over": over,
                "has_cap": bool(cap and cap > 0),
            }
        )
    crop_order = ["Corn", "Soybeans"]
    bin_groups: list[tuple[str, list]] = []
    for crop in crop_order:
        group = [c for c in bin_cards if c["bin"].crop == crop]
        if group:
            bin_groups.append((crop, group))
    other = [c for c in bin_cards if c["bin"].crop not in crop_order]
    if other:
        bin_groups.append(("Other", other))
    return bins, bin_cards, bin_groups, by_bin


@router.get("/bins", response_class=HTMLResponse)
def bins_page(request: Request, view: Optional[str] = None, db: Session = Depends(get_db), preselect_field_id: Optional[str] = None):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bins, bin_cards, bin_groups, by_bin = _bin_cards(db)
    year = _year(db)
    settings = db.scalar(select(AppSettings).limit(1))
    contracts = []
    if year:
        contracts = list(
            db.scalars(
                select(GrainContract)
                .where(
                    GrainContract.status == "open",
                    GrainContract.crop_year_id == year.id,
                )
                .order_by(GrainContract.id.desc())
            )
        )
    fields = []
    if year:
        fields = list(
            db.scalars(
                select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)
            )
        )
    moves = list(db.scalars(select(GrainMovement).order_by(GrainMovement.id.desc()).limit(40)))
    notes = list(db.scalars(select(BinConditionNote).order_by(BinConditionNote.id.desc()).limit(40)))
    bin_name = {b.id: b.name for b in bins}
    field_name = {f.id: f.name for f in fields}

    view_mode = (view or "overview").strip().lower()
    if view_mode not in ("overview", "sheet", "ticket"):
        view_mode = "overview"

    corn_bu = sum(c["total_bu"] for c in bin_cards if c["bin"].crop == "Corn")
    soy_bu = sum(c["total_bu"] for c in bin_cards if c["bin"].crop == "Soybeans")
    corn_cap = sum((c["capacity"] or 0) for c in bin_cards if c["bin"].crop == "Corn" and c["has_cap"])
    soy_cap = sum((c["capacity"] or 0) for c in bin_cards if c["bin"].crop == "Soybeans" and c["has_cap"])
    stats = {
        "bin_count": len(bins),
        "corn_bu": round(corn_bu, 0),
        "soy_bu": round(soy_bu, 0),
        "total_bu": round(corn_bu + soy_bu, 0),
        "corn_cap": round(corn_cap, 0),
        "soy_cap": round(soy_cap, 0),
    }

    show_carry = bool(settings and (settings.bins_show_carry or 0))
    carry_rates = {"Corn": None, "Soybeans": None}
    carry_farm_mo = 0.0
    if show_carry:
        board = cme_quotes.board_from_settings(settings)
        bin_bu = {"Corn": corn_bu, "Soybeans": soy_bu}
        fallback = {
            "Corn": settings.corn_price_assumption if settings else None,
            "Soybeans": settings.soy_price_assumption if settings else None,
        }
        snap = mkt.build_carry_snapshot(settings, board, bin_bu, market_fallback=fallback)
        for crop in ("Corn", "Soybeans"):
            carry_rates[crop] = (snap["by_crop"].get(crop) or {}).get("rate")
        for card in bin_cards:
            crop = card["bin"].crop
            rate = carry_rates.get(crop)
            card["carry_mo"] = mkt.carry_farm_mo(card["total_bu"] or 0, rate)
            if card["carry_mo"] is not None:
                carry_farm_mo += card["carry_mo"]
        carry_farm_mo = round(carry_farm_mo, 2)
        stats["carry_corn_mo"] = (snap["by_crop"].get("Corn") or {}).get("farm_mo")
        stats["carry_soy_mo"] = (snap["by_crop"].get("Soybeans") or {}).get("farm_mo")
        stats["carry_total_mo"] = carry_farm_mo

    active = {
        "sheet": "bins_sheet",
        "ticket": "bins_ticket",
    }.get(view_mode, "bins")
    template = {
        "sheet": "bins_sheet.html",
        "ticket": "bins_ticket.html",
    }.get(view_mode, "bins.html")
    ctx = {
        "request": request,
        "user": user,
        "active": active,
        "farm_name": _farm(db),
        "year": year,
        "bins": bins,
        "bin_cards": bin_cards,
        "bin_groups": bin_groups,
        "by_bin": by_bin,
        "contracts": contracts,
        "fields": fields,
        "moves": moves,
        "notes": notes,
        "bin_name": bin_name,
        "field_name": field_name,
        "today": date.today().isoformat(),
        "view": view_mode,
        "stats": stats,
        "saved": request.query_params.get("saved"),
        "settings": settings,
        "bins_show_carry": show_carry,
        "carry_rates": carry_rates,
        "preselect_field_id": int(preselect_field_id) if (preselect_field_id or "").isdigit() else None,
    }
    return templates.TemplateResponse(template, ctx)


@router.post("/bins/carry-toggle")
def bins_carry_toggle(
    request: Request,
    bins_show_carry: str = Form("0"),
    next: str = Form("/bins"),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.bins_show_carry = 1 if str(bins_show_carry).strip() in ("1", "on", "true") else 0
        db.commit()
    dest = (next or "/bins").strip() or "/bins"
    if not dest.startswith("/"):
        dest = "/bins"
    return RedirectResponse(dest, status_code=303)


@router.get("/bins/sheet", response_class=HTMLResponse)
def bins_sheet(request: Request, db: Session = Depends(get_db)):
    return bins_page(request, view="sheet", db=db)


@router.get("/bins/ticket", response_class=HTMLResponse)
def bins_ticket_page(request: Request, db: Session = Depends(get_db)):
    return bins_page(request, view="ticket", db=db, preselect_field_id=request.query_params.get("field_id"))


@router.post("/bins/sheet/save")
def bins_sheet_save(
    request: Request,
    bin_id: list[int] = Form(default=[]),
    name: list[str] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    capacity_bu: list[str] = Form(default=[]),
    on_hand_bu: list[str] = Form(default=[]),
    notes: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user

    def as_list(vals):
        if vals is None:
            return []
        if isinstance(vals, (str, int, float)):
            return [vals]
        return list(vals)

    ids = [int(x) for x in as_list(bin_id)]
    names = as_list(name)
    crops = as_list(crop)
    caps = as_list(capacity_bu)
    ons = as_list(on_hand_bu)
    notes_l = as_list(notes)
    updated = 0

    for i, bid in enumerate(ids):
        bin_row = db.get(GrainBin, bid)
        if not bin_row:
            continue
        new_name = str(names[i] if i < len(names) else bin_row.name).strip() or bin_row.name
        new_crop = str(crops[i] if i < len(crops) else bin_row.crop).strip() or bin_row.crop
        if new_crop not in ("Corn", "Soybeans"):
            new_crop = bin_row.crop
        cap_raw = str(caps[i] if i < len(caps) else "")
        new_cap = _f(cap_raw, None) if cap_raw.strip() else None
        note_raw = str(notes_l[i] if i < len(notes_l) else (bin_row.notes or "")).strip() or None
        on_raw = str(ons[i] if i < len(ons) else "")

        changed = (
            bin_row.name != new_name
            or bin_row.crop != new_crop
            or (bin_row.capacity_bu != new_cap)
            or (bin_row.notes or None) != note_raw
        )

        shares = list(db.scalars(select(BinShare).where(BinShare.bin_id == bid)))
        me = next((s for s in shares if (s.owner_name or "").lower() == "me"), None)
        if me is None and shares:
            me = shares[0]
        old_me = (me.bushels if me else 0) or 0
        if on_raw.strip() != "":
            amount = _f(on_raw) or 0
            if abs(float(old_me) - float(amount)) > 1e-9:
                changed = True
                if me is None:
                    me = BinShare(bin_id=bid, owner_name="Me", bushels=amount)
                    db.add(me)
                else:
                    me.bushels = amount

        if not changed:
            continue

        bin_row.name = new_name
        bin_row.crop = new_crop
        bin_row.capacity_bu = new_cap
        bin_row.notes = note_raw
        updated += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/bins/sheet?error=bin_name_taken", status_code=303)
    return RedirectResponse(f"/bins/sheet?saved={updated}", status_code=303)


@router.post("/bins/create")
def bins_create(
    request: Request,
    name: str = Form(...),
    crop: str = Form("Corn"),
    capacity_bu: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = GrainBin(
        name=name.strip(),
        crop=crop,
        capacity_bu=_f(capacity_bu, None) if capacity_bu.strip() else None,
    )
    db.add(bin_row)
    try:
        db.flush()
        db.add(BinShare(bin_id=bin_row.id, owner_name="Me", bushels=0))
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/bins?error=bin_name_taken", status_code=303)
    return RedirectResponse("/bins", status_code=303)


@router.post("/bins/move")
def bins_move(
    request: Request,
    move_type: str = Form(...),
    bin_id: str = Form(""),
    to_bin_id: str = Form(""),
    field_id: str = Form(""),
    owner_name: str = Form("Me"),
    crop: str = Form("Corn"),
    wet_bu: str = Form(""),
    net_bu: str = Form("0"),
    moisture: str = Form(""),
    ticket_number: str = Form(""),
    destination: str = Form(""),
    contract_id: str = Form(""),
    notes: str = Form(""),
    move_date: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    moist = _f(moisture, None) if moisture.strip() else None
    wet = _f(wet_bu, None) if wet_bu.strip() else None
    bu = _f(net_bu) or 0
    if wet is not None:
        bu = shrink_net_bu(wet, moist, crop)
    elif moist is not None and bu:
        # treat entered net_bu as wet if moisture given and wet blank
        wet = bu
        bu = shrink_net_bu(wet, moist, crop)
    bid = int(bin_id) if bin_id.isdigit() else None
    to_bid = int(to_bin_id) if to_bin_id.isdigit() else None
    fid = int(field_id) if field_id.isdigit() else None
    cid = int(contract_id) if contract_id.isdigit() else None
    owner = owner_name.strip() or "Me"
    db.add(
        GrainMovement(
            bin_id=bid,
            to_bin_id=to_bid,
            field_id=fid,
            contract_id=cid,
            move_date=_d(move_date) or date.today(),
            move_type=move_type,
            crop=crop,
            owner_name=owner,
            wet_bu=wet,
            moisture=moist,
            net_bu=bu,
            ticket_number=ticket_number.strip() or None,
            destination=destination.strip() or None,
            notes=notes.strip() or None,
        )
    )

    def adjust(bin_pk: int, owner_nm: str, delta: float):
        share = db.scalar(
            select(BinShare).where(BinShare.bin_id == bin_pk, BinShare.owner_name == owner_nm)
        )
        if not share:
            share = BinShare(bin_id=bin_pk, owner_name=owner_nm, bushels=0)
            db.add(share)
            db.flush()
        share.bushels = (share.bushels or 0) + delta

    if move_type == "fill" and bid:
        adjust(bid, owner, bu)
    elif move_type == "delivery" and bid:
        adjust(bid, owner, -bu)
        if cid:
            contract = db.get(GrainContract, cid)
            if contract:
                contract.delivered_bu = (contract.delivered_bu or 0) + bu
    elif move_type == "elevator" and fid:
        # harvest direct to elevator / delivery without bin
        if cid:
            contract = db.get(GrainContract, cid)
            if contract:
                contract.delivered_bu = (contract.delivered_bu or 0) + bu
    elif move_type == "transfer" and bid and to_bid:
        adjust(bid, owner, -bu)
        adjust(to_bid, owner, bu)
    log_activity(db, user.get("username"), f"grain_{move_type}", f"{bu} bu {crop}")
    db.commit()
    return RedirectResponse("/bins/ticket", status_code=303)


@router.post("/bins/{bin_id}/update")
def bins_update(
    bin_id: int,
    request: Request,
    name: str = Form(...),
    crop: str = Form("Corn"),
    capacity_bu: str = Form(""),
    on_hand_bu: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename bin, change crop/capacity, and set Me (or primary) on-hand bushels."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.get(GrainBin, bin_id)
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)
    new_name = name.strip()
    if not new_name:
        return RedirectResponse("/bins?error=bin_name_taken", status_code=303)
    bin_row.name = new_name
    bin_row.crop = crop
    bin_row.capacity_bu = _f(capacity_bu, None) if capacity_bu.strip() else None
    if on_hand_bu.strip() != "":
        amount = _f(on_hand_bu) or 0
        shares = list(db.scalars(select(BinShare).where(BinShare.bin_id == bin_id)))
        me = next((s for s in shares if (s.owner_name or "").lower() == "me"), None)
        if me is None and shares:
            me = shares[0]
        if me is None:
            me = BinShare(bin_id=bin_id, owner_name="Me", bushels=amount)
            db.add(me)
        else:
            me.bushels = amount
    try:
        log_activity(db, user.get("username"), "bin_update", new_name)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/bins?error=bin_name_taken", status_code=303)
    return RedirectResponse("/bins", status_code=303)


@router.post("/bins/ticket")
async def bins_ticket(
    request: Request,
    db: Session = Depends(get_db),
):
    """Scale ticket: pull from one or more bins and/or a field."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    crop = str(form.get("crop") or "Corn")
    wet_bu = str(form.get("wet_bu") or "")
    net_bu = str(form.get("net_bu") or "")
    moisture = str(form.get("moisture") or "")
    ticket_number = str(form.get("ticket_number") or "")
    destination = str(form.get("destination") or "")
    contract_id = str(form.get("contract_id") or "")
    owner_name = str(form.get("owner_name") or "Me")
    move_date = str(form.get("move_date") or "")
    notes = str(form.get("notes") or "")
    field_id = str(form.get("field_id") or "")
    field_bu = str(form.get("field_bu") or "")

    moist = _f(moisture, None) if moisture.strip() else None
    wet = _f(wet_bu, None) if wet_bu.strip() else None
    ticket_bu = _f(net_bu) or 0
    if wet is not None:
        ticket_bu = shrink_net_bu(wet, moist, crop)
    elif moist is not None and ticket_bu:
        wet = ticket_bu
        ticket_bu = shrink_net_bu(wet, moist, crop)

    owner = owner_name.strip() or "Me"
    when = _d(move_date) or date.today()
    cid = int(contract_id) if contract_id.isdigit() else None
    fid = int(field_id) if field_id.isdigit() else None
    f_bu = _f(field_bu) or 0
    ticket = ticket_number.strip() or None
    dest = destination.strip() or None
    note = notes.strip() or None

    bin_ids = [str(v) for v in form.getlist("alloc_bin_id")]
    bu_vals = [str(v) for v in form.getlist("alloc_bu")]
    # Pad so zip never drops a bin with blank bu
    while len(bu_vals) < len(bin_ids):
        bu_vals.append("")

    allocations: list[tuple[int, float]] = []
    for bid_s, bu_s in zip(bin_ids, bu_vals):
        if not bid_s.strip() or not bid_s.isdigit():
            continue
        amt = _f(bu_s) or 0
        if amt > 0:
            allocations.append((int(bid_s), amt))

    bare_bins = [int(x) for x in bin_ids if x.strip().isdigit()]
    if ticket_bu > 0 and not allocations and len(bare_bins) == 1:
        allocations = [(bare_bins[0], ticket_bu)]
    if ticket_bu > 0 and fid and f_bu <= 0 and not allocations:
        f_bu = ticket_bu

    sourced = sum(a[1] for a in allocations) + (f_bu if fid and f_bu > 0 else 0)
    if ticket_bu <= 0 and sourced > 0:
        ticket_bu = sourced
    if ticket_bu <= 0 or sourced <= 0:
        return RedirectResponse("/bins/ticket?error=ticket_source", status_code=303)
    if abs(sourced - ticket_bu) > 0.51:
        return RedirectResponse("/bins/ticket?error=ticket_mismatch", status_code=303)

    def adjust(bin_pk: int, owner_nm: str, delta: float):
        share = db.scalar(
            select(BinShare).where(BinShare.bin_id == bin_pk, BinShare.owner_name == owner_nm)
        )
        if not share:
            share = BinShare(bin_id=bin_pk, owner_name=owner_nm, bushels=0)
            db.add(share)
            db.flush()
        share.bushels = (share.bushels or 0) + delta

    delivered_total = 0.0
    for bid, amt in allocations:
        db.add(
            GrainMovement(
                bin_id=bid,
                to_bin_id=None,
                field_id=None,
                contract_id=cid,
                move_date=when,
                move_type="delivery",
                crop=crop,
                owner_name=owner,
                wet_bu=None,
                moisture=moist,
                net_bu=amt,
                ticket_number=ticket,
                destination=dest,
                notes=note,
            )
        )
        adjust(bid, owner, -amt)
        delivered_total += amt

    if fid and f_bu > 0:
        db.add(
            GrainMovement(
                bin_id=None,
                to_bin_id=None,
                field_id=fid,
                contract_id=cid,
                move_date=when,
                move_type="elevator",
                crop=crop,
                owner_name=owner,
                wet_bu=wet,
                moisture=moist,
                net_bu=f_bu,
                ticket_number=ticket,
                destination=dest,
                notes=note,
            )
        )
        delivered_total += f_bu

    if cid and delivered_total > 0:
        contract = db.get(GrainContract, cid)
        if contract:
            contract.delivered_bu = (contract.delivered_bu or 0) + delivered_total

    log_activity(
        db,
        user.get("username"),
        "grain_ticket",
        f"{ticket or 'ticket'} {delivered_total} bu {crop}",
    )
    db.commit()
    return RedirectResponse("/bins/ticket", status_code=303)


@router.post("/bins/condition")
def bins_condition(
    request: Request,
    bin_id: int = Form(...),
    note_date: str = Form(""),
    moisture: str = Form(""),
    temperature: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    db.add(
        BinConditionNote(
            bin_id=bin_id,
            note_date=_d(note_date) or date.today(),
            moisture=_f(moisture, None) if moisture.strip() else None,
            temperature=_f(temperature, None) if temperature.strip() else None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/bins", status_code=303)


# ---------- Contracts / risk ----------
def _production_from_fields(fields: list[Field]):
    """Sum expected bu from each field's expected_yield × my acres."""
    estimates: dict[str, float] = {"Corn": 0.0, "Soybeans": 0.0}
    acres: dict[str, float] = {"Corn": 0.0, "Soybeans": 0.0}
    missing: list[Field] = []
    rows: list[dict] = []
    for f in fields:
        if f.crop not in ("Corn", "Soybeans"):
            continue
        acres[f.crop] = acres.get(f.crop, 0) + (f.acres_mine or 0)
        if f.expected_yield is None:
            missing.append(f)
            continue
        bu = round((f.acres_mine or 0) * f.expected_yield, 1)
        estimates[f.crop] = estimates.get(f.crop, 0) + bu
        rows.append(
            {
                "field": f,
                "bu": bu,
                "yield": f.expected_yield,
                "acres": f.acres_mine or 0,
            }
        )
    for crop in estimates:
        estimates[crop] = round(estimates[crop], 1)
    return estimates, missing, rows, acres


def _quote_updated_info(settings) -> dict:
    """Human-readable last futures update for the marketing board."""
    if not settings or not getattr(settings, "quote_as_of", None):
        return {
            "when": None,
            "label": "Never updated — refresh or enter prices manually",
            "source": None,
            "source_label": None,
            "age_minutes": None,
        }
    when = settings.quote_as_of
    if getattr(when, "tzinfo", None) is not None:
        when = when.replace(tzinfo=None)
    src = (getattr(settings, "quote_source", None) or "stored").lower()
    source_label = {
        "yahoo_delayed": "delayed CME (Yahoo)",
        "manual": "manual override",
        "stored": "saved quote",
    }.get(src, src)
    age_min = None
    try:
        age_min = int(max(0, (datetime.utcnow() - when).total_seconds() // 60))
    except Exception:
        age_min = None
    stamp = when.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    if age_min is None:
        age_txt = ""
    elif age_min < 1:
        age_txt = " (just now)"
    elif age_min < 60:
        age_txt = f" ({age_min} min ago)"
    elif age_min < 60 * 24:
        hrs = age_min // 60
        age_txt = f" ({hrs} hr{'s' if hrs != 1 else ''} ago)"
    else:
        days = age_min // (60 * 24)
        age_txt = f" ({days} day{'s' if days != 1 else ''} ago)"
    return {
        "when": when,
        "label": f"Last updated {stamp}{age_txt}",
        "stamp": stamp,
        "age_txt": age_txt.strip(),
        "source": src,
        "source_label": source_label,
        "age_minutes": age_min,
    }


def _risk_page_payload(request: Request, db: Session, user):
    # Alias used by risk_page body below — kept for clarity at call sites.
    year = _year(db)
    settings = db.scalar(select(AppSettings).limit(1))
    contracts = []
    estimates = {"Corn": 0.0, "Soybeans": 0.0}
    missing_yield = []
    prod_rows = []
    crop_acres = {"Corn": 0.0, "Soybeans": 0.0}
    insurance = []
    targets = []
    events_by = {}
    if year:
        fields = [
            f
            for f in db.scalars(
                select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)
            )
            if not is_summary_field_name(f.name)
        ]
        estimates, missing_yield, prod_rows, crop_acres = _production_from_fields(fields)
        for crop, bu in estimates.items():
            row = db.scalar(
                select(ProductionEstimate).where(
                    ProductionEstimate.crop_year_id == year.id,
                    ProductionEstimate.crop == crop,
                )
            )
            if not row:
                row = ProductionEstimate(crop_year_id=year.id, crop=crop)
                db.add(row)
            row.expected_bu = bu
            row.notes = "Auto from field expected yields"
        db.commit()

        contracts = list(
            db.scalars(
                select(GrainContract)
                .where(GrainContract.crop_year_id == year.id)
                .order_by(GrainContract.crop, GrainContract.id)
            )
        )
        insurance = list(db.scalars(select(CropInsurance).where(CropInsurance.crop_year_id == year.id)))
        targets = list(db.scalars(select(MarketingTarget).where(MarketingTarget.crop_year_id == year.id)))
        for ev in db.scalars(
            select(ContractEvent)
            .where(ContractEvent.contract_id.in_([c.id for c in contracts] or [0]))
            .order_by(ContractEvent.id.desc())
        ):
            events_by.setdefault(ev.contract_id, []).append(ev)

    bin_bu = {"Corn": 0.0, "Soybeans": 0.0}
    for share, bin_row in db.execute(
        select(BinShare, GrainBin).join(GrainBin, BinShare.bin_id == GrainBin.id)
    ):
        crop = bin_row.crop if bin_row.crop in bin_bu else None
        if crop:
            bin_bu[crop] += share.bushels or 0
    for crop in bin_bu:
        bin_bu[crop] = round(bin_bu[crop], 1)

    by_crop: dict[str, list] = {}
    for c in contracts:
        by_crop.setdefault(c.crop, []).append(c)
    cards = []
    today = date.today()
    due_soon = []
    marketing_bars = []

    farm_expected = 0.0
    farm_fut = 0.0
    farm_bas = 0.0
    for crop in ("Corn", "Soybeans"):
        items = by_crop.get(crop, [])
        sold = sum(i.bushels for i in items)
        expected = estimates.get(crop) or 0
        fut_bu = sum(i.bushels for i in items if mkt.futures_locked(i))
        bas_bu = sum(i.bushels for i in items if mkt.basis_locked(i))
        pct = round(100 * sold / expected, 1) if expected else None
        fut_pct = round(100 * fut_bu / expected, 1) if expected else None
        bas_pct = round(100 * bas_bu / expected, 1) if expected else None
        left = sum(max(0, i.bushels - (i.delivered_bu or 0)) for i in items)
        cards.append(
            {
                "crop": crop,
                "sold": sold,
                "expected": expected,
                "pct": pct,
                "left_deliver": left,
                "acres": crop_acres.get(crop) or 0,
                "futures_bu": fut_bu,
                "basis_bu": bas_bu,
                "futures_pct": fut_pct,
                "basis_pct": bas_pct,
            }
        )
        marketing_bars.append(
            {
                "label": crop,
                "expected": expected,
                "futures_bu": fut_bu,
                "basis_bu": bas_bu,
                "futures_pct": fut_pct if fut_pct is not None else 0,
                "basis_pct": bas_pct if bas_pct is not None else 0,
                "futures_width": min(100, fut_pct or 0),
                "basis_width": min(100, bas_pct or 0),
            }
        )
        farm_expected += expected
        farm_fut += fut_bu
        farm_bas += bas_bu

    farm_fut_pct = round(100 * farm_fut / farm_expected, 1) if farm_expected else None
    farm_bas_pct = round(100 * farm_bas / farm_expected, 1) if farm_expected else None
    marketing_summary = {
        "expected": farm_expected,
        "futures_bu": farm_fut,
        "basis_bu": farm_bas,
        "futures_pct": farm_fut_pct if farm_fut_pct is not None else 0,
        "basis_pct": farm_bas_pct if farm_bas_pct is not None else 0,
        "futures_width": min(100, farm_fut_pct or 0),
        "basis_width": min(100, farm_bas_pct or 0),
        "has_expected": farm_expected > 0,
    }

    for c in contracts:
        if c.delivery_end and c.status == "open":
            days = (c.delivery_end - today).days
            if 0 <= days <= 30:
                due_soon.append({"contract": c, "days": days})
            elif days < 0 and (c.bushels - (c.delivered_bu or 0)) > 0:
                due_soon.append({"contract": c, "days": days})

    corn_cost = (settings.corn_cost_per_ac if settings else 0) or 0
    soy_cost = (settings.soy_cost_per_ac if settings else 0) or 0
    corn_price = settings.corn_price_assumption if settings else None
    soy_price = settings.soy_price_assumption if settings else None
    be = []
    be_by_crop: dict[str, float | None] = {}
    for crop, cost, price in (("Corn", corn_cost, corn_price), ("Soybeans", soy_cost, soy_price)):
        expected = estimates.get(crop) or 0
        acres = crop_acres.get(crop) or 0
        yield_assumed = round(expected / acres, 1) if acres and expected else None
        be_yield = round(cost / price, 1) if cost and price else None
        be_price = round(cost / yield_assumed, 2) if cost and yield_assumed else None
        be_by_crop[crop] = be_price
        be.append(
            {
                "crop": crop,
                "cost_ac": cost,
                "price": price,
                "acres": acres,
                "yield_assumed": yield_assumed,
                "be_yield": be_yield,
                "be_price": be_price,
            }
        )

    board = cme_quotes.board_from_settings(settings)
    futures_strip = cme_quotes.strip_from_settings(settings)
    if settings and not getattr(settings, "futures_strip_json", None):
        shells = {
            "Corn": cme_quotes.strip_contracts("Corn"),
            "Soybeans": cme_quotes.strip_contracts("Soybeans"),
        }
        settings.futures_strip_json = json.dumps(shells)
        db.commit()
        futures_strip = cme_quotes.strip_from_settings(settings)

    strip_spreads = mkt.strip_spreads_with_carry(futures_strip, settings)

    corn_basis = (settings.corn_local_basis if settings else None)
    soy_basis = (settings.soy_local_basis if settings else None)
    market_by_crop = {
        "Corn": mkt.market_cash_equiv(
            board["Corn"]["price"] if board.get("Corn") else (settings.corn_price_assumption if settings else None),
            corn_basis,
        ),
        "Soybeans": mkt.market_cash_equiv(
            board["Soybeans"]["price"] if board.get("Soybeans") else (settings.soy_price_assumption if settings else None),
            soy_basis,
        ),
    }

    ranks = mkt.rank_contracts(
        contracts,
        breakeven_by_crop=be_by_crop,
        market_by_crop=market_by_crop,
    )
    desk = mkt.desk_rows(contracts, events_by, be_by_crop, market_by_crop)

    exposure = {}
    risk_tracker = {
        "by_crop": {},
        "stress_total": 0.0,
        "money_at_risk_total": 0.0,
        "unsold_total": 0.0,
        "bin_total": 0.0,
        "futures_open_total": 0.0,
    }
    for crop in ("Corn", "Soybeans"):
        exp = mkt.crop_exposure(
            by_crop.get(crop, []),
            estimates.get(crop) or 0,
            bin_bu.get(crop) or 0,
        )
        shock = 0.5 if crop == "Corn" else 1.0
        if settings:
            shock = (
                settings.corn_stress_shock if crop == "Corn" else settings.soy_stress_shock
            ) or shock
        mark = market_by_crop.get(crop)
        if mark is None and board.get(crop):
            mark = board[crop]["price"]
        stress = mkt.stress_dollars(exp["futures_open"], shock)
        at_risk_bu = exp["bin_bu"] + exp["unsold"]
        mar = mkt.money_at_risk(at_risk_bu, mark)
        exposure[crop] = exp
        risk_tracker["by_crop"][crop] = {
            **exp,
            "shock": shock,
            "stress": stress,
            "mark": mark,
            "money_at_risk": mar,
            "at_risk_bu": round(at_risk_bu, 1),
        }
        risk_tracker["stress_total"] += stress
        risk_tracker["money_at_risk_total"] += mar or 0
        risk_tracker["unsold_total"] += exp["unsold"]
        risk_tracker["bin_total"] += exp["bin_bu"]
        risk_tracker["futures_open_total"] += exp["futures_open"]

    quote_updated = _quote_updated_info(settings)

    return {
        "request": request,
        "user": user,
        "farm_name": _farm(db),
        "year": year,
        "contracts": contracts,
        "cards": cards,
        "contract_types": CONTRACT_TYPES,
        "today": today.isoformat(),
        "due_soon": due_soon,
        "insurance": insurance,
        "targets": targets,
        "events_by": events_by,
        "be": be,
        "settings": settings,
        "missing_yield": missing_yield,
        "prod_rows": prod_rows,
        "marketing_summary": marketing_summary,
        "marketing_bars": marketing_bars,
        "board": board,
        "futures_strip": futures_strip,
        "strip_spreads": strip_spreads,
        "quote_updated": quote_updated,
        "ranks": ranks,
        "desk": desk,
        "bin_bu": bin_bu,
        "exposure": exposure,
        "risk_tracker": risk_tracker,
        "market_by_crop": market_by_crop,
    }


@router.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk"
    return templates.TemplateResponse("risk.html", ctx)


@router.get("/risk/contracts", response_class=HTMLResponse)
def risk_contracts_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts"
    return templates.TemplateResponse("risk_contracts.html", ctx)


@router.get("/risk/settings", response_class=HTMLResponse)
def risk_settings_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_settings"
    return templates.TemplateResponse("risk_settings.html", ctx)


@router.get("/risk/carry", response_class=HTMLResponse)
def risk_carry_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    year = _year(db)

    bin_bu = {"Corn": 0.0, "Soybeans": 0.0}
    for share, bin_row in db.execute(
        select(BinShare, GrainBin).join(GrainBin, BinShare.bin_id == GrainBin.id)
    ):
        crop = bin_row.crop if bin_row.crop in bin_bu else None
        if crop:
            bin_bu[crop] += share.bushels or 0
    for crop in bin_bu:
        bin_bu[crop] = round(bin_bu[crop], 1)

    # Stored quotes only — live refresh is explicit (Refresh quotes), not on every page open
    board = cme_quotes.board_from_settings(settings)

    fallback = {
        "Corn": settings.corn_price_assumption if settings else None,
        "Soybeans": settings.soy_price_assumption if settings else None,
    }
    carry = mkt.build_carry_snapshot(settings, board, bin_bu, market_fallback=fallback)
    quote_updated = _quote_updated_info(settings)

    return templates.TemplateResponse(
        "carry.html",
        {
            "request": request,
            "user": user,
            "active": "carry",
            "farm_name": _farm(db),
            "year": year,
            "settings": settings,
            "board": board,
            "bin_bu": bin_bu,
            "carry": carry,
            "quote_updated": quote_updated,
        },
    )


@router.post("/risk/quotes")
def risk_quotes_manual(
    request: Request,
    corn_futures: str = Form(""),
    soy_futures: str = Form(""),
    corn_local_basis: str = Form(""),
    soy_local_basis: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        if corn_futures.strip():
            settings.corn_futures = _f(corn_futures, None)
        if soy_futures.strip():
            settings.soy_futures = _f(soy_futures, None)
        settings.corn_local_basis = (
            _f(corn_local_basis, None) if corn_local_basis.strip() else settings.corn_local_basis
        )
        settings.soy_local_basis = (
            _f(soy_local_basis, None) if soy_local_basis.strip() else settings.soy_local_basis
        )
        if corn_local_basis.strip() == "" and "corn_local_basis" in (request.query_params or {}):
            pass
        settings.quote_as_of = datetime.utcnow()
        settings.quote_source = "manual"
        db.commit()
    return RedirectResponse("/risk", status_code=303)


@router.post("/risk/quotes/refresh")
def risk_quotes_refresh(
    request: Request,
    next: str = Form("/risk"),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    quote_status = "fail"
    if settings:
        fetched = cme_quotes.fetch_nearby_quotes()
        live_ok = cme_quotes.apply_fetch_to_settings(settings, fetched)
        # Live feed often rate-limits; still push calculator/board futures to the
        # latest strip quote we already have so Refresh always updates the inputs.
        had_live = any(
            (fetched.get(crop) or {}).get("price") is not None
            for crop in ("Corn", "Soybeans")
        )
        if not had_live:
            synced = cme_quotes.sync_nearby_from_strip(settings, force=True)
            if synced:
                quote_status = "stored"
            else:
                quote_status = "fail"
        elif live_ok:
            quote_status = "ok"
        else:
            quote_status = "fail"
        db.commit()
    dest = (next or "/risk").strip() or "/risk"
    if not dest.startswith("/"):
        dest = "/risk"
    sep = "&" if "?" in dest else "?"
    dest = f"{dest}{sep}quotes={quote_status}"
    return RedirectResponse(dest, status_code=303)


@router.post("/risk/carry")
def risk_carry(
    request: Request,
    carry_interest_apr: str = Form("7"),
    carry_mark_mode: str = Form("cash"),
    corn_futures: str = Form(""),
    soy_futures: str = Form(""),
    corn_local_basis: str = Form(""),
    soy_local_basis: str = Form(""),
    corn_storage_per_bu_mo: str = Form("0.03"),
    soy_storage_per_bu_mo: str = Form("0.04"),
    corn_shrink_per_bu_mo: str = Form("0.003"),
    soy_shrink_per_bu_mo: str = Form("0.004"),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        mode = (carry_mark_mode or "cash").strip().lower()
        if mode not in ("futures", "cash"):
            mode = "cash"
        settings.carry_mark_mode = mode
        settings.carry_interest_apr = _f(carry_interest_apr) or 0
        settings.corn_storage_per_bu_mo = _f(corn_storage_per_bu_mo) or 0
        settings.soy_storage_per_bu_mo = _f(soy_storage_per_bu_mo) or 0
        settings.corn_shrink_per_bu_mo = _f(corn_shrink_per_bu_mo) or 0
        settings.soy_shrink_per_bu_mo = _f(soy_shrink_per_bu_mo) or 0
        if corn_futures.strip():
            settings.corn_futures = _f(corn_futures, None)
        if soy_futures.strip():
            settings.soy_futures = _f(soy_futures, None)
        if corn_local_basis.strip() != "":
            settings.corn_local_basis = _f(corn_local_basis, None)
        if soy_local_basis.strip() != "":
            settings.soy_local_basis = _f(soy_local_basis, None)
        db.commit()
    return redirect_flash(request, "/risk/carry", "Carry assumptions saved.")


@router.post("/risk/stress")
def risk_stress(
    request: Request,
    corn_stress_shock: str = Form("0.50"),
    soy_stress_shock: str = Form("1.00"),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.corn_stress_shock = _f(corn_stress_shock) or 0.5
        settings.soy_stress_shock = _f(soy_stress_shock) or 1.0
        db.commit()
    return RedirectResponse("/risk#risk-tracker", status_code=303)


@router.post("/risk/cop")
def risk_cop(
    request: Request,
    corn_cost_per_ac: str = Form("0"),
    soy_cost_per_ac: str = Form("0"),
    corn_price_assumption: str = Form(""),
    soy_price_assumption: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.corn_cost_per_ac = _f(corn_cost_per_ac) or 0
        settings.soy_cost_per_ac = _f(soy_cost_per_ac) or 0
        settings.corn_price_assumption = _f(corn_price_assumption, None) if corn_price_assumption.strip() else None
        settings.soy_price_assumption = _f(soy_price_assumption, None) if soy_price_assumption.strip() else None
        db.commit()
    return RedirectResponse("/risk/settings", status_code=303)


@router.post("/risk/contract")
def risk_contract(
    request: Request,
    crop: str = Form("Corn"),
    contract_type: str = Form("cash"),
    buyer: str = Form(""),
    bushels: str = Form("0"),
    cash_price: str = Form(""),
    futures_price: str = Form(""),
    basis: str = Form(""),
    futures_month: str = Form(""),
    delivery_end: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/risk/contracts", status_code=303)
    db.add(
        GrainContract(
            crop_year_id=year.id,
            crop=crop,
            contract_type=contract_type,
            buyer=buyer.strip() or None,
            bushels=_f(bushels) or 0,
            cash_price=_f(cash_price, None) if cash_price.strip() else None,
            futures_price=_f(futures_price, None) if futures_price.strip() else None,
            basis=_f(basis, None) if basis.strip() else None,
            futures_month=futures_month.strip() or None,
            delivery_end=_d(delivery_end),
            notes=notes.strip() or None,
        )
    )
    log_activity(db, user.get("username"), "contract_add", f"{crop} {bushels} bu")
    db.commit()
    return RedirectResponse("/risk/contracts", status_code=303)


@router.post("/risk/event")
def risk_event(
    request: Request,
    contract_id: int = Form(...),
    event_type: str = Form("partial_price"),
    event_date: str = Form(""),
    bushels: str = Form(""),
    price: str = Form(""),
    futures_month: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    contract = db.get(GrainContract, contract_id)
    if not contract:
        return RedirectResponse("/risk/contracts", status_code=303)
    db.add(
        ContractEvent(
            contract_id=contract_id,
            event_date=_d(event_date) or date.today(),
            event_type=event_type,
            bushels=_f(bushels, None) if bushels.strip() else None,
            price=_f(price, None) if price.strip() else None,
            futures_month=futures_month.strip() or None,
            notes=notes.strip() or None,
        )
    )
    if event_type == "hta_roll" and futures_month.strip():
        contract.futures_month = futures_month.strip()
        if price.strip():
            contract.futures_price = _f(price, None)
    if event_type == "partial_price" and price.strip():
        # keep latest cash/futures as reference
        if contract.contract_type in ("cash", "forward", "minimum_price"):
            contract.cash_price = _f(price, None)
        else:
            contract.futures_price = _f(price, None)
    if event_type == "basis_set" and price.strip():
        contract.basis = _f(price, None)
    db.commit()
    return RedirectResponse("/risk/contracts", status_code=303)


@router.post("/risk/insurance")
def risk_insurance(
    request: Request,
    crop: str = Form("Corn"),
    policy_type: str = Form("RP"),
    coverage_level: str = Form(""),
    acres: str = Form(""),
    premium: str = Form(""),
    guarantee_bu: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/risk/settings", status_code=303)
    cov = _f(coverage_level, None) if coverage_level.strip() else None
    if cov and cov > 1:
        cov = cov / 100.0
    db.add(
        CropInsurance(
            crop_year_id=year.id,
            crop=crop,
            policy_type=policy_type,
            coverage_level=cov,
            acres=_f(acres, None) if acres.strip() else None,
            premium=_f(premium, None) if premium.strip() else None,
            guarantee_bu=_f(guarantee_bu, None) if guarantee_bu.strip() else None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/risk/settings", status_code=303)


@router.post("/risk/target")
def risk_target(
    request: Request,
    crop: str = Form("Corn"),
    target_pct: str = Form("0"),
    by_date: str = Form(""),
    price_floor: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/risk/settings", status_code=303)
    db.add(
        MarketingTarget(
            crop_year_id=year.id,
            crop=crop,
            target_pct=_f(target_pct) or 0,
            by_date=_d(by_date),
            price_floor=_f(price_floor, None) if price_floor.strip() else None,
            notes=notes.strip() or None,
        )
    )
    db.commit()
    return RedirectResponse("/risk/settings", status_code=303)


# ---------- Inputs & plans (hybrids / sprays / field plans) ----------
def _money(value: str) -> float | None:
    return _f(value, default=None)


def _line_cost_per_acre(rate: float | None, cost_per_unit: float | None, cost_per_acre: float | None) -> float | None:
    if cost_per_acre is not None:
        return cost_per_acre
    if rate is not None and cost_per_unit is not None:
        return round(rate * cost_per_unit, 4)
    return None


def _label_key(value: str) -> str:
    """Normalize for brand/trait matching (ignore case, spaces, punctuation)."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _vocabulary_labels(values: Iterable[str | None]) -> list[str]:
    """Unique labels; near-spellings collapse to the most-used spelling."""
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        buckets[_label_key(text)][text] += 1
    labels = [counts.most_common(1)[0][0] for counts in buckets.values()]
    return sorted(labels, key=str.casefold)


def _split_trait_tokens(value: str) -> list[str]:
    parts = re.split(r"[,;/|]+", value or "")
    return [p.strip() for p in parts if p.strip()]


def _canonicalize_label(value: str, known: list[str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    key = _label_key(text)
    for label in known:
        if _label_key(label) == key:
            return label
    return text


def _canonicalize_traits(value: str, known_traits: list[str]) -> str | None:
    tokens = _split_trait_tokens(value)
    if not tokens and (value or "").strip():
        tokens = [(value or "").strip()]
    if not tokens:
        return None
    canon = [_canonicalize_label(t, known_traits) for t in tokens]
    # de-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in canon:
        k = _label_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return ", ".join(out) if out else None


def _hybrid_brand_trait_vocab(db: Session) -> tuple[list[str], list[str]]:
    rows = list(db.execute(select(Hybrid.brand, Hybrid.traits)).all())
    brands = _vocabulary_labels(brand for brand, _traits in rows)
    trait_tokens: list[str] = []
    for _brand, traits in rows:
        if traits:
            trait_tokens.extend(_split_trait_tokens(traits))
            trait_tokens.append(traits.strip())
    return brands, _vocabulary_labels(trait_tokens)


def _sync_hybrid_label_spellings(db: Session) -> None:
    """Collapse near-duplicate brand/trait spellings onto the most-used form."""
    brands, traits = _hybrid_brand_trait_vocab(db)
    if not brands and not traits:
        return
    brand_map = {_label_key(b): b for b in brands}
    changed = False
    for hybrid in db.scalars(select(Hybrid)):
        if hybrid.brand:
            canon = brand_map.get(_label_key(hybrid.brand))
            if canon and hybrid.brand != canon:
                hybrid.brand = canon
                changed = True
        if hybrid.traits:
            new_traits = _canonicalize_traits(hybrid.traits, traits)
            if new_traits != hybrid.traits:
                hybrid.traits = new_traits
                changed = True
    if changed:
        db.commit()


def _library_payload(request: Request, db: Session, user) -> dict:
    year = _year(db)
    hybrids = sprays = plans = fields = []
    spray_lines: dict[int, list] = {}
    _sync_hybrid_label_spellings(db)
    hybrid_brands, hybrid_traits = _hybrid_brand_trait_vocab(db)
    hybrid_suggest_json = json.dumps(
        {"brands": hybrid_brands, "traits": hybrid_traits},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    if year:
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
        hybrids.sort(
            key=lambda h: (
                (h.brand or "\uffff").casefold(),
                (h.name or "").casefold(),
            )
        )
        sprays = list(db.scalars(select(SprayMix).where(SprayMix.crop_year_id == year.id).order_by(SprayMix.name)))
        plans = list(db.scalars(select(FieldPlan).where(FieldPlan.crop_year_id == year.id).order_by(FieldPlan.id.desc())))
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)))
        if sprays:
            lines = list(
                db.scalars(
                    select(SprayMixLine)
                    .where(SprayMixLine.spray_mix_id.in_([s.id for s in sprays]))
                    .order_by(SprayMixLine.sort_order, SprayMixLine.id)
                )
            )
            for line in lines:
                spray_lines.setdefault(line.spray_mix_id, []).append(line)
    return {
        "request": request,
        "user": user,
        "farm_name": _farm(db),
        "year": year,
        "hybrids": hybrids,
        "hybrid_brands": hybrid_brands,
        "hybrid_traits": hybrid_traits,
        "hybrid_suggest_json": hybrid_suggest_json,
        "sprays": sprays,
        "spray_lines": spray_lines,
        "plans": plans,
        "fields": fields,
        "today": date.today().isoformat(),
    }


@router.get("/inputs", response_class=HTMLResponse)
def inputs_alias(request: Request, db: Session = Depends(get_db)):
    return library_page(request, db)


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _library_payload(request, db, user)
    ctx["active"] = "library"
    return templates.TemplateResponse("library.html", ctx)


@router.get("/inputs/assign", response_class=HTMLResponse)
def inputs_assign_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _library_payload(request, db, user)
    ctx["active"] = "library_assign"
    return templates.TemplateResponse("library_assign.html", ctx)


@router.post("/library/hybrid")
def library_hybrid(
    request: Request,
    name: str = Form(...),
    crop: str = Form("Corn"),
    brand: str = Form(""),
    maturity: str = Form(""),
    traits: str = Form(""),
    unit_label: str = Form(""),
    cost_per_unit: str = Form(""),
    cost_per_acre: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/library", status_code=303)
    known_brands, known_traits = _hybrid_brand_trait_vocab(db)
    brand_s = _canonicalize_label(brand, known_brands) or None
    traits_s = _canonicalize_traits(traits, known_traits)
    db.add(
        Hybrid(
            crop_year_id=year.id,
            name=name.strip(),
            crop=crop,
            brand=brand_s,
            maturity=maturity.strip() or None,
            traits=traits_s,
            unit_label=unit_label.strip() or None,
            cost_per_unit=_money(cost_per_unit),
            cost_per_acre=_money(cost_per_acre),
        )
    )
    db.commit()
    return redirect_flash(request, "/library", f"Added hybrid “{name.strip()}”.")


@router.post("/library/assign-hybrid")
def library_assign_hybrid(
    request: Request,
    hybrid_id: int = Form(...),
    rate: str = Form(""),
    applied_date: str = Form(""),
    field_ids: list[int] = Form(default=[]),
    field_id: Optional[int] = Form(None),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ids = list(field_ids or [])
    if not ids and field_id:
        ids = [field_id]
    if not ids:
        return redirect_flash(
            request,
            "/library",
            "No fields selected — hybrid was not assigned.",
            "error",
        )
    hybrid = db.get(Hybrid, hybrid_id)
    if not hybrid:
        return redirect_flash(request, "/library", "Hybrid not found — nothing assigned.", "error")
    # Gate: if hybrid has $/unit cost but no cost_per_acre fallback, units are needed for costing
    if hybrid.cost_per_unit and not hybrid.cost_per_acre:
        return redirect_flash(
            request,
            "/library",
            f'Hybrid "{hybrid.name}" has $/unit cost but no $/ac fallback. '
            "Enter units applied via the field wizard (Add operation \u2192 Planting) so seed cost is captured, "
            "or set a $/ac on the hybrid in the library.",
            "warn",
        )
    when = _d(applied_date)
    pairs = [("hybrid_id", str(hybrid_id)), ("rate", rate), ("applied_date", applied_date)]
    pairs += [("field_ids", str(fid)) for fid in ids]
    block = confirm_if_duplicates(
        request,
        hits=find_hybrid_assign_dups(db, ids, hybrid_id, when),
        confirm_duplicate=confirm_duplicate,
        action="/library/assign-hybrid",
        cancel_url="/library",
        heading="Possible duplicate hybrid assign",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="library",
    )
    if block:
        return block
    for fid in ids:
        db.add(
            FieldHybrid(
                field_id=fid,
                hybrid_id=hybrid_id,
                rate=rate.strip() or None,
                applied_date=when,
            )
        )
    db.commit()
    names = [
        f.name
        for f in db.scalars(select(Field).where(Field.id.in_(ids)).order_by(Field.name))
    ]
    date_bit = f" · applied {when.isoformat()}" if when else ""
    return redirect_flash(
        request,
        f"/fields/{ids[0]}/operations",
        f"Assigned {hybrid.name} to {len(ids)} field{'s' if len(ids) != 1 else ''}"
        f" ({', '.join(names)}){date_bit}.",
    )


@router.post("/library/spray")
def library_spray(
    request: Request,
    name: str = Form(...),
    timing: str = Form(""),
    crop: str = Form(""),
    notes: str = Form(""),
    line_product: list[str] = Form(default=[]),
    line_rate: list[str] = Form(default=[]),
    line_rate_unit: list[str] = Form(default=[]),
    line_unit: list[str] = Form(default=[]),
    line_cost_unit: list[str] = Form(default=[]),
    line_cost_acre: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/library", status_code=303)

    # Normalize single-value form posts to lists
    def as_list(vals: list[str] | str) -> list[str]:
        if isinstance(vals, str):
            return [vals]
        return list(vals or [])

    products = as_list(line_product)
    rates = as_list(line_rate)
    rate_units = as_list(line_rate_unit)
    units = as_list(line_unit)
    costs_u = as_list(line_cost_unit)
    costs_a = as_list(line_cost_acre)

    mix = SprayMix(
        crop_year_id=year.id,
        name=name.strip(),
        timing=timing.strip() or None,
        crop=crop.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(mix)
    db.flush()

    summaries: list[str] = []
    total_cpa = 0.0
    has_cpa = False
    n = max(len(products), 1)
    for i in range(n):
        pname = (products[i] if i < len(products) else "").strip()
        if not pname:
            continue
        rate = _money(rates[i] if i < len(rates) else "")
        cpu = _money(costs_u[i] if i < len(costs_u) else "")
        cpa = _line_cost_per_acre(rate, cpu, _money(costs_a[i] if i < len(costs_a) else ""))
        ru = (rate_units[i] if i < len(rate_units) else "").strip() or None
        ul = (units[i] if i < len(units) else "").strip() or None
        db.add(
            SprayMixLine(
                spray_mix_id=mix.id,
                product_name=pname,
                rate=rate,
                rate_unit=ru,
                unit_label=ul,
                cost_per_unit=cpu,
                cost_per_acre=cpa,
                sort_order=i,
            )
        )
        bit = pname
        if rate is not None:
            bit += f" {rate:g}"
            if ru:
                bit += f" {ru}"
        if cpa is not None:
            bit += f" (${cpa:.2f}/ac)"
            total_cpa += cpa
            has_cpa = True
        summaries.append(bit)

    mix.products_json = "; ".join(summaries) if summaries else None
    mix.cost_per_acre = round(total_cpa, 4) if has_cpa else None
    db.commit()
    return redirect_flash(request, "/library", f"Added spray mix “{mix.name}”.")


@router.post("/library/assign-spray")
def library_assign_spray(
    request: Request,
    timing_label: str = Form(""),
    field_ids: list[int] = Form(default=[]),
    field_id: Optional[int] = Form(None),
    job_mix_id: list[int] = Form(default=[]),
    job_date: list[str] = Form(default=[]),
    # legacy single-mix form (if still posted)
    spray_mix_id: Optional[int] = Form(None),
    applied_date: str = Form(""),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ids = list(field_ids or [])
    if not ids and field_id:
        ids = [field_id]
    if not ids:
        return redirect_flash(
            request,
            "/library",
            "No fields selected — spray mix was not assigned.",
            "error",
        )

    jobs: list[tuple[int, Optional[date]]] = []
    job_dates_raw: list[str] = []
    if job_mix_id:
        dates = list(job_date or [])
        for i, mid in enumerate(job_mix_id):
            raw = dates[i] if i < len(dates) else ""
            job_dates_raw.append(raw or "")
            jobs.append((int(mid), _d(raw or "")))
    elif spray_mix_id:
        job_dates_raw.append(applied_date)
        jobs.append((int(spray_mix_id), _d(applied_date)))

    if not jobs:
        return redirect_flash(
            request,
            "/library",
            "Select at least one spray mix — nothing assigned.",
            "error",
        )

    mix_ids = {mid for mid, _ in jobs}
    mixes = {
        m.id: m
        for m in db.scalars(select(SprayMix).where(SprayMix.id.in_(mix_ids))).all()
    }
    missing = mix_ids - set(mixes)
    if missing:
        return redirect_flash(request, "/library", "Spray mix not found — nothing assigned.", "error")

    pairs = [("timing_label", timing_label)]
    pairs += [("field_ids", str(fid)) for fid in ids]
    for mid, raw in zip([j[0] for j in jobs], job_dates_raw):
        pairs.append(("job_mix_id", str(mid)))
        pairs.append(("job_date", raw))
    block = confirm_if_duplicates(
        request,
        hits=find_spray_assign_dups(db, ids, jobs),
        confirm_duplicate=confirm_duplicate,
        action="/library/assign-spray",
        cancel_url="/library",
        heading="Possible duplicate spray assign",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="library",
    )
    if block:
        return block

    timing = timing_label.strip() or None
    created = 0
    for fid in ids:
        for mid, when in jobs:
            db.add(
                FieldSprayMix(
                    field_id=fid,
                    spray_mix_id=mid,
                    timing_label=timing,
                    applied_date=when,
                )
            )
            created += 1
    db.commit()

    names = [
        f.name
        for f in db.scalars(select(Field).where(Field.id.in_(ids)).order_by(Field.name))
    ]
    mix_names = [mixes[mid].name for mid in sorted(mix_ids, key=lambda x: mixes[x].name.lower())]
    dated = sum(1 for _, when in jobs if when)
    undated = len(jobs) - dated
    date_bit = ""
    if dated and not undated:
        date_bit = f" · {dated} date{'s' if dated != 1 else ''}"
    elif dated and undated:
        date_bit = f" · {dated} dated / {undated} undated"
    return redirect_flash(
        request,
        f"/fields/{ids[0]}/operations",
        f"Assigned {len(mix_names)} mix{'es' if len(mix_names) != 1 else ''} "
        f"({', '.join(mix_names)}) × {len(ids)} field{'s' if len(ids) != 1 else ''}"
        f" ({', '.join(names)}) — {created} assignment{'s' if created != 1 else ''}{date_bit}.",
    )


@router.post("/library/plan")
def library_plan(
    request: Request,
    plan_type: str = Form(...),
    title: str = Form(...),
    details: str = Form(""),
    target_date: str = Form(""),
    estimated_cost_per_acre: str = Form(""),
    field_ids: list[int] = Form(default=[]),
    field_id: Optional[int] = Form(None),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return redirect_flash(request, "/library", "No working crop year — plan not saved.", "error")
    ids = list(field_ids or [])
    if not ids and field_id:
        ids = [field_id]
    if not ids:
        return redirect_flash(
            request,
            "/library",
            "No fields selected — plan was not added.",
            "error",
        )
    when = _d(target_date)
    est = _money(estimated_cost_per_acre)
    title_s = title.strip()
    details_s = details.strip() or None
    pairs = [
        ("plan_type", plan_type),
        ("title", title),
        ("details", details),
        ("target_date", target_date),
        ("estimated_cost_per_acre", estimated_cost_per_acre),
    ]
    pairs += [("field_ids", str(fid)) for fid in ids]
    block = confirm_if_duplicates(
        request,
        hits=find_plan_dups(db, ids, year.id, plan_type, title_s, when),
        confirm_duplicate=confirm_duplicate,
        action="/library/plan",
        cancel_url="/library",
        heading="Possible duplicate field plan",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="library",
    )
    if block:
        return block
    for fid in ids:
        db.add(
            FieldPlan(
                field_id=fid,
                crop_year_id=year.id,
                plan_type=plan_type,
                title=title_s,
                details=details_s,
                target_date=when,
                estimated_cost_per_acre=est,
                status="planned",
            )
        )
    db.commit()
    names = [
        f.name
        for f in db.scalars(select(Field).where(Field.id.in_(ids)).order_by(Field.name))
    ]
    return redirect_flash(
        request,
        f"/fields/{ids[0]}/operations",
        f"Added plan “{title_s}” on {len(ids)} field{'s' if len(ids) != 1 else ''}"
        f" ({', '.join(names)}).",
    )


# ---------- Guided Inputs upload (chem prices / mix programs) ----------
def _upload_root() -> Path:
    root = Path(__file__).resolve().parent.parent / "data" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (name or "upload.bin"))


def _catalog_for_match(db: Session, year: CropYear | None):
    products = list(db.scalars(select(InputProduct).order_by(InputProduct.name)))
    mixes = []
    if year:
        mixes = list(db.scalars(select(SprayMix).where(SprayMix.crop_year_id == year.id).order_by(SprayMix.name)))
    return products, mixes


def _start_guided_import(
    db: Session,
    *,
    user: dict,
    filename: str,
    content: bytes,
    import_type: str = "inputs_guided",
) -> ImportBatch:
    from app import input_import as iimp

    safe = _safe_filename(filename)
    path = _upload_root() / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path.write_bytes(content)
    extract = iimp.extract_file(path, content)
    # Persist full sheet matrices alongside payload for sheet switching
    full_path = path.with_suffix(path.suffix + ".extract.json")
    full_path.write_text(json.dumps(extract, default=str), encoding="utf-8")

    year = _year(db)
    products, mixes = _catalog_for_match(db, year)
    payload = iimp.build_payload_from_extract(filename or safe, str(path), extract, products, mixes)
    payload["extract_path"] = str(full_path)

    batch = ImportBatch(
        filename=path.name,
        import_type=import_type,
        status="pending_review",
        row_count=len(payload.get("rows") or []),
        notes=f"Guided import · guessed {payload.get('guessed_type')}",
        payload_json=iimp.dump_payload(payload),
    )
    db.add(batch)
    log_activity(db, user.get("username"), "inputs_upload", f"{safe} → review")
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/inputs/upload", response_class=HTMLResponse)
def inputs_upload_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    pending = list(
        db.scalars(
            select(ImportBatch)
            .where(ImportBatch.import_type.in_(["inputs_guided", "spray_mix"]))
            .where(ImportBatch.status == "pending_review")
            .order_by(ImportBatch.id.desc())
            .limit(12)
        )
    )
    return templates.TemplateResponse(
        "input_upload.html",
        {
            "request": request,
            "user": user,
            "active": "input_upload",
            "farm_name": _farm(db),
            "year": _year(db),
            "pending": pending,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/inputs/upload")
async def inputs_upload_post(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    content = await file.read()
    if not content:
        return RedirectResponse("/inputs/upload?msg=Empty+file", status_code=303)
    batch = _start_guided_import(db, user=user, filename=file.filename or "upload.bin", content=content)
    return RedirectResponse(f"/inputs/upload/{batch.id}", status_code=303)


@router.get("/inputs/upload/{batch_id}", response_class=HTMLResponse)
def inputs_upload_review(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    payload = iimp.load_payload(batch)
    return templates.TemplateResponse(
        "input_upload_review.html",
        {
            "request": request,
            "user": user,
            "active": "input_upload",
            "farm_name": _farm(db),
            "year": _year(db),
            "batch": batch,
            "payload": payload,
            "type_labels": iimp.TYPE_LABELS,
            "field_keys": iimp.FIELD_KEYS,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/inputs/upload/{batch_id}/type")
def inputs_upload_type(
    request: Request,
    batch_id: int,
    confirmed_type: str = Form(...),
    db: Session = Depends(get_db),
):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    payload = iimp.load_payload(batch)
    ct = (confirmed_type or "").strip()
    if ct not in iimp.TYPE_LABELS:
        ct = "unknown"
    payload["confirmed_type"] = ct
    payload["step"] = "map"
    year = _year(db)
    products, mixes = _catalog_for_match(db, year)
    payload = iimp.rebuild_proposals_after_map(payload, products, mixes)
    batch.payload_json = iimp.dump_payload(payload)
    batch.notes = f"Type confirmed: {ct}"
    db.commit()
    return RedirectResponse(f"/inputs/upload/{batch_id}", status_code=303)


@router.post("/inputs/upload/{batch_id}/map")
async def inputs_upload_map(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    form = await request.form()
    payload = iimp.load_payload(batch)
    headers = list((payload.get("active_sheet") or {}).get("headers") or [])
    sheet_only = str(form.get("sheet_only") or "") == "1"

    # Optional sheet switch
    if "sheet_index" in form:
        try:
            si = int(str(form.get("sheet_index")))
        except ValueError:
            si = payload.get("sheet_index") or 0
        extract_path = payload.get("extract_path")
        if extract_path and Path(extract_path).exists():
            extract = json.loads(Path(extract_path).read_text(encoding="utf-8"))
            payload = iimp.apply_sheet_switch(payload, si, extract.get("sheets") or [])
            headers = list((payload.get("active_sheet") or {}).get("headers") or [])
            year = _year(db)
            products, mixes = _catalog_for_match(db, year)
            payload = iimp.rebuild_proposals_after_map(payload, products, mixes)

    if sheet_only:
        payload["step"] = "map"
        batch.payload_json = iimp.dump_payload(payload)
        batch.notes = "Sheet switched"
        db.commit()
        return RedirectResponse(f"/inputs/upload/{batch_id}", status_code=303)

    cmap: dict[str, int | None] = {k: None for k in iimp.FIELD_KEYS}
    for key in iimp.FIELD_KEYS:
        raw = str(form.get(f"map_{key}") or "").strip()
        if raw == "" or raw == "-1":
            cmap[key] = None
        else:
            try:
                idx = int(raw)
            except ValueError:
                idx = -1
            cmap[key] = idx if 0 <= idx < len(headers) else None
    payload["column_map"] = cmap
    payload["step"] = "rows"
    year = _year(db)
    products, mixes = _catalog_for_match(db, year)
    payload = iimp.rebuild_proposals_after_map(payload, products, mixes)
    batch.payload_json = iimp.dump_payload(payload)
    batch.row_count = len(payload.get("rows") or [])
    batch.notes = "Column map set — review rows"
    db.commit()
    return RedirectResponse(f"/inputs/upload/{batch_id}", status_code=303)


@router.post("/inputs/upload/{batch_id}/rows")
async def inputs_upload_rows(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    form = await request.form()
    payload = iimp.load_payload(batch)
    rows = list(payload.get("rows") or [])
    updated = []
    for row in rows:
        rid = str(row.get("id"))
        include = form.get(f"include_{rid}") == "1"
        action = str(form.get(f"action_{rid}") or row.get("action") or "skip").strip()
        row = dict(row)
        row["include"] = include
        row["action"] = action
        row["product_name"] = str(form.get(f"product_{rid}") or row.get("product_name") or "").strip()
        row["unit"] = str(form.get(f"unit_{rid}") or row.get("unit") or "gal").strip() or "gal"
        row["mix_name"] = str(form.get(f"mix_{rid}") or row.get("mix_name") or "").strip()
        row["timing"] = str(form.get(f"timing_{rid}") or row.get("timing") or "").strip()
        row["crop"] = str(form.get(f"crop_{rid}") or row.get("crop") or "").strip()
        row["rate_unit"] = str(form.get(f"rate_unit_{rid}") or row.get("rate_unit") or "").strip() or None
        row["cost_per_unit"] = iimp._to_float(form.get(f"price_{rid}"))
        if row["cost_per_unit"] is None:
            row["cost_per_unit"] = iimp._to_float(row.get("cost_per_unit"))
        row["rate"] = iimp._to_float(form.get(f"rate_{rid}"))
        if row["rate"] is None:
            row["rate"] = iimp._to_float(row.get("rate"))
        row["cost_per_acre"] = iimp._to_float(form.get(f"cpa_{rid}"))
        if row["cost_per_acre"] is None:
            row["cost_per_acre"] = iimp._to_float(row.get("cost_per_acre"))
        updated.append(row)
    payload["rows"] = updated
    batch.payload_json = iimp.dump_payload(payload)
    db.commit()

    go = str(form.get("go") or "save").strip()
    if go == "commit":
        return RedirectResponse(f"/inputs/upload/{batch_id}/commit", status_code=303)
    return RedirectResponse(f"/inputs/upload/{batch_id}?msg=Row+edits+saved", status_code=303)


@router.post("/inputs/upload/{batch_id}/commit")
@router.get("/inputs/upload/{batch_id}/commit")
def inputs_upload_commit(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    payload = iimp.load_payload(batch)
    year = _year(db)
    summary = iimp.commit_rows(
        db,
        payload,
        year,
        InputProduct=InputProduct,
        InputPurchase=InputPurchase,
        SprayMix=SprayMix,
        SprayMixLine=SprayMixLine,
    )
    payload["step"] = "done"
    payload["commit_summary"] = summary
    batch.payload_json = iimp.dump_payload(payload)
    batch.status = "imported"
    batch.row_count = summary.get("lines_written", 0) + summary.get("created_products", 0) + summary.get(
        "updated_products", 0
    )
    batch.notes = (
        f"Imported: {summary.get('created_products', 0)} new products, "
        f"{summary.get('updated_products', 0)} updated, "
        f"{summary.get('mixes_updated', 0)} mixes, "
        f"{summary.get('lines_written', 0)} mix lines"
    )
    log_activity(db, user.get("username"), "inputs_import_commit", batch.notes)
    db.commit()
    return RedirectResponse(f"/inputs/upload/{batch_id}?msg=Import+complete", status_code=303)


@router.post("/inputs/upload/{batch_id}/discard")
def inputs_upload_discard(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    payload = iimp.load_payload(batch)
    payload["step"] = "done"
    batch.payload_json = iimp.dump_payload(payload)
    batch.status = "discarded"
    batch.notes = "Discarded by user"
    db.commit()
    return RedirectResponse("/inputs/upload?msg=Import+discarded", status_code=303)


@router.post("/inputs/upload/{batch_id}/back")
def inputs_upload_back(
    request: Request,
    batch_id: int,
    to_step: str = Form("type"),
    db: Session = Depends(get_db),
):
    from app import input_import as iimp

    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        return RedirectResponse("/inputs/upload", status_code=303)
    payload = iimp.load_payload(batch)
    step = (to_step or "type").strip()
    if step not in ("type", "map", "rows"):
        step = "type"
    payload["step"] = step
    batch.payload_json = iimp.dump_payload(payload)
    db.commit()
    return RedirectResponse(f"/inputs/upload/{batch_id}", status_code=303)


# ---------- Purchases ----------
@router.get("/purchases", response_class=HTMLResponse)
def purchases_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "purchases")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    products = list(db.scalars(select(InputProduct).order_by(InputProduct.name)))
    if year:
        if year.year <= 2026:
            purchases = list(
                db.scalars(
                    select(InputPurchase)

                    .where(
                        (InputPurchase.crop_year_id == year.id)
                        | (InputPurchase.crop_year_id.is_(None))
                    )
                    .order_by(InputPurchase.id.desc())
                    .limit(50)
                )
            )
        else:
            purchases = list(
                db.scalars(
                    select(InputPurchase)
                    .where(InputPurchase.crop_year_id == year.id)
                    .order_by(InputPurchase.id.desc())
                    .limit(50)
                )
            )
    else:
        purchases = list(db.scalars(select(InputPurchase).order_by(InputPurchase.id.desc()).limit(50)))
    fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))) if year else []
    return templates.TemplateResponse(
        "purchases.html",
        {
            "request": request,
            "user": user,
            "active": "purchases",
            "farm_name": _farm(db),
            "year": year,
            "products": products,
            "purchases": purchases,
            "fields": fields,
            "today": date.today().isoformat(),
        },
    )


@router.post("/purchases/product")
def purchases_product(
    request: Request,
    name: str = Form(...),
    category: str = Form("chemical"),
    unit: str = Form("gal"),
    db: Session = Depends(get_db),
):
    user = _need(request, "purchases")
    if isinstance(user, RedirectResponse):
        return user
    db.add(InputProduct(name=name.strip(), category=category, unit=unit))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/purchases?error=product_name_taken", status_code=303)
    return RedirectResponse("/purchases", status_code=303)


@router.post("/purchases/buy")
def purchases_buy(
    request: Request,
    product_id: int = Form(...),
    purchase_date: str = Form(...),
    vendor: str = Form(""),
    quantity: str = Form("0"),
    total_cost: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _need(request, "purchases")
    if isinstance(user, RedirectResponse):
        return user
    qty = _f(quantity) or 0
    cost = _f(total_cost) or 0
    product = db.get(InputProduct, product_id)
    if not product:
        return RedirectResponse("/purchases", status_code=303)
    year = _year(db)
    # weighted average
    old_qty = product.on_hand or 0
    old_cost = (product.avg_unit_cost or 0) * old_qty
    new_qty = old_qty + qty
    product.on_hand = new_qty
    if new_qty > 0:
        product.avg_unit_cost = (old_cost + cost) / new_qty
    db.add(
        InputPurchase(
            product_id=product.id,
            crop_year_id=year.id if year else None,
            purchase_date=_d(purchase_date) or date.today(),
            vendor=vendor.strip() or None,
            quantity=qty,
            total_cost=cost,
        )
    )
    db.commit()
    return RedirectResponse("/purchases", status_code=303)


@router.post("/purchases/assign")
def purchases_assign(
    request: Request,
    product_id: int = Form(...),
    field_id: int = Form(...),
    quantity: str = Form("0"),
    assign_date: str = Form(""),
    confirm_duplicate: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "purchases")
    if isinstance(user, RedirectResponse):
        return user
    product = db.get(InputProduct, product_id)
    qty = _f(quantity) or 0
    if not product or qty <= 0:
        return redirect_flash(request, "/purchases", "Assign failed — check product and quantity.", "error")
    when = _d(assign_date) or date.today()
    pairs = [
        ("product_id", str(product_id)),
        ("field_id", str(field_id)),
        ("quantity", quantity),
        ("assign_date", when.isoformat()),
    ]
    block = confirm_if_duplicates(
        request,
        hits=find_assignment_dups(db, field_id, product_id, when),
        confirm_duplicate=confirm_duplicate,
        action="/purchases/assign",
        cancel_url="/purchases",
        heading="Possible duplicate product assign",
        form_pairs=pairs,
        user=user,
        farm_name=_farm(db),
        active="purchases",
    )
    if block:
        return block
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
    db.commit()
    field = db.get(Field, field_id)
    fname = field.name if field else f"#{field_id}"
    return redirect_flash(
        request,
        f"/fields/{field_id}/operations",
        f"Assigned {qty:g} {product.unit or 'units'} of {product.name} to {fname}.",
    )


@router.post("/purchases/return")
def purchases_return(
    request: Request,
    product_id: int = Form(...),
    quantity: str = Form("0"),
    field_id: str = Form(""),
    return_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "purchases")
    if isinstance(user, RedirectResponse):
        return user
    product = db.get(InputProduct, product_id)
    qty = _f(quantity) or 0
    if not product or qty <= 0:
        return RedirectResponse("/purchases", status_code=303)
    product.on_hand = (product.on_hand or 0) + qty
    fid = int(field_id) if field_id.isdigit() else None
    ret_date = _d(return_date) or date.today()
    unit_cost = product.avg_unit_cost or 0

    # Find most recent non-voided assignment for this product+field to link
    assignment_id: Optional[int] = None
    if fid:
        last_assign = db.scalar(
            select(FieldAssignment)
            .where(
                FieldAssignment.product_id == product.id,
                FieldAssignment.field_id == fid,
                FieldAssignment.voided == 0,
                FieldAssignment.quantity > 0,
            )
            .order_by(FieldAssignment.id.desc())
            .limit(1)
        )
        if last_assign:
            assignment_id = last_assign.id
            unit_cost = last_assign.unit_cost or unit_cost
        # Create a negative FieldAssignment to credit the field ledger
        db.add(
            FieldAssignment(
                product_id=product.id,
                field_id=fid,
                assign_date=ret_date,
                quantity=-qty,
                unit_cost=unit_cost,
                notes=f"Return credit: {notes.strip()}" if notes.strip() else "Return credit",
            )
        )

    pr = ProductReturn(
        product_id=product.id,
        field_id=fid,
        assignment_id=assignment_id,
        return_date=ret_date,
        quantity=qty,
        unit_cost=unit_cost,
        notes=notes.strip() or None,
    )
    db.add(pr)
    log_activity(db, user.get("username"), "product_return", f"{product.name} +{qty}")
    db.commit()
    return RedirectResponse("/purchases", status_code=303)


# ---------- Invoices ----------
@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "invoices")
    if isinstance(user, RedirectResponse):
        return user
    invoices = list(db.scalars(select(Invoice).order_by(Invoice.id.desc())))
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    return templates.TemplateResponse(
        "invoices.html",
        {
            "request": request,
            "user": user,
            "active": "invoices",
            "farm_name": _farm(db),
            "invoices": invoices,
            "parties": parties,
            "today": date.today().isoformat(),
        },
    )


@router.post("/invoices/create")
def invoices_create(
    request: Request,
    party_id: str = Form(""),
    invoice_date: str = Form(...),
    description: str = Form(...),
    quantity: str = Form("1"),
    rate: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _need(request, "invoices")
    if isinstance(user, RedirectResponse):
        return user
    qty = _f(quantity) or 1
    rate_v = _f(rate) or 0
    inv = Invoice(
        party_id=int(party_id) if party_id.isdigit() else None,
        invoice_date=_d(invoice_date) or date.today(),
        status="unpaid",
        total=qty * rate_v,
    )
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id,
            description=description.strip(),
            quantity=qty,
            rate=rate_v,
        )
    )
    db.commit()
    return RedirectResponse("/invoices", status_code=303)


@router.post("/invoices/{invoice_id}/paid")
def invoices_paid(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = _need(request, "invoices")
    if isinstance(user, RedirectResponse):
        return user
    inv = db.get(Invoice, invoice_id)
    if inv:
        inv.status = "paid"
        db.commit()
    return RedirectResponse("/invoices", status_code=303)


# ---------- Master Upload ----------
@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app.models import ImportMappingTemplate

    batches = list(db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(30)))
    templates_saved = list(db.scalars(select(ImportMappingTemplate).order_by(ImportMappingTemplate.name)))
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "user": user,
            "active": "upload",
            "farm_name": _farm(db),
            "batches": batches,
            "mapping_templates": templates_saved,
            "live_ready": panorama_api.is_live_configured(),
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/upload/file")
async def upload_file(
    request: Request,
    import_type: str = Form("generic"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app.importers import import_cargill_csv, import_fields_excel
    from app.activity import log_activity

    content = await file.read()
    if import_type in ("spray_mix", "inputs_guided"):
        batch = _start_guided_import(
            db,
            user=user,
            filename=file.filename or "upload.bin",
            content=content,
            import_type="inputs_guided",
        )
        return RedirectResponse(f"/inputs/upload/{batch.id}", status_code=303)

    if import_type == "planting_csv":
        batch = _start_planting_import(
            db,
            user=user,
            filename=file.filename or "planting.csv",
            content=content,
        )
        return RedirectResponse(f"/upload/planting/{batch.id}", status_code=303)

    upload_root = Path(__file__).resolve().parent.parent / "data" / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (file.filename or "upload.bin"))
    path = upload_root / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path.write_bytes(content)

    rows = 0
    notes = "Stored for later review."
    status = "stored"
    year = _year(db)

    if import_type == "panorama_file":
        panorama_api.save_uploaded_file(file.filename or safe, content)
        notes = "Copied to panorama upload folder."
        status = "stored"
    elif import_type == "fields_excel" and year and safe.lower().endswith((".xlsx", ".xlsm", ".xls")):
        created, updated, notes = import_fields_excel(db, path, year)
        rows = created + updated
        status = "imported"
    elif import_type == "cargill_csv" and year and safe.lower().endswith((".csv", ".txt")):
        c_made, tickets, notes = import_cargill_csv(db, content, year)
        rows = tickets
        status = "imported"
    elif safe.lower().endswith((".csv", ".txt")):
        try:
            text = content.decode("utf-8", errors="ignore")
            rows = max(0, len(text.splitlines()) - 1)
            notes = f"CSV stored with ~{rows} data rows. Choose Cargill recipe next time to auto-import."
        except Exception:  # noqa: BLE001
            pass

    db.add(
        ImportBatch(
            filename=path.name,
            import_type=import_type,
            status=status,
            row_count=rows,
            notes=notes,
        )
    )
    log_activity(db, user.get("username"), "upload", f"{import_type}: {safe}")
    db.commit()
    return RedirectResponse(f"/upload?msg={notes[:120]}", status_code=303)


# ---------- Guided planting / season-report CSV ----------
def _guess_crop_from_filename(name: str) -> str:
    n = (name or "").lower()
    if "soy" in n or "bean" in n:
        return "Soybeans"
    return "Corn"


def _start_planting_import(
    db: Session,
    *,
    user: dict,
    filename: str,
    content: bytes,
) -> ImportBatch:
    from app import planting_import as pimp
    from app.activity import log_activity

    safe = _safe_filename(filename)
    path = _upload_root() / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path.write_bytes(content)
    crop_guess = _guess_crop_from_filename(filename or safe)
    payload = pimp.build_payload(filename or safe, str(path), content, crop_guess=crop_guess)
    batch = ImportBatch(
        filename=path.name,
        import_type="planting_csv",
        status="pending_review",
        row_count=len(payload.get("rows") or []),
        notes="Guided planting import · map columns",
        payload_json=pimp.dump_payload(payload),
    )
    db.add(batch)
    log_activity(db, user.get("username"), "planting_upload", f"{safe} → review")
    db.commit()
    db.refresh(batch)
    return batch


def _planting_batch(db: Session, batch_id: int) -> ImportBatch | None:
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.import_type != "planting_csv":
        return None
    return batch


def _save_planting_payload(db: Session, batch: ImportBatch, payload: dict) -> None:
    from app import planting_import as pimp

    batch.payload_json = pimp.dump_payload(payload)
    batch.row_count = len(payload.get("proposals") or payload.get("rows") or [])
    db.commit()


@router.get("/upload/planting/{batch_id}", response_class=HTMLResponse)
def planting_upload_review(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import planting_import as pimp

    batch = _planting_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload?msg=Planting import not found", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    year = _year(db)
    hybrids = []
    if year:
        hybrids = list(
            db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id).order_by(Hybrid.name))
        )
    return templates.TemplateResponse(
        "planting_upload_review.html",
        {
            "request": request,
            "user": user,
            "active": "upload",
            "farm_name": _farm(db),
            "batch": batch,
            "payload": payload,
            "proposals": payload.get("proposals") or [],
            "hybrid_catalog": pimp.hybrid_catalog(hybrids),
            "field_keys": pimp.FIELD_KEYS,
            "field_labels": pimp.FIELD_LABELS,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/upload/planting/{batch_id}/map")
async def planting_upload_map(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import planting_import as pimp

    batch = _planting_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    form = await request.form()
    cmap: dict[str, int | None] = {}
    for key in pimp.FIELD_KEYS:
        raw = str(form.get(f"map_{key}") or "-1")
        try:
            idx = int(raw)
        except ValueError:
            idx = -1
        cmap[key] = None if idx < 0 else idx
    payload["column_map"] = cmap
    payload["crop_default"] = str(form.get("crop_default") or payload.get("crop_default") or "Corn")
    year = _year(db)
    fields = []
    hybrids = []
    if year:
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id)))
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
    payload["proposals"] = pimp.build_proposals(payload, fields, hybrids)
    payload["step"] = "rows"
    batch.notes = f"Mapped columns · {len(payload['proposals'])} planting rows"
    _save_planting_payload(db, batch, payload)
    return RedirectResponse(f"/upload/planting/{batch_id}", status_code=303)


@router.post("/upload/planting/{batch_id}/back")
def planting_upload_back(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import planting_import as pimp

    batch = _planting_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    payload["step"] = "map"
    _save_planting_payload(db, batch, payload)
    return RedirectResponse(f"/upload/planting/{batch_id}", status_code=303)


@router.post("/upload/planting/{batch_id}/discard")
def planting_upload_discard(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    batch = _planting_batch(db, batch_id)
    if batch:
        batch.status = "discarded"
        batch.notes = "Discarded by user"
        db.commit()
    return RedirectResponse("/upload?msg=Planting import discarded", status_code=303)


@router.post("/upload/planting/{batch_id}/commit")
async def planting_upload_commit(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import planting_import as pimp
    from app.activity import log_activity

    batch = _planting_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    year = _year(db)
    if not year:
        return RedirectResponse(
            f"/upload/planting/{batch_id}?err=No active crop year",
            status_code=303,
        )
    payload = pimp.load_payload(batch.payload_json)
    form = await request.form()
    proposals = payload.get("proposals") or []
    updated: list[dict] = []
    for row in proposals:
        rid = row.get("id")
        if rid is None:
            continue
        include = str(form.get(f"include_{rid}") or "") in ("1", "on", "true", "yes")
        create_field = str(form.get(f"create_{rid}") or "") in ("1", "on", "true", "yes")
        update_field_crop = str(form.get(f"update_field_crop_{rid}") or "") in ("1", "on", "true", "yes")
        crop = str(form.get(f"crop_{rid}") or row.get("crop") or "Corn").strip() or "Corn"
        matched_raw = str(form.get(f"matched_{rid}") or row.get("matched_field_id") or "").strip()
        matched_id = None
        if matched_raw:
            try:
                matched_id = int(matched_raw)
            except ValueError:
                matched_id = None
        op_date = str(form.get(f"date_{rid}") or "").strip() or None
        try:
            hcount = int(str(form.get(f"hybrid_count_{rid}") or "0"))
        except ValueError:
            hcount = 0
        hybrids = []
        for i in range(hcount):
            name = str(form.get(f"hybrid_{rid}_{i}") or "").strip()
            if not name:
                continue
            rate = str(form.get(f"rate_{rid}_{i}") or "").strip()
            units_raw = str(form.get(f"units_{rid}_{i}") or "").strip()
            units = None
            if units_raw:
                try:
                    units = float(units_raw.replace(",", ""))
                except ValueError:
                    units = None
            sel = str(form.get(f"hybrid_sel_{rid}_{i}") or "").strip()
            # sel: "new" | hybrid id
            if sel == "new" or sel == "":
                resolve = "new"
                selected_hybrid_id = None
            else:
                resolve = "pick"
                try:
                    selected_hybrid_id = int(sel)
                except ValueError:
                    resolve = "new"
                    selected_hybrid_id = None
            detail_mode = str(form.get(f"hybrid_detail_mode_{rid}_{i}") or "later").strip()
            detail_now = detail_mode == "now"
            name_edit = str(form.get(f"hybrid_name_edit_{rid}_{i}") or name).strip() or name
            details = {
                "detail_now": detail_now,
                "name": name_edit if detail_now else name,
                "brand": str(form.get(f"hybrid_brand_{rid}_{i}") or "").strip() if detail_now else "",
                "maturity": str(form.get(f"hybrid_maturity_{rid}_{i}") or "").strip() if detail_now else "",
                "traits": str(form.get(f"hybrid_traits_{rid}_{i}") or "").strip() if detail_now else "",
                "unit_label": str(form.get(f"hybrid_unit_{rid}_{i}") or "unit").strip() or "unit",
                "cost_per_unit": str(form.get(f"hybrid_cpu_{rid}_{i}") or "").strip() if detail_now else "",
                "cost_per_acre": str(form.get(f"hybrid_cpa_{rid}_{i}") or "").strip() if detail_now else "",
                "notes": str(form.get(f"hybrid_notes_{rid}_{i}") or "").strip() if detail_now else "",
            }
            hybrids.append(
                {
                    "name": name,
                    "rate": rate or None,
                    "units": units,
                    "resolve": resolve,
                    "selected_hybrid_id": selected_hybrid_id,
                    "details": details,
                }
            )
        updated.append(
            {
                **row,
                "include": include,
                "create_field": create_field if matched_id is None else False,
                "update_field_crop": update_field_crop,
                "matched_field_id": matched_id,
                "op_date": op_date,
                "crop": crop,
                "hybrids": hybrids,
            }
        )

    result = pimp.commit_proposals(db, crop_year_id=year.id, proposals=updated)
    payload["proposals"] = updated
    payload["result"] = result
    payload["step"] = "done"
    batch.status = "imported"
    batch.notes = (
        f"Planting import: {result['operations']} ops, "
        f"{result['hybrid_links']} hybrid links, "
        f"{result.get('hybrids_created', 0)} hybrids created, "
        f"{result['fields_created']} fields created"
    )
    batch.row_count = result["operations"]
    _save_planting_payload(db, batch, payload)
    log_activity(
        db,
        user.get("username"),
        "planting_import",
        batch.notes,
    )
    db.commit()
    return RedirectResponse(f"/upload/planting/{batch_id}", status_code=303)

