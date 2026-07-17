from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.auth import DEFAULT_USERS, hash_password, verify_password
from app.database import Base, SessionLocal, engine, get_db
from app.equipment_costs import cost_per_acre, equipment_annual_costs
from app.migrate import run_migrations
from app.models import (
    AppSettings,
    BalanceSheetItem,
    BalanceSnapshot,
    CropYear,
    Equipment,
    EquipmentEvent,
    EquipmentFuel,
    EquipmentMaintenance,
    Field,
    FieldShare,
    Party,
    User,
)
from app import permissions as perms
from app.importers import is_summary_field_name
from app.routers_extra import router as extra_router
from app.routers_more import router as more_router
from app.templating import templates

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

app = FastAPI(title="Beam Farm Ops")
app.add_middleware(
    SessionMiddleware,
    secret_key=__import__("os").getenv("SECRET_KEY", "beam-farm-ops-change-me-in-production"),
)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

OWNERSHIP_LABELS = {
    "operated_by_me": "Operated by me",
    "on_shares": "On shares",
    "custom_work": "Custom work",
}
LEASE_LABELS = {
    "cash_rent": "Cash Rent/Property Taxes",
    "flex_rent": "Flex rent",
    "crop_share": "Crop share",
    "none": "None",
    "": "—",
    None: "—",
}
FINANCE_LABELS = {
    "owned": "Owned (free & clear)",
    "loan": "Owned — making payments",
    "leased": "Leased",
}
EQUIP_CATEGORIES = [
    "tractor",
    "planter",
    "sprayer",
    "combine",
    "tillage",
    "truck",
    "trailer",
    "other",
]


def seed_initial_data(db: Session) -> None:
    if not db.scalar(select(User).limit(1)):
        for user in DEFAULT_USERS:
            db.add(
                User(
                    username=user["username"],
                    display_name=user["display_name"],
                    password_hash=hash_password(user["password"]),
                    role=user.get("role", "owner"),
                )
            )

    year = db.scalar(select(CropYear).where(CropYear.year == 2026))
    if not year:
        year = CropYear(year=2026, label="2026 Crop Year", is_active=1)
        db.add(year)
        db.flush()

    year_2027 = db.scalar(select(CropYear).where(CropYear.year == 2027))
    if not year_2027:
        db.add(CropYear(year=2027, label="2027 Crop Year", is_active=0))

    settings = db.scalar(select(AppSettings).limit(1))
    if not settings:
        db.add(AppSettings(farm_name="Beam Farm", active_crop_year_id=year.id))
    elif not settings.active_crop_year_id:
        settings.active_crop_year_id = year.id

    db.commit()


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    run_migrations()
    db = SessionLocal()
    try:
        seed_initial_data(db)
    finally:
        db.close()


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def require_login(request: Request) -> dict | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return user


def require_module(request: Request, module: str) -> dict | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not perms.can_access(user.get("role"), module):
        return perms.deny()
    return user


def real_fields(fields: list[Field]) -> list[Field]:
    """Drop spreadsheet rollup rows (e.g. name 'Totals') that are not real fields."""
    return [f for f in fields if not is_summary_field_name(f.name)]


def crop_acres_mine(db: Session, year: CropYear | None) -> float:
    if not year:
        return 0.0
    fields = real_fields(list(db.scalars(select(Field).where(Field.crop_year_id == year.id))))
    return round(sum(f.acres_mine or 0 for f in fields if f.crop in ("Corn", "Soybeans")), 1)


def get_active_year(db: Session) -> Optional[CropYear]:
    settings = db.scalar(select(AppSettings).limit(1))
    if settings and settings.active_crop_year_id:
        year = db.get(CropYear, settings.active_crop_year_id)
        if year:
            return year
    return db.scalar(select(CropYear).where(CropYear.is_active == 1).limit(1))


def farm_name(db: Session) -> str:
    settings = db.scalar(select(AppSettings).limit(1))
    return settings.farm_name if settings else "Beam Farm"


def field_stats(fields: list[Field]) -> dict:
    fields = real_fields(fields)
    corn = sum(f.acres_mine for f in fields if f.crop == "Corn")
    soy = sum(f.acres_mine for f in fields if f.crop == "Soybeans")
    none = sum(f.acres_mine for f in fields if f.crop not in ("Corn", "Soybeans"))
    rent = sum(f.rent_my_share for f in fields)
    corn_bu = sum((f.expected_bushels or 0) for f in fields if f.crop == "Corn")
    soy_bu = sum((f.expected_bushels or 0) for f in fields if f.crop == "Soybeans")
    corn_rent = sum(f.rent_my_share for f in fields if f.crop == "Corn")
    soy_rent = sum(f.rent_my_share for f in fields if f.crop == "Soybeans")
    return {
        "field_count": len(fields),
        "corn_acres": round(corn, 1),
        "soy_acres": round(soy, 1),
        "none_acres": round(none, 1),
        "rent_total": round(rent, 2),
        "corn_bu": round(corn_bu, 0),
        "soy_bu": round(soy_bu, 0),
        "corn_rent": round(corn_rent, 2),
        "soy_rent": round(soy_rent, 2),
        "total_mine_acres": round(corn + soy + none, 1),
        "operated": sum(1 for f in fields if f.ownership_mode == "operated_by_me"),
        "shares": sum(1 for f in fields if f.ownership_mode == "on_shares"),
        "custom": sum(1 for f in fields if f.ownership_mode == "custom_work"),
    }


def _cop_rates(db: Session) -> tuple[float, float]:
    settings = db.scalar(select(AppSettings).limit(1))
    corn = float((settings.corn_cost_per_ac if settings else 0) or 0)
    soy = float((settings.soy_cost_per_ac if settings else 0) or 0)
    return corn, soy


def _plan_cop_for_crop(crop: str, corn_cop: float, soy_cop: float) -> float:
    if crop == "Corn":
        return corn_cop
    if crop == "Soybeans":
        return soy_cop
    return 0.0


def _plan_cost_total(fields: list[Field], corn_cop: float, soy_cop: float) -> float:
    total = 0.0
    for f in fields:
        total += _plan_cop_for_crop(f.crop, corn_cop, soy_cop) * (f.acres_mine or 0)
    return round(total, 2)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if (
        not user
        or not verify_password(password, user.password_hash)
        or getattr(user, "is_active", 1) == 0
    ):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid username or password"},
            status_code=401,
        )
    request.session["user"] = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": getattr(user, "role", None) or "owner",
    }
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    fields = []
    stats = field_stats([])
    if year:
        fields = real_fields(
            list(
                db.scalars(
                    select(Field)
                    .where(Field.crop_year_id == year.id)
                    .options(joinedload(Field.party))
                    .order_by(Field.name)
                ).unique()
            )
        )
        stats = field_stats(fields)

    years = list(db.scalars(select(CropYear).order_by(CropYear.year.desc())))
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "active": "dashboard",
            "farm_name": farm_name(db),
            "year": year,
            "years": years,
            "stats": stats,
            "fields": fields[:8],
            "ownership_labels": OWNERSHIP_LABELS,
            "modules": [
                {"name": "Fields", "href": "/fields", "status": "ready", "module": "fields", "tone": "green", "blurb": "Acres, rent, costs"},
                {"name": "Trials", "href": "/trials", "status": "ready", "module": "trials", "tone": "sky", "blurb": "Compare what works"},
                {"name": "Insights / AI", "href": "/insights", "status": "ready", "module": "insights", "tone": "gold", "blurb": "Briefing + what-if"},
                {"name": "Panorama sync", "href": "/panorama", "status": "ready", "module": "panorama", "tone": "clay", "blurb": "File upload ready"},
                {"name": "Storage bins", "href": "/bins", "status": "ready", "module": "grain", "tone": "gold", "blurb": "Inventory & tickets"},
                {"name": "Marketing and Storage", "href": "/risk", "status": "ready", "module": "risk", "tone": "clay", "blurb": "Futures & basis % sold"},
                {"name": "Inputs & plans", "href": "/inputs", "status": "ready", "module": "library", "tone": "green", "blurb": "Seed, chem & costs"},
                {"name": "Purchases", "href": "/purchases", "status": "ready", "module": "purchases", "tone": "sky", "blurb": "Avg cost → fields"},
                {"name": "Invoices", "href": "/invoices", "status": "ready", "module": "invoices", "tone": "gold", "blurb": "Custom work"},
                {"name": "Settlements", "href": "/settlements", "status": "ready", "module": "settlements", "tone": "clay", "blurb": "Landlord / partner"},
                {"name": "Trucking", "href": "/trucking", "status": "ready", "module": "trucking", "tone": "sky", "blurb": "Rates & loads"},
                {"name": "Equipment", "href": "/equipment", "status": "ready", "module": "equipment", "tone": "green", "blurb": "$/acre ownership"},
                {"name": "Balance sheet", "href": "/balance", "status": "ready", "module": "balance", "tone": "gold", "blurb": "Banker pack"},
                {"name": "Scan receipts", "href": "/scan", "status": "ready", "module": "upload", "tone": "sky", "blurb": "Phone photo + guided file"},
                {"name": "Master Upload", "href": "/upload", "status": "ready", "module": "upload", "tone": "sky", "blurb": "Cargill & Excel"},
                {"name": "Team access", "href": "/team", "status": "ready", "module": "team", "tone": "green", "blurb": "Roles & logins"},
            ],
            "role": user.get("role"),
            "can_access": perms.can_access,
        },
    )


@app.post("/years/activate")
def activate_year(
    request: Request,
    year_id: int = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    year = db.get(CropYear, year_id)
    if not year:
        return RedirectResponse("/", status_code=303)
    for y in db.scalars(select(CropYear)):
        y.is_active = 1 if y.id == year.id else 0
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.active_crop_year_id = year.id
    db.commit()
    dest = (next or "").strip()
    if not dest.startswith("/") or dest.startswith("//"):
        dest = request.headers.get("referer") or "/"
        # prefer path only from referer
        if dest.startswith("http"):
            from urllib.parse import urlparse

            dest = urlparse(dest).path or "/"
    if not dest.startswith("/") or dest.startswith("//"):
        dest = "/"
    return RedirectResponse(dest, status_code=303)


@app.post("/years/add")
def add_year(
    request: Request,
    year: int = Form(...),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    existing = db.scalar(select(CropYear).where(CropYear.year == year))
    if not existing:
        db.add(CropYear(year=year, label=label or f"{year} Crop Year", is_active=0))
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.get("/fields", response_class=HTMLResponse)
def fields_list(
    request: Request,
    crop: Optional[str] = None,
    ownership: Optional[str] = None,
    view: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    fields: list[Field] = []
    if year:
        q = (
            select(Field)
            .where(Field.crop_year_id == year.id)
            .options(joinedload(Field.party))
            .order_by(Field.crop, Field.name)
        )
        fields = real_fields(list(db.scalars(q).unique()))
        if crop and crop != "all":
            fields = [f for f in fields if f.crop == crop]
        if ownership and ownership != "all":
            fields = [f for f in fields if f.ownership_mode == ownership]

    corn_cop, soy_cop = _cop_rates(db)
    stats = field_stats(fields)
    stats["plan_cost_total"] = _plan_cost_total(fields, corn_cop, soy_cop)
    stats["corn_cop"] = corn_cop
    stats["soy_cop"] = soy_cop

    # Year operations summary for field cards (no need to open each field)
    field_ops_summary: dict[int, dict] = {}
    if year and fields:
        from app.models import FieldHybrid, FieldOperation, FieldSprayMix

        fids = [f.id for f in fields]
        ops = list(
            db.scalars(
                select(FieldOperation)
                .where(FieldOperation.field_id.in_(fids))
                .order_by(FieldOperation.op_date.desc(), FieldOperation.id.desc())
            )
        )
        hybrid_links = list(
            db.scalars(select(FieldHybrid).where(FieldHybrid.field_id.in_(fids)))
        )
        spray_links = list(
            db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id.in_(fids)))
        )
        for fid in fids:
            field_ops_summary[fid] = {"types": [], "labels": [], "count": 0, "latest": None}

        type_order: dict[int, list[str]] = {fid: [] for fid in fids}
        latest: dict[int, object] = {}
        counts: dict[int, int] = {fid: 0 for fid in fids}

        for o in ops:
            fid = o.field_id
            counts[fid] = counts.get(fid, 0) + 1
            label = (o.op_type or "Op").strip() or "Op"
            if label not in type_order[fid]:
                type_order[fid].append(label)
            d = o.op_date
            if d and (fid not in latest or (latest[fid] is None or d > latest[fid])):
                latest[fid] = d

        # Fallbacks when assignments exist without a FieldOperation row yet
        hybrid_fields = {link.field_id for link in hybrid_links}
        spray_fields = {link.field_id for link in spray_links}
        for fid in hybrid_fields:
            if "Planting" not in type_order.get(fid, []) and not any(
                t.casefold().startswith("plant") for t in type_order.get(fid, [])
            ):
                type_order.setdefault(fid, []).append("Seed assigned")
                counts[fid] = counts.get(fid, 0) + 1
        for fid in spray_fields:
            if "Spraying" not in type_order.get(fid, []) and not any(
                "spray" in t.casefold() for t in type_order.get(fid, [])
            ):
                type_order.setdefault(fid, []).append("Spray assigned")
                counts[fid] = counts.get(fid, 0) + 1

        for fid in fids:
            field_ops_summary[fid] = {
                "types": type_order.get(fid, [])[:6],
                "count": counts.get(fid, 0),
                "latest": latest.get(fid),
            }

    view_mode = (view or "overview").strip().lower()
    if view_mode not in ("overview", "sheet"):
        view_mode = "overview"

    template = "fields_sheet.html" if view_mode == "sheet" else "fields.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": farm_name(db),
            "year": year,
            "fields": fields,
            "stats": stats,
            "filter_crop": crop or "all",
            "filter_ownership": ownership or "all",
            "ownership_labels": OWNERSHIP_LABELS,
            "lease_labels": LEASE_LABELS,
            "view": view_mode,
            "corn_cop": corn_cop,
            "soy_cop": soy_cop,
            "saved": request.query_params.get("saved"),
            "msg": request.query_params.get("msg"),
            "field_ops_summary": field_ops_summary,
        },
    )


@app.get("/fields/sheet", response_class=HTMLResponse)
def fields_sheet(
    request: Request,
    crop: Optional[str] = None,
    ownership: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return fields_list(request, crop=crop, ownership=ownership, view="sheet", db=db)


@app.post("/fields/sheet/save")
def fields_sheet_save(
    request: Request,
    field_id: list[int] = Form(default=[]),
    crop: list[str] = Form(default=[]),
    acres_total: list[str] = Form(default=[]),
    acres_mine: list[str] = Form(default=[]),
    expected_yield: list[str] = Form(default=[]),
    rent_per_acre: list[str] = Form(default=[]),
    ownership_mode: list[str] = Form(default=[]),
    notes: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    if not year:
        return RedirectResponse("/settings", status_code=303)

    def as_list(vals):
        if vals is None:
            return []
        if isinstance(vals, (str, int, float)):
            return [vals]
        return list(vals)

    ids = [int(x) for x in as_list(field_id)]
    crops = as_list(crop)
    totals = as_list(acres_total)
    mines = as_list(acres_mine)
    yields = as_list(expected_yield)
    rents = as_list(rent_per_acre)
    owns = as_list(ownership_mode)
    notes_l = as_list(notes)

    allowed_crops = {"Corn", "Soybeans", "None"}
    allowed_own = set(OWNERSHIP_LABELS.keys())
    updated = 0

    for i, fid in enumerate(ids):
        field = db.get(Field, fid)
        if not field or field.crop_year_id != year.id:
            continue
        new_crop = str(crops[i] if i < len(crops) else field.crop).strip() or "None"
        if new_crop not in allowed_crops:
            new_crop = field.crop
        new_own = str(owns[i] if i < len(owns) else field.ownership_mode).strip()
        if new_own not in allowed_own:
            new_own = field.ownership_mode

        at = _parse_float(str(totals[i] if i < len(totals) else field.acres_total)) or 0
        am = _parse_float(str(mines[i] if i < len(mines) else field.acres_mine)) or 0
        ey_raw = str(yields[i] if i < len(yields) else "")
        ey = _parse_float(ey_raw, default=None) if ey_raw.strip() else None
        rp = _parse_float(str(rents[i] if i < len(rents) else field.rent_per_acre)) or 0
        nt = str(notes_l[i] if i < len(notes_l) else (field.notes or "")).strip() or None

        changed = (
            field.crop != new_crop
            or float(field.acres_total or 0) != float(at)
            or float(field.acres_mine or 0) != float(am)
            or (field.expected_yield != ey)
            or float(field.rent_per_acre or 0) != float(rp)
            or field.ownership_mode != new_own
            or (field.notes or None) != nt
        )
        if not changed:
            continue

        field.crop = new_crop
        field.acres_total = at
        field.acres_mine = am
        field.expected_yield = ey
        field.rent_per_acre = rp
        field.ownership_mode = new_own
        field.notes = nt
        field.updated_at = datetime.utcnow()
        updated += 1

    db.commit()
    return RedirectResponse(f"/fields/sheet?saved={updated}", status_code=303)


def _as_form_list(vals):
    if vals is None:
        return []
    if isinstance(vals, (str, int, float)):
        return [vals]
    return list(vals)


def _ensure_party(db: Session, name: str, party_type: str = "partner") -> Party | None:
    clean = (name or "").strip()
    if not clean:
        return None
    existing = db.scalar(select(Party).where(Party.name == clean))
    if existing:
        return existing
    party = Party(name=clean, party_type=party_type)
    db.add(party)
    db.flush()
    return party


def _replace_field_shares(
    db: Session,
    field: Field,
    *,
    share_name: list,
    share_pct: list,
    share_party_id: list,
    share_is_me: list,
) -> None:
    names = _as_form_list(share_name)
    pcts = _as_form_list(share_pct)
    party_ids = _as_form_list(share_party_id)
    is_me_flags = _as_form_list(share_is_me)
    n = max(len(names), len(pcts), len(party_ids), len(is_me_flags), 0)

    # clear existing
    for existing in list(field.shares or []):
        db.delete(existing)
    db.flush()

    me_pct = None
    first_partner_id = None
    order = 0
    for i in range(n):
        name = str(names[i] if i < len(names) else "").strip()
        pct = _parse_float(str(pcts[i] if i < len(pcts) else ""), default=None)
        pid_raw = str(party_ids[i] if i < len(party_ids) else "").strip()
        is_me_raw = str(is_me_flags[i] if i < len(is_me_flags) else "0").strip().lower()
        is_me = 1 if is_me_raw in ("1", "true", "yes", "on", "me") else 0
        if not name and not pid_raw and pct is None and not is_me:
            continue
        if pct is None:
            continue
        party_id = int(pid_raw) if pid_raw.isdigit() else None
        if not is_me and party_id is None and name and name.lower() != "me":
            party = _ensure_party(db, name)
            party_id = party.id if party else None
        if is_me or name.lower() == "me":
            is_me = 1
            name = name if name and name.lower() != "me" else "Me"
            party_id = None
            me_pct = pct
        elif first_partner_id is None and party_id:
            first_partner_id = party_id
        elif not name and party_id:
            p = db.get(Party, party_id)
            name = p.name if p else "Partner"
        db.add(
            FieldShare(
                field_id=field.id,
                party_id=party_id,
                partner_name=name or ("Me" if is_me else "Partner"),
                share_pct=pct,
                is_me=is_me,
                sort_order=order,
            )
        )
        order += 1

    if me_pct is not None:
        field.my_share_pct = me_pct
        if field.acres_total and field.ownership_mode == "on_shares":
            field.acres_mine = round((field.acres_total or 0) * (me_pct / 100.0), 2)
    if first_partner_id is not None:
        field.party_id = first_partner_id


def _seed_shares_from_legacy(field: Field) -> list[FieldShare]:
    """Build display rows when field_shares is empty but legacy columns exist."""
    if field.shares:
        return list(field.shares)
    rows: list[FieldShare] = []
    if field.my_share_pct is not None or field.ownership_mode == "on_shares":
        rows.append(
            FieldShare(
                field_id=field.id,
                partner_name="Me",
                share_pct=field.my_share_pct if field.my_share_pct is not None else 100.0,
                is_me=1,
                sort_order=0,
            )
        )
    if field.party_id:
        other = 0.0
        if field.my_share_pct is not None:
            other = max(0.0, 100.0 - float(field.my_share_pct))
        rows.append(
            FieldShare(
                field_id=field.id,
                party_id=field.party_id,
                partner_name=field.party.name if field.party else "Partner",
                share_pct=other,
                is_me=0,
                sort_order=1,
            )
        )
    return rows


@app.get("/fields/new", response_class=HTMLResponse)
def field_new(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    year = get_active_year(db)
    if not year:
        return RedirectResponse("/settings", status_code=303)
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    return templates.TemplateResponse(
        "field_form.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": farm_name(db),
            "year": year,
            "field": None,
            "shares": [
                FieldShare(partner_name="Me", share_pct=100.0, is_me=1, sort_order=0),
            ],
            "parties": parties,
            "ownership_labels": OWNERSHIP_LABELS,
            "error": None,
        },
    )


@app.get("/fields/{field_id}/edit", response_class=HTMLResponse)
def field_edit(request: Request, field_id: int, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    field = db.scalar(
        select(Field)
        .where(Field.id == field_id)
        .options(joinedload(Field.party), joinedload(Field.shares).joinedload(FieldShare.party))
    )
    if not field:
        return RedirectResponse("/fields", status_code=303)
    year = db.get(CropYear, field.crop_year_id)
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    shares = _seed_shares_from_legacy(field)
    return templates.TemplateResponse(
        "field_form.html",
        {
            "request": request,
            "user": user,
            "active": "fields",
            "farm_name": farm_name(db),
            "year": year,
            "field": field,
            "shares": shares,
            "parties": parties,
            "ownership_labels": OWNERSHIP_LABELS,
            "error": None,
        },
    )


def _parse_float(value: str, default: float | None = 0.0) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@app.post("/fields/save")
async def field_save(
    request: Request,
    field_id: Optional[int] = Form(None),
    name: str = Form(...),
    acres_total: str = Form("0"),
    acres_mine: str = Form("0"),
    crop: str = Form("None"),
    ownership_mode: str = Form("operated_by_me"),
    party_id: str = Form(""),
    my_share_pct: str = Form(""),
    lease_type: str = Form("cash_rent"),
    rent_per_acre: str = Form("0"),
    expected_yield: str = Form(""),
    notes: str = Form(""),
    share_name: list[str] = Form(default=[]),
    share_pct: list[str] = Form(default=[]),
    share_party_id: list[str] = Form(default=[]),
    share_is_me: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    if not year and not field_id:
        return RedirectResponse("/settings", status_code=303)

    clean_name = name.strip()
    if not clean_name:
        dest = f"/fields/{field_id}/edit" if field_id else "/fields/new"
        return RedirectResponse(f"{dest}?msg=name_required", status_code=303)
    if is_summary_field_name(clean_name):
        return RedirectResponse("/fields?msg=totals_not_a_field", status_code=303)

    field = db.get(Field, field_id) if field_id else None
    if field is None:
        field = Field(crop_year_id=year.id, name=clean_name)
        db.add(field)
        db.flush()
    else:
        field.name = clean_name

    field.acres_total = _parse_float(acres_total)
    field.acres_mine = _parse_float(acres_mine)
    field.crop = crop
    field.ownership_mode = ownership_mode
    field.party_id = int(party_id) if party_id.strip().isdigit() else None
    field.my_share_pct = _parse_float(my_share_pct, default=None) if my_share_pct.strip() else None
    field.lease_type = lease_type or None
    field.rent_per_acre = _parse_float(rent_per_acre)
    field.expected_yield = (
        _parse_float(expected_yield, default=None) if expected_yield.strip() else None
    )
    field.notes = notes.strip() or None
    field.updated_at = datetime.utcnow()

    share_names = _as_form_list(share_name)
    share_pcts = _as_form_list(share_pct)
    if ownership_mode == "on_shares" or any(str(x).strip() for x in share_names) or any(
        str(x).strip() for x in share_pcts
    ):
        _replace_field_shares(
            db,
            field,
            share_name=share_names,
            share_pct=share_pcts,
            share_party_id=_as_form_list(share_party_id),
            share_is_me=_as_form_list(share_is_me),
        )

    db.commit()
    return RedirectResponse(f"/fields/{field.id}", status_code=303)


@app.post("/fields/{field_id}/delete")
def field_delete(request: Request, field_id: int, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    field = db.get(Field, field_id)
    if field:
        db.delete(field)
        db.commit()
    return RedirectResponse("/fields", status_code=303)


@app.get("/parties", response_class=HTMLResponse)
def parties_list(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    return templates.TemplateResponse(
        "parties.html",
        {
            "request": request,
            "user": user,
            "active": "parties",
            "farm_name": farm_name(db),
            "parties": parties,
        },
    )


@app.post("/parties/save")
def party_save(
    request: Request,
    party_id: Optional[int] = Form(None),
    name: str = Form(...),
    party_type: str = Form("other"),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    party = db.get(Party, party_id) if party_id else None
    if party is None:
        party = Party()
        db.add(party)
    party.name = name.strip()
    party.party_type = party_type
    party.phone = phone.strip() or None
    party.email = email.strip() or None
    party.notes = notes.strip() or None
    db.commit()
    return RedirectResponse("/parties", status_code=303)


@app.post("/parties/{party_id}/delete")
def party_delete(request: Request, party_id: int, db: Session = Depends(get_db)):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    party = db.get(Party, party_id)
    if party:
        for field in list(party.fields):
            field.party_id = None
        for share in list(db.scalars(select(FieldShare).where(FieldShare.party_id == party_id))):
            if not share.partner_name:
                share.partner_name = party.name
            share.party_id = None
        db.delete(party)
        db.commit()
    return RedirectResponse("/parties", status_code=303)


@app.get("/setting")
def settings_typo_redirect():
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    settings = db.scalar(select(AppSettings).limit(1))
    years = list(db.scalars(select(CropYear).order_by(CropYear.year.desc())))
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "active": "settings",
            "farm_name": farm_name(db),
            "settings": settings,
            "years": years,
            "active_year": get_active_year(db),
        },
    )


@app.post("/settings/farm")
def settings_farm(
    request: Request,
    farm_name_value: str = Form(...),
    db: Session = Depends(get_db),
):
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    settings = db.scalar(select(AppSettings).limit(1))
    if settings:
        settings.farm_name = farm_name_value.strip() or "Beam Farm"
        db.commit()
    return RedirectResponse("/settings", status_code=303)


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@app.get("/equipment", response_class=HTMLResponse)
def equipment_list(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    year = get_active_year(db)
    acres = crop_acres_mine(db, year)
    items = list(
        db.scalars(
            select(Equipment)
            .options(
                joinedload(Equipment.maintenance),
                joinedload(Equipment.events),
            )
            .order_by(Equipment.name)
        ).unique()
    )
    rows = []
    fleet_cash = 0.0
    fleet_total = 0.0
    market_sum = 0.0
    loan_sum = 0.0
    y = year.year if year else date.today().year
    for eq in items:
        if eq.status != "active":
            continue
        costs = equipment_annual_costs(eq, y)
        cpa_cash = cost_per_acre(costs["cash_cost"], acres)
        cpa_total = cost_per_acre(costs["total_cost"], acres)
        fleet_cash += costs["cash_cost"]
        fleet_total += costs["total_cost"]
        market_sum += eq.market_value or 0
        if eq.finance_status in ("loan", "leased"):
            loan_sum += eq.loan_balance or 0
        rows.append(
            {
                "eq": eq,
                "costs": costs,
                "cpa_cash": cpa_cash,
                "cpa_total": cpa_total,
            }
        )
    return templates.TemplateResponse(
        "equipment.html",
        {
            "request": request,
            "user": user,
            "active": "equipment",
            "farm_name": farm_name(db),
            "year": year,
            "acres": acres,
            "rows": rows,
            "finance_labels": FINANCE_LABELS,
            "fleet_cash": round(fleet_cash, 2),
            "fleet_total": round(fleet_total, 2),
            "fleet_cpa_cash": cost_per_acre(fleet_cash, acres),
            "fleet_cpa_total": cost_per_acre(fleet_total, acres),
            "market_sum": round(market_sum, 2),
            "loan_sum": round(loan_sum, 2),
            "can_edit": perms.can_edit_finance(user.get("role")),
        },
    )


@app.get("/equipment/new", response_class=HTMLResponse)
def equipment_new(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    return templates.TemplateResponse(
        "equipment_form.html",
        {
            "request": request,
            "user": user,
            "active": "equipment",
            "farm_name": farm_name(db),
            "eq": None,
            "categories": EQUIP_CATEGORIES,
            "finance_labels": FINANCE_LABELS,
        },
    )


@app.get("/equipment/{equip_id}", response_class=HTMLResponse)
def equipment_detail(request: Request, equip_id: int, db: Session = Depends(get_db)):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    eq = db.scalar(
        select(Equipment)
        .where(Equipment.id == equip_id)
        .options(joinedload(Equipment.maintenance), joinedload(Equipment.events))
    )
    if not eq:
        return RedirectResponse("/equipment", status_code=303)
    year = get_active_year(db)
    acres = crop_acres_mine(db, year)
    y = year.year if year else date.today().year
    costs = equipment_annual_costs(eq, y)
    fuel = list(
        db.scalars(
            select(EquipmentFuel)
            .where(EquipmentFuel.equipment_id == equip_id)
            .order_by(EquipmentFuel.fill_date.desc())
        )
    )
    fuel_cost_yr = sum(f.cost or 0 for f in fuel if f.fill_date and f.fill_date.year == y)
    return templates.TemplateResponse(
        "equipment_detail.html",
        {
            "request": request,
            "user": user,
            "active": "equipment",
            "farm_name": farm_name(db),
            "eq": eq,
            "costs": costs,
            "acres": acres,
            "cpa_cash": cost_per_acre(costs["cash_cost"], acres),
            "cpa_total": cost_per_acre(costs["total_cost"], acres),
            "finance_labels": FINANCE_LABELS,
            "can_edit": perms.can_edit_finance(user.get("role")),
            "today": date.today().isoformat(),
            "fuel": fuel,
            "fuel_cost_yr": fuel_cost_yr,
        },
    )


@app.get("/equipment/{equip_id}/edit", response_class=HTMLResponse)
def equipment_edit(request: Request, equip_id: int, db: Session = Depends(get_db)):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    eq = db.get(Equipment, equip_id)
    if not eq:
        return RedirectResponse("/equipment", status_code=303)
    return templates.TemplateResponse(
        "equipment_form.html",
        {
            "request": request,
            "user": user,
            "active": "equipment",
            "farm_name": farm_name(db),
            "eq": eq,
            "categories": EQUIP_CATEGORIES,
            "finance_labels": FINANCE_LABELS,
        },
    )


@app.post("/equipment/save")
def equipment_save(
    request: Request,
    equip_id: Optional[int] = Form(None),
    name: str = Form(...),
    category: str = Form("other"),
    make: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    serial_number: str = Form(""),
    status: str = Form("active"),
    finance_status: str = Form("owned"),
    purchase_date: str = Form(""),
    purchase_price: str = Form(""),
    market_value: str = Form(""),
    salvage_value: str = Form(""),
    useful_life_years: str = Form(""),
    hours: str = Form(""),
    payment_amount: str = Form(""),
    payment_frequency: str = Form("annual"),
    loan_balance: str = Form(""),
    lender_name: str = Form(""),
    lease_end_date: str = Form(""),
    annual_insurance: str = Form("0"),
    annual_taxes: str = Form("0"),
    annual_housing: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()

    eq = db.get(Equipment, equip_id) if equip_id else None
    creating = eq is None
    if creating:
        eq = Equipment()
        db.add(eq)

    eq.name = name.strip()
    eq.category = category
    eq.make = make.strip() or None
    eq.model = model.strip() or None
    eq.year = int(year) if year.strip().isdigit() else None
    eq.serial_number = serial_number.strip() or None
    eq.status = status
    eq.finance_status = finance_status
    eq.purchase_date = _parse_date(purchase_date)
    eq.purchase_price = _parse_float(purchase_price, None) if purchase_price.strip() else None
    eq.market_value = _parse_float(market_value, None) if market_value.strip() else None
    eq.salvage_value = _parse_float(salvage_value, None) if salvage_value.strip() else None
    eq.useful_life_years = (
        _parse_float(useful_life_years, None) if useful_life_years.strip() else None
    )
    eq.hours = _parse_float(hours, None) if hours.strip() else None
    eq.payment_amount = _parse_float(payment_amount, None) if payment_amount.strip() else None
    eq.payment_frequency = payment_frequency or None
    eq.loan_balance = _parse_float(loan_balance, None) if loan_balance.strip() else None
    eq.lender_name = lender_name.strip() or None
    eq.lease_end_date = _parse_date(lease_end_date)
    eq.annual_insurance = _parse_float(annual_insurance) or 0
    eq.annual_taxes = _parse_float(annual_taxes) or 0
    eq.annual_housing = _parse_float(annual_housing) or 0
    eq.notes = notes.strip() or None
    eq.updated_at = datetime.utcnow()
    db.flush()

    if creating and eq.purchase_price:
        db.add(
            EquipmentEvent(
                equipment_id=eq.id,
                event_type="buy",
                event_date=eq.purchase_date or date.today(),
                amount=eq.purchase_price,
                notes="Initial purchase",
            )
        )
    db.commit()
    return RedirectResponse(f"/equipment/{eq.id}", status_code=303)


@app.post("/equipment/{equip_id}/maintenance")
def equipment_add_maintenance(
    request: Request,
    equip_id: int,
    service_date: str = Form(...),
    description: str = Form(...),
    cost: str = Form("0"),
    hours_at_service: str = Form(""),
    vendor: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    eq = db.get(Equipment, equip_id)
    if not eq:
        return RedirectResponse("/equipment", status_code=303)
    db.add(
        EquipmentMaintenance(
            equipment_id=eq.id,
            service_date=_parse_date(service_date) or date.today(),
            description=description.strip(),
            cost=_parse_float(cost) or 0,
            hours_at_service=_parse_float(hours_at_service, None)
            if hours_at_service.strip()
            else None,
            vendor=vendor.strip() or None,
            notes=notes.strip() or None,
        )
    )
    if hours_at_service.strip():
        eq.hours = _parse_float(hours_at_service, eq.hours)
    db.commit()
    return RedirectResponse(f"/equipment/{equip_id}", status_code=303)


@app.post("/equipment/{equip_id}/event")
def equipment_add_event(
    request: Request,
    equip_id: int,
    event_type: str = Form(...),
    event_date: str = Form(...),
    amount: str = Form("0"),
    counterparty: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_module(request, "equipment")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    eq = db.get(Equipment, equip_id)
    if not eq:
        return RedirectResponse("/equipment", status_code=303)
    amt = _parse_float(amount) or 0
    db.add(
        EquipmentEvent(
            equipment_id=eq.id,
            event_type=event_type,
            event_date=_parse_date(event_date) or date.today(),
            amount=amt,
            counterparty=counterparty.strip() or None,
            notes=notes.strip() or None,
        )
    )
    if event_type == "sell":
        eq.status = "sold"
        if amt:
            eq.market_value = amt
    elif event_type == "trade_out":
        eq.status = "traded"
    db.commit()
    return RedirectResponse(f"/equipment/{equip_id}", status_code=303)


@app.get("/balance", response_class=HTMLResponse)
def balance_sheet(
    request: Request,
    as_of: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = require_module(request, "balance")
    if isinstance(user, RedirectResponse):
        return user
    as_of_date = _parse_date(as_of or "") or date.today()
    manual = list(
        db.scalars(
            select(BalanceSheetItem)
            .where(BalanceSheetItem.as_of_date == as_of_date)
            .order_by(BalanceSheetItem.side, BalanceSheetItem.category, BalanceSheetItem.label)
        )
    )
    equipment = list(db.scalars(select(Equipment).where(Equipment.status == "active")))
    equip_market = sum((e.market_value or e.purchase_price or 0) for e in equipment)
    equip_loans = sum((e.loan_balance or 0) for e in equipment if e.finance_status == "loan")

    assets = [i for i in manual if i.side == "asset"]
    liabilities = [i for i in manual if i.side == "liability"]
    assets_total = sum(i.amount for i in assets) + equip_market
    liabilities_total = sum(i.amount for i in liabilities) + equip_loans
    equity = assets_total - liabilities_total
    current_assets = sum(i.amount for i in assets if i.is_current) + 0
    current_liab = sum(i.amount for i in liabilities if i.is_current)
    # equipment treated as long-term; loans as long-term unless marked current manually
    working_capital = current_assets - current_liab
    snapshots = list(db.scalars(select(BalanceSnapshot).order_by(BalanceSnapshot.id.desc()).limit(12)))

    return templates.TemplateResponse(
        "balance.html",
        {
            "request": request,
            "user": user,
            "active": "balance",
            "farm_name": farm_name(db),
            "as_of_date": as_of_date,
            "assets": assets,
            "liabilities": liabilities,
            "equip_market": round(equip_market, 2),
            "equip_loans": round(equip_loans, 2),
            "equip_count": len(equipment),
            "assets_total": round(assets_total, 2),
            "liabilities_total": round(liabilities_total, 2),
            "equity": round(equity, 2),
            "working_capital": round(working_capital, 2),
            "can_edit": perms.can_edit_finance(user.get("role")),
            "snapshots": snapshots,
        },
    )


@app.post("/balance/item")
def balance_add_item(
    request: Request,
    as_of_date: str = Form(...),
    side: str = Form(...),
    category: str = Form(...),
    label: str = Form(...),
    amount: str = Form("0"),
    is_current: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_module(request, "balance")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    d = _parse_date(as_of_date) or date.today()
    db.add(
        BalanceSheetItem(
            as_of_date=d,
            side=side,
            category=category,
            label=label.strip(),
            amount=_parse_float(amount) or 0,
            is_current=1 if is_current == "1" else 0,
            notes=notes.strip() or None,
            source="manual",
        )
    )
    db.commit()
    return RedirectResponse(f"/balance?as_of={d.isoformat()}", status_code=303)


@app.post("/balance/item/{item_id}/delete")
def balance_delete_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    user = require_module(request, "balance")
    if isinstance(user, RedirectResponse):
        return user
    if not perms.can_edit_finance(user.get("role")):
        return perms.deny()
    item = db.get(BalanceSheetItem, item_id)
    as_of = item.as_of_date.isoformat() if item else date.today().isoformat()
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(f"/balance?as_of={as_of}", status_code=303)


@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "team")
    if isinstance(user, RedirectResponse):
        return user
    users = list(db.scalars(select(User).order_by(User.username)))
    return templates.TemplateResponse(
        "team.html",
        {
            "request": request,
            "user": user,
            "active": "team",
            "farm_name": farm_name(db),
            "users": users,
            "role_labels": perms.ROLE_LABELS,
            "error": None,
        },
    )


@app.post("/team/add")
def team_add(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    role: str = Form("agronomist"),
    db: Session = Depends(get_db),
):
    user = require_module(request, "team")
    if isinstance(user, RedirectResponse):
        return user
    uname = username.strip().lower()
    if db.scalar(select(User).where(User.username == uname)):
        users = list(db.scalars(select(User).order_by(User.username)))
        return templates.TemplateResponse(
            "team.html",
            {
                "request": request,
                "user": user,
                "active": "team",
                "farm_name": farm_name(db),
                "users": users,
                "role_labels": perms.ROLE_LABELS,
                "error": "That username already exists.",
            },
            status_code=400,
        )
    if role not in perms.ROLE_LABELS:
        role = "viewer"
    db.add(
        User(
            username=uname,
            display_name=display_name.strip() or uname,
            password_hash=hash_password(password),
            role=role,
            is_active=1,
        )
    )
    db.commit()
    return RedirectResponse("/team", status_code=303)


@app.post("/team/{user_id}/role")
def team_set_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_module(request, "team")
    if isinstance(user, RedirectResponse):
        return user
    target = db.get(User, user_id)
    if target and role in perms.ROLE_LABELS:
        # keep at least one owner
        if target.role == "owner" and role != "owner":
            owners = list(db.scalars(select(User).where(User.role == "owner", User.is_active == 1)))
            if len(owners) <= 1:
                return RedirectResponse("/team", status_code=303)
        target.role = role
        db.commit()
    return RedirectResponse("/team", status_code=303)


@app.post("/team/{user_id}/toggle")
def team_toggle(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = require_module(request, "team")
    if isinstance(user, RedirectResponse):
        return user
    target = db.get(User, user_id)
    if target and target.id != user.get("id"):
        target.is_active = 0 if target.is_active else 1
        db.commit()
    return RedirectResponse("/team", status_code=303)


# After core routes so /fields/new is registered before /fields/{field_id}
app.include_router(extra_router)
app.include_router(more_router)
