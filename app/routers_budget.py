"""Budget hub: board, field desk/sheet/detail, category defaults, what-if, money landing."""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import budgeting as bud
from app import budget_detail as bdet
from app.database import get_db
from app.flash import redirect_flash
from app.formutil import as_list, parse_float
from app.importers import is_summary_field_name
from app.models import (
    AppSettings,
    CropYear,
    FertilizerProduct,
    Field,
    FieldBudgetFertLine,
    FieldBudgetPass,
    FieldBudgetSeedLine,
    FieldBudgetSprayPass,
    Hybrid,
    SprayMix,
)
from app import permissions as perms
from app.templating import templates

router = APIRouter(tags=["budget"])


def _need(request: Request, module: str = "budget"):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not perms.can_access(user.get("role"), module):
        return perms.deny()
    return user


def _year(db: Session) -> CropYear | None:
    settings = db.scalar(select(AppSettings).limit(1))
    if settings and settings.active_crop_year_id:
        return db.get(CropYear, settings.active_crop_year_id)
    return db.scalar(select(CropYear).where(CropYear.is_active == 1))


def _settings(db: Session) -> AppSettings:
    s = db.scalar(select(AppSettings).limit(1))
    if not s:
        s = AppSettings(farm_name="Beam Farm")
        db.add(s)
        db.flush()
    return s


def _farm_name(db: Session) -> str:
    return (_settings(db).farm_name or "Beam Farm").strip() or "Beam Farm"


def _real_fields(db: Session, year: CropYear) -> list[Field]:
    rows = list(
        db.scalars(
            select(Field).where(Field.crop_year_id == year.id).order_by(Field.name)
        )
    )
    return [f for f in rows if not is_summary_field_name(f.name or "")]


def _wants_json(request: Request) -> bool:
    return (
        (request.headers.get("x-requested-with") or "").lower() == "fetch"
        or (request.query_params.get("format") or "").lower() == "json"
        or "application/json" in (request.headers.get("accept") or "")
    )


def _safe_next(raw: str | None, *, default: str) -> str:
    s = (raw or "").strip()
    if s.startswith("/") and not s.startswith("//"):
        return s
    return default


def _slug_key(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return (base or "custom")[:50]


@router.get("/budget", response_class=HTMLResponse)
def budget_board(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    settings = _settings(db)
    cards: list = []
    rollup = {
        "planned": 0,
        "actual": 0,
        "remaining": 0,
        "revenue": 0,
        "profit": 0,
        "used_pct": None,
        "field_count": 0,
        "with_plan": 0,
        "loss_n": 0,
        "profit_n": 0,
        "over_plan_n": 0,
        "over_affordable_n": 0,
        "tone": "neutral",
    }
    if year:
        fields = _real_fields(db, year)
        cards, rollup = bud.load_budget_board(db, year, fields, settings)
    return templates.TemplateResponse(
        "budget_board.html",
        {
            "request": request,
            "user": user,
            "active": "budget_board",
            "farm_name": _farm_name(db),
            "year": year,
            "cards": cards,
            "rollup": rollup,
            "categories": bud.BUDGET_CATEGORIES,
        },
    )


@router.get("/budget/fields", response_class=HTMLResponse)
def budget_fields_desk(
    request: Request,
    crop: str = Query("all"),
    tone: str = Query("all"),
    db: Session = Depends(get_db),
):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    settings = _settings(db)
    cards: list = []
    rollup = {"tone": "neutral", "profit_n": 0, "loss_n": 0, "with_plan": 0, "field_count": 0}
    if year:
        fields = _real_fields(db, year)
        cards, rollup = bud.load_budget_board(db, year, fields, settings)
        if crop and crop != "all":
            cards = [c for c in cards if c["crop"] == crop]
        if tone and tone != "all":
            cards = [c for c in cards if c["tone"] == tone]
    return templates.TemplateResponse(
        "budget_fields.html",
        {
            "request": request,
            "user": user,
            "active": "budget_fields",
            "farm_name": _farm_name(db),
            "year": year,
            "cards": cards,
            "rollup": rollup,
            "filter_crop": crop or "all",
            "filter_tone": tone or "all",
        },
    )


@router.get("/budget/sheet", response_class=HTMLResponse)
def budget_sheet(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    settings = _settings(db)
    cards: list = []
    if year:
        fields = _real_fields(db, year)
        defaults = bud.load_budget_defaults(settings)
        for f in fields:
            bud.ensure_field_budget(db, f, year.id, defaults=defaults, settings=settings)
        db.commit()
        cards, _rollup = bud.load_budget_board(db, year, fields, settings)
    return templates.TemplateResponse(
        "budget_sheet.html",
        {
            "request": request,
            "user": user,
            "active": "budget_sheet",
            "farm_name": _farm_name(db),
            "year": year,
            "cards": cards,
            "categories": bud.BUDGET_CATEGORIES,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/budget/sheet/save")
async def budget_sheet_save(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    if not year:
        return RedirectResponse("/settings", status_code=303)

    form = await request.form()
    ids = [int(x) for x in as_list(form.getlist("field_id"))]
    prices = as_list(form.getlist("price_override"))
    margins = as_list(form.getlist("margin_goal_ac"))
    n = len(ids)
    cat_lists: dict[str, list] = {
        key: as_list(form.getlist(key)) for key, _ in bud.BUDGET_CATEGORIES
    }

    for label, vals in [("price_override", prices), ("margin_goal_ac", margins), *cat_lists.items()]:
        if len(vals) != n:
            msg = f"Budget sheet row mismatch ({label}: {len(vals)} vs {n}) — nothing saved."
            if _wants_json(request):
                return JSONResponse({"ok": False, "error": msg}, status_code=400)
            return redirect_flash(request, "/budget/sheet", msg, "error")

    settings = _settings(db)
    defaults = bud.load_budget_defaults(settings)
    updated = 0
    for i, fid in enumerate(ids):
        field = db.get(Field, fid)
        if not field or field.crop_year_id != year.id:
            continue
        budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)
        po_raw = str(prices[i] or "").strip()
        budget.price_override = parse_float(po_raw, default=None) if po_raw else None
        budget.margin_goal_ac = parse_float(str(margins[i] or "0")) or 0.0
        amounts = {k: parse_float(str(cat_lists[k][i] or "0")) or 0.0 for k in cat_lists}
        bud.set_budget_lines(db, budget, amounts)
        updated += 1

    db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "saved": updated})
    return RedirectResponse(f"/budget/sheet?saved={updated}", status_code=303)


@router.get("/budget/fields/{field_id}", response_class=HTMLResponse)
def budget_field_detail(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    field = db.get(Field, field_id)
    if not year or not field or field.crop_year_id != year.id:
        return RedirectResponse("/budget/fields", status_code=303)
    settings = _settings(db)
    defaults = bud.load_budget_defaults(settings)
    budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)
    db.commit()
    budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)
    detail = bdet.compute_detailed_budget(field, budget, settings=settings)
    cards, _ = bud.load_budget_board(db, year, [field], settings)
    card = cards[0] if cards else None

    hybrids = list(
        db.scalars(
            select(Hybrid)
            .where(Hybrid.crop_year_id == year.id)
            .order_by(Hybrid.crop, Hybrid.name)
        )
    )
    fert_products = list(
        db.scalars(
            select(FertilizerProduct)
            .where(FertilizerProduct.is_active == 1)
            .order_by(FertilizerProduct.sort_order, FertilizerProduct.name)
        )
    )
    sprays = list(
        db.scalars(
            select(SprayMix)
            .where(SprayMix.crop_year_id == year.id)
            .order_by(SprayMix.name)
        )
    )
    next_url = f"/budget/fields/{field_id}"
    return templates.TemplateResponse(
        "budget_field_detail.html",
        {
            "request": request,
            "user": user,
            "active": "budget_fields",
            "farm_name": _farm_name(db),
            "year": year,
            "field": field,
            "budget": budget,
            "card": card,
            "detail": detail,
            "hybrids": hybrids,
            "fert_products": fert_products,
            "sprays": sprays,
            "pass_catalog": bdet.DEFAULT_PASSES,
            "categories": bud.BUDGET_CATEGORIES,
            "next_url": next_url,
            "next_q": quote(next_url, safe=""),
            "travel_speed": float(settings.budget_travel_speed_mph or 30),
            "travel_rate": float(settings.budget_travel_rate_per_hr or 150),
        },
    )


@router.post("/budget/fields/{field_id}/save")
async def budget_field_save(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    field = db.get(Field, field_id)
    if not year or not field or field.crop_year_id != year.id:
        return RedirectResponse("/budget/fields", status_code=303)

    form = await request.form()
    settings = _settings(db)
    defaults = bud.load_budget_defaults(settings)
    budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)

    miles_raw = str(form.get("distance_miles") or "").strip()
    field.distance_miles = parse_float(miles_raw, default=None) if miles_raw else None
    po = str(form.get("price_override") or "").strip()
    budget.price_override = parse_float(po, default=None) if po else None
    budget.margin_goal_ac = parse_float(str(form.get("margin_goal_ac") or "0")) or 0.0
    budget.notes = str(form.get("notes") or "").strip() or None

    # Seed lines — replace
    for ln in list(budget.seed_lines or []):
        db.delete(ln)
    db.flush()
    seed_hids = as_list(form.getlist("seed_hybrid_id"))
    seed_acres = as_list(form.getlist("seed_acres"))
    seed_pops = as_list(form.getlist("seed_population"))
    seed_bags = as_list(form.getlist("seed_bag_kernels"))
    seed_costs = as_list(form.getlist("seed_cost_per_bag"))
    for i, hid_raw in enumerate(seed_hids):
        hid_s = str(hid_raw or "").strip()
        if not hid_s or hid_s == "__add_new__":
            continue
        try:
            hid = int(hid_s)
        except ValueError:
            continue
        ac = parse_float(str(seed_acres[i] if i < len(seed_acres) else "0")) or 0.0
        if ac <= 0:
            continue
        pop = parse_float(str(seed_pops[i] if i < len(seed_pops) else ""), default=None)
        bag_k = parse_float(str(seed_bags[i] if i < len(seed_bags) else "80000")) or 80000.0
        cpb = parse_float(str(seed_costs[i] if i < len(seed_costs) else ""), default=None)
        # Autofill cost from catalog when blank
        if cpb is None:
            hyb = db.get(Hybrid, hid)
            if hyb and hyb.cost_per_unit is not None:
                cpb = float(hyb.cost_per_unit)
        db.add(
            FieldBudgetSeedLine(
                budget_id=budget.id,
                hybrid_id=hid,
                acres=ac,
                population=pop,
                bag_kernels=bag_k,
                cost_per_bag=cpb,
                sort_order=i,
            )
        )

    for ln in list(budget.fert_lines or []):
        db.delete(ln)
    db.flush()
    prod_ids = as_list(form.getlist("fert_product_id"))
    fert_rates = as_list(form.getlist("fert_rate"))
    for i, pid_raw in enumerate(prod_ids):
        pid_s = str(pid_raw or "").strip()
        if not pid_s or pid_s == "__add_new__":
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        rate = parse_float(str(fert_rates[i] if i < len(fert_rates) else "0")) or 0.0
        if rate <= 0:
            continue
        db.add(
            FieldBudgetFertLine(
                budget_id=budget.id,
                product_id=pid,
                rate_per_ac=rate,
                sort_order=i,
            )
        )

    pass_ids = as_list(form.getlist("pass_id"))
    pass_enabled = as_list(form.getlist("pass_enabled"))
    pass_hired = as_list(form.getlist("pass_hired"))
    pass_self = as_list(form.getlist("pass_self_rate"))
    pass_hire_rate = as_list(form.getlist("pass_hired_rate"))
    pass_trips = as_list(form.getlist("pass_round_trips"))
    enabled_set = {str(x) for x in pass_enabled}
    hired_set = {str(x) for x in pass_hired}
    by_id = {p.id: p for p in (budget.passes or [])}
    for i, pid_raw in enumerate(pass_ids):
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        p = by_id.get(pid)
        if not p:
            continue
        p.enabled = 1 if str(pid) in enabled_set else 0
        p.is_hired = 1 if str(pid) in hired_set else 0
        p.self_rate_ac = parse_float(str(pass_self[i] if i < len(pass_self) else "0")) or 0.0
        p.hired_rate_ac = parse_float(str(pass_hire_rate[i] if i < len(pass_hire_rate) else "0")) or 0.0
        p.round_trips = parse_float(str(pass_trips[i] if i < len(pass_trips) else "1")) or 0.0

    for sp in list(budget.spray_passes or []):
        db.delete(sp)
    db.flush()
    spray_labels = as_list(form.getlist("spray_label"))
    spray_mix_ids = as_list(form.getlist("spray_mix_id"))
    spray_hired = as_list(form.getlist("spray_hired"))
    spray_hire_rates = as_list(form.getlist("spray_hired_rate"))
    spray_trips = as_list(form.getlist("spray_round_trips"))
    spray_hired_set = {str(i) for i in spray_hired}
    for i, mix_raw in enumerate(spray_mix_ids):
        mix_s = str(mix_raw or "").strip()
        if not mix_s or mix_s == "__add_new__":
            continue
        try:
            mid = int(mix_s)
        except ValueError:
            continue
        label = str(spray_labels[i] if i < len(spray_labels) else f"Spray {i + 1}").strip() or f"Spray {i + 1}"
        db.add(
            FieldBudgetSprayPass(
                budget_id=budget.id,
                label=label,
                spray_mix_id=mid,
                is_hired=1 if str(i) in spray_hired_set else 0,
                hired_rate_ac=parse_float(str(spray_hire_rates[i] if i < len(spray_hire_rates) else "0")) or 0.0,
                round_trips=parse_float(str(spray_trips[i] if i < len(spray_trips) else "1")) or 0.0,
                sort_order=i,
            )
        )

    db.flush()
    budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)
    detail = bdet.compute_detailed_budget(field, budget, settings=settings)
    amounts = dict(detail["rollup"])
    amounts["insurance"] = parse_float(str(form.get("insurance") or "0")) or 0.0
    amounts["other"] = parse_float(str(form.get("other") or "0")) or 0.0
    # Rent comes from field; keep rollup rent in sync
    bud.set_budget_lines(db, budget, amounts)
    bdet.sync_budget_to_operations(db, field, budget, detail)
    db.commit()
    return redirect_flash(
        request,
        f"/budget/fields/{field_id}",
        f"Budget saved · plan ${detail['planned_total']:,.0f} · synced to field records.",
        "ok",
    )


@router.get("/budget/fields/{field_id}/new-hybrid", response_class=HTMLResponse)
def budget_new_hybrid(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default=f"/budget/fields/{field_id}")
    return RedirectResponse(
        f"/inputs?focus=seed&next={quote(next_url, safe='')}",
        status_code=303,
    )


@router.get("/budget/fields/{field_id}/new-pass", response_class=HTMLResponse)
def budget_new_pass(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    field = db.get(Field, field_id)
    if not field:
        return RedirectResponse("/budget/fields", status_code=303)
    next_url = _safe_next(request.query_params.get("next"), default=f"/budget/fields/{field_id}")
    return templates.TemplateResponse(
        "budget_new_item.html",
        {
            "request": request,
            "user": user,
            "active": "budget_fields",
            "farm_name": _farm_name(db),
            "page_title": "Add field pass",
            "page_lede": "Custom tillage / application / harvest pass for this field budget.",
            "form_action": f"/budget/fields/{field_id}/new-pass",
            "next_url": next_url,
            "kind": "pass",
            "default_crop": field.crop or "",
        },
    )


@router.post("/budget/fields/{field_id}/new-pass")
def budget_new_pass_save(
    field_id: int,
    request: Request,
    label: str = Form(...),
    self_rate_ac: str = Form("0"),
    round_trips: str = Form("1"),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    field = db.get(Field, field_id)
    next_url = _safe_next(next or request.query_params.get("next"), default=f"/budget/fields/{field_id}")
    if not year or not field:
        return RedirectResponse("/budget/fields", status_code=303)
    settings = _settings(db)
    defaults = bud.load_budget_defaults(settings)
    budget = bud.ensure_field_budget(db, field, year.id, defaults=defaults, settings=settings)
    key = f"custom_{_slug_key(label)}"
    existing_keys = {p.pass_key for p in (budget.passes or [])}
    base = key
    n = 2
    while key in existing_keys:
        key = f"{base}_{n}"
        n += 1
    sort = max((p.sort_order for p in (budget.passes or [])), default=-1) + 1
    db.add(
        FieldBudgetPass(
            budget_id=budget.id,
            pass_key=key,
            pass_label=label.strip(),
            enabled=1,
            is_hired=0,
            hired_rate_ac=0.0,
            self_rate_ac=parse_float(self_rate_ac) or 0.0,
            round_trips=parse_float(round_trips) or 1.0,
            sort_order=sort,
        )
    )
    db.commit()
    return redirect_flash(request, next_url, f"Added pass “{label.strip()}”.", "ok")


@router.get("/budget/fields/{field_id}/new-spray", response_class=HTMLResponse)
def budget_new_spray(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default=f"/budget/fields/{field_id}")
    return RedirectResponse(
        f"/inputs?focus=spray&next={quote(next_url, safe='')}",
        status_code=303,
    )


@router.get("/budget/products", response_class=HTMLResponse)
def budget_products(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default="/inputs?focus=fert")
    q = quote(next_url, safe="") if next_url and next_url != "/inputs?focus=fert" else ""
    dest = "/inputs?focus=fert"
    if q:
        dest = f"/inputs?focus=fert&next={q}"
    return RedirectResponse(dest, status_code=303)


@router.post("/budget/products/save")
async def budget_products_save(request: Request, db: Session = Depends(get_db)):
    """Legacy endpoint — fertilizer edits live under Inputs."""
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse("/inputs?focus=fert", status_code=303)


@router.get("/budget/fields/{field_id}/new-fert", response_class=HTMLResponse)
def budget_new_fert_redirect(field_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    next_url = _safe_next(request.query_params.get("next"), default=f"/budget/fields/{field_id}")
    return RedirectResponse(
        f"/inputs?focus=fert&next={quote(next_url, safe='')}",
        status_code=303,
    )


@router.get("/budget/defaults", response_class=HTMLResponse)
@router.get("/budget/categories", response_class=HTMLResponse)
def budget_defaults(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    settings = _settings(db)
    crop_tmpls = bud.load_budget_templates(settings)
    corn = crop_tmpls.get("Corn") or bud._normalize_crop_template("Corn", {})
    soy = crop_tmpls.get("Soybeans") or bud._normalize_crop_template("Soybeans", {})
    # Seed categories from COP if empty
    if sum((corn.get("categories") or {}).values()) <= 0 and settings.corn_cost_per_ac:
        corn["categories"]["other"] = float(settings.corn_cost_per_ac or 0)
    if sum((soy.get("categories") or {}).values()) <= 0 and settings.soy_cost_per_ac:
        soy["categories"]["other"] = float(settings.soy_cost_per_ac or 0)

    year = _year(db)
    fields = _real_fields(db, year) if year else []
    bdet.seed_fertilizer_catalog(db)
    db.commit()
    fert_products = list(
        db.scalars(
            select(FertilizerProduct)
            .where(FertilizerProduct.is_active == 1)
            .order_by(FertilizerProduct.sort_order, FertilizerProduct.name)
        )
    )
    # Enrich template fert rows from catalog when price/unit/density not stored yet
    by_name = {p.name: p for p in fert_products}
    for tmpl in (corn, soy):
        for row in tmpl.get("fert") or []:
            prod = by_name.get(str(row.get("product_name") or ""))
            if not prod:
                continue
            if row.get("price_per_ton") is None:
                row["price_per_ton"] = float(prod.price_per_ton or 0)
            if not row.get("apply_unit"):
                row["apply_unit"] = prod.apply_unit or "lb"
            if row.get("density_lb_per_gal") is None and prod.density_lb_per_gal is not None:
                row["density_lb_per_gal"] = float(prod.density_lb_per_gal)
    return templates.TemplateResponse(
        "budget_defaults.html",
        {
            "request": request,
            "user": user,
            "active": "budget_defaults",
            "farm_name": _farm_name(db),
            "year": year,
            "fields": fields,
            "categories": bud.BUDGET_CATEGORIES,
            "pass_catalog": bdet.DEFAULT_PASSES,
            "corn": corn,
            "soy": soy,
            "fert_products": fert_products,
            "travel_speed": float(settings.budget_travel_speed_mph or 30),
            "travel_rate": float(settings.budget_travel_rate_per_hr or 150),
            "corn_cop": float(settings.corn_cost_per_ac or 0),
            "soy_cop": float(settings.soy_cost_per_ac or 0),
        },
    )


def _parse_crop_template_from_form(form, prefix: str, crop: str) -> dict:
    from app.formutil import as_list

    cats = {k: parse_float(str(form.get(f"{prefix}_cat_{k}") or "0")) or 0.0 for k, _ in bud.BUDGET_CATEGORIES}
    seed_pop = parse_float(str(form.get(f"{prefix}_seed_pop") or ""), default=None)
    seed_bag = parse_float(str(form.get(f"{prefix}_seed_bag") or "80000")) or 80000.0
    seed_cost = parse_float(str(form.get(f"{prefix}_seed_cost") or ""), default=None)
    margin = parse_float(str(form.get(f"{prefix}_margin") or "0")) or 0.0

    pass_keys = as_list(form.getlist(f"{prefix}_pass_key"))
    pass_on = {str(x) for x in as_list(form.getlist(f"{prefix}_pass_on"))}
    pass_hired = {str(x) for x in as_list(form.getlist(f"{prefix}_pass_hired"))}
    pass_self = as_list(form.getlist(f"{prefix}_pass_self"))
    pass_hire = as_list(form.getlist(f"{prefix}_pass_hire"))
    pass_trips = as_list(form.getlist(f"{prefix}_pass_trips"))
    passes: dict = {}
    label_by_key = {k: lab for k, lab, _t, _o in bdet.DEFAULT_PASSES}
    for i, key in enumerate(pass_keys):
        key_s = str(key)
        passes[key_s] = {
            "label": label_by_key.get(key_s, key_s),
            "enabled": 1 if key_s in pass_on else 0,
            "is_hired": 1 if key_s in pass_hired else 0,
            "self_rate_ac": parse_float(str(pass_self[i] if i < len(pass_self) else "0")) or 0.0,
            "hired_rate_ac": parse_float(str(pass_hire[i] if i < len(pass_hire) else "0")) or 0.0,
            "round_trips": parse_float(str(pass_trips[i] if i < len(pass_trips) else "1")) or 1.0,
        }

    fert_names = as_list(form.getlist(f"{prefix}_fert_name"))
    fert_rates = as_list(form.getlist(f"{prefix}_fert_rate"))
    fert_units = as_list(form.getlist(f"{prefix}_fert_unit"))
    fert_prices = as_list(form.getlist(f"{prefix}_fert_price"))
    fert_dens = as_list(form.getlist(f"{prefix}_fert_density"))
    fert = []
    fert_ac_total = 0.0
    fert_ac_any = False
    for i, name_raw in enumerate(fert_names):
        name = str(name_raw or "").strip()
        if not name:
            continue
        rate = parse_float(str(fert_rates[i] if i < len(fert_rates) else "0")) or 0.0
        if rate <= 0:
            continue
        unit = str(fert_units[i] if i < len(fert_units) else "lb").strip().lower()
        if unit not in ("lb", "gal"):
            unit = "lb"
        price = parse_float(str(fert_prices[i] if i < len(fert_prices) else ""), default=None)
        dens = parse_float(str(fert_dens[i] if i < len(fert_dens) else ""), default=None)
        row: dict = {
            "product_name": name,
            "rate_per_ac": rate,
            "apply_unit": unit,
        }
        if price is not None:
            row["price_per_ton"] = price
        if dens is not None:
            row["density_lb_per_gal"] = dens
        fert.append(row)
        # Calculator: dry (lb/ac÷2000)×$/ton · liquid (gal×lb/gal÷2000)×$/ton
        if price is not None and price >= 0:
            if unit == "gal":
                if dens and dens > 0:
                    tons = (rate * dens) / 2000.0
                    fert_ac_total += tons * price
                    fert_ac_any = True
            else:
                tons = rate / 2000.0
                fert_ac_total += tons * price
                fert_ac_any = True

    # Seed/fert category $/ac always follow the calculators when those inputs are present
    if seed_pop and seed_bag and seed_cost is not None:
        cats["seed"] = round((float(seed_pop) / float(seed_bag)) * float(seed_cost), 2)
    if fert_ac_any:
        cats["fertilizer"] = round(fert_ac_total, 2)

    return {
        "categories": cats,
        "seed": {"population": seed_pop, "bag_kernels": seed_bag, "cost_per_bag": seed_cost},
        "passes": passes,
        "fert": fert,
        "margin_goal_ac": margin,
    }


@router.post("/budget/defaults/save")
@router.post("/budget/categories/save")
async def budget_defaults_save(request: Request, db: Session = Depends(get_db)):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    settings = _settings(db)
    settings.budget_travel_speed_mph = parse_float(str(form.get("travel_speed") or "30")) or 30.0
    settings.budget_travel_rate_per_hr = parse_float(str(form.get("travel_rate") or "150")) or 150.0

    crop_templates = {
        "Corn": _parse_crop_template_from_form(form, "corn", "Corn"),
        "Soybeans": _parse_crop_template_from_form(form, "soy", "Soybeans"),
    }
    bud.save_budget_templates(settings, crop_templates)

    action = str(form.get("action") or "save")
    msg = "Budget defaults saved."
    year = _year(db)
    if action in ("apply_empty", "apply_overwrite") and year:
        ids = []
        for raw in as_list(form.getlist("field_ids")):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if not ids:
            db.commit()
            return redirect_flash(
                request,
                "/budget/defaults",
                "Defaults saved, but no fields were selected to apply.",
                "warn",
            )
        all_fields = {f.id: f for f in _real_fields(db, year)}
        selected = [all_fields[i] for i in ids if i in all_fields]
        parts: set[str] = set()
        if str(form.get("part_categories") or ""):
            parts.add("categories")
        if str(form.get("part_passes") or ""):
            parts.add("passes")
        if str(form.get("part_seed") or ""):
            parts.add("seed")
        if str(form.get("part_fert") or ""):
            parts.add("fert")
        if str(form.get("part_margin") or ""):
            parts.add("margin")
        if not parts:
            parts = {"categories", "passes", "seed", "fert", "margin"}
        n = bud.apply_budget_templates_to_fields(
            db,
            year.id,
            selected,
            crop_templates,
            settings=settings,
            only_empty=(action == "apply_empty"),
            parts=parts,
        )
        if action == "apply_empty":
            msg = f"Defaults saved and applied to {n} empty field{'s' if n != 1 else ''}."
        else:
            msg = f"Defaults saved and overwritten on {n} field{'s' if n != 1 else ''}."
    db.commit()
    return redirect_flash(request, "/budget/defaults", msg, "ok")


@router.get("/budget/what-if", response_class=HTMLResponse)
def budget_what_if(
    request: Request,
    field_id: int | None = Query(None),
    extra: str = Query(""),
    yield_gain: str = Query(""),
    db: Session = Depends(get_db),
):
    user = _need(request)
    if isinstance(user, RedirectResponse):
        return user
    year = _year(db)
    settings = _settings(db)
    fields = _real_fields(db, year) if year else []
    cards, _ = bud.load_budget_board(db, year, fields, settings) if year else ([], {})
    card = None
    if field_id:
        card = next((c for c in cards if c["field_id"] == field_id), None)
    result = None
    if card:
        result = bud.what_if_extra_spend(
            acres=card["acres"],
            expected_bu=card["expected_bu"],
            price=card["price"],
            planned_total=card["planned_total"],
            extra_spend=parse_float(extra) or 0.0,
            yield_gain_bu_ac=parse_float(yield_gain) or 0.0,
        )
    return templates.TemplateResponse(
        "budget_whatif.html",
        {
            "request": request,
            "user": user,
            "active": "budget_whatif",
            "farm_name": _farm_name(db),
            "year": year,
            "cards": cards,
            "card": card,
            "field_id": field_id,
            "extra": extra,
            "yield_gain": yield_gain,
            "result": result,
        },
    )


@router.get("/budget/money", response_class=HTMLResponse)
def budget_money_ops(request: Request, db: Session = Depends(get_db)):
    """Former Money main ops — now under Budget."""
    user = _need(request, "settlements")
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "hub_ops.html",
        {
            "request": request,
            "user": user,
            "active": "money_ops",
            "farm_name": _farm_name(db),
            "ops_title": "Money — under Budget",
            "ops_lede": "Settlements, balance sheet, invoices, and equipment. Budget planning lives in the Budget tabs.",
            "ops_actions": [
                {"href": "/settlements", "label": "Settlements", "primary": True},
                {"href": "/balance", "label": "Balance sheet"},
                {"href": "/invoices", "label": "Invoices"},
                {"href": "/equipment", "label": "Equipment costs"},
                {"href": "/budget", "label": "Back to Budget board"},
            ],
            "ops_note": "",
            "ops_lists_href": "/money/lists",
        },
    )
