"""Additional farm modules: trials, grain, marketing, plans, purchases, panorama, insights."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
import csv
import io
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.flash import redirect_flash
from app.formutil import parse_date, parse_float
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
    FieldShare,
    FieldSprayMix,
    FertilizerProduct,
    FertilizerPurchase,
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
    PlantingRecord,
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
    TruckingRate,
    TruckLoad,
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

CONTRACT_STATUSES = ["open", "closed", "cancelled"]


def _safe_next(raw: str | None, default: str = "/risk/contracts") -> str:
    dest = (raw or "").strip() or default
    if not dest.startswith("/") or dest.startswith("//"):
        return default
    return dest


def _next_with_query(dest: str, **params: str) -> str:
    parts = [f"{k}={quote(str(v))}" for k, v in params.items() if v is not None and str(v) != ""]
    if not parts:
        return dest
    sep = "&" if "?" in dest else "?"
    return f"{dest}{sep}{'&'.join(parts)}"


def _as_form_list(vals):
    if vals is None:
        return []
    if isinstance(vals, (str, int, float)):
        return [vals]
    return list(vals)


def _ticket_lookups(db: Session) -> dict[str, list]:
    """Haulers, destinations, and $/bu freight rates from editable Lists (+ seed harvest)."""
    from app import lookups as lu

    haulers = set(lu.names(db, lu.HAULER))
    destinations = set(lu.names(db, lu.DESTINATION))
    rates = set(lu.freight_rates(db))

    # Keep harvesting so brand-new free-text history still appears until Lists catch up
    for row in db.scalars(select(GrainMovement).order_by(GrainMovement.id.desc()).limit(500)):
        if row.hauler and row.hauler.strip():
            haulers.add(row.hauler.strip())
        if row.destination and row.destination.strip():
            destinations.add(row.destination.strip())
        if row.freight_per_bu is not None:
            rates.add(round(float(row.freight_per_bu), 4))

    for row in db.scalars(select(TruckingRate)):
        if row.hauler and row.hauler.strip():
            haulers.add(row.hauler.strip())
        if row.destination and row.destination.strip() and row.destination.strip() != "—":
            destinations.add(row.destination.strip())
        if row.rate_per_bu is not None:
            rates.add(round(float(row.rate_per_bu), 4))

    return {
        "haulers": sorted(haulers, key=str.lower),
        "destinations": sorted(destinations, key=str.lower),
        "freight_rates": sorted(rates),
    }


def _pick_listed_or_new(listed: str, new: str) -> str | None:
    """Prefer a newly typed value; otherwise use the dropdown selection."""
    typed = (new or "").strip()
    if typed:
        return typed
    chosen = (listed or "").strip()
    return chosen or None


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
    return parse_float(value, default=default)


def _d(value: str) -> date | None:
    return parse_date(value)


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


def _start_planting_import(
    db: Session,
    *,
    user: dict,
    filename: str,
    content: bytes,
) -> ImportBatch:
    from app import planting_import as pimp
    from app.activity import log_activity

    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (filename or "planting.csv"))
    upload_root = Path(__file__).resolve().parent.parent / "data" / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    path = upload_root / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path.write_bytes(content)
    # Also keep a copy in panorama uploads
    panorama_api.save_uploaded_file(filename or safe, content)

    parsed = pimp.parse_csv_bytes(content, filename=filename or safe)
    year = _year(db)
    fields = []
    hybrids = []
    if year:
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)))
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
    rows = pimp.match_fields(parsed.get("rows") or [], fields)
    rows = pimp.match_hybrids(rows, hybrids)
    payload = {
        "filename": filename or safe,
        "path": str(path),
        "headers": parsed.get("headers") or [],
        "column_map": parsed.get("column_map") or {},
        "crop": parsed.get("crop") or "Corn",
        "has_field_column": bool(parsed.get("has_field_column")),
        "has_client_column": bool(parsed.get("has_client_column")),
        "is_seasonal_inputs": bool(parsed.get("is_seasonal_inputs")),
        "parse_ok": bool(parsed.get("ok")),
        "parse_error": parsed.get("error"),
        "rows": rows,
        "summary": pimp.summarize(rows),
    }
    batch = ImportBatch(
        filename=path.name,
        import_type="panorama_planting",
        status="pending_review" if parsed.get("ok") else "error",
        row_count=len(rows),
        notes=(
            parsed.get("error")
            or f"Seasonal inputs · {len(rows)} hybrid rows · review then import"
        ),
        payload_json=pimp.dump_payload(payload),
    )
    db.add(batch)
    db.add(
        PanoramaSyncLog(
            action="planting_upload",
            detail=f"{path.name}: {len(rows)} hybrid rows",
            ok=1 if parsed.get("ok") else 0,
        )
    )
    log_activity(db, user.get("username"), "planting_upload", path.name)
    db.commit()
    db.refresh(batch)
    return batch


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
    name = file.filename or "panorama.bin"
    lower = name.lower()

    # Seasonal Inputs / planting CSV → guided review
    if lower.endswith((".csv", ".txt")):
        from app import planting_import as pimp

        # Peek headers — if it looks like planting data, open the wizard
        try:
            text = content.decode("utf-8-sig", errors="replace")
            first = next(csv.reader(io.StringIO(text)), [])
        except Exception:  # noqa: BLE001
            first = []
        if pimp.is_seasonal_inputs_headers([str(h) for h in first]) or (
            any("hybrid" in str(h).lower() for h in first)
            and any("unit" in str(h).lower() or "area" in str(h).lower() for h in first)
        ):
            batch = _start_planting_import(db, user=user, filename=name, content=content)
            return RedirectResponse(f"/panorama/planting/{batch.id}", status_code=303)

    path = panorama_api.save_uploaded_file(name, content)
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


@router.get("/panorama/planting/{batch_id}", response_class=HTMLResponse)
def panorama_planting_review(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import planting_import as pimp

    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.import_type != "panorama_planting":
        return RedirectResponse("/panorama", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    year = _year(db)
    fields = []
    if year:
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)))
    return templates.TemplateResponse(
        "panorama_planting.html",
        {
            "request": request,
            "user": user,
            "active": "panorama",
            "farm_name": _farm(db),
            "batch": batch,
            "payload": payload,
            "rows": payload.get("rows") or [],
            "summary": payload.get("summary") or pimp.summarize(payload.get("rows") or []),
            "fields": fields,
            "year": year,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("err") or payload.get("parse_error"),
        },
    )


@router.post("/panorama/planting/{batch_id}/save")
def panorama_planting_save(
    request: Request,
    batch_id: int,
    row_idx: list[int] = Form(default=[]),
    include: list[str] = Form(default=[]),
    field_id: list[str] = Form(default=[]),
    hybrid_name: list[str] = Form(default=[]),
    acres: list[str] = Form(default=[]),
    units: list[str] = Form(default=[]),
    population: list[str] = Form(default=[]),
    client_name: list[str] = Form(default=[]),
    field_name: list[str] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    from app import planting_import as pimp

    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.import_type != "panorama_planting":
        return RedirectResponse("/panorama", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    rows = list(payload.get("rows") or [])
    include_set = {int(x) for x in include if str(x).isdigit()}

    def as_list(vals):
        if vals is None:
            return []
        if isinstance(vals, (str, int, float)):
            return [vals]
        return list(vals)

    idxs = [int(x) for x in as_list(row_idx)]
    fields_l = as_list(field_id)
    hybrids_l = as_list(hybrid_name)
    acres_l = as_list(acres)
    units_l = as_list(units)
    pop_l = as_list(population)
    clients_l = as_list(client_name)
    field_names_l = as_list(field_name)
    crops_l = as_list(crop)

    for i, idx in enumerate(idxs):
        if idx < 0 or idx >= len(rows):
            continue
        r = dict(rows[idx])
        r["include"] = idx in include_set
        fid_raw = str(fields_l[i] if i < len(fields_l) else "").strip()
        r["field_id"] = int(fid_raw) if fid_raw.isdigit() else None
        if r["field_id"]:
            r["field_match"] = "manual"
        name = str(hybrids_l[i] if i < len(hybrids_l) else r.get("hybrid_name") or "").strip()
        if name:
            r["hybrid_name"] = name
        r["acres"] = _f(str(acres_l[i] if i < len(acres_l) else ""), None)
        r["units"] = _f(str(units_l[i] if i < len(units_l) else ""), None)
        r["population"] = _f(str(pop_l[i] if i < len(pop_l) else ""), None)
        r["client_name"] = str(clients_l[i] if i < len(clients_l) else r.get("client_name") or "").strip() or None
        r["field_name"] = str(field_names_l[i] if i < len(field_names_l) else r.get("field_name") or "").strip() or None
        crop_v = str(crops_l[i] if i < len(crops_l) else r.get("crop") or "Corn").strip() or "Corn"
        r["crop"] = crop_v
        rows[idx] = r

    year = _year(db)
    fields = []
    hybrids = []
    if year:
        fields = list(db.scalars(select(Field).where(Field.crop_year_id == year.id)))
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
    # Re-tag hybrid match after edits
    rows = pimp.match_hybrids(rows, hybrids)
    # Keep manual field_ids
    payload["rows"] = rows
    payload["summary"] = pimp.summarize(rows)
    batch.payload_json = pimp.dump_payload(payload)
    batch.row_count = payload["summary"]["row_count"]
    db.commit()
    return RedirectResponse(f"/panorama/planting/{batch_id}?msg=Saved+review", status_code=303)


@router.post("/panorama/planting/{batch_id}/commit")
def panorama_planting_commit(request: Request, batch_id: int, db: Session = Depends(get_db)):
    from app import planting_import as pimp
    from app.activity import log_activity

    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.import_type != "panorama_planting":
        return RedirectResponse("/panorama", status_code=303)
    year = _year(db)
    if not year:
        return RedirectResponse(f"/panorama/planting/{batch_id}?err=No+active+crop+year", status_code=303)
    payload = pimp.load_payload(batch.payload_json)
    rows = [r for r in (payload.get("rows") or []) if r.get("include")]
    if not rows:
        return RedirectResponse(f"/panorama/planting/{batch_id}?err=No+rows+selected", status_code=303)

    # Cache hybrids by (crop, name lower)
    existing = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
    hybrid_by: dict[tuple[str, str], Hybrid] = {
        ((h.crop or "Corn"), (h.name or "").strip().lower()): h for h in existing if h.name
    }

    created_hybrids = 0
    assigned = 0
    records = 0
    for r in rows:
        name = (r.get("hybrid_name") or "").strip()
        if not name:
            continue
        crop = (r.get("crop") or payload.get("crop") or "Corn").strip() or "Corn"
        key = (crop, name.lower())
        hybrid = hybrid_by.get(key)
        if not hybrid:
            hybrid = Hybrid(
                crop_year_id=year.id,
                crop=crop,
                name=name,
                unit_label="unit",
                notes="Imported from Panorama Seasonal Inputs",
            )
            db.add(hybrid)
            db.flush()
            hybrid_by[key] = hybrid
            created_hybrids += 1

        acres = r.get("acres")
        units = r.get("units")
        population = r.get("population")
        client = (r.get("client_name") or "").strip() or None
        farm = (r.get("farm_name") or "").strip() or None
        field_name = (r.get("field_name") or "").strip() or None
        field_id = r.get("field_id")
        if field_id and not db.get(Field, int(field_id)):
            field_id = None

        rate = None
        if population is not None:
            rate = f"{int(round(float(population))):,} seeds/ac"

        db.add(
            PlantingRecord(
                crop_year_id=year.id,
                import_batch_id=batch.id,
                field_id=int(field_id) if field_id else None,
                hybrid_id=hybrid.id,
                crop=crop,
                hybrid_name=name,
                client_name=client,
                farm_name=farm,
                field_name=field_name,
                acres=float(acres) if acres is not None else None,
                units=float(units) if units is not None else None,
                population=float(population) if population is not None else None,
                source="panorama_seasonal_inputs",
            )
        )
        records += 1

        if field_id:
            # Upsert FieldHybrid for this field+hybrid
            fh = db.scalar(
                select(FieldHybrid).where(
                    FieldHybrid.field_id == int(field_id),
                    FieldHybrid.hybrid_id == hybrid.id,
                )
            )
            if fh is None:
                fh = FieldHybrid(field_id=int(field_id), hybrid_id=hybrid.id)
                db.add(fh)
            fh.acres = float(acres) if acres is not None else fh.acres
            fh.units = float(units) if units is not None else fh.units
            fh.population = float(population) if population is not None else fh.population
            fh.client_name = client or fh.client_name
            if rate:
                fh.rate = rate
            # Acres from units × seeds/unit ÷ population (import acres often missing/0)
            from app.field_ledger import planted_acres_from_units

            calc_ac = planted_acres_from_units(
                fh.units,
                fh.population,
                crop=hybrid.crop,
                brand=hybrid.brand,
            )
            if calc_ac is not None:
                fh.acres = calc_ac
            assigned += 1

    batch.status = "imported"
    batch.row_count = records
    batch.notes = (
        f"Imported {records} planting rows · {created_hybrids} new hybrids · "
        f"{assigned} field assigns"
    )
    db.add(
        PanoramaSyncLog(
            action="planting_import",
            detail=batch.notes,
            ok=1,
        )
    )
    log_activity(db, user.get("username"), "planting_import", batch.notes)
    # Auto machinery cost for corn fields that are now fully planted
    from app.models import Field as FieldModel
    from app.op_cost_ensure import ensure_all_completed_corn_planting_costs

    if year:
        planted_fields = list(
            db.scalars(select(FieldModel).where(FieldModel.crop_year_id == year.id))
        )
        ensure_all_completed_corn_planting_costs(db, planted_fields)
    db.commit()
    return RedirectResponse(
        f"/panorama/planted?msg=Imported+{records}+rows",
        status_code=303,
    )


@router.get("/panorama/planted", response_class=HTMLResponse)
def panorama_planted(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "panorama")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    rows = []
    if year:
        rows = list(
            db.scalars(
                select(PlantingRecord)
                .where(PlantingRecord.crop_year_id == year.id)
                .order_by(PlantingRecord.crop, PlantingRecord.hybrid_name, PlantingRecord.id)
            )
        )
    field_name = {}
    if year:
        for f in db.scalars(select(Field).where(Field.crop_year_id == year.id)):
            field_name[f.id] = f.name
    return templates.TemplateResponse(
        "panorama_planted.html",
        {
            "request": request,
            "user": user,
            "active": "panorama",
            "farm_name": _farm(db),
            "year": year,
            "rows": rows,
            "field_name": field_name,
            "message": request.query_params.get("msg"),
            "total_acres": round(sum((r.acres or 0) for r in rows), 1),
            "total_units": round(sum((r.units or 0) for r in rows), 2),
        },
    )


# ---------- Grain bins ----------
def _split_bushels_by_pct(total: float, owners: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Split total bu by ownership %; last owner gets remainder so the sum matches."""
    if not owners:
        return [("Me", round(total, 1))]
    if len(owners) == 1:
        return [(owners[0][0], round(total, 1))]
    out: list[tuple[str, float]] = []
    used = 0.0
    for i, (name, pct) in enumerate(owners):
        if i == len(owners) - 1:
            amt = round(float(total) - used, 1)
        else:
            amt = round(float(total) * float(pct) / 100.0, 1)
            used += amt
        out.append((name, amt))
    return out


def _bin_ownership_pcts(db: Session, bin_row: GrainBin) -> list[tuple[str, float]]:
    """
    Ownership shares for a bin.
    Prefer explicit BinShare.share_pct values that total ~100%.
    If the bin is farmed-with a party and % aren't set, default to Me 50% / partner 50%.
    """
    shares = list(db.scalars(select(BinShare).where(BinShare.bin_id == bin_row.id)))
    named: list[tuple[str, float]] = []
    for s in shares:
        name = (s.owner_name or "").strip()
        if not name:
            continue
        if s.share_pct is not None and float(s.share_pct) > 0:
            named.append((name, float(s.share_pct)))
    total = sum(p for _, p in named)
    if named and abs(total - 100.0) <= 1.5:
        return named

    partner = None
    if bin_row.with_party_id:
        party = getattr(bin_row, "with_party", None) or db.get(Party, bin_row.with_party_id)
        if party and (party.name or "").strip():
            partner = party.name.strip()
    if partner:
        return [("Me", 50.0), (partner, 50.0)]
    return [("Me", 100.0)]


def _ensure_bin_ownership_shares(db: Session, bin_row: GrainBin) -> list[tuple[str, float]]:
    """Create/update BinShare rows so Me + farmed-with party have the right share %."""
    pcts = _bin_ownership_pcts(db, bin_row)
    existing = {
        (s.owner_name or "").strip().lower(): s
        for s in db.scalars(select(BinShare).where(BinShare.bin_id == bin_row.id))
    }
    keep: set[str] = set()
    for name, pct in pcts:
        key = name.lower()
        keep.add(key)
        row = existing.get(key)
        if not row:
            row = BinShare(bin_id=bin_row.id, owner_name=name, bushels=0.0, share_pct=pct)
            db.add(row)
            db.flush()
        else:
            row.owner_name = name
            row.share_pct = pct
    # Drop empty extra owners that are no longer part of ownership
    for key, row in existing.items():
        if key not in keep and (row.bushels or 0) <= 0 and key != "me":
            db.delete(row)
    return pcts


def _set_bin_total_by_ownership(
    db: Session, bin_row: GrainBin, total: float
) -> list[tuple[str, float]]:
    """Set bin on-hand to a total, splitting bushels by ownership %. Carry keeps running if Me stays nonempty."""
    pcts = _ensure_bin_ownership_shares(db, bin_row)
    splits = _split_bushels_by_pct(max(0.0, float(total or 0)), pcts)
    pct_by = {n.lower(): p for n, p in pcts}
    for name, bu in splits:
        share = db.scalar(
            select(BinShare).where(BinShare.bin_id == bin_row.id, BinShare.owner_name == name)
        )
        if not share:
            share = BinShare(
                bin_id=bin_row.id,
                owner_name=name,
                bushels=0.0,
                share_pct=pct_by.get(name.lower()),
            )
            db.add(share)
            db.flush()
        old = float(share.bushels or 0)
        share.bushels = bu
        if share.share_pct is None:
            share.share_pct = pct_by.get(name.lower())
        _touch_me_carry_start(share, old, bu)
    return splits


def _fill_bin_by_ownership(db: Session, bin_row: GrainBin, cap: float) -> list[tuple[str, float]]:
    """Fill bin to capacity, splitting bushels by ownership %."""
    return _set_bin_total_by_ownership(db, bin_row, cap)


def _add_to_bin_by_ownership(
    db: Session, bin_row: GrainBin, add_bu: float
) -> tuple[float, list[tuple[str, float]]]:
    """Add bushels to current total, then re-split the new total by ownership %."""
    current = sum(
        float(s.bushels or 0)
        for s in db.scalars(select(BinShare).where(BinShare.bin_id == bin_row.id))
    )
    new_total = max(0.0, current + float(add_bu or 0))
    return new_total, _set_bin_total_by_ownership(db, bin_row, new_total)


def _bin_cards(db: Session):
    bins = list(
        db.scalars(
            select(GrainBin)
            .options(joinedload(GrainBin.with_party))
            .order_by(GrainBin.crop, GrainBin.name)
        ).unique()
    )
    shares = list(db.scalars(select(BinShare)))
    by_bin: dict[int, list] = {}
    for s in shares:
        by_bin.setdefault(s.bin_id, []).append(s)
    bin_cards = []
    for b in bins:
        share_list = by_bin.get(b.id, [])
        ownership = _bin_ownership_pcts(db, b)
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
        # Display shares in ownership order with % labels
        bu_by = {(s.owner_name or "").strip().lower(): (s.bushels or 0) for s in share_list}
        display_shares = []
        for name, sp in ownership:
            display_shares.append(
                {
                    "owner_name": name,
                    "share_pct": sp,
                    "bushels": bu_by.get(name.lower(), 0),
                }
            )
        for s in share_list:
            key = (s.owner_name or "").strip().lower()
            if key and not any(d["owner_name"].lower() == key for d in display_shares):
                display_shares.append(
                    {
                        "owner_name": s.owner_name,
                        "share_pct": s.share_pct,
                        "bushels": s.bushels or 0,
                    }
                )
        bin_cards.append(
            {
                "bin": b,
                "shares": share_list,
                "display_shares": display_shares,
                "ownership": ownership,
                "is_shared": len(ownership) > 1,
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
def bins_page(request: Request, view: Optional[str] = None, db: Session = Depends(get_db)):
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
                .options(joinedload(GrainContract.with_party))
                .order_by(GrainContract.id.desc())
            ).unique()
        )
    fields = []
    field_shares: dict[str, list] = {}
    if year:
        fields = list(
            db.scalars(
                select(Field)
                .where(Field.crop_year_id == year.id)
                .options(joinedload(Field.shares).joinedload(FieldShare.party))
                .order_by(Field.name)
            ).unique()
        )
        for f in fields:
            rows = []
            for s in list(f.shares or []):
                name = s.display_name
                if not name:
                    continue
                rows.append(
                    {
                        "name": name,
                        "share_pct": float(s.share_pct or 0),
                        "is_me": bool(s.is_me),
                        "party_id": int(s.party_id) if s.party_id else None,
                    }
                )
            if not rows and f.ownership_mode == "on_shares" and f.my_share_pct is not None:
                rows = [
                    {
                        "name": "Me",
                        "share_pct": float(f.my_share_pct or 0),
                        "is_me": True,
                        "party_id": None,
                    },
                ]
                rem = max(0.0, 100.0 - float(f.my_share_pct or 0))
                if rem > 0 and f.party:
                    rows.append(
                        {
                            "name": f.party.name,
                            "share_pct": rem,
                            "is_me": False,
                            "party_id": int(f.party_id) if f.party_id else None,
                        }
                    )
            if not rows:
                rows = [
                    {
                        "name": "Me",
                        "share_pct": 100.0,
                        "is_me": True,
                        "party_id": None,
                    }
                ]
            field_shares[str(f.id)] = rows
    moves = list(db.scalars(select(GrainMovement).order_by(GrainMovement.id.desc()).limit(40)))
    notes = list(db.scalars(select(BinConditionNote).order_by(BinConditionNote.id.desc()).limit(40)))
    bin_name = {b.id: b.name for b in bins}
    field_name = {f.id: f.name for f in fields}
    field_crops = {str(f.id): f.crop for f in fields}
    ticket_lookups = _ticket_lookups(db)
    bin_owners = {}
    for b in bins:
        pcts = _bin_ownership_pcts(db, b)
        share_rows = by_bin.get(b.id, [])
        bu_by = {(s.owner_name or "").strip().lower(): (s.bushels or 0) for s in share_rows}
        bin_owners[str(b.id)] = [
            {
                "name": name,
                "share_pct": pct,
                "bushels": bu_by.get(name.lower(), 0),
            }
            for name, pct in pcts
        ]
    bin_crops = {str(b.id): b.crop for b in bins}
    owner_names: set[str] = {"Me"}
    for entries in bin_owners.values():
        for e in entries:
            if e["name"]:
                owner_names.add(e["name"])
    for m in moves:
        if m.owner_name and m.owner_name.strip():
            owner_names.add(m.owner_name.strip())
    ticket_owners = sorted(owner_names, key=lambda n: (n.lower() != "me", n.lower()))
    bin_name_map = {str(b.id): b.name for b in bins}
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    # Same Me + parties list as field share partners / bins "farmed with".
    preferred = {"partner", "landlord"}
    preferred_list = [p for p in parties if (p.party_type or "").lower() in preferred]
    farm_with_parties = preferred_list if preferred_list else list(parties)
    rank = {"partner": 0, "landlord": 1, "customer": 2, "buyer": 3, "other": 4}
    farm_with_parties = sorted(
        farm_with_parties,
        key=lambda p: (rank.get((p.party_type or "other").lower(), 9), (p.name or "").lower()),
    )
    ownership_parties = [{"name": "Me", "is_me": True, "party_id": None}] + [
        {
            "name": p.name,
            "is_me": False,
            "party_id": int(p.id),
            "party_type": p.party_type or "",
        }
        for p in farm_with_parties
        if (p.name or "").strip()
    ]
    bin_with_party = {
        str(b.id): {
            "party_id": int(b.with_party_id) if b.with_party_id else None,
            "name": b.with_party.name if b.with_party else None,
        }
        for b in bins
    }
    with_party_preselect = None
    added = (request.query_params.get("with_party_added") or "").strip()
    if added.isdigit():
        with_party_preselect = int(added)

    view_mode = (view or "overview").strip().lower()
    if view_mode not in ("overview", "sheet", "ticket", "ticket_scale", "ticket_other", "new"):
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

    # Carry on Me's share only — accrued since grain entered, refreshed on every page open.
    carry_rates = {"Corn": None, "Soybeans": None}
    board = cme_quotes.board_from_settings(settings)
    me_bu_by_crop = {"Corn": 0.0, "Soybeans": 0.0}
    for card in bin_cards:
        crop = card["bin"].crop
        if crop in me_bu_by_crop:
            me_bu_by_crop[crop] += float(card["me_bu"] or 0)
    fallback = {
        "Corn": settings.corn_price_assumption if settings else None,
        "Soybeans": settings.soy_price_assumption if settings else None,
    }
    snap = mkt.build_carry_snapshot(settings, board, me_bu_by_crop, market_fallback=fallback)
    for crop in ("Corn", "Soybeans"):
        carry_rates[crop] = (snap["by_crop"].get(crop) or {}).get("rate")

    today = date.today()
    carry_accrued_total = 0.0
    carry_mo_me_total = 0.0
    dirty_carry = False
    for card in bin_cards:
        crop = card["bin"].crop
        rate = carry_rates.get(crop)
        me_bu = float(card["me_bu"] or 0)
        me_share = next(
            (s for s in card["shares"] if (s.owner_name or "").lower() == "me"),
            None,
        )
        frozen = float(me_share.carry_accrued or 0) if me_share is not None else 0.0
        period_base = float(me_share.carry_period_base or 0) if me_share is not None else 0.0

        if me_bu > 0:
            # Counting: refresh accrued from period base + current inventory days
            if me_share is not None and me_share.carry_start_date is None:
                me_share.carry_start_date = today
                me_share.carry_period_base = frozen
                period_base = frozen
                dirty_carry = True
            start = me_share.carry_start_date if me_share is not None else today
            days = max(0, (today - start).days) + 1 if start else 0
            period = mkt.carry_accrued(me_bu, rate, days) or 0.0
            accrued = round(period_base + period, 2)
            if me_share is not None and abs(float(me_share.carry_accrued or 0) - accrued) > 0.001:
                me_share.carry_accrued = accrued
                dirty_carry = True
            mo_me = mkt.carry_farm_mo(me_bu, rate)
            card["carry_start"] = start
            card["carry_days"] = days
            card["carry_accrued"] = accrued
            card["carry_counting"] = True
            card["carry_mo"] = accrued
            card["carry_mo_me"] = mo_me
            carry_accrued_total += accrued
            if mo_me is not None:
                carry_mo_me_total += mo_me
        else:
            # Empty: stop counting, keep last accrued total for analysis
            if me_share is not None and me_share.carry_start_date is not None:
                me_share.carry_start_date = None
                dirty_carry = True
            card["carry_start"] = None
            card["carry_days"] = 0
            card["carry_accrued"] = frozen if frozen > 0 else None
            card["carry_counting"] = False
            card["carry_mo"] = frozen if frozen > 0 else None
            card["carry_mo_me"] = None
            if frozen > 0:
                carry_accrued_total += frozen

    if dirty_carry:
        db.commit()

    stats["carry_corn_mo"] = (snap["by_crop"].get("Corn") or {}).get("farm_mo")
    stats["carry_soy_mo"] = (snap["by_crop"].get("Soybeans") or {}).get("farm_mo")
    stats["carry_total_mo"] = round(carry_mo_me_total, 2)
    stats["carry_accrued_total"] = round(carry_accrued_total, 2)

    active = {
        "sheet": "bins_sheet",
        "ticket": "bins_ticket",
        "ticket_scale": "bins_ticket_scale",
        "ticket_other": "bins_ticket",
        "new": "bins_new",
    }.get(view_mode, "bins")
    template = {
        "sheet": "bins_sheet.html",
        "ticket": "bins_ticket.html",
        "ticket_scale": "bins_ticket_scale.html",
        "ticket_other": "bins_ticket_other.html",
        "new": "bins_new.html",
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
        "field_shares": field_shares,
        "field_crops": field_crops,
        "moves": moves,
        "notes": notes,
        "bin_name": bin_name,
        "field_name": field_name,
        "today": date.today().isoformat(),
        "view": view_mode,
        "stats": stats,
        "saved": request.query_params.get("saved"),
        "settings": settings,
        "carry_rates": carry_rates,
        "ticket_haulers": ticket_lookups["haulers"],
        "ticket_destinations": ticket_lookups["destinations"],
        "ticket_freight_rates": ticket_lookups["freight_rates"],
        "bin_owners": bin_owners,
        "bin_crops": bin_crops,
        "bin_name_map": bin_name_map,
        "ticket_owners": ticket_owners,
        "parties": parties,
        "farm_with_parties": farm_with_parties,
        "ownership_parties": ownership_parties,
        "bin_with_party": bin_with_party,
        "with_party_preselect": with_party_preselect,
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
    return bins_page(request, view="ticket", db=db)


@router.get("/bins/ticket/scale", response_class=HTMLResponse)
def bins_ticket_scale_page(request: Request, db: Session = Depends(get_db)):
    return bins_page(request, view="ticket_scale", db=db)


@router.get("/bins/ticket/other", response_class=HTMLResponse)
def bins_ticket_other_page(request: Request, db: Session = Depends(get_db)):
    return bins_page(request, view="ticket_other", db=db)


@router.get("/bins/new", response_class=HTMLResponse)
def bins_new_page(request: Request, db: Session = Depends(get_db)):
    return bins_page(request, view="new", db=db)


@router.get("/bins/ticket/new/owner", response_class=HTMLResponse)
def bins_ticket_new_owner(request: Request, bin_id: Optional[int] = None, db: Session = Depends(get_db)):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bins, _cards, _groups, by_bin = _bin_cards(db)
    bin_owners = {
        str(bid): [
            {"name": s.owner_name, "share_pct": s.share_pct, "bushels": s.bushels or 0}
            for s in shares
            if (s.owner_name or "").strip()
        ]
        for bid, shares in by_bin.items()
    }
    return templates.TemplateResponse(
        "bins_ticket_new_owner.html",
        {
            "request": request,
            "user": user,
            "active": "bins_ticket",
            "farm_name": _farm(db),
            "bins": bins,
            "bin_owners": bin_owners,
            "prefill_bin_id": bin_id,
        },
    )


@router.get("/bins/ticket/new/hauler", response_class=HTMLResponse)
def bins_ticket_new_hauler(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "bins_ticket",
            "farm_name": _farm(db),
            "page_kind": "hauler",
            "page_title": "Add trucking company",
            "page_lede": "Enter the company name. It will appear in the Trucking company dropdown on scale tickets.",
            "form_action": "/bins/ticket/new/hauler",
            "error": None,
            "ticket_haulers": [],
            "ticket_destinations": [],
        },
    )


@router.post("/bins/ticket/new/hauler")
def bins_ticket_new_hauler_save(
    request: Request,
    hauler: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    name = hauler.strip()
    if not name:
        return RedirectResponse("/bins/ticket/new/hauler", status_code=303)
    from app import lookups as lu

    lu.ensure(db, lu.HAULER, name, notes=(notes.strip() or None))
    # Persist via rate table so trucking page stays consistent
    existing = db.scalar(select(TruckingRate).where(TruckingRate.hauler == name).limit(1))
    if not existing:
        db.add(
            TruckingRate(
                destination="—",
                hauler=name,
                notes=(notes.strip() or None),
            )
        )
        log_activity(db, user.get("username"), "hauler_add", name)
    db.commit()
    return RedirectResponse(f"/bins/ticket/scale?hauler_added={quote(name)}", status_code=303)


@router.get("/bins/ticket/new/destination", response_class=HTMLResponse)
def bins_ticket_new_destination(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "bins_ticket",
            "farm_name": _farm(db),
            "page_kind": "destination",
            "page_title": "Add destination",
            "page_lede": "Enter the elevator / buyer destination. It will appear in the Destination dropdown on scale tickets.",
            "form_action": "/bins/ticket/new/destination",
            "error": None,
            "ticket_haulers": [],
            "ticket_destinations": [],
        },
    )


@router.post("/bins/ticket/new/destination")
def bins_ticket_new_destination_save(
    request: Request,
    destination: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    name = destination.strip()
    if not name:
        return RedirectResponse("/bins/ticket/new/destination", status_code=303)
    from app import lookups as lu

    lu.ensure(db, lu.DESTINATION, name, notes=(notes.strip() or None))
    existing = db.scalar(select(TruckingRate).where(TruckingRate.destination == name).limit(1))
    if not existing:
        db.add(
            TruckingRate(
                destination=name,
                notes=(notes.strip() or None),
            )
        )
        log_activity(db, user.get("username"), "destination_add", name)
    db.commit()
    return RedirectResponse(f"/bins/ticket/scale?destination_added={quote(name)}", status_code=303)


@router.get("/bins/ticket/new/freight", response_class=HTMLResponse)
def bins_ticket_new_freight(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    lookups = _ticket_lookups(db)
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "bins_ticket",
            "farm_name": _farm(db),
            "page_kind": "freight",
            "page_title": "Add freight rate",
            "page_lede": "Enter $/bu and optional company / destination so rates stay consistent for tickets and reports.",
            "form_action": "/bins/ticket/new/freight",
            "error": None,
            "ticket_haulers": lookups["haulers"],
            "ticket_destinations": lookups["destinations"],
        },
    )


@router.post("/bins/ticket/new/freight")
def bins_ticket_new_freight_save(
    request: Request,
    rate_per_bu: str = Form(...),
    hauler: str = Form(""),
    hauler_new: str = Form(""),
    destination: str = Form(""),
    destination_new: str = Form(""),
    rate_per_load: str = Form(""),
    miles: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    rate = _f(rate_per_bu, None)
    if rate is None:
        return RedirectResponse("/bins/ticket/new/freight", status_code=303)
    from app import lookups as lu

    trucker = _pick_listed_or_new(hauler, hauler_new)
    dest = _pick_listed_or_new(destination, destination_new) or "—"
    if trucker:
        lu.ensure(db, lu.HAULER, trucker)
    if dest and dest != "—":
        lu.ensure(db, lu.DESTINATION, dest)
    lu.ensure_freight(db, rate)
    db.add(
        TruckingRate(
            destination=dest,
            hauler=trucker,
            rate_per_bu=rate,
            rate_per_load=_f(rate_per_load, None) if (rate_per_load or "").strip() else None,
            miles=_f(miles, None) if (miles or "").strip() else None,
            notes=(notes.strip() or None),
        )
    )
    log_activity(db, user.get("username"), "freight_rate_add", f"${rate}/bu")
    db.commit()
    return RedirectResponse(f"/bins/ticket/scale?freight_added={rate}", status_code=303)


@router.post("/bins/share/add")
def bins_share_add(
    request: Request,
    bin_id: int = Form(...),
    owner_name: str = Form(...),
    share_pct: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add a new owner/share on a bin (from scale-ticket Add New page)."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.get(GrainBin, bin_id)
    name = (owner_name or "").strip()
    if not bin_row or not name:
        return RedirectResponse("/bins/ticket/new/owner?error=bin", status_code=303)

    pct = _f(share_pct, None) if (share_pct or "").strip() else None
    existing = db.scalar(
        select(BinShare).where(BinShare.bin_id == bin_id, BinShare.owner_name == name)
    )
    if existing:
        if pct is not None:
            existing.share_pct = pct
        db.commit()
        return RedirectResponse(
            f"/bins/ticket/scale?share_added={quote(name)}&bin_id={bin_id}",
            status_code=303,
        )

    db.add(
        BinShare(
            bin_id=bin_id,
            owner_name=name,
            bushels=0.0,
            share_pct=pct,
        )
    )
    from app import lookups as lu

    lu.ensure(db, lu.GRAIN_OWNER, name)
    log_activity(db, user.get("username"), "bin_share_add", f"{name} on {bin_row.name}")
    db.commit()
    return RedirectResponse(
        f"/bins/ticket/scale?share_added={quote(name)}&bin_id={bin_id}",
        status_code=303,
    )


@router.post("/bins/sheet/save")
def bins_sheet_save(
    request: Request,
    bin_id: list[int] = Form(default=[]),
    name: list[str] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    with_party_id: list[str] = Form(default=[]),
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
    withs = as_list(with_party_id)
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
        wid_raw = str(withs[i] if i < len(withs) else (bin_row.with_party_id or "")).strip()
        new_wid = int(wid_raw) if wid_raw.isdigit() else None
        if new_wid and not db.get(Party, new_wid):
            new_wid = None
        cap_raw = str(caps[i] if i < len(caps) else "")
        new_cap = _f(cap_raw, None) if cap_raw.strip() else None
        note_raw = str(notes_l[i] if i < len(notes_l) else (bin_row.notes or "")).strip() or None
        on_raw = str(ons[i] if i < len(ons) else "")

        changed = (
            bin_row.name != new_name
            or bin_row.crop != new_crop
            or (bin_row.with_party_id or None) != new_wid
            or (bin_row.capacity_bu != new_cap)
            or (bin_row.notes or None) != note_raw
        )

        shares = list(db.scalars(select(BinShare).where(BinShare.bin_id == bid)))
        old_total = sum(float(s.bushels or 0) for s in shares)
        if on_raw.strip() != "":
            amount = _f(on_raw)
            if amount is not None and abs(float(old_total) - float(amount)) > 1e-9:
                changed = True

        if not changed:
            continue

        bin_row.name = new_name
        bin_row.crop = new_crop
        bin_row.with_party_id = new_wid
        bin_row.capacity_bu = new_cap
        bin_row.notes = note_raw
        if new_wid:
            db.refresh(bin_row, attribute_names=["with_party"])
        _ensure_bin_ownership_shares(db, bin_row)
        if on_raw.strip() != "":
            amount = _f(on_raw)
            if amount is not None:
                _set_bin_total_by_ownership(db, bin_row, amount)
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
    with_party_id: str = Form(""),
    capacity_bu: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    wid = int(with_party_id) if with_party_id.strip().isdigit() else None
    if wid and not db.get(Party, wid):
        wid = None
    bin_row = GrainBin(
        name=name.strip(),
        crop=crop,
        with_party_id=wid,
        capacity_bu=_f(capacity_bu, None) if capacity_bu.strip() else None,
    )
    db.add(bin_row)
    try:
        db.flush()
        if wid:
            db.refresh(bin_row, attribute_names=["with_party"])
        _ensure_bin_ownership_shares(db, bin_row)
        me = db.scalar(
            select(BinShare).where(BinShare.bin_id == bin_row.id, BinShare.owner_name == "Me")
        )
        if not me:
            db.add(BinShare(bin_id=bin_row.id, owner_name="Me", bushels=0, share_pct=100.0))
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/bins/new?error=bin_name_taken", status_code=303)
    return RedirectResponse("/bins?saved=bin", status_code=303)


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
    from app import lookups as lu

    if destination.strip():
        lu.ensure(db, lu.DESTINATION, destination.strip())
    if owner:
        lu.ensure(db, lu.GRAIN_OWNER, owner)
    if crop:
        lu.ensure(db, lu.CROP, crop)
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

    if move_type == "fill" and bid:
        _adjust_bin_share(db, bid, owner, bu)
    elif move_type == "delivery" and bid:
        _adjust_bin_share(db, bid, owner, -bu)
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
        _adjust_bin_share(db, bid, owner, -bu)
        _adjust_bin_share(db, to_bid, owner, bu)
    log_activity(db, user.get("username"), f"grain_{move_type}", f"{bu} bu {crop}")
    db.commit()
    return RedirectResponse("/bins/ticket/other?saved=1", status_code=303)


def _touch_me_carry_start(
    share: BinShare,
    old_bu: float,
    new_bu: float,
    when: date | None = None,
) -> None:
    """Start Me carry clock when inventory goes empty→nonempty; stop (don't clear $) when empty."""
    if (share.owner_name or "").strip().lower() != "me":
        return
    when = when or date.today()
    old_v = float(old_bu or 0)
    new_v = float(new_bu or 0)
    if old_v <= 0 and new_v > 0:
        share.carry_start_date = when
        # New counting period builds on top of whatever was already accrued
        share.carry_period_base = float(share.carry_accrued or 0)
    elif new_v <= 0:
        # Stop the clock — keep carry_accrued for analysis
        share.carry_start_date = None


def _adjust_bin_share(db: Session, bin_pk: int, owner_nm: str, delta: float) -> None:
    share = db.scalar(
        select(BinShare).where(BinShare.bin_id == bin_pk, BinShare.owner_name == owner_nm)
    )
    if not share:
        share = BinShare(bin_id=bin_pk, owner_name=owner_nm, bushels=0)
        db.add(share)
        db.flush()
    old = float(share.bushels or 0)
    new = old + float(delta)
    share.bushels = new
    _touch_me_carry_start(share, old, new)


def _reverse_grain_movement(
    db: Session,
    move: GrainMovement,
    *,
    reverse_truck: bool = True,
) -> None:
    """Undo inventory / contract effects of a movement before deleting the row."""
    bu = float(move.net_bu or 0)
    owner = (move.owner_name or "Me").strip() or "Me"
    mt = (move.move_type or "").strip().lower()
    if mt == "fill" and move.bin_id:
        _adjust_bin_share(db, move.bin_id, owner, -bu)
    elif mt == "delivery" and move.bin_id:
        _adjust_bin_share(db, move.bin_id, owner, bu)
        if move.contract_id and bu:
            contract = db.get(GrainContract, move.contract_id)
            if contract:
                contract.delivered_bu = max(0.0, (contract.delivered_bu or 0) - bu)
    elif mt == "elevator":
        if move.contract_id and bu:
            contract = db.get(GrainContract, move.contract_id)
            if contract:
                contract.delivered_bu = max(0.0, (contract.delivered_bu or 0) - bu)
    elif mt == "transfer" and move.bin_id and move.to_bin_id:
        _adjust_bin_share(db, move.bin_id, owner, bu)
        _adjust_bin_share(db, move.to_bin_id, owner, -bu)

    # Best-effort: remove matching truck loads for this ticket (once per void)
    if reverse_truck and move.ticket_number and move.move_date:
        loads = list(
            db.scalars(
                select(TruckLoad).where(
                    TruckLoad.ticket_number == move.ticket_number,
                    TruckLoad.load_date == move.move_date,
                )
            )
        )
        for load in loads:
            db.delete(load)


@router.post("/bins/moves/{move_id}/delete")
def bins_move_delete(
    move_id: int,
    request: Request,
    next: str = Form("/bins/ticket/scale"),
    db: Session = Depends(get_db),
):
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    move = db.get(GrainMovement, move_id)
    dest = (next or "/bins/ticket/scale").strip() or "/bins/ticket/scale"
    if not dest.startswith("/") or dest.startswith("//"):
        dest = "/bins/ticket/scale"
    if not move:
        return RedirectResponse(dest, status_code=303)

    # Voiding a scale ticket reverses every chunk with the same ticket # + date
    # (primary + overflow/spot), then deletes those rows.
    if move.ticket_number and move.move_date:
        siblings = list(
            db.scalars(
                select(GrainMovement).where(
                    GrainMovement.ticket_number == move.ticket_number,
                    GrainMovement.move_date == move.move_date,
                )
            )
        )
    else:
        siblings = [move]

    label = f"#{move.id} {move.move_type} {move.net_bu} bu {move.crop}"
    if move.ticket_number:
        label += f" ticket {move.ticket_number}"
        if len(siblings) > 1:
            label += f" ({len(siblings)} chunks)"

    for m in siblings:
        _reverse_grain_movement(db, m, reverse_truck=False)
        db.delete(m)
    if move.ticket_number and move.move_date:
        for load in list(
            db.scalars(
                select(TruckLoad).where(
                    TruckLoad.ticket_number == move.ticket_number,
                    TruckLoad.load_date == move.move_date,
                )
            )
        ):
            db.delete(load)

    log_activity(db, user.get("username"), "grain_move_delete", label)
    db.commit()
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}deleted=1", status_code=303)


@router.post("/bins/{bin_id}/fill")
def bins_fill(bin_id: int, request: Request, db: Session = Depends(get_db)):
    """Fill bin to capacity, splitting bushels by ownership % (Me / farmed-with party)."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.scalar(
        select(GrainBin)
        .where(GrainBin.id == bin_id)
        .options(joinedload(GrainBin.with_party))
    )
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)
    cap = float(bin_row.capacity_bu) if bin_row.capacity_bu is not None else None
    if cap is None or cap <= 0:
        return redirect_flash(
            request,
            "/bins",
            "Set a capacity on the bin before filling it.",
            "error",
        )

    splits = _fill_bin_by_ownership(db, bin_row, cap)
    split_txt = " · ".join(f"{n} {bu:g}" for n, bu in splits)
    log_activity(
        db,
        user.get("username"),
        "bin_fill",
        f"{bin_row.name}: filled to {cap:g} bu ({split_txt})",
    )
    db.commit()
    return redirect_flash(
        request,
        "/bins",
        f"Filled “{bin_row.name}” to {cap:g} bu — {split_txt}. Carry ticker keeps running.",
    )


@router.post("/bins/{bin_id}/set-total")
def bins_set_total(
    bin_id: int,
    request: Request,
    bushels: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set bin total on-hand; ownership % splits are calculated from that total."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.scalar(
        select(GrainBin)
        .where(GrainBin.id == bin_id)
        .options(joinedload(GrainBin.with_party))
    )
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)
    if not bushels.strip():
        return redirect_flash(
            request, "/bins", "Enter a bin total (bushels) before Set total.", "error"
        )
    total = _f(bushels)
    if total is None or total < 0:
        return redirect_flash(request, "/bins", "Enter a valid bushel total.", "error")

    splits = _set_bin_total_by_ownership(db, bin_row, total)
    split_txt = " · ".join(f"{n} {bu:g}" for n, bu in splits)
    log_activity(
        db,
        user.get("username"),
        "bin_set_total",
        f"{bin_row.name}: set total {total:g} bu ({split_txt})",
    )
    db.commit()
    return redirect_flash(
        request,
        "/bins",
        f"Set “{bin_row.name}” to {total:g} bu total — {split_txt}. Carry ticker keeps running.",
    )


@router.post("/bins/{bin_id}/add")
def bins_add(
    bin_id: int,
    request: Request,
    bushels: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add bushels to the bin total; then re-split by ownership %. Carry keeps running."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.scalar(
        select(GrainBin)
        .where(GrainBin.id == bin_id)
        .options(joinedload(GrainBin.with_party))
    )
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)
    if not bushels.strip():
        return redirect_flash(
            request, "/bins", "Enter how many bushels to add before Add to bin.", "error"
        )
    add_bu = _f(bushels)
    if add_bu is None or add_bu == 0:
        return redirect_flash(request, "/bins", "Enter a non-zero amount to add.", "error")

    new_total, splits = _add_to_bin_by_ownership(db, bin_row, add_bu)
    split_txt = " · ".join(f"{n} {bu:g}" for n, bu in splits)
    log_activity(
        db,
        user.get("username"),
        "bin_add",
        f"{bin_row.name}: added {add_bu:g} → {new_total:g} bu ({split_txt})",
    )
    db.commit()
    return redirect_flash(
        request,
        "/bins",
        f"Added {add_bu:g} bu to “{bin_row.name}” → {new_total:g} total — {split_txt}. "
        "Carry ticker keeps running.",
    )


@router.post("/bins/{bin_id}/empty")
def bins_empty(
    bin_id: int,
    request: Request,
    carry_action: str = Form("keep"),
    db: Session = Depends(get_db),
):
    """Zero all on-hand shares. Optionally keep or reset the Me carry ticker."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.get(GrainBin, bin_id)
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)

    action = (carry_action or "keep").strip().lower()
    if action not in ("keep", "reset"):
        action = "keep"

    shares = list(db.scalars(select(BinShare).where(BinShare.bin_id == bin_id)))
    if not shares:
        shares = [BinShare(bin_id=bin_id, owner_name="Me", bushels=0)]
        db.add(shares[0])
        db.flush()

    before_total = sum(float(s.bushels or 0) for s in shares)
    me = next((s for s in shares if (s.owner_name or "").lower() == "me"), None)

    # Finalize Me carry up to today before emptying (so "keep" has a current total)
    if action == "keep" and me is not None and float(me.bushels or 0) > 0:
        settings = db.scalar(select(AppSettings).limit(1))
        board = cme_quotes.board_from_settings(settings)
        me_bu = float(me.bushels or 0)
        snap = mkt.build_carry_snapshot(
            settings,
            board,
            {bin_row.crop: me_bu},
            market_fallback={
                "Corn": settings.corn_price_assumption if settings else None,
                "Soybeans": settings.soy_price_assumption if settings else None,
            },
        )
        rate = (snap["by_crop"].get(bin_row.crop) or {}).get("rate")
        start = me.carry_start_date or date.today()
        days = max(0, (date.today() - start).days) + 1
        period = mkt.carry_accrued(me_bu, rate, days) or 0.0
        base = float(me.carry_period_base or 0)
        me.carry_accrued = round(base + period, 2)

    before_carry = float(me.carry_accrued or 0) if me is not None else 0.0

    for share in shares:
        old = float(share.bushels or 0)
        share.bushels = 0.0
        _touch_me_carry_start(share, old, 0.0)
        if (share.owner_name or "").lower() == "me":
            if action == "reset":
                share.carry_accrued = 0.0
                share.carry_period_base = 0.0
                share.carry_start_date = None
            # keep: leave carry_accrued; clock already stopped

    log_activity(
        db,
        user.get("username"),
        "bin_empty",
        f"{bin_row.name}: emptied {before_total:g} bu · carry {'reset' if action == 'reset' else f'kept ${before_carry:.2f}'}",
    )
    db.commit()
    if action == "reset":
        msg = f"Emptied “{bin_row.name}”. Carry ticker reset to $0."
    else:
        msg = (
            f"Emptied “{bin_row.name}”. Carry ticker kept at ${before_carry:,.2f} "
            "(continues from there the next time you fill)."
        )
    return redirect_flash(request, "/bins", msg)


@router.post("/bins/{bin_id}/update")
def bins_update(
    bin_id: int,
    request: Request,
    name: str = Form(...),
    crop: str = Form("Corn"),
    with_party_id: str = Form(""),
    capacity_bu: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename bin, change crop/capacity/with-party. Inventory uses Set total / Add / Fill."""
    user = _need(request, "grain")
    if isinstance(user, RedirectResponse):
        return user
    bin_row = db.get(GrainBin, bin_id)
    if not bin_row:
        return RedirectResponse("/bins", status_code=303)
    new_name = name.strip()
    if not new_name:
        return RedirectResponse("/bins?error=bin_name_taken", status_code=303)
    wid = int(with_party_id) if with_party_id.strip().isdigit() else None
    if wid and not db.get(Party, wid):
        wid = None
    bin_row.name = new_name
    bin_row.crop = crop
    bin_row.with_party_id = wid
    bin_row.capacity_bu = _f(capacity_bu, None) if capacity_bu.strip() else None
    db.flush()
    if wid:
        db.refresh(bin_row, attribute_names=["with_party"])
    _ensure_bin_ownership_shares(db, bin_row)
    try:
        log_activity(db, user.get("username"), "bin_update", new_name)
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/bins?error=bin_name_taken", status_code=303)
    return RedirectResponse("/bins", status_code=303)


def _contract_bu_left(contract: GrainContract | None) -> float:
    if not contract:
        return 0.0
    return max(0.0, float(contract.bushels or 0) - float(contract.delivered_bu or 0))


def _mark_contract_if_filled(contract: GrainContract) -> None:
    left = _contract_bu_left(contract)
    if left <= 0.05 and (contract.status or "").lower() == "open":
        contract.status = "closed"


def _plan_scale_ticket_contracts(
    db: Session,
    *,
    year: CropYear | None,
    crop: str,
    amount: float,
    primary_id: int | None,
    overflow_mode: str,
    overflow_contract_id: int | None,
    spot_futures: float | None,
    spot_basis: float | None,
    buyer: str | None,
    when: date,
    ticket: str | None,
) -> list[tuple[int | None, float, str]]:
    """Split ownership delivery across primary contract, overflow contract, and/or spot.

    Returns [(contract_id|None, bu, kind)] where kind is primary|overflow|spot|open.
    """
    remaining = round(max(0.0, float(amount or 0)), 1)
    if remaining <= 0:
        return []

    apps: list[tuple[int | None, float, str]] = []
    mode = (overflow_mode or "none").strip().lower()

    def take_from(contract: GrainContract | None, kind: str) -> None:
        nonlocal remaining
        if not contract or remaining <= 0.05:
            return
        left = _contract_bu_left(contract)
        if left <= 0.05:
            return
        take = round(min(remaining, left), 1)
        if take <= 0:
            return
        apps.append((contract.id, take, kind))
        remaining = round(remaining - take, 1)

    if primary_id:
        take_from(db.get(GrainContract, primary_id), "primary")

    if remaining > 0.05:
        if mode == "contract" and overflow_contract_id and overflow_contract_id != primary_id:
            take_from(db.get(GrainContract, overflow_contract_id), "overflow")
        if remaining > 0.05 and mode == "spot":
            if year is None:
                apps.append((None, remaining, "open"))
                remaining = 0.0
            else:
                fut = float(spot_futures) if spot_futures is not None else None
                bas = float(spot_basis) if spot_basis is not None else 0.0
                cash = round(fut + bas, 4) if fut is not None else None
                spot = GrainContract(
                    crop_year_id=year.id,
                    crop=crop,
                    contract_type="cash",
                    buyer=buyer,
                    bushels=remaining,
                    delivered_bu=0.0,
                    futures_price=fut,
                    basis=bas if fut is not None else None,
                    cash_price=cash,
                    status="open",
                    notes=(
                        f"spot from scale ticket{(' ' + ticket) if ticket else ''}"
                        f" · {when.isoformat()}"
                    ),
                )
                db.add(spot)
                db.flush()
                apps.append((spot.id, remaining, "spot"))
                remaining = 0.0
        elif remaining > 0.05 and mode == "contract":
            # Overflow contract missing/full — require spot (or another contract) explicitly
            raise ValueError("contract_overflow")
        elif remaining > 0.05 and mode in ("", "none"):
            if primary_id:
                # Primary filled (or leftover) with no overflow disposition — never silent open
                raise ValueError("contract_overflow")
            apps.append((None, remaining, "open"))
            remaining = 0.0
        elif remaining > 0.05:
            apps.append((None, remaining, "open"))
            remaining = 0.0

    if not apps and amount > 0:
        apps.append((primary_id, round(amount, 1), "primary" if primary_id else "open"))
    return apps


def _apply_contract_deliveries(
    db: Session,
    apps: list[tuple[int | None, float, str]],
) -> None:
    for cid, bu, _kind in apps:
        if not cid or bu <= 0:
            continue
        contract = db.get(GrainContract, cid)
        if not contract:
            continue
        contract.delivered_bu = round(float(contract.delivered_bu or 0) + float(bu), 1)
        _mark_contract_if_filled(contract)


@router.post("/bins/ticket")
async def bins_ticket(
    request: Request,
    db: Session = Depends(get_db),
):
    """Scale ticket: pull from a bin or field; ownership share sizes contract delivery."""
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
    destination_new = str(form.get("destination_new") or "")
    hauler = str(form.get("hauler") or "")
    hauler_new = str(form.get("hauler_new") or "")
    freight_per_bu = str(form.get("freight_per_bu") or "")
    freight_per_bu_new = str(form.get("freight_per_bu_new") or "")
    contract_id = str(form.get("contract_id") or "")
    overflow_mode = str(form.get("overflow_mode") or "none")
    overflow_contract_id = str(form.get("overflow_contract_id") or "")
    spot_futures_raw = str(form.get("spot_futures") or "")
    spot_basis_raw = str(form.get("spot_basis") or "")
    owner_name = str(form.get("owner_name") or "Me")
    owner_bu_raw = str(form.get("owner_bu") or "")
    owner_share_pct_raw = str(form.get("owner_share_pct") or "")
    source_mode = str(form.get("source_mode") or "bin").strip().lower()
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
    overflow_cid = int(overflow_contract_id) if overflow_contract_id.isdigit() else None
    spot_futures = _f(spot_futures_raw, None) if spot_futures_raw.strip() else None
    spot_basis = _f(spot_basis_raw, None) if spot_basis_raw.strip() else None
    fid = int(field_id) if field_id.isdigit() else None
    f_bu = _f(field_bu) or 0
    ticket = ticket_number.strip() or None
    dest = _pick_listed_or_new(destination, destination_new)
    trucker = _pick_listed_or_new(hauler, hauler_new)
    freight_raw = _pick_listed_or_new(freight_per_bu, freight_per_bu_new)
    freight = _f(freight_raw, None) if freight_raw else None
    note = notes.strip() or None
    share_pct = _f(owner_share_pct_raw, None) if owner_share_pct_raw.strip() else None
    year = _year(db)

    from app import lookups as lu

    if dest:
        lu.ensure(db, lu.DESTINATION, dest)
    if trucker:
        lu.ensure(db, lu.HAULER, trucker)
    if freight is not None:
        lu.ensure_freight(db, freight)
    if owner:
        lu.ensure(db, lu.GRAIN_OWNER, owner)
    if crop:
        lu.ensure(db, lu.CROP, crop)

    bin_ids = [str(v) for v in form.getlist("alloc_bin_id")]
    bu_vals = [str(v) for v in form.getlist("alloc_bu")]
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

    # Ownership share of the ticket (auto-calc from %). Applied to contracts.
    owner_bu = _f(owner_bu_raw) or 0
    if owner_bu <= 0 and ticket_bu > 0:
        if share_pct is not None and share_pct > 0:
            owner_bu = round(ticket_bu * (share_pct / 100.0), 1)
        else:
            owner_bu = ticket_bu

    shared_bin_splits: list[tuple[int, str, float, bool]] = []
    # (bin_id, owner_name, amt, applies_to_contract)

    if source_mode == "field":
        allocations = []
        if fid and f_bu <= 0:
            f_bu = owner_bu if owner_bu > 0 else ticket_bu
        elif fid and owner_bu > 0:
            f_bu = owner_bu
    else:
        fid = None
        f_bu = 0
        # Shared bin: full ticket leaves the bin, split across owners by %.
        # Only the selected owner's portion hits the contract.
        if ticket_bu > 0 and len(bare_bins) == 1 and not allocations:
            bid = bare_bins[0]
            bin_row = db.scalar(
                select(GrainBin)
                .where(GrainBin.id == bid)
                .options(joinedload(GrainBin.with_party))
            )
            if bin_row:
                pcts = _ensure_bin_ownership_shares(db, bin_row)
                if len(pcts) > 1:
                    splits = _split_bushels_by_pct(ticket_bu, pcts)
                    sel_pct = next(
                        (p for n, p in pcts if n.lower() == owner.lower()),
                        None,
                    )
                    if sel_pct is None and share_pct is not None and share_pct > 0:
                        sel_pct = share_pct
                    if sel_pct is None:
                        sel_pct = 100.0
                    owner_bu = round(ticket_bu * float(sel_pct) / 100.0, 1)
                    share_pct = float(sel_pct)
                    for name, amt in splits:
                        if amt <= 0:
                            continue
                        is_sel = name.lower() == owner.lower()
                        shared_bin_splits.append((bid, name, amt, is_sel))
                    allocations = []
                else:
                    allocations = [(bid, ticket_bu)]
                    owner_bu = ticket_bu
            else:
                allocations = [(bid, owner_bu if owner_bu > 0 else ticket_bu)]
        elif owner_bu > 0 and not allocations and len(bare_bins) == 1:
            allocations = [(bare_bins[0], owner_bu)]
        elif owner_bu > 0 and len(allocations) == 1:
            allocations = [(allocations[0][0], owner_bu)]
        elif ticket_bu > 0 and not allocations and len(bare_bins) == 1:
            allocations = [(bare_bins[0], owner_bu if owner_bu > 0 else ticket_bu)]

    if ticket_bu > 0 and fid and f_bu <= 0 and not allocations and not shared_bin_splits:
        f_bu = owner_bu if owner_bu > 0 else ticket_bu

    sourced = (
        sum(a[1] for a in allocations)
        + sum(a[2] for a in shared_bin_splits)
        + (f_bu if fid and f_bu > 0 else 0)
    )
    if ticket_bu <= 0 and sourced > 0:
        ticket_bu = sourced
    if ticket_bu <= 0 or sourced <= 0:
        return RedirectResponse("/bins/ticket/scale?error=ticket_source", status_code=303)
    # Shared bin: sourced should equal full ticket; selected owner_bu may be less
    if shared_bin_splits:
        if abs(sourced - ticket_bu) > 0.51:
            return RedirectResponse("/bins/ticket/scale?error=ticket_mismatch", status_code=303)
    elif source_mode != "field" and abs(sourced - ticket_bu) > 0.51 and (
        share_pct is None or share_pct >= 99.5
    ):
        return RedirectResponse("/bins/ticket/scale?error=ticket_mismatch", status_code=303)

    # If primary contract would overflow, require overflow disposition
    if cid and owner_bu > 0:
        primary = db.get(GrainContract, cid)
        left = _contract_bu_left(primary)
        if owner_bu > left + 0.05:
            mode = (overflow_mode or "none").strip().lower()
            if mode not in ("contract", "spot"):
                return RedirectResponse("/bins/ticket/scale?error=contract_overflow", status_code=303)
            if mode == "contract" and not overflow_cid:
                return RedirectResponse("/bins/ticket/scale?error=contract_overflow", status_code=303)
            if mode == "spot" and spot_futures is None:
                return RedirectResponse("/bins/ticket/scale?error=spot_price", status_code=303)

    def adjust(bin_pk: int, owner_nm: str, delta: float):
        _adjust_bin_share(db, bin_pk, owner_nm, delta)

    try:
        contract_apps = _plan_scale_ticket_contracts(
            db,
            year=year,
            crop=crop,
            amount=owner_bu if owner_bu > 0 else 0.0,
            primary_id=cid,
            overflow_mode=overflow_mode if cid else "none",
            overflow_contract_id=overflow_cid,
            spot_futures=spot_futures,
            spot_basis=spot_basis if spot_basis is not None else 0.0,
            buyer=dest,
            when=when,
            ticket=ticket,
        )
    except ValueError as exc:
        if str(exc) == "contract_overflow":
            return RedirectResponse("/bins/ticket/scale?error=contract_overflow", status_code=303)
        if str(exc) == "spot_price":
            return RedirectResponse("/bins/ticket/scale?error=spot_price", status_code=303)
        raise
    # If no contract selected, still one open bucket for movement linking
    if not contract_apps and owner_bu > 0:
        contract_apps = [(None, round(owner_bu, 1), "open")]

    share_note = ""
    if shared_bin_splits:
        parts = [f"{n} {a:g}" for _, n, a, _ in shared_bin_splits]
        share_note = f" · ticket {ticket_bu:g} bu split ({' / '.join(parts)}); contract {owner} {owner_bu:g}"
    elif share_pct is not None and share_pct < 99.5 and ticket_bu > 0:
        share_note = f" · share {share_pct:g}% of ticket {ticket_bu:g} bu"
    if contract_apps and len(contract_apps) > 1:
        bits = []
        for app_cid, app_bu, kind in contract_apps:
            if kind == "spot":
                bits.append(f"spot {app_bu:g}")
            elif app_cid:
                bits.append(f"#{app_cid} {app_bu:g}")
            else:
                bits.append(f"open {app_bu:g}")
        share_note += f" · applied {' + '.join(bits)}"
    if note and share_note:
        note = f"{note}{share_note}"
    elif share_note:
        note = share_note.strip(" ·")

    delivered_total = 0.0
    truck_bu = 0.0

    def _emit_owner_moves(
        *,
        bin_id: int | None,
        field_id: int | None,
        move_type: str,
        amt: float,
        wet_val: float | None,
        apply_contracts: bool,
    ) -> None:
        nonlocal delivered_total, truck_bu
        if amt <= 0:
            return
        if apply_contracts and contract_apps:
            # Scale app chunks to this amt (normally amt == owner_bu)
            scale = amt / owner_bu if owner_bu > 0 else 1.0
            chunks = [
                (app_cid, round(app_bu * scale, 1), kind)
                for app_cid, app_bu, kind in contract_apps
            ]
            # Fix rounding drift on last chunk
            drift = round(amt - sum(c[1] for c in chunks), 1)
            if chunks and abs(drift) >= 0.05:
                last = chunks[-1]
                chunks[-1] = (last[0], round(last[1] + drift, 1), last[2])
            for app_cid, app_bu, _kind in chunks:
                if app_bu <= 0:
                    continue
                db.add(
                    GrainMovement(
                        bin_id=bin_id,
                        to_bin_id=None,
                        field_id=field_id,
                        contract_id=app_cid,
                        move_date=when,
                        move_type=move_type,
                        crop=crop,
                        owner_name=owner,
                        wet_bu=wet_val,
                        moisture=moist,
                        net_bu=app_bu,
                        ticket_number=ticket,
                        destination=dest,
                        hauler=trucker,
                        freight_per_bu=freight,
                        notes=note,
                    )
                )
                if bin_id:
                    adjust(bin_id, owner, -app_bu)
                delivered_total += app_bu
                truck_bu += app_bu
        else:
            db.add(
                GrainMovement(
                    bin_id=bin_id,
                    to_bin_id=None,
                    field_id=field_id,
                    contract_id=None,
                    move_date=when,
                    move_type=move_type,
                    crop=crop,
                    owner_name=owner if apply_contracts else owner,  # overwritten below for partners
                    wet_bu=wet_val,
                    moisture=moist,
                    net_bu=amt,
                    ticket_number=ticket,
                    destination=dest,
                    hauler=trucker,
                    freight_per_bu=freight,
                    notes=note,
                )
            )
            if bin_id:
                adjust(bin_id, owner, -amt)
            truck_bu += amt

    for bid, name, amt, to_contract in shared_bin_splits:
        if to_contract:
            # Split selected owner across contract applications
            if contract_apps:
                scale = amt / owner_bu if owner_bu > 0 else 1.0
                chunks = [
                    (app_cid, round(app_bu * scale, 1), kind)
                    for app_cid, app_bu, kind in contract_apps
                ]
                drift = round(amt - sum(c[1] for c in chunks), 1)
                if chunks and abs(drift) >= 0.05:
                    last = chunks[-1]
                    chunks[-1] = (last[0], round(last[1] + drift, 1), last[2])
                for app_cid, app_bu, _kind in chunks:
                    if app_bu <= 0:
                        continue
                    db.add(
                        GrainMovement(
                            bin_id=bid,
                            to_bin_id=None,
                            field_id=None,
                            contract_id=app_cid,
                            move_date=when,
                            move_type="delivery",
                            crop=crop,
                            owner_name=name,
                            wet_bu=None,
                            moisture=moist,
                            net_bu=app_bu,
                            ticket_number=ticket,
                            destination=dest,
                            hauler=trucker,
                            freight_per_bu=freight,
                            notes=note,
                        )
                    )
                    adjust(bid, name, -app_bu)
                    truck_bu += app_bu
                    delivered_total += app_bu
            else:
                db.add(
                    GrainMovement(
                        bin_id=bid,
                        to_bin_id=None,
                        field_id=None,
                        contract_id=cid,
                        move_date=when,
                        move_type="delivery",
                        crop=crop,
                        owner_name=name,
                        wet_bu=None,
                        moisture=moist,
                        net_bu=amt,
                        ticket_number=ticket,
                        destination=dest,
                        hauler=trucker,
                        freight_per_bu=freight,
                        notes=note,
                    )
                )
                adjust(bid, name, -amt)
                truck_bu += amt
                delivered_total += amt
        else:
            db.add(
                GrainMovement(
                    bin_id=bid,
                    to_bin_id=None,
                    field_id=None,
                    contract_id=None,
                    move_date=when,
                    move_type="delivery",
                    crop=crop,
                    owner_name=name,
                    wet_bu=None,
                    moisture=moist,
                    net_bu=amt,
                    ticket_number=ticket,
                    destination=dest,
                    hauler=trucker,
                    freight_per_bu=freight,
                    notes=note,
                )
            )
            adjust(bid, name, -amt)
            truck_bu += amt

    for bid, amt in allocations:
        _emit_owner_moves(
            bin_id=bid,
            field_id=None,
            move_type="delivery",
            amt=amt,
            wet_val=None,
            apply_contracts=True,
        )

    if fid and f_bu > 0:
        _emit_owner_moves(
            bin_id=None,
            field_id=fid,
            move_type="elevator",
            amt=f_bu,
            wet_val=wet,
            apply_contracts=True,
        )

    # Apply delivered_bu on each contract (spot contracts start at 0 then get filled)
    _apply_contract_deliveries(db, contract_apps)

    if dest and (trucker or freight is not None):
        rate_q = select(TruckingRate).where(TruckingRate.destination == dest)
        if trucker:
            rate_q = rate_q.where(TruckingRate.hauler == trucker)
        else:
            rate_q = rate_q.where(TruckingRate.hauler.is_(None))
        existing_rate = db.scalar(rate_q.limit(1))
        if existing_rate:
            if freight is not None:
                existing_rate.rate_per_bu = freight
            if trucker and not existing_rate.hauler:
                existing_rate.hauler = trucker
        else:
            db.add(
                TruckingRate(
                    destination=dest,
                    hauler=trucker,
                    rate_per_bu=freight,
                )
            )

    if dest and truck_bu > 0 and (trucker or freight is not None):
        db.add(
            TruckLoad(
                load_date=when,
                crop=crop,
                destination=dest,
                hauler=trucker,
                bushels=truck_bu,
                rate_paid=freight,
                ticket_number=ticket,
                notes=note,
            )
        )

    log_activity(
        db,
        user.get("username"),
        "grain_ticket",
        f"{ticket or 'ticket'} {ticket_bu} bu {crop} ({owner} {owner_bu} bu to contract)",
    )
    db.commit()
    return RedirectResponse("/bins/ticket/scale?saved=1", status_code=303)


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
    fields: list = []
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
                select(Field)
                .where(Field.crop_year_id == year.id)
                .options(
                    joinedload(Field.shares).joinedload(FieldShare.party),
                    joinedload(Field.party),
                )
                .order_by(Field.name)
            ).unique()
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
                .options(joinedload(GrainContract.with_party))
                .order_by(GrainContract.crop, GrainContract.id)
            ).unique()
        )
        contracts.sort(key=mkt.contract_desk_sort_key)
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

    # Carry $/bu/mo from Carry cost page assumptions — used under each strip spread
    carry_for_strip = mkt.build_carry_snapshot(settings, board, bin_bu)
    strip_rates = {
        crop: (carry_for_strip.get("by_crop") or {}).get(crop, {}).get("rate")
        for crop in ("Corn", "Soybeans")
    }
    strip_spreads = cme_quotes.strip_spreads_map(futures_strip, rates_by_crop=strip_rates)

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

    # Gross income & profit boxes (under futures strip)
    income_pnl: dict[str, dict] = {}
    for crop in ("Corn", "Soybeans"):
        items = by_crop.get(crop, [])
        expected = float(estimates.get(crop) or 0)
        acres = float(crop_acres.get(crop) or 0)
        priced_bu = 0.0
        priced_dollars = 0.0
        for c in items:
            px = mkt.equiv_price(c)
            bu = float(c.bushels or 0)
            if px is None or bu <= 0:
                continue
            priced_bu += bu
            priced_dollars += bu * float(px)
        contract_avg = round(priced_dollars / priced_bu, 4) if priced_bu > 0 else None
        board_row = board.get(crop) or {}
        futures_px = board_row.get("price")
        if futures_px is None and settings:
            futures_px = (
                settings.corn_price_assumption if crop == "Corn" else settings.soy_price_assumption
            )
        sold_for_blend = min(priced_bu, expected) if expected > 0 else priced_bu
        unsold = max(0.0, expected - sold_for_blend) if expected > 0 else 0.0
        blend = None
        if expected > 0:
            if contract_avg is not None and futures_px is not None:
                blend = (sold_for_blend * contract_avg + unsold * float(futures_px)) / expected
            elif contract_avg is not None:
                blend = contract_avg
            elif futures_px is not None:
                blend = float(futures_px)
        if blend is not None:
            blend = round(blend, 4)
        gross = round(expected * blend, 0) if blend is not None and expected > 0 else None
        gross_ac = round(gross / acres, 2) if gross is not None and acres > 0 else None
        cop = float(
            (settings.corn_cost_per_ac if crop == "Corn" else settings.soy_cost_per_ac)
            if settings
            else 0
        ) or 0.0
        profit_ac = round(gross_ac - cop, 2) if gross_ac is not None else None
        income_pnl[crop] = {
            "expected_bu": expected,
            "acres": acres,
            "contract_avg": contract_avg,
            "futures": float(futures_px) if futures_px is not None else None,
            "contracted_bu": round(sold_for_blend, 1),
            "unsold_bu": round(unsold, 1),
            "blend_price": blend,
            "gross": gross,
            "gross_ac": gross_ac,
            "cop_ac": cop,
            "profit_ac": profit_ac,
        }

    corn_p = income_pnl["Corn"].get("profit_ac")
    soy_p = income_pnl["Soybeans"].get("profit_ac")
    corn_a = income_pnl["Corn"].get("acres") or 0
    soy_a = income_pnl["Soybeans"].get("acres") or 0
    if corn_p is not None and soy_p is not None and (corn_a + soy_a) > 0:
        farm_profit_ac = round((corn_p * corn_a + soy_p * soy_a) / (corn_a + soy_a), 2)
    elif corn_p is not None and soy_p is not None:
        farm_profit_ac = round((corn_p + soy_p) / 2, 2)
    elif corn_p is not None:
        farm_profit_ac = corn_p
    elif soy_p is not None:
        farm_profit_ac = soy_p
    else:
        farm_profit_ac = None
    income_pnl["farm"] = {"profit_ac": farm_profit_ac, "acres": corn_a + soy_a}

    ranks = mkt.rank_contracts(
        contracts,
        breakeven_by_crop=be_by_crop,
        market_by_crop=market_by_crop,
    )
    desk = mkt.desk_rows(contracts, events_by, be_by_crop, market_by_crop)
    def _futures_px(crop: str) -> float | None:
        row = board.get(crop) or {}
        px = row.get("price")
        if px is not None:
            return float(px)
        if not settings:
            return None
        if crop == "Corn":
            return settings.corn_futures if settings.corn_futures is not None else settings.corn_price_assumption
        return settings.soy_futures if settings.soy_futures is not None else settings.soy_price_assumption

    futures_by_crop = {"Corn": _futures_px("Corn"), "Soybeans": _futures_px("Soybeans")}
    desk_analysis = mkt.desk_analysis(
        contracts,
        expected_by_crop={
            "Corn": float((estimates or {}).get("Corn") or 0),
            "Soybeans": float((estimates or {}).get("Soybeans") or 0),
        },
        futures_by_crop=futures_by_crop,
        market_by_crop=market_by_crop,
        breakeven_by_crop=be_by_crop,
    )
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    preferred = {"partner", "landlord"}
    preferred_list = [p for p in parties if (p.party_type or "").lower() in preferred]
    farm_with_parties = preferred_list if preferred_list else list(parties)
    rank = {"partner": 0, "landlord": 1, "customer": 2, "buyer": 3, "other": 4}
    farm_with_parties = sorted(
        farm_with_parties,
        key=lambda p: (rank.get((p.party_type or "other").lower(), 9), (p.name or "").lower()),
    )
    ownership_sold = mkt.ownership_sold_breakdown(fields, contracts, parties=parties)
    fields_need_partner: list = []
    # Link share partners onto on-share fields that were never assigned (Simpson → Ed Simpson)
    if year and fields and parties:
        healed = mkt.heal_missing_field_share_partners(db, fields, parties)
        if healed:
            db.commit()
            for f in fields:
                db.expire(f, ["shares", "party"])
            ownership_sold = mkt.ownership_sold_breakdown(fields, contracts, parties=parties)
    fields_need_partner = mkt.fields_missing_share_partner(fields)

    exposure = {}
    risk_tracker = {
        "by_crop": {},
        "stress_total": 0.0,
        "futures_stress_total": 0.0,
        "basis_stress_total": 0.0,
        "money_at_risk_total": 0.0,
        "unsold_total": 0.0,
        "bin_total": 0.0,
        "futures_open_total": 0.0,
        "basis_open_total": 0.0,
    }
    risk_calc_steps: list[dict] = []
    for crop in ("Corn", "Soybeans"):
        crop_contracts = by_crop.get(crop, [])
        exp = mkt.crop_exposure(
            crop_contracts,
            estimates.get(crop) or 0,
            bin_bu.get(crop) or 0,
        )
        fut_shock = 0.5 if crop == "Corn" else 1.0
        bas_shock = 0.20 if crop == "Corn" else 0.30
        if settings:
            fut_shock = (
                settings.corn_stress_shock if crop == "Corn" else settings.soy_stress_shock
            ) or fut_shock
            bas_shock = (
                settings.corn_basis_shock if crop == "Corn" else settings.soy_basis_shock
            ) or bas_shock
        mark = market_by_crop.get(crop)
        if mark is None and board.get(crop):
            mark = board[crop]["price"]
        fut_stress = mkt.stress_dollars(exp["futures_open"], fut_shock)
        bas_stress = mkt.stress_dollars(exp["basis_open"], bas_shock)
        stress = fut_stress + bas_stress
        at_risk_bu = exp["bin_bu"] + exp["unsold"]
        mar = mkt.money_at_risk(at_risk_bu, mark)
        exposure[crop] = exp
        risk_tracker["by_crop"][crop] = {
            **exp,
            "futures_shock": fut_shock,
            "basis_shock": bas_shock,
            "shock": fut_shock,  # legacy key used by older template bits
            "futures_stress": fut_stress,
            "basis_stress": bas_stress,
            "stress": stress,
            "mark": mark,
            "money_at_risk": mar,
            "at_risk_bu": round(at_risk_bu, 1),
        }
        risk_tracker["stress_total"] += stress
        risk_tracker["futures_stress_total"] += fut_stress
        risk_tracker["basis_stress_total"] += bas_stress
        risk_tracker["money_at_risk_total"] += mar or 0
        risk_tracker["unsold_total"] += exp["unsold"]
        risk_tracker["bin_total"] += exp["bin_bu"]
        risk_tracker["futures_open_total"] += exp["futures_open"]
        risk_tracker["basis_open_total"] += exp["basis_open"]

        # Open-contract breakdown for the explain window
        open_cs = [
            c
            for c in crop_contracts
            if (getattr(c, "status", "open") or "open") == "open"
        ]
        type_rows = []
        for c in open_cs:
            bu = float(getattr(c, "bushels", 0) or 0)
            if bu <= 0:
                continue
            type_rows.append(
                {
                    "id": getattr(c, "id", None),
                    "buyer": getattr(c, "buyer", None) or "—",
                    "type": getattr(c, "contract_type", None) or "—",
                    "bushels": round(bu, 1),
                    "futures_locked": mkt.futures_locked(c),
                    "basis_locked": mkt.basis_locked(c),
                }
            )
        risk_calc_steps.append(
            {
                "crop": crop,
                "expected": exp["expected"],
                "sold": exp["sold"],
                "unsold": exp["unsold"],
                "futures_locked": exp["futures_locked"],
                "basis_locked": exp["basis_locked"],
                "futures_open": exp["futures_open"],
                "basis_open": exp["basis_open"],
                "bin_bu": exp["bin_bu"],
                "futures_shock": fut_shock,
                "basis_shock": bas_shock,
                "futures_stress": fut_stress,
                "basis_stress": bas_stress,
                "stress": stress,
                "mark": mark,
                "at_risk_bu": round(at_risk_bu, 1),
                "money_at_risk": mar,
                "contracts": type_rows,
            }
        )

    quote_updated = _quote_updated_info(settings)

    with_party_added = request.query_params.get("with_party_added")
    with_party_preselect = int(with_party_added) if with_party_added and with_party_added.isdigit() else None
    buyer_added = (request.query_params.get("buyer_added") or "").strip() or None
    crop_added = (request.query_params.get("crop_added") or "").strip() or None
    type_added = (request.query_params.get("type_added") or "").strip() or None

    from app import lookups as lu

    buyers = set(lu.names(db, lu.DESTINATION))
    for c in contracts:
        if c.buyer and c.buyer.strip():
            buyers.add(c.buyer.strip())
    buyer_list = sorted(buyers, key=str.lower)

    futures_months_by_crop = {
        "Corn": cme_quotes.futures_month_choices("Corn"),
        "Soybeans": cme_quotes.futures_month_choices("Soybeans"),
    }

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
        "ownership_sold": ownership_sold,
        "fields_need_partner": fields_need_partner,
        "farm_with_parties": farm_with_parties,
        "board": board,
        "futures_strip": futures_strip,
        "strip_spreads": strip_spreads,
        "quote_updated": quote_updated,
        "ranks": ranks,
        "desk": desk,
        "desk_analysis": desk_analysis,
        "bin_bu": bin_bu,
        "exposure": exposure,
        "risk_tracker": risk_tracker,
        "risk_calc_steps": risk_calc_steps,
        "market_by_crop": market_by_crop,
        "parties": parties,
        "with_party_preselect": with_party_preselect,
        "buyer_list": buyer_list,
        "buyer_preselect": buyer_added,
        "crop_preselect": crop_added,
        "type_preselect": type_added,
        "futures_months_by_crop": futures_months_by_crop,
        "contract_statuses": CONTRACT_STATUSES,
        "income_pnl": income_pnl,
    }


@router.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk"
    ctx["linked"] = request.query_params.get("linked")
    return templates.TemplateResponse("risk.html", ctx)


@router.post("/risk/link-share-partner")
def risk_link_share_partner(
    request: Request,
    field_id: int = Form(...),
    party_id: int = Form(...),
    me_share_pct: str = Form("50"),
    db: Session = Depends(get_db),
):
    """Quick-assign who you farm on shares with for a field (from Marketing board)."""
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    party = db.get(Party, party_id)
    if not field or not party:
        return RedirectResponse("/risk#ownership-sold", status_code=303)
    try:
        me_pct = float(str(me_share_pct).replace(",", "").strip() or "50")
    except ValueError:
        me_pct = 50.0
    mkt.link_field_to_share_partner(db, field, party, me_pct=me_pct)
    log_activity(
        db,
        user.get("username"),
        "field_share_link",
        f"{field.name} ↔ {party.name} (me {me_pct:g}%)",
    )
    db.commit()
    return RedirectResponse("/risk?linked=1#ownership-sold", status_code=303)


@router.post("/risk/link-share-partners-bulk")
def risk_link_share_partners_bulk(
    request: Request,
    party_id: int = Form(...),
    me_share_pct: str = Form("50"),
    field_id: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Link several on-share fields to the same farm partner in one step."""
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    party = db.get(Party, party_id)
    if not party:
        return RedirectResponse("/risk#ownership-sold", status_code=303)
    try:
        me_pct = float(str(me_share_pct).replace(",", "").strip() or "50")
    except ValueError:
        me_pct = 50.0
    ids = []
    for raw in field_id if isinstance(field_id, list) else [field_id]:
        s = str(raw).strip()
        if s.isdigit():
            ids.append(int(s))
    linked = 0
    names: list[str] = []
    for fid in ids:
        field = db.get(Field, fid)
        if not field:
            continue
        mkt.link_field_to_share_partner(db, field, party, me_pct=me_pct)
        linked += 1
        names.append(field.name or f"#{fid}")
    if linked:
        log_activity(
            db,
            user.get("username"),
            "field_share_link_bulk",
            f"{linked} fields ↔ {party.name}: {', '.join(names[:8])}",
        )
        db.commit()
        return RedirectResponse(f"/risk?linked={linked}#ownership-sold", status_code=303)
    return RedirectResponse("/risk#link-partners", status_code=303)


@router.get("/risk/contracts", response_class=HTMLResponse)
def risk_contracts_page(request: Request, view: str = "", db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    if (view or "").strip().lower() == "sheet":
        return RedirectResponse("/risk/contracts/sheet", status_code=303)
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts"
    ctx["contracts_view"] = "desk"
    return templates.TemplateResponse("risk_contracts.html", ctx)


@router.get("/risk/contracts/add", response_class=HTMLResponse)
def risk_contracts_add(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts_add"
    ctx["add_next"] = "/risk/contracts/add"
    return templates.TemplateResponse("risk_contract_add.html", ctx)


@router.get("/risk/contracts/roll", response_class=HTMLResponse)
def risk_contracts_roll(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts_roll"
    return templates.TemplateResponse("risk_contract_roll.html", ctx)


@router.get("/risk/contracts/sheet", response_class=HTMLResponse)
def risk_contracts_sheet(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts"
    ctx["contracts_view"] = "sheet"
    ctx["saved"] = request.query_params.get("saved")
    return templates.TemplateResponse("risk_contracts_sheet.html", ctx)


@router.post("/risk/contracts/sheet/save")
def risk_contracts_sheet_save(
    request: Request,
    contract_id: list[int] = Form(default=[]),
    contract_number: list[str] = Form(default=[]),
    crop_year_id: list[str] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    contract_type: list[str] = Form(default=[]),
    buyer: list[str] = Form(default=[]),
    with_party_id: list[str] = Form(default=[]),
    bushels: list[str] = Form(default=[]),
    cash_price: list[str] = Form(default=[]),
    futures_price: list[str] = Form(default=[]),
    basis: list[str] = Form(default=[]),
    futures_month: list[str] = Form(default=[]),
    delivery_end: list[str] = Form(default=[]),
    status: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/risk/contracts/sheet", status_code=303)

    from app import lookups as lu

    ids = [int(x) for x in _as_form_list(contract_id)]
    nums = _as_form_list(contract_number)
    years = _as_form_list(crop_year_id)
    crops = _as_form_list(crop)
    types = _as_form_list(contract_type)
    buyers = _as_form_list(buyer)
    withs = _as_form_list(with_party_id)
    bus = _as_form_list(bushels)
    cash = _as_form_list(cash_price)
    futs = _as_form_list(futures_price)
    bases = _as_form_list(basis)
    months = _as_form_list(futures_month)
    ends = _as_form_list(delivery_end)
    statuses = _as_form_list(status)
    allowed_status = set(CONTRACT_STATUSES)
    valid_year_ids = {y.id for y in db.scalars(select(CropYear)).all()}
    updated = 0

    for i, cid in enumerate(ids):
        contract = db.get(GrainContract, cid)
        if not contract or contract.crop_year_id != year.id:
            continue

        new_cno = str(nums[i] if i < len(nums) else (contract.contract_number or "")).strip() or None
        year_raw = str(years[i] if i < len(years) else contract.crop_year_id).strip()
        new_year_id = int(year_raw) if year_raw.isdigit() else contract.crop_year_id
        if new_year_id not in valid_year_ids:
            new_year_id = contract.crop_year_id
        new_crop = str(crops[i] if i < len(crops) else contract.crop).strip() or contract.crop
        new_type = str(types[i] if i < len(types) else contract.contract_type).strip() or contract.contract_type
        new_buyer = str(buyers[i] if i < len(buyers) else (contract.buyer or "")).strip() or None
        wid_raw = str(withs[i] if i < len(withs) else (contract.with_party_id or "")).strip()
        new_wid = int(wid_raw) if wid_raw.isdigit() else None
        if new_wid and not db.get(Party, new_wid):
            new_wid = None
        new_bu = _f(str(bus[i] if i < len(bus) else contract.bushels)) or 0
        cash_raw = str(cash[i] if i < len(cash) else "").strip()
        new_cash = _f(cash_raw, None) if cash_raw else None
        fut_raw = str(futs[i] if i < len(futs) else "").strip()
        new_fut = _f(fut_raw, None) if fut_raw else None
        bas_raw = str(bases[i] if i < len(bases) else "").strip()
        new_bas = _f(bas_raw, None) if bas_raw else None
        new_month = str(months[i] if i < len(months) else (contract.futures_month or "")).strip() or None
        if i < len(ends):
            end_raw = str(ends[i]).strip()
            new_end = _d(end_raw) if end_raw else None
        else:
            new_end = contract.delivery_end
        new_status = str(statuses[i] if i < len(statuses) else contract.status).strip() or "open"
        if new_status not in allowed_status:
            new_status = contract.status

        if new_crop:
            lu.ensure(db, lu.CROP, new_crop)
        if new_type:
            lu.ensure(db, lu.CONTRACT_TYPE, new_type)
        if new_buyer:
            lu.ensure(db, lu.DESTINATION, new_buyer)

        changed = (
            (contract.contract_number or None) != new_cno
            or contract.crop_year_id != new_year_id
            or contract.crop != new_crop
            or contract.contract_type != new_type
            or (contract.buyer or None) != new_buyer
            or (contract.with_party_id or None) != new_wid
            or float(contract.bushels or 0) != float(new_bu)
            or (contract.cash_price != new_cash)
            or (contract.futures_price != new_fut)
            or (contract.basis != new_bas)
            or (contract.futures_month or None) != new_month
            or (contract.delivery_end != new_end)
            or contract.status != new_status
        )
        if not changed:
            continue

        contract.contract_number = new_cno
        contract.crop_year_id = new_year_id
        contract.crop = new_crop
        contract.contract_type = new_type
        contract.buyer = new_buyer
        contract.with_party_id = new_wid
        contract.bushels = new_bu
        contract.cash_price = new_cash
        contract.futures_price = new_fut
        contract.basis = new_bas
        contract.futures_month = new_month
        contract.delivery_end = new_end
        contract.status = new_status
        updated += 1

    log_activity(db, user.get("username"), "contracts_sheet_save", f"{updated} updated")
    db.commit()
    return RedirectResponse(f"/risk/contracts/sheet?saved={updated}", status_code=303)


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


@router.get("/risk/hold-sell", response_class=HTMLResponse)
def hold_sell_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    path = Path(__file__).resolve().parent / "static" / "hold-or-sell.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/risk/hold-sell/quotes")
def hold_sell_quotes(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    payload = cme_quotes.fetch_hold_sell_quotes()
    return JSONResponse(payload)


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
        fetched = cme_quotes.fetch_nearby_quotes(max_seconds=90.0)
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
                err = (fetched.get("error") or "").strip()
                if err:
                    # Keep a short hint in settings so the board can show why
                    settings.quote_source = settings.quote_source or "yahoo_delayed"
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
    corn_basis_shock: str = Form("0.20"),
    soy_basis_shock: str = Form("0.30"),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.corn_stress_shock = _f(corn_stress_shock) or 0.5
        settings.soy_stress_shock = _f(soy_stress_shock) or 1.0
        settings.corn_basis_shock = _f(corn_basis_shock) if corn_basis_shock.strip() != "" else 0.20
        settings.soy_basis_shock = _f(soy_basis_shock) if soy_basis_shock.strip() != "" else 0.30
        if settings.corn_basis_shock is None:
            settings.corn_basis_shock = 0.20
        if settings.soy_basis_shock is None:
            settings.soy_basis_shock = 0.30
        db.commit()
    return RedirectResponse("/risk#risk-tracker", status_code=303)


@router.post("/risk/cop")
def risk_cop(
    request: Request,
    corn_cost_per_ac: str = Form("0"),
    soy_cost_per_ac: str = Form("0"),
    corn_price_assumption: str = Form(""),
    soy_price_assumption: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.corn_cost_per_ac = _f(corn_cost_per_ac) or 0
        settings.soy_cost_per_ac = _f(soy_cost_per_ac) or 0
        if corn_price_assumption.strip() != "" or soy_price_assumption.strip() != "":
            settings.corn_price_assumption = (
                _f(corn_price_assumption, None) if corn_price_assumption.strip() else settings.corn_price_assumption
            )
            settings.soy_price_assumption = (
                _f(soy_price_assumption, None) if soy_price_assumption.strip() else settings.soy_price_assumption
            )
        db.commit()
    dest = (next or "").strip()
    if dest.startswith("/") and not dest.startswith("//"):
        return RedirectResponse(dest, status_code=303)
    return RedirectResponse("/risk/settings", status_code=303)


@router.get("/risk/contracts/new/buyer", response_class=HTMLResponse)
def risk_contract_new_buyer(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "risk_contracts",
            "farm_name": _farm(db),
            "page_kind": "destination",
            "page_title": "Add elevator / buyer",
            "page_lede": "Enter the elevator or grain buyer. It will appear on the contract Buyer list.",
            "form_action": "/risk/contracts/new/buyer",
            "error": None,
            "ticket_haulers": [],
            "ticket_destinations": [],
            "back_href": next_url,
            "next_url": next_url,
        },
    )


@router.post("/risk/contracts/new/buyer")
def risk_contract_new_buyer_save(
    request: Request,
    destination: str = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(next or request.query_params.get("next"))
    name = destination.strip()
    if not name:
        return RedirectResponse(
            _next_with_query("/risk/contracts/new/buyer", next=next_url if next_url != "/risk/contracts" else ""),
            status_code=303,
        )
    from app import lookups as lu

    lu.ensure(db, lu.DESTINATION, name, notes=(notes.strip() or None))
    existing = db.scalar(select(TruckingRate).where(TruckingRate.destination == name).limit(1))
    if not existing:
        db.add(TruckingRate(destination=name, notes=(notes.strip() or None)))
    log_activity(db, user.get("username"), "buyer_add", name)
    db.commit()
    return RedirectResponse(_next_with_query(next_url, buyer_added=name), status_code=303)


@router.get("/risk/contracts/new/with-party", response_class=HTMLResponse)
def risk_contract_new_with_party(request: Request, db: Session = Depends(get_db)):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not (
        perms.can_access(user.get("role"), "risk")
        or perms.can_access(user.get("role"), "grain")
        or perms.can_access(user.get("role"), "fields")
        or perms.can_access(user.get("role"), "parties")
    ):
        return perms.deny()
    next_url = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        "risk_contract_new_with.html",
        {
            "request": request,
            "user": user,
            "active": "risk_contracts",
            "farm_name": _farm(db),
            "party_types": [
                {"value": "partner", "label": "Partner"},
                {"value": "landlord", "label": "Landlord"},
                {"value": "other", "label": "Other"},
            ],
            "next_url": next_url,
            "back_href": next_url,
        },
    )


@router.post("/risk/contracts/new/with-party")
def risk_contract_new_with_party_save(
    request: Request,
    name: str = Form(...),
    party_type: str = Form("partner"),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not (
        perms.can_access(user.get("role"), "risk")
        or perms.can_access(user.get("role"), "grain")
        or perms.can_access(user.get("role"), "fields")
        or perms.can_access(user.get("role"), "parties")
    ):
        return perms.deny()
    next_url = _safe_next(next or request.query_params.get("next"))
    raw = (name or "").strip()
    if not raw:
        dest = "/risk/contracts/new/with-party"
        if next_url != "/risk/contracts":
            dest = _next_with_query(dest, next=next_url)
        return RedirectResponse(dest, status_code=303)
    existing = db.scalar(select(Party).where(Party.name == raw).limit(1))
    if existing:
        party = existing
    else:
        party = Party(
            name=raw,
            party_type=(party_type or "partner").strip() or "partner",
            notes=(notes.strip() or None),
        )
        db.add(party)
        log_activity(db, user.get("username"), "party_add", f"{raw} (contract with)")
        db.commit()
        db.refresh(party)
    return RedirectResponse(_next_with_query(next_url, with_party_added=str(party.id)), status_code=303)


@router.get("/risk/contracts/new/crop", response_class=HTMLResponse)
def risk_contract_new_crop(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "risk_contracts",
            "farm_name": _farm(db),
            "page_kind": "crop",
            "page_title": "Add crop",
            "page_lede": "Enter a crop name. It will appear on the contract Crop list.",
            "form_action": "/risk/contracts/new/crop",
            "error": None,
            "ticket_haulers": [],
            "ticket_destinations": [],
            "back_href": next_url,
            "next_url": next_url,
        },
    )


@router.post("/risk/contracts/new/crop")
def risk_contract_new_crop_save(
    request: Request,
    crop: str = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(next or request.query_params.get("next"))
    name = (crop or "").strip()
    if not name:
        return RedirectResponse("/risk/contracts/new/crop", status_code=303)
    from app import lookups as lu

    lu.ensure(db, lu.CROP, name, notes=(notes.strip() or None), commit=True)
    log_activity(db, user.get("username"), "crop_add", name)
    db.commit()
    return RedirectResponse(_next_with_query(next_url, crop_added=name), status_code=303)


@router.get("/risk/contracts/new/contract-type", response_class=HTMLResponse)
def risk_contract_new_type(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        "bins_ticket_new_lookup.html",
        {
            "request": request,
            "user": user,
            "active": "risk_contracts",
            "farm_name": _farm(db),
            "page_kind": "contract_type",
            "page_title": "Add contract type",
            "page_lede": "Enter a contract type (e.g. cash, HTA, basis). It will appear on the Type list.",
            "form_action": "/risk/contracts/new/contract-type",
            "error": None,
            "ticket_haulers": [],
            "ticket_destinations": [],
            "back_href": next_url,
            "next_url": next_url,
        },
    )


@router.post("/risk/contracts/new/contract-type")
def risk_contract_new_type_save(
    request: Request,
    contract_type: str = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(next or request.query_params.get("next"))
    name = (contract_type or "").strip()
    if not name:
        return RedirectResponse("/risk/contracts/new/contract-type", status_code=303)
    from app import lookups as lu

    lu.ensure(db, lu.CONTRACT_TYPE, name, notes=(notes.strip() or None), commit=True)
    log_activity(db, user.get("username"), "contract_type_add", name)
    db.commit()
    return RedirectResponse(_next_with_query(next_url, type_added=name), status_code=303)


@router.get("/risk/contracts/{contract_id}/edit", response_class=HTMLResponse)
def risk_contract_edit_page(request: Request, contract_id: int, db: Session = Depends(get_db)):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    contract = db.get(GrainContract, contract_id)
    if not contract:
        return RedirectResponse("/risk/contracts", status_code=303)
    ctx = _risk_page_payload(request, db, user)
    ctx["active"] = "risk_contracts"
    ctx["contract"] = contract
    ctx["edit_next"] = f"/risk/contracts/{contract_id}/edit"
    # Prefer newly added values for preselect on return from Add New
    type_added = (request.query_params.get("type_added") or "").strip() or None
    crop_added = (request.query_params.get("crop_added") or "").strip() or None
    if type_added:
        ctx["type_preselect"] = type_added
    if crop_added:
        ctx["crop_preselect"] = crop_added
    return templates.TemplateResponse("risk_contract_edit.html", ctx)


@router.post("/risk/contracts/{contract_id}/edit")
def risk_contract_edit_save(
    request: Request,
    contract_id: int,
    contract_number: str = Form(""),
    crop: str = Form("Corn"),
    contract_type: str = Form("cash"),
    buyer: str = Form(""),
    with_party_id: str = Form(""),
    crop_year_id: str = Form(""),
    bushels: str = Form("0"),
    cash_price: str = Form(""),
    futures_price: str = Form(""),
    basis: str = Form(""),
    futures_month: str = Form(""),
    delivery_start: str = Form(""),
    delivery_end: str = Form(""),
    status: str = Form("open"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    contract = db.get(GrainContract, contract_id)
    if not contract:
        return RedirectResponse("/risk/contracts", status_code=303)
    wid = int(with_party_id) if with_party_id.isdigit() else None
    if wid and not db.get(Party, wid):
        wid = None
    buyer_name = buyer.strip() or None
    from app import lookups as lu

    if buyer_name:
        lu.ensure(db, lu.DESTINATION, buyer_name)
    crop_name = (crop or "").strip() or contract.crop
    type_name = (contract_type or "").strip() or contract.contract_type
    lu.ensure(db, lu.CROP, crop_name)
    lu.ensure(db, lu.CONTRACT_TYPE, type_name)
    st = (status or "open").strip() or "open"
    if st not in CONTRACT_STATUSES:
        st = contract.status or "open"

    new_year_id = int(crop_year_id) if crop_year_id.isdigit() else contract.crop_year_id
    if new_year_id and not db.get(CropYear, new_year_id):
        new_year_id = contract.crop_year_id

    contract.contract_number = contract_number.strip() or None
    contract.crop = crop_name
    contract.contract_type = type_name
    contract.buyer = buyer_name
    contract.with_party_id = wid
    contract.crop_year_id = new_year_id
    contract.bushels = _f(bushels) or 0
    contract.cash_price = _f(cash_price, None) if cash_price.strip() else None
    contract.futures_price = _f(futures_price, None) if futures_price.strip() else None
    contract.basis = _f(basis, None) if basis.strip() else None
    contract.futures_month = futures_month.strip() or None
    contract.delivery_start = _d(delivery_start)
    contract.delivery_end = _d(delivery_end)
    contract.status = st
    contract.notes = notes.strip() or None
    log_activity(
        db,
        user.get("username"),
        "contract_edit",
        f"#{contract_id} {crop_name} year={new_year_id}",
    )
    db.commit()
    return RedirectResponse("/risk/contracts", status_code=303)


@router.post("/risk/contracts/{contract_id}/delete")
def risk_contract_delete(
    request: Request,
    contract_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    contract = db.get(GrainContract, contract_id)
    if not contract:
        return RedirectResponse("/risk/contracts", status_code=303)
    # Keep ticket history; just unlink from this contract
    for move in list(
        db.scalars(select(GrainMovement).where(GrainMovement.contract_id == contract_id))
    ):
        move.contract_id = None
    for ev in list(
        db.scalars(select(ContractEvent).where(ContractEvent.contract_id == contract_id))
    ):
        db.delete(ev)
    label = f"#{contract.id} {contract.crop} {contract.buyer or ''} {contract.bushels} bu"
    db.delete(contract)
    log_activity(db, user.get("username"), "contract_delete", label.strip())
    db.commit()
    return RedirectResponse("/risk/contracts?deleted=1", status_code=303)


@router.post("/risk/contract")
def risk_contract(
    request: Request,
    contract_number: str = Form(""),
    crop: str = Form("Corn"),
    contract_type: str = Form("cash"),
    buyer: str = Form(""),
    with_party_id: str = Form(""),
    bushels: str = Form("0"),
    cash_price: str = Form(""),
    futures_price: str = Form(""),
    basis: str = Form(""),
    futures_month: str = Form(""),
    delivery_start: str = Form(""),
    delivery_end: str = Form(""),
    status: str = Form("open"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "risk")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/risk/contracts", status_code=303)
    wid = int(with_party_id) if with_party_id.isdigit() else None
    if wid and not db.get(Party, wid):
        wid = None
    buyer_name = buyer.strip() or None
    from app import lookups as lu

    if buyer_name:
        lu.ensure(db, lu.DESTINATION, buyer_name)
    crop_name = (crop or "").strip() or "Corn"
    type_name = (contract_type or "").strip() or "cash"
    lu.ensure(db, lu.CROP, crop_name)
    lu.ensure(db, lu.CONTRACT_TYPE, type_name)
    st = (status or "open").strip() or "open"
    if st not in CONTRACT_STATUSES:
        st = "open"
    db.add(
        GrainContract(
            crop_year_id=year.id,
            contract_number=contract_number.strip() or None,
            crop=crop_name,
            contract_type=type_name,
            buyer=buyer_name,
            with_party_id=wid,
            bushels=_f(bushels) or 0,
            cash_price=_f(cash_price, None) if cash_price.strip() else None,
            futures_price=_f(futures_price, None) if futures_price.strip() else None,
            basis=_f(basis, None) if basis.strip() else None,
            futures_month=futures_month.strip() or None,
            delivery_start=_d(delivery_start),
            delivery_end=_d(delivery_end),
            status=st,
            notes=notes.strip() or None,
        )
    )
    log_activity(db, user.get("username"), "contract_add", f"{crop_name} {bushels} bu")
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
    from app import lookups as lu
    from app.budget_detail import seed_fertilizer_catalog

    seed_fertilizer_catalog(db)
    db.flush()

    hybrid_brands = sorted(
        {*(lu.names(db, lu.HYBRID_BRAND) or []), *hybrid_brands},
        key=str.casefold,
    )
    hybrid_traits = sorted(
        {*(lu.names(db, lu.HYBRID_TRAIT) or []), *hybrid_traits},
        key=str.casefold,
    )
    hybrid_suggest_json = json.dumps(
        {"brands": hybrid_brands, "traits": hybrid_traits},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    fert_products = list(
        db.scalars(
            select(FertilizerProduct)
            .options(joinedload(FertilizerProduct.purchases))
            .order_by(FertilizerProduct.sort_order, FertilizerProduct.name)
        ).unique()
    )
    inventory_n = int(db.scalar(select(func.count()).select_from(InputProduct)) or 0)
    if year:
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
        hybrids.sort(
            key=lambda h: (
                (h.brand or "\uffff").casefold(),
                (h.name or "").casefold(),
            )
        )
        sprays = list(
            db.scalars(
                select(SprayMix).where(SprayMix.crop_year_id == year.id).order_by(SprayMix.name)
            )
        )
        plans = list(
            db.scalars(
                select(FieldPlan)
                .where(FieldPlan.crop_year_id == year.id)
                .order_by(FieldPlan.id.desc())
            )
        )
        fields = list(
            db.scalars(select(Field).where(Field.crop_year_id == year.id).order_by(Field.name))
        )
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

    focus = (request.query_params.get("focus") or "").strip().lower()
    if focus not in ("seed", "fert", "spray", "inventory"):
        focus = ""
    raw_next = (request.query_params.get("next") or "").strip()
    next_url = _safe_next(raw_next, default="/inputs") if raw_next else "/inputs"

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
        "fert_products": fert_products,
        "inventory_n": inventory_n,
        "plans": plans,
        "fields": fields,
        "today": date.today().isoformat(),
        "focus": focus,
        "next_url": next_url,
        "from_budget": raw_next.startswith("/budget"),
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
    db.commit()
    return templates.TemplateResponse("library.html", ctx)


@router.get("/inputs/hybrids/sheet", response_class=HTMLResponse)
@router.get("/library/hybrids/sheet", response_class=HTMLResponse)
def hybrids_sheet(request: Request, crop: Optional[str] = None, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    from app import lookups as lu

    year = _year(db)
    hybrids = []
    if year:
        _sync_hybrid_label_spellings(db)
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
        hybrids.sort(
            key=lambda h: (
                0 if h.crop == "Corn" else 1 if h.crop == "Soybeans" else 2,
                (h.brand or "").lower(),
                (h.name or "").lower(),
            )
        )
    filter_crop = (crop or "all").strip()
    if filter_crop not in ("all", "Corn", "Soybeans"):
        filter_crop = "all"
    if filter_crop != "all":
        hybrids = [h for h in hybrids if h.crop == filter_crop]

    vocab_brands, vocab_traits = _hybrid_brand_trait_vocab(db)
    brands = sorted(
        {*(lu.names(db, lu.HYBRID_BRAND) or []), *vocab_brands},
        key=str.casefold,
    )
    traits = sorted(
        {*(lu.names(db, lu.HYBRID_TRAIT) or []), *vocab_traits},
        key=str.casefold,
    )
    brand_added = (request.query_params.get("brand_added") or "").strip() or None
    trait_added = (request.query_params.get("trait_added") or "").strip() or None
    focus_hybrid_id = None
    raw_focus = (request.query_params.get("hybrid_id") or "").strip()
    if raw_focus.isdigit():
        focus_hybrid_id = int(raw_focus)

    return templates.TemplateResponse(
        "hybrids_sheet.html",
        {
            "request": request,
            "user": user,
            "active": "library_sheet",
            "farm_name": _farm(db),
            "year": year,
            "hybrids": hybrids,
            "filter_crop": filter_crop,
            "saved": request.query_params.get("saved"),
            "hybrid_brands": brands,
            "hybrid_traits": traits,
            "brand_added": brand_added,
            "trait_added": trait_added,
            "focus_hybrid_id": focus_hybrid_id,
        },
    )


@router.post("/inputs/hybrids/sheet/save")
@router.post("/library/hybrids/sheet/save")
def hybrids_sheet_save(
    request: Request,
    hybrid_id: list[int] = Form(default=[]),
    name: list[str] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    brand: list[str] = Form(default=[]),
    maturity: list[str] = Form(default=[]),
    traits: list[str] = Form(default=[]),
    unit_label: list[str] = Form(default=[]),
    cost_per_unit: list[str] = Form(default=[]),
    cost_per_acre: list[str] = Form(default=[]),
    notes: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user

    def as_list(vals):
        if vals is None:
            return []
        if isinstance(vals, (str, int, float)):
            return [vals]
        return list(vals)

    ids = [int(x) for x in as_list(hybrid_id)]
    names = as_list(name)
    crops = as_list(crop)
    brands = as_list(brand)
    mats = as_list(maturity)
    traits_l = as_list(traits)
    units = as_list(unit_label)
    cpus = as_list(cost_per_unit)
    cpas = as_list(cost_per_acre)
    notes_l = as_list(notes)
    known_brands, known_traits = _hybrid_brand_trait_vocab(db)
    updated = 0
    from app import lookups as lu

    for i, hid in enumerate(ids):
        row = db.get(Hybrid, hid)
        if not row:
            continue
        new_name = str(names[i] if i < len(names) else row.name).strip() or row.name
        new_crop = str(crops[i] if i < len(crops) else row.crop).strip() or row.crop
        brand_raw = str(brands[i] if i < len(brands) else (row.brand or "")).strip()
        if brand_raw == "__add_new__":
            brand_raw = (row.brand or "").strip()
        new_brand = _canonicalize_label(brand_raw, known_brands) if brand_raw else None
        new_mat = str(mats[i] if i < len(mats) else (row.maturity or "")).strip() or None
        traits_raw = str(traits_l[i] if i < len(traits_l) else (row.traits or "")).strip()
        if traits_raw == "__add_new__":
            traits_raw = (row.traits or "").strip()
        new_traits = _canonicalize_traits(traits_raw, known_traits) if traits_raw else None
        new_unit = str(units[i] if i < len(units) else (row.unit_label or "")).strip() or None
        cpu_raw = str(cpus[i] if i < len(cpus) else "").strip()
        new_cpu = _money(cpu_raw) if cpu_raw != "" else None
        if i >= len(cpus):
            new_cpu = row.cost_per_unit
        cpa_raw = str(cpas[i] if i < len(cpas) else "").strip()
        new_cpa = _money(cpa_raw) if cpa_raw != "" else None
        if i >= len(cpas):
            new_cpa = row.cost_per_acre
        note_raw = str(notes_l[i] if i < len(notes_l) else (row.notes or "")).strip() or None

        changed = (
            row.name != new_name
            or row.crop != new_crop
            or (row.brand or None) != new_brand
            or (row.maturity or None) != new_mat
            or (row.traits or None) != new_traits
            or (row.unit_label or None) != new_unit
            or (row.cost_per_unit != new_cpu)
            or (row.cost_per_acre != new_cpa)
            or (row.notes or None) != note_raw
        )
        if not changed:
            continue
        row.name = new_name
        row.crop = new_crop
        row.brand = new_brand
        row.maturity = new_mat
        row.traits = new_traits
        row.unit_label = new_unit
        row.cost_per_unit = new_cpu
        row.cost_per_acre = new_cpa
        row.notes = note_raw
        if new_brand:
            lu.ensure(db, lu.HYBRID_BRAND, new_brand, commit=False)
        if new_traits:
            lu.ensure(db, lu.HYBRID_TRAIT, new_traits, commit=False)
            for part in re.split(r"[,;/|]+", new_traits):
                if part.strip():
                    lu.ensure(db, lu.HYBRID_TRAIT, part.strip(), commit=False)
        updated += 1

    db.commit()
    dest = "/inputs/hybrids/sheet"
    crop_q = (request.query_params.get("crop") or "").strip()
    if crop_q:
        dest = f"{dest}?crop={crop_q}&saved={updated}"
    else:
        dest = f"{dest}?saved={updated}"
    return RedirectResponse(dest, status_code=303)


@router.get("/inputs/hybrids/new/brand", response_class=HTMLResponse)
@router.get("/library/hybrids/new/brand", response_class=HTMLResponse)
def hybrids_new_brand(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default="/inputs/hybrids/sheet")
    return templates.TemplateResponse(
        "hybrid_lookup_new.html",
        {
            "request": request,
            "user": user,
            "active": "library_sheet",
            "farm_name": _farm(db),
            "page_kind": "brand",
            "page_title": "Add hybrid brand",
            "page_lede": "Enter a seed brand. It will appear in the Brand list on the hybrid sheet.",
            "field_name": "brand",
            "field_label": "Brand",
            "placeholder": "e.g. Dekalb, Pioneer, Channel",
            "form_action": "/inputs/hybrids/new/brand",
            "next_url": next_url,
            "back_href": next_url,
            "hybrid_id": (request.query_params.get("hybrid_id") or "").strip(),
            "error": None,
        },
    )


@router.post("/inputs/hybrids/new/brand")
@router.post("/library/hybrids/new/brand")
def hybrids_new_brand_save(
    request: Request,
    brand: str = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    hybrid_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    from app import lookups as lu

    next_url = _safe_next(next or request.query_params.get("next"), default="/inputs/hybrids/sheet")
    raw = (brand or "").strip()
    if not raw:
        dest = "/inputs/hybrids/new/brand"
        if next_url != "/inputs/hybrids/sheet":
            dest = _next_with_query(dest, next=next_url)
        return RedirectResponse(dest, status_code=303)
    known_brands, _ = _hybrid_brand_trait_vocab(db)
    name = _canonicalize_label(raw, known_brands) or raw
    lu.ensure(db, lu.HYBRID_BRAND, name, notes=(notes.strip() or None), commit=True)
    log_activity(db, user.get("username"), "hybrid_brand_add", name)
    params = {"brand_added": name}
    if (hybrid_id or "").strip().isdigit():
        params["hybrid_id"] = hybrid_id.strip()
    return RedirectResponse(_next_with_query(next_url, **params), status_code=303)


@router.get("/inputs/hybrids/new/trait", response_class=HTMLResponse)
@router.get("/library/hybrids/new/trait", response_class=HTMLResponse)
def hybrids_new_trait(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default="/inputs/hybrids/sheet")
    return templates.TemplateResponse(
        "hybrid_lookup_new.html",
        {
            "request": request,
            "user": user,
            "active": "library_sheet",
            "farm_name": _farm(db),
            "page_kind": "trait",
            "page_title": "Add hybrid trait",
            "page_lede": "Enter a trait or trait package. It will appear in the Trait list on the hybrid sheet.",
            "field_name": "trait",
            "field_label": "Trait",
            "placeholder": "e.g. VT2PRIB, Enlist E3, XtendFlex",
            "form_action": "/inputs/hybrids/new/trait",
            "next_url": next_url,
            "back_href": next_url,
            "hybrid_id": (request.query_params.get("hybrid_id") or "").strip(),
            "error": None,
        },
    )


@router.post("/inputs/hybrids/new/trait")
@router.post("/library/hybrids/new/trait")
def hybrids_new_trait_save(
    request: Request,
    trait: str = Form(...),
    notes: str = Form(""),
    next: str = Form(""),
    hybrid_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    from app import lookups as lu

    next_url = _safe_next(next or request.query_params.get("next"), default="/inputs/hybrids/sheet")
    raw = (trait or "").strip()
    if not raw:
        dest = "/inputs/hybrids/new/trait"
        if next_url != "/inputs/hybrids/sheet":
            dest = _next_with_query(dest, next=next_url)
        return RedirectResponse(dest, status_code=303)
    _, known_traits = _hybrid_brand_trait_vocab(db)
    name = _canonicalize_label(raw, known_traits) or raw
    lu.ensure(db, lu.HYBRID_TRAIT, name, notes=(notes.strip() or None), commit=True)
    log_activity(db, user.get("username"), "hybrid_trait_add", name)
    params = {"trait_added": name}
    if (hybrid_id or "").strip().isdigit():
        params["hybrid_id"] = hybrid_id.strip()
    return RedirectResponse(_next_with_query(next_url, **params), status_code=303)


def _hybrid_norm_key(name: str | None, crop: str | None) -> tuple[str, str]:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    # ignore common separators that create false uniques: 2111aa vs 2111 AA
    n = n.replace("-", "").replace("_", "").replace(" ", "")
    c = (crop or "Corn").strip() or "Corn"
    return (c, n)


def _hybrid_usage(db: Session, hybrid_ids: list[int]) -> dict[int, dict[str, int]]:
    out = {hid: {"fields": 0, "plantings": 0} for hid in hybrid_ids}
    if not hybrid_ids:
        return out
    for hid, cnt in db.execute(
        select(FieldHybrid.hybrid_id, func.count())
        .where(FieldHybrid.hybrid_id.in_(hybrid_ids))
        .group_by(FieldHybrid.hybrid_id)
    ):
        out[int(hid)]["fields"] = int(cnt or 0)
    for hid, cnt in db.execute(
        select(PlantingRecord.hybrid_id, func.count())
        .where(PlantingRecord.hybrid_id.in_(hybrid_ids))
        .group_by(PlantingRecord.hybrid_id)
    ):
        if hid is not None:
            out[int(hid)]["plantings"] = int(cnt or 0)
    return out


def merge_hybrids_into(db: Session, keep_id: int, merge_ids: list[int]) -> dict[str, int]:
    """Reassign FieldHybrid + PlantingRecord to keep_id, fill blank catalog fields, delete merges."""
    keep = db.get(Hybrid, keep_id)
    if not keep:
        raise ValueError("Keep hybrid not found")
    merge_ids = [int(x) for x in merge_ids if int(x) != keep_id]
    if not merge_ids:
        return {"merged": 0, "fields": 0, "plantings": 0}

    merged_rows = [db.get(Hybrid, mid) for mid in merge_ids]
    merged_rows = [h for h in merged_rows if h is not None]
    if not merged_rows:
        return {"merged": 0, "fields": 0, "plantings": 0}

    # Fill blank keep fields from first donor that has a value
    def fill(attr: str) -> None:
        if getattr(keep, attr, None) not in (None, ""):
            return
        for h in merged_rows:
            val = getattr(h, attr, None)
            if val not in (None, ""):
                setattr(keep, attr, val)
                return

    for attr in ("brand", "maturity", "traits", "notes", "unit_label", "cost_per_unit", "cost_per_acre"):
        fill(attr)

    # Re-point planting records
    plant_moved = 0
    for pr in db.scalars(select(PlantingRecord).where(PlantingRecord.hybrid_id.in_(merge_ids))):
        pr.hybrid_id = keep_id
        if not (pr.hybrid_name or "").strip():
            pr.hybrid_name = keep.name
        plant_moved += 1

    # Merge field links: same field → combine into keep link
    field_moved = 0
    keep_links = {
        link.field_id: link
        for link in db.scalars(select(FieldHybrid).where(FieldHybrid.hybrid_id == keep_id))
    }
    for link in list(db.scalars(select(FieldHybrid).where(FieldHybrid.hybrid_id.in_(merge_ids)))):
        existing = keep_links.get(link.field_id)
        if existing is None:
            link.hybrid_id = keep_id
            keep_links[link.field_id] = link
            field_moved += 1
            continue
        # Combine numeric as-planted values
        for attr in ("acres", "units", "population"):
            a = getattr(existing, attr, None)
            b = getattr(link, attr, None)
            if a is None and b is not None:
                setattr(existing, attr, b)
            elif a is not None and b is not None:
                setattr(existing, attr, float(a) + float(b))
        if not existing.rate and link.rate:
            existing.rate = link.rate
        if not existing.client_name and link.client_name:
            existing.client_name = link.client_name
        if not existing.applied_date and link.applied_date:
            existing.applied_date = link.applied_date
        if link.notes:
            existing.notes = ((existing.notes or "") + " · " + link.notes).strip(" ·")
        db.delete(link)
        field_moved += 1

    for h in merged_rows:
        db.delete(h)

    return {"merged": len(merged_rows), "fields": field_moved, "plantings": plant_moved}


@router.get("/inputs/hybrids/merge", response_class=HTMLResponse)
@router.get("/library/hybrids/merge", response_class=HTMLResponse)
def hybrids_merge_page(request: Request, crop: Optional[str] = None, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    groups: list[dict] = []
    singles: list[Hybrid] = []
    if year:
        hybrids = list(db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id)))
        usage = _hybrid_usage(db, [h.id for h in hybrids])
        buckets: dict[tuple[str, str], list[Hybrid]] = {}
        for h in hybrids:
            buckets.setdefault(_hybrid_norm_key(h.name, h.crop), []).append(h)
        filter_crop = (crop or "all").strip()
        if filter_crop not in ("all", "Corn", "Soybeans"):
            filter_crop = "all"
        for (crop_key, _norm), rows in sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1])):
            if filter_crop != "all" and crop_key != filter_crop:
                continue
            rows = sorted(rows, key=lambda h: (h.id,))
            if len(rows) < 2:
                continue
            groups.append(
                {
                    "key": f"{crop_key}:{_norm}",
                    "crop": crop_key,
                    "label": rows[0].name,
                    "rows": [
                        {
                            "hybrid": h,
                            "fields": usage.get(h.id, {}).get("fields", 0),
                            "plantings": usage.get(h.id, {}).get("plantings", 0),
                        }
                        for h in rows
                    ],
                }
            )
        # Also offer all hybrids for custom merge
        singles = sorted(
            hybrids if filter_crop == "all" else [h for h in hybrids if h.crop == filter_crop],
            key=lambda h: ((h.crop or ""), (h.name or "").lower(), h.id),
        )
    else:
        filter_crop = "all"

    return templates.TemplateResponse(
        "hybrids_merge.html",
        {
            "request": request,
            "user": user,
            "active": "library_merge",
            "farm_name": _farm(db),
            "year": year,
            "groups": groups,
            "singles": singles,
            "filter_crop": filter_crop,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("err"),
        },
    )


@router.post("/inputs/hybrids/merge")
@router.post("/library/hybrids/merge")
def hybrids_merge_save(
    request: Request,
    keep_id: int = Form(...),
    merge_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ids = [int(x) for x in (merge_ids or []) if str(x).isdigit()]
    if keep_id not in ids:
        ids.append(keep_id)
    # Only merge selected (keep + others checked). Form posts keep_id separately and
    # merge_ids as the duplicates to absorb — include keep is fine.
    others = [i for i in ids if i != keep_id]
    if not others:
        return RedirectResponse("/inputs/hybrids/merge?err=Select+at+least+one+duplicate+to+merge", status_code=303)
    keep = db.get(Hybrid, keep_id)
    if not keep:
        return RedirectResponse("/inputs/hybrids/merge?err=Keep+hybrid+not+found", status_code=303)
    # Safety: only same crop year
    bad = []
    for mid in others:
        row = db.get(Hybrid, mid)
        if not row or row.crop_year_id != keep.crop_year_id:
            bad.append(mid)
    if bad:
        return RedirectResponse("/inputs/hybrids/merge?err=All+hybrids+must+be+in+the+same+crop+year", status_code=303)
    try:
        result = merge_hybrids_into(db, keep_id, others)
        log_activity(
            db,
            user.get("username"),
            "hybrid_merge",
            f"Kept #{keep_id} {keep.name}; merged {result['merged']} "
            f"({result['fields']} field links, {result['plantings']} plantings)",
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return RedirectResponse(f"/inputs/hybrids/merge?err={quote(str(exc)[:120])}", status_code=303)
    msg = quote(
        f"Merged {result['merged']} into “{keep.name}” "
        f"({result['fields']} field links, {result['plantings']} plantings)"
    )
    return RedirectResponse(f"/inputs/hybrids/merge?msg={msg}", status_code=303)


@router.get("/inputs/assign", response_class=HTMLResponse)
def inputs_assign_page(request: Request, db: Session = Depends(get_db)):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    ctx = _library_payload(request, db, user)
    ctx["active"] = "library_assign"
    return templates.TemplateResponse("library_assign.html", ctx)


@router.post("/library/hybrid")
@router.post("/inputs/hybrid")
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
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    dest = _safe_next(next, default="/inputs") if (next or "").strip() else "/inputs"
    if not year:
        return RedirectResponse(dest, status_code=303)
    known_brands, known_traits = _hybrid_brand_trait_vocab(db)
    brand_s = _canonicalize_label(brand, known_brands) or None
    if brand and brand.strip() == "__add_new__":
        brand_s = None
    traits_s = _canonicalize_traits(traits, known_traits)
    if traits and traits.strip() == "__add_new__":
        traits_s = None
    from app import lookups as lu

    if brand_s:
        lu.ensure(db, lu.HYBRID_BRAND, brand_s, commit=False)
    if traits_s:
        lu.ensure(db, lu.HYBRID_TRAIT, traits_s, commit=False)
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
    return redirect_flash(request, dest, f"Added hybrid “{name.strip()}”.")


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
@router.post("/inputs/spray")
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
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    dest = _safe_next(next, default="/inputs") if (next or "").strip() else "/inputs"
    if not year:
        return RedirectResponse(dest, status_code=303)

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
    return redirect_flash(request, dest, f"Added spray mix “{mix.name}”.")


@router.post("/inputs/fertilizer/save")
@router.post("/library/fertilizer/save")
async def inputs_fertilizer_save(request: Request, db: Session = Depends(get_db)):
    """Edit / add fertilizer products used by field budgets."""
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    raw_next = str(form.get("next") or "").strip()
    dest = _safe_next(raw_next, default="/inputs?focus=fert") if raw_next else "/inputs?focus=fert"
    if dest == "/inputs":
        dest = "/inputs?focus=fert"

    ids = _as_form_list(form.getlist("product_id"))
    names = _as_form_list(form.getlist("name"))
    forms = _as_form_list(form.getlist("form"))
    units = _as_form_list(form.getlist("apply_unit"))
    prices = _as_form_list(form.getlist("price_per_ton"))
    dens = _as_form_list(form.getlist("density_lb_per_gal"))
    for i, pid_raw in enumerate(ids):
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        p = db.get(FertilizerProduct, pid)
        if not p:
            continue
        name = str(names[i] if i < len(names) else p.name).strip()
        if name:
            p.name = name
        form_v = str(forms[i] if i < len(forms) else p.form).strip().lower()
        p.form = form_v if form_v in ("liquid", "dry") else p.form
        unit_v = str(units[i] if i < len(units) else p.apply_unit).strip().lower()
        p.apply_unit = unit_v if unit_v in ("gal", "lb") else p.apply_unit
        p.price_per_ton = parse_float(str(prices[i] if i < len(prices) else "0")) or 0.0
        d_raw = str(dens[i] if i < len(dens) else "").strip()
        p.density_lb_per_gal = parse_float(d_raw, default=None) if d_raw else None

    new_name = str(form.get("new_name") or "").strip()
    if new_name:
        form_v = str(form.get("new_form") or "dry").strip().lower()
        unit_v = str(form.get("new_apply_unit") or "lb").strip().lower()
        db.add(
            FertilizerProduct(
                name=new_name,
                form=form_v if form_v in ("liquid", "dry") else "dry",
                apply_unit=unit_v if unit_v in ("gal", "lb") else "lb",
                price_per_ton=parse_float(str(form.get("new_price_per_ton") or "0")) or 0.0,
                density_lb_per_gal=parse_float(str(form.get("new_density") or ""), default=None),
                is_active=1,
                sort_order=900,
            )
        )
    db.commit()
    return redirect_flash(request, dest, "Fertilizer products saved.", "ok")


@router.post("/inputs/fertilizer/buy")
@router.post("/library/fertilizer/buy")
def inputs_fertilizer_buy(
    request: Request,
    product_id: int = Form(...),
    purchase_date: str = Form(""),
    vendor: str = Form(""),
    tons: str = Form("0"),
    total_cost: str = Form(""),
    price_per_ton: str = Form(""),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Log a fertilizer purchase lot and roll into weighted avg $/ton."""
    user = _need(request, "library")
    if isinstance(user, RedirectResponse):
        return user
    from app.budget_detail import apply_fertilizer_purchase

    dest = _safe_next(next, default="/inputs?focus=fert") if (next or "").strip() else "/inputs?focus=fert"
    product = db.get(FertilizerProduct, product_id)
    if not product:
        return redirect_flash(request, dest, "Fertilizer product not found.", "error")

    t = parse_float(tons) or 0.0
    if t <= 0:
        return redirect_flash(request, dest, "Enter tons purchased (> 0).", "error")

    total = parse_float(total_cost, default=None)
    ppt = parse_float(price_per_ton, default=None)
    if total is None and ppt is not None:
        total = round(t * float(ppt), 2)
    if total is None or total < 0:
        return redirect_flash(
            request,
            dest,
            "Enter total $ or $/ton for this purchase.",
            "error",
        )

    year = _year(db)
    when = parse_date(purchase_date) or date.today()
    apply_fertilizer_purchase(
        db,
        product,
        tons=t,
        total_cost=float(total),
        purchase_date=when,
        vendor=vendor.strip() or None,
        notes=notes.strip() or None,
        crop_year_id=year.id if year else None,
    )
    db.commit()
    avg = float(product.price_per_ton or 0)
    return redirect_flash(
        request,
        dest,
        f"Logged {t:g} ton of {product.name} · new avg ${avg:,.2f}/ton.",
        "ok",
    )


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
    from app import lookups as lu

    cat = category.strip() or "other"
    unt = unit.strip() or "gal"
    lu.ensure(db, lu.PRODUCT_CATEGORY, cat)
    lu.ensure(db, lu.PRODUCT_UNIT, unt)
    db.add(InputProduct(name=name.strip(), category=cat, unit=unt))
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
    from app import lookups as lu

    if vendor.strip():
        lu.ensure(db, lu.VENDOR, vendor.strip())
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
    db.add(
        ProductReturn(
            product_id=product.id,
            field_id=int(field_id) if field_id.isdigit() else None,
            return_date=_d(return_date) or date.today(),
            quantity=qty,
            unit_cost=product.avg_unit_cost or 0,
            notes=notes.strip() or None,
        )
    )
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
    party_name = {p.id: p.name for p in parties}
    return templates.TemplateResponse(
        "invoices.html",
        {
            "request": request,
            "user": user,
            "active": "invoices",
            "farm_name": _farm(db),
            "invoices": invoices,
            "parties": parties,
            "party_name": party_name,
            "today": date.today().isoformat(),
        },
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not (
        perms.can_access(user.get("role"), "invoices")
        or perms.can_access(user.get("role"), "fields")
        or perms.can_access(user.get("role"), "money")
    ):
        return perms.deny()
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    lines = list(
        db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.id))
    )
    party = db.get(Party, inv.party_id) if inv.party_id else None
    field = db.get(Field, inv.field_id) if getattr(inv, "field_id", None) else None
    invoiced_ops = [
        f"{op.op_date} {op.op_type}"
        for op in db.scalars(
            select(FieldOperation).where(FieldOperation.invoice_id == invoice_id)
        )
    ]
    return templates.TemplateResponse(
        "invoice_detail.html",
        {
            "request": request,
            "user": user,
            "active": "invoices",
            "farm_name": _farm(db),
            "invoice": inv,
            "lines": lines,
            "party": party,
            "field": field,
            "invoiced_ops": invoiced_ops,
        },
    )


@router.get("/invoices/{invoice_id}/export.xlsx")
def invoice_export_xlsx(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not (
        perms.can_access(user.get("role"), "invoices")
        or perms.can_access(user.get("role"), "fields")
    ):
        return perms.deny()
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return RedirectResponse("/invoices", status_code=303)
    lines = list(
        db.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.id))
    )
    party = db.get(Party, inv.party_id) if inv.party_id else None
    field = db.get(Field, inv.field_id) if getattr(inv, "field_id", None) else None

    from openpyxl import Workbook
    from fastapi.responses import StreamingResponse
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append([_farm(db)])
    ws.append([f"Invoice #{inv.id}"])
    ws.append(["Bill to", party.name if party else ""])
    if field:
        ws.append(["Field", field.name])
    ws.append(["Date", str(inv.invoice_date)])
    ws.append(["Status", inv.status])
    ws.append([])
    ws.append(["Description", "Qty", "Rate", "Amount"])
    for line in lines:
        ws.append(
            [
                line.description,
                line.quantity,
                line.rate,
                round(float(line.quantity or 0) * float(line.rate or 0), 2),
            ]
        )
    ws.append([])
    ws.append(["Total", "", "", inv.total])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"invoice-{inv.id}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    return RedirectResponse(f"/invoices/{inv.id}", status_code=303)


@router.post("/invoices/{invoice_id}/paid")
def invoices_paid(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = _need(request, "invoices")
    if isinstance(user, RedirectResponse):
        return user
    inv = db.get(Invoice, invoice_id)
    if inv:
        inv.status = "paid"
        db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


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

    if import_type in ("cargill_contracts", "cgb_contracts"):
        batch = _start_contract_import(
            db,
            user=user,
            filename=file.filename or "contracts.csv",
            content=content,
            import_type=import_type,
        )
        return RedirectResponse(f"/upload/contracts/{batch.id}", status_code=303)

    if import_type in ("panorama_planting", "planting_csv", "seasonal_inputs"):
        batch = _start_planting_import(
            db,
            user=user,
            filename=file.filename or "planting.csv",
            content=content,
        )
        return RedirectResponse(f"/panorama/planting/{batch.id}", status_code=303)

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
        # Auto-open planting wizard when the file is a Seasonal Inputs CSV
        if safe.lower().endswith((".csv", ".txt")):
            from app import planting_import as pimp

            try:
                first = next(csv.reader(io.StringIO(content.decode("utf-8-sig", errors="replace"))), [])
            except Exception:  # noqa: BLE001
                first = []
            if pimp.is_seasonal_inputs_headers([str(h) for h in first]):
                batch = _start_planting_import(
                    db, user=user, filename=file.filename or safe, content=content
                )
                return RedirectResponse(f"/panorama/planting/{batch.id}", status_code=303)
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


def _start_contract_import(
    db: Session,
    *,
    user: dict,
    filename: str,
    content: bytes,
    import_type: str = "cargill_contracts",
) -> ImportBatch:
    from app import cargill_contract_import as cci

    safe = _safe_filename(filename)
    path = _upload_root() / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{safe}"
    path.write_bytes(content)
    extract = cci.read_tabular(path, content)
    source = "cgb" if import_type == "cgb_contracts" else "cargill"
    # Auto-detect when user picks Cargill but drops a CG&B Schedules file (or vice versa)
    headers = []
    if extract.get("sheets"):
        headers = list((extract["sheets"][0] or {}).get("headers") or [])
    detected = cci.detect_source_kind(headers)
    if detected in ("cgb", "cargill"):
        source = detected
        import_type = "cgb_contracts" if source == "cgb" else "cargill_contracts"
    payload = cci.build_payload(
        filename or safe,
        str(path),
        extract,
        import_source=source,
    )
    year = _year(db)
    if year:
        cci.mark_db_duplicates(db, year, payload["proposals"])
    label = "CG&B Schedules" if source == "cgb" else "Cargill contracts"
    batch = ImportBatch(
        filename=path.name,
        import_type=import_type,
        status="pending_review",
        row_count=len(payload.get("proposals") or []),
        notes=f"{label} · map columns then import",
        payload_json=cci.dump_payload(payload),
    )
    db.add(batch)
    log_activity(db, user.get("username"), "upload", f"{import_type}: {safe}")
    db.commit()
    db.refresh(batch)
    return batch


def _contract_import_batch(db: Session, batch_id: int) -> ImportBatch | None:
    batch = db.get(ImportBatch, batch_id)
    if not batch or batch.import_type not in ("cargill_contracts", "cgb_contracts"):
        return None
    return batch


# Back-compat aliases
def _start_cargill_contract_import(
    db: Session,
    *,
    user: dict,
    filename: str,
    content: bytes,
) -> ImportBatch:
    return _start_contract_import(
        db,
        user=user,
        filename=filename,
        content=content,
        import_type="cargill_contracts",
    )


def _cargill_batch(db: Session, batch_id: int) -> ImportBatch | None:
    return _contract_import_batch(db, batch_id)


@router.get("/upload/contracts/{batch_id}", response_class=HTMLResponse)
def cargill_contracts_review(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import cargill_contract_import as cci

    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = cci.load_payload(batch.payload_json)

    # Returning from Add New — apply party to that row
    added = request.query_params.get("with_party_added")
    row_raw = request.query_params.get("row")
    if added and added.isdigit() and row_raw is not None and str(row_raw).isdigit():
        party = db.get(Party, int(added))
        row_i = int(row_raw)
        proposals = list(payload.get("proposals") or [])
        if party and 0 <= row_i < len(proposals):
            proposals[row_i]["with_party_id"] = party.id
            payload["proposals"] = proposals
            payload["step"] = "rows"
            batch.payload_json = cci.dump_payload(payload)
            db.commit()

    parties = list(db.scalars(select(Party).order_by(Party.name)))
    return templates.TemplateResponse(
        "cargill_contract_upload.html",
        {
            "request": request,
            "user": user,
            "active": "upload",
            "farm_name": _farm(db),
            "batch": batch,
            "payload": payload,
            "field_defs": cci.CONTRACT_FIELDS,
            "parties": parties,
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/upload/contracts/{batch_id}/map")
async def cargill_contracts_map(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import cargill_contract_import as cci

    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = cci.load_payload(batch.payload_json)
    form = await request.form()

    if str(form.get("sheet_only") or "") == "1":
        try:
            sheet_index = int(form.get("sheet_index") or 0)
        except ValueError:
            sheet_index = 0
        stored = Path(payload.get("stored_path") or "")
        if stored.exists():
            extract = cci.read_tabular(stored)
            sheets = extract.get("sheets") or []
            if 0 <= sheet_index < len(sheets):
                sheet = sheets[sheet_index]
                payload["sheet_index"] = sheet_index
                payload["sheets"] = [
                    {
                        "name": s.get("name"),
                        "headers": s.get("headers") or [],
                        "row_count": len(s.get("rows") or []),
                    }
                    for s in sheets
                ]
                payload["active_sheet"] = {
                    "name": sheet.get("name"),
                    "headers": list(sheet.get("headers") or []),
                    "rows": list(sheet.get("rows") or [])[:800],
                }
                payload["column_map"] = cci.guess_column_map(
                    payload["active_sheet"]["headers"],
                    payload.get("import_source"),
                )
                payload["proposals"] = cci.build_proposals(payload)
                year = _year(db)
                if year:
                    cci.mark_db_duplicates(db, year, payload["proposals"])
                payload["step"] = "map"
                batch.payload_json = cci.dump_payload(payload)
                batch.row_count = len(payload["proposals"])
                db.commit()
        return RedirectResponse(f"/upload/contracts/{batch_id}", status_code=303)

    cmap: dict[str, int | None] = {}
    for key in cci.FIELD_KEYS:
        raw = form.get(f"map_{key}")
        if raw in (None, ""):
            cmap[key] = None
        else:
            try:
                cmap[key] = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cmap[key] = None
    payload["column_map"] = cmap
    payload["include_only_contract_rows"] = form.get("include_only_contract_rows") == "1"
    old_proposals = list(payload.get("proposals") or [])
    payload["proposals"] = cci.merge_proposal_choices(
        cci.build_proposals(payload),
        old_proposals,
    )
    year = _year(db)
    if year:
        cci.mark_db_duplicates(db, year, payload["proposals"])
    payload["step"] = "rows"
    batch.payload_json = cci.dump_payload(payload)
    batch.row_count = len(payload["proposals"])
    db.commit()
    return RedirectResponse(f"/upload/contracts/{batch_id}", status_code=303)


@router.post("/upload/contracts/{batch_id}/back")
def cargill_contracts_back(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import cargill_contract_import as cci

    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = cci.load_payload(batch.payload_json)
    payload["step"] = "map"
    batch.payload_json = cci.dump_payload(payload)
    db.commit()
    return RedirectResponse(f"/upload/contracts/{batch_id}", status_code=303)


@router.post("/upload/contracts/{batch_id}/commit")
async def cargill_contracts_commit(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import cargill_contract_import as cci

    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    year = _year(db)
    if not year:
        return RedirectResponse(
            f"/upload/contracts/{batch_id}?msg=Set+an+active+crop+year+first",
            status_code=303,
        )
    payload = cci.load_payload(batch.payload_json)
    proposals = list(payload.get("proposals") or [])
    form = await request.form()
    for i, p in enumerate(proposals):
        if p.get("dup_in_db") or p.get("dup_in_file"):
            p["include"] = False
            continue
        p["include"] = form.get(f"include_{i}") == "1"
        raw_with = str(form.get(f"with_party_{i}") or "").strip()
        if raw_with.isdigit():
            wid = int(raw_with)
            p["with_party_id"] = wid if db.get(Party, wid) else None
        else:
            p["with_party_id"] = None
    summary = cci.commit_proposals(db, year, proposals)
    payload["proposals"] = proposals
    payload["commit_summary"] = summary
    payload["step"] = "done"
    batch.payload_json = cci.dump_payload(payload)
    batch.status = "imported"
    batch.row_count = summary.get("created", 0)
    batch.notes = (
        f"Created {summary.get('created', 0)}; "
        f"skipped dups {summary.get('skipped_duplicates', 0)}; "
        f"unchecked {summary.get('skipped_unchecked', 0)}"
    )
    log_activity(
        db,
        user.get("username"),
        "cargill_contracts_import",
        batch.notes,
    )
    db.commit()
    return RedirectResponse(f"/upload/contracts/{batch_id}", status_code=303)


def _apply_review_form_to_proposals(
    db: Session,
    proposals: list,
    form,
) -> None:
    for i, p in enumerate(proposals):
        if p.get("dup_in_db") or p.get("dup_in_file"):
            p["include"] = False
            continue
        # When checkbox is present it's included; disabled dups use hidden 0
        p["include"] = form.get(f"include_{i}") == "1"
        raw_with = str(form.get(f"with_party_{i}") or "").strip()
        if raw_with.isdigit():
            wid = int(raw_with)
            p["with_party_id"] = wid if db.get(Party, wid) else None
        elif raw_with == "__add_new__":
            pass  # keep previous
        else:
            p["with_party_id"] = None


@router.post("/upload/contracts/{batch_id}/save-and-add-party")
async def cargill_contracts_save_and_add_party(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    from app import cargill_contract_import as cci

    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    payload = cci.load_payload(batch.payload_json)
    proposals = list(payload.get("proposals") or [])
    form = await request.form()
    _apply_review_form_to_proposals(db, proposals, form)
    payload["proposals"] = proposals
    payload["step"] = "rows"
    batch.payload_json = cci.dump_payload(payload)
    db.commit()
    row = str(form.get("add_party_row") or "0")
    if not row.isdigit():
        row = "0"
    return RedirectResponse(
        f"/upload/contracts/{batch_id}/new-with-party?row={row}",
        status_code=303,
    )


@router.get("/upload/contracts/{batch_id}/new-with-party", response_class=HTMLResponse)
def cargill_contracts_new_with_party(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    row = request.query_params.get("row") or "0"
    if not str(row).isdigit():
        row = "0"
    return templates.TemplateResponse(
        "cargill_contract_new_with.html",
        {
            "request": request,
            "user": user,
            "active": "upload",
            "farm_name": _farm(db),
            "batch": batch,
            "row": row,
            "party_types": [
                {"value": "partner", "label": "Partner"},
                {"value": "landlord", "label": "Landlord"},
                {"value": "other", "label": "Other"},
            ],
        },
    )


@router.post("/upload/contracts/{batch_id}/new-with-party")
def cargill_contracts_new_with_party_save(
    request: Request,
    batch_id: int,
    name: str = Form(...),
    party_type: str = Form("partner"),
    notes: str = Form(""),
    row: str = Form("0"),
    db: Session = Depends(get_db),
):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    batch = _cargill_batch(db, batch_id)
    if not batch:
        return RedirectResponse("/upload", status_code=303)
    raw = (name or "").strip()
    if not raw:
        return RedirectResponse(
            f"/upload/contracts/{batch_id}/new-with-party?row={row}",
            status_code=303,
        )
    existing = db.scalar(select(Party).where(Party.name == raw).limit(1))
    if existing:
        party = existing
    else:
        party = Party(
            name=raw,
            party_type=(party_type or "partner").strip() or "partner",
            notes=(notes.strip() or None),
        )
        db.add(party)
        log_activity(db, user.get("username"), "party_add", f"{raw} (contract with)")
        db.commit()
        db.refresh(party)
    row_q = row if str(row).isdigit() else "0"
    return RedirectResponse(
        f"/upload/contracts/{batch_id}?with_party_added={party.id}&row={row_q}",
        status_code=303,
    )


@router.post("/upload/contracts/{batch_id}/discard")
def cargill_contracts_discard(request: Request, batch_id: int, db: Session = Depends(get_db)):
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    batch = _cargill_batch(db, batch_id)
    if batch:
        batch.status = "discarded"
        db.commit()
    return RedirectResponse("/upload?msg=Import+discarded", status_code=303)


@router.post("/upload/batches/{batch_id}/discard")
def upload_batch_discard(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """Discard any master-upload import batch (with confirm on the form)."""
    user = _need(request, "upload")
    if isinstance(user, RedirectResponse):
        return user
    batch = db.get(ImportBatch, batch_id)
    if batch and (batch.status or "") not in ("discarded",):
        batch.status = "discarded"
        log_activity(db, user.get("username"), "import_discard", f"#{batch_id} {batch.filename}")
        db.commit()
    return RedirectResponse("/upload?msg=Import+discarded", status_code=303)
