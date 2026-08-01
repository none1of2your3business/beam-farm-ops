import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.auth import DEFAULT_USERS, hash_password, verify_password
from app.database import Base, SessionLocal, engine, get_db
from app.equipment_costs import cost_per_acre, equipment_annual_costs
from app.flash import redirect_flash, set_flash
from app.httputil import wants_json
from app.formutil import as_list, parse_float
from app import local_backup
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
from app.routers_lists import router as lists_router
from app.routers_budget import router as budget_router
from app.templating import templates

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

DEFAULT_SECRET_KEY = "beam-farm-ops-change-me-in-production"
WEAK_SECRET_KEYS = {
    DEFAULT_SECRET_KEY,
    "change-me-to-a-long-random-string",
    "",
}
_log = logging.getLogger("beam_farm_ops")


def _secret_key() -> str:
    return (os.getenv("SECRET_KEY") or DEFAULT_SECRET_KEY).strip() or DEFAULT_SECRET_KEY


def _warn_if_weak_secret() -> None:
    secret = _secret_key()
    if secret in WEAK_SECRET_KEYS:
        msg = (
            "SECURITY: SECRET_KEY is missing or still the default. "
            "Set a long random SECRET_KEY in .env before any shared/deployed use."
        )
        _log.warning(msg)
        logging.getLogger("uvicorn.error").warning(msg)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _warn_if_weak_secret()
    Base.metadata.create_all(bind=engine)
    run_migrations()
    db = SessionLocal()
    try:
        seed_initial_data(db)
        from app.lookups import seed_lookups

        seed_lookups(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Beam Farm Ops", lifespan=_lifespan)

@app.middleware("http")
async def fetch_post_json_redirects(request: Request, call_next):
    """Turn POST redirects into JSON for app-wide fetch autosave.

    Registered before SessionMiddleware so we can clear flash and return JSON
    before the session cookie is written.
    """
    response = await call_next(request)
    if request.method != "POST" or not wants_json(request):
        return response
    if response.status_code not in (301, 302, 303, 307, 308):
        return response
    loc = response.headers.get("location") or ""
    path = loc.split("?", 1)[0]
    if path.endswith("/login") or path == "/login":
        return JSONResponse(
            {"ok": False, "error": "Please sign in again"},
            status_code=401,
        )
    try:
        request.session.pop("flash", None)
    except Exception:
        pass
    return JSONResponse({"ok": True, "redirect": loc})

# Last added = outermost: session wraps the fetch JSON middleware.
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key(),
)

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
# Register before /fields/{field_id} so /fields/lists is not captured as an id
app.include_router(lists_router)

OWNERSHIP_LABELS = {
    "operated_by_me": "Operated by me",
    "on_shares": "On shares",
    "custom_work": "Custom work",
}


def _normalize_ownership_filter(raw: list[str] | str | None) -> list[str]:
    """Parse ownership filter query values. Empty list = show all."""
    allowed = list(OWNERSHIP_LABELS.keys())
    allowed_set = set(allowed)
    items: list[str] = []
    if raw is None:
        return []
    parts = [raw] if isinstance(raw, str) else list(raw)
    for part in parts:
        for bit in str(part or "").split(","):
            bit = bit.strip()
            if not bit or bit == "all":
                continue
            if bit in allowed_set and bit not in items:
                items.append(bit)
    if not items or set(items) == allowed_set:
        return []
    return items


def _ownership_query(selected: list[str]) -> str:
    """Query string fragment for ownership multi-select (no leading &)."""
    if not selected:
        return ""
    return "&".join(f"ownership={k}" for k in selected)
LEASE_LABELS = {
    "cash_rent": "Cash rent",
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


def admin_still_on_default_password(db: Session) -> bool:
    """True when the seeded admin account still verifies as farm2026."""
    admin = db.scalar(select(User).where(User.username == "admin"))
    if not admin or not admin.password_hash:
        return False
    try:
        return verify_password("farm2026", admin.password_hash)
    except Exception:
        return False


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
            "warn_default_password": admin_still_on_default_password(db),
            "hubs": [
                {"name": "Fields", "href": "/fields", "tone": "green", "blurb": "List, sheet, add field"},
                {"name": "Inputs", "href": "/inputs/ops", "tone": "sky", "blurb": "Catalog, assign, inventory"},
                {"name": "Bins", "href": "/bins", "tone": "gold", "blurb": "Bins, tickets, truck"},
                {"name": "Marketing", "href": "/risk", "tone": "board", "blurb": "Board, contracts, carry"},
                {"name": "Budget", "href": "/budget", "tone": "clay", "blurb": "Field spend, P/L color, money"},
                {"name": "Capture", "href": "/capture", "tone": "green", "blurb": "Scan, upload, photos"},
                {"name": "Admin", "href": "/admin", "tone": "sky", "blurb": "Settings, team, log", "owner_only": True},
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
    ownership: list[str] = Query(default=[]),
    view: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    fields: list[Field] = []
    ownership_sel = _normalize_ownership_filter(ownership)
    if year:
        q = (
            select(Field)
            .where(Field.crop_year_id == year.id)
            .options(
                joinedload(Field.party),
                joinedload(Field.shares).joinedload(FieldShare.party),
            )
            .order_by(Field.crop, Field.name)
        )
        fields = real_fields(list(db.scalars(q).unique()))
        if crop and crop != "all":
            fields = [f for f in fields if f.crop == crop]
        if ownership_sel:
            allowed = set(ownership_sel)
            fields = [f for f in fields if f.ownership_mode in allowed]

    # Backfill corn planting machinery $ when coverage is already complete
    if fields:
        from app.op_cost_ensure import ensure_all_completed_corn_planting_costs

        n_plant = ensure_all_completed_corn_planting_costs(db, fields)
        if n_plant:
            db.commit()

    corn_cop, soy_cop = _cop_rates(db)
    stats = field_stats(fields)
    stats["plan_cost_total"] = _plan_cost_total(fields, corn_cop, soy_cop)
    stats["corn_cop"] = corn_cop
    stats["soy_cop"] = soy_cop

    view_mode = (view or "overview").strip().lower()
    if view_mode not in ("overview", "sheet"):
        view_mode = "overview"

    farm_with_parties: list[Party] = []
    share_partners_by_field: dict[int, list[dict]] = {}
    if view_mode == "sheet":
        farm_with_parties = _farm_with_parties(db)
        for f in fields:
            share_partners_by_field[f.id] = _field_share_partner_rows(f)

    field_cards: list[dict] = []
    ledger_by_id: dict[int, dict] = {}
    year_cost = 0.0
    year_acres = 0.0
    year_cost_ac = None
    if view_mode == "overview" and year and fields:
        from app.field_ledger import load_year_field_cards

        field_cards, meta = load_year_field_cards(db, year, fields)
        ledger_by_id = {c["field"].id: c for c in field_cards}
        year_cost = meta["year_cost"]
        year_acres = meta["year_acres"]
        year_cost_ac = meta["year_cost_ac"]
        stats["year_cost"] = year_cost
        stats["year_cost_ac"] = year_cost_ac

    ownership_qs = _ownership_query(ownership_sel)
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
            "field_cards": field_cards,
            "ledger_by_id": ledger_by_id,
            "stats": stats,
            "year_cost": year_cost,
            "year_acres": year_acres,
            "year_cost_ac": year_cost_ac,
            "filter_crop": crop or "all",
            "filter_ownerships": ownership_sel,
            "filter_ownership": ",".join(ownership_sel) if ownership_sel else "all",
            "ownership_qs": ownership_qs,
            "ownership_labels": OWNERSHIP_LABELS,
            "lease_labels": LEASE_LABELS,
            "view": view_mode,
            "corn_cop": corn_cop,
            "soy_cop": soy_cop,
            "farm_with_parties": farm_with_parties,
            "share_partners_by_field": share_partners_by_field,
            "saved": request.query_params.get("saved"),
            "msg": request.query_params.get("msg"),
        },
    )


@app.get("/fields/sheet", response_class=HTMLResponse)
def fields_sheet(
    request: Request,
    crop: Optional[str] = None,
    ownership: list[str] = Query(default=[]),
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
    share_partners: list[str] = Form(default=[]),
    notes: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    year = get_active_year(db)
    if not year:
        return RedirectResponse("/settings", status_code=303)

    wants_json = (
        (request.headers.get("x-requested-with") or "").lower() == "fetch"
        or (request.query_params.get("format") or "").lower() == "json"
        or "application/json" in (request.headers.get("accept") or "")
    )

    def _reject(message: str):
        if wants_json:
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return redirect_flash(request, "/fields/sheet", message, "error")

    ids = [int(x) for x in as_list(field_id)]
    crops = as_list(crop)
    totals = as_list(acres_total)
    mines = as_list(acres_mine)
    yields = as_list(expected_yield)
    rents = as_list(rent_per_acre)
    owns = as_list(ownership_mode)
    partners_raw = as_list(share_partners)
    notes_l = as_list(notes)

    n = len(ids)
    aligned = {
        "crop": crops,
        "acres_total": totals,
        "acres_mine": mines,
        "expected_yield": yields,
        "rent_per_acre": rents,
        "ownership_mode": owns,
        "share_partners": partners_raw,
        "notes": notes_l,
    }
    for label, vals in aligned.items():
        if len(vals) != n:
            return _reject(
                f"Fields sheet row mismatch ({label}: {len(vals)} vs {n} fields) — nothing saved."
            )

    # Bind each submitted field_id to its own row values (never rely on sparse index fallback)
    by_id: dict[int, dict] = {}
    for i, fid in enumerate(ids):
        by_id[fid] = {
            "crop": crops[i],
            "acres_total": totals[i],
            "acres_mine": mines[i],
            "expected_yield": yields[i],
            "rent_per_acre": rents[i],
            "ownership_mode": owns[i],
            "share_partners": partners_raw[i],
            "notes": notes_l[i],
        }

    allowed_crops = {"Corn", "Soybeans", "None"}
    allowed_own = set(OWNERSHIP_LABELS.keys())
    updated = 0
    partner_pct_warn = False

    from app import marketing_analytics as mkt

    for fid, row in by_id.items():
        field = db.get(Field, fid)
        if not field or field.crop_year_id != year.id:
            continue
        _ = list(field.shares or [])
        new_crop = str(row["crop"] or "").strip() or "None"
        if new_crop not in allowed_crops:
            new_crop = field.crop
        new_own = str(row["ownership_mode"] or "").strip()
        if new_own not in allowed_own:
            new_own = field.ownership_mode

        at = parse_float(str(row["acres_total"])) or 0
        am = parse_float(str(row["acres_mine"])) or 0
        ey_raw = str(row["expected_yield"] or "")
        ey = parse_float(ey_raw, default=None) if ey_raw.strip() else None
        rp = parse_float(str(row["rent_per_acre"])) or 0
        nt = str(row["notes"] or "").strip() or None

        parsed_partners: list[tuple[int, float | None]] = []
        if new_own == "on_shares":
            parsed_partners = _parse_sheet_share_partners(str(row["share_partners"] or ""))

        old_rows = _field_share_partner_rows(field)
        old_ids = [r["party_id"] for r in old_rows]
        new_ids = [pid for pid, _ in parsed_partners]
        old_pct = {r["party_id"]: round(float(r["pct"]), 2) for r in old_rows}
        new_pct = {
            pid: (round(float(pct), 2) if pct is not None else None) for pid, pct in parsed_partners
        }
        pct_changed = any(
            new_pct.get(pid) is not None and new_pct.get(pid) != old_pct.get(pid) for pid in new_ids
        )
        partner_changed = new_own == "on_shares" and (
            field.ownership_mode != "on_shares"
            or old_ids != new_ids
            or pct_changed
        )

        changed = (
            field.crop != new_crop
            or float(field.acres_total or 0) != float(at)
            or float(field.acres_mine or 0) != float(am)
            or (field.expected_yield != ey)
            or float(field.rent_per_acre or 0) != float(rp)
            or field.ownership_mode != new_own
            or (field.notes or None) != nt
            or partner_changed
        )

        party_objs: list = []
        pcts: list[float] = []
        me_pct: float | None = None
        if new_own == "on_shares" and parsed_partners:
            old_pct_by_id = {r["party_id"]: float(r["pct"]) for r in old_rows}
            me_pct = float(field.my_share_pct) if field.my_share_pct is not None else None
            if me_pct is None and at > 0:
                me_pct = round(100.0 * am / at, 1)
            if me_pct is None:
                me_pct = 50.0
            remaining = max(0.0, 100.0 - me_pct)

            for pid, pct in parsed_partners:
                party = db.get(Party, pid)
                if not party:
                    continue
                party_objs.append(party)
                if pct is not None:
                    pcts.append(float(pct))
                elif pid in old_pct_by_id and old_ids == new_ids:
                    pcts.append(old_pct_by_id[pid])
                else:
                    pcts.append(-1.0)

            if party_objs:
                if any(p < 0 for p in pcts):
                    known_sum = sum(p for p in pcts if p >= 0)
                    slots = [i for i, p in enumerate(pcts) if p < 0]
                    left = max(0.0, remaining - known_sum)
                    if slots:
                        each = round(left / len(slots), 2)
                        for j, idx in enumerate(slots):
                            pcts[idx] = (
                                each
                                if j < len(slots) - 1
                                else round(left - each * (len(slots) - 1), 2)
                            )
                partner_sum = sum(float(p) for p in pcts)
                if abs((me_pct + partner_sum) - 100.0) > 0.5:
                    partner_pct_warn = True

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

        if partner_changed and party_objs and me_pct is not None:
            mkt.set_field_share_partners(
                db,
                field,
                party_objs,
                me_pct=me_pct,
                partner_pcts=pcts,
            )

        updated += 1

    db.commit()
    warn_txt = "Partners don’t total 100% — check %s." if partner_pct_warn else None
    if wants_json:
        payload = {"ok": True, "saved": updated}
        if warn_txt:
            payload["warning"] = warn_txt
        return JSONResponse(payload)
    if warn_txt:
        set_flash(request, warn_txt, "warn")
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


def _farm_with_parties(db: Session) -> list[Party]:
    """People you farm with — partners & landlords, then other party types."""
    parties = list(db.scalars(select(Party).order_by(Party.name)))
    preferred = {"partner", "landlord"}
    preferred_list = [p for p in parties if (p.party_type or "").lower() in preferred]
    # If the farm only has partners/landlords tagged, use that; otherwise show all
    # (same people list used for grain bins / contracts "farmed with").
    pool = preferred_list if preferred_list else parties
    rank = {"partner": 0, "landlord": 1, "customer": 2, "buyer": 3, "other": 4}

    def sort_key(p: Party):
        return (rank.get((p.party_type or "other").lower(), 9), (p.name or "").lower())

    return sorted(pool, key=sort_key)


def _field_primary_share_partner_id(field: Field) -> int | None:
    """Primary share partner for sheet/marketing: field.party_id or first non-Me share."""
    rows = _field_share_partner_rows(field)
    if not rows:
        return None
    if getattr(field, "party_id", None):
        for r in rows:
            if r["party_id"] == int(field.party_id):
                return int(field.party_id)
    return int(rows[0]["party_id"])


def _field_share_partner_rows(field: Field) -> list[dict]:
    """Non-Me share partners for a field: [{party_id, name, pct}, ...]."""
    shares = list(getattr(field, "shares", None) or [])
    partners = sorted(
        [
            s
            for s in shares
            if int(getattr(s, "is_me", 0) or 0) != 1 and getattr(s, "party_id", None)
        ],
        key=lambda s: (int(getattr(s, "sort_order", 0) or 0), int(getattr(s, "id", 0) or 0)),
    )
    out: list[dict] = []
    for s in partners:
        name = None
        if getattr(s, "party", None) is not None:
            name = getattr(s.party, "name", None)
        name = name or getattr(s, "display_name", None) or getattr(s, "partner_name", None) or "Partner"
        out.append(
            {
                "party_id": int(s.party_id),
                "name": name,
                "pct": float(getattr(s, "share_pct", 0) or 0),
            }
        )
    if out:
        return out
    # Legacy: party_id set but no share rows yet
    if getattr(field, "party_id", None) and getattr(field, "party", None):
        rem = max(0.0, 100.0 - float(getattr(field, "my_share_pct", None) or 50.0))
        return [
            {
                "party_id": int(field.party_id),
                "name": field.party.name,
                "pct": rem,
            }
        ]
    return []


def _parse_sheet_share_partners(raw: str) -> list[tuple[int, float | None]]:
    """Parse '8:16,9:34' or '8,9' into [(party_id, pct|None), ...]."""
    out: list[tuple[int, float | None]] = []
    seen: set[int] = set()
    for bit in str(raw or "").split(","):
        bit = bit.strip()
        if not bit:
            continue
        if ":" in bit:
            left, right = bit.split(":", 1)
            if not left.strip().isdigit():
                continue
            pid = int(left.strip())
            try:
                pct: float | None = float(right.strip().replace(",", ""))
            except ValueError:
                pct = None
        else:
            if not bit.isdigit():
                continue
            pid = int(bit)
            pct = None
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, pct))
    return out


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
        if pid_raw in ("__me__", "me"):
            is_me = 1
            pid_raw = ""
        if pid_raw == "__add_new__":
            continue
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
            name = "Me"
            party_id = None
            me_pct = pct
        elif first_partner_id is None and party_id:
            first_partner_id = party_id
        if not name and party_id:
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


def _field_form_context(
    request: Request,
    db: Session,
    *,
    user,
    year,
    field: Field | None,
    shares: list,
    error: str | None = None,
) -> dict:
    farm_with = _farm_with_parties(db)
    party_added = None
    added = (request.query_params.get("with_party_added") or "").strip()
    if added.isdigit():
        party_added = db.get(Party, int(added))
    return {
        "request": request,
        "user": user,
        "active": "fields",
        "farm_name": farm_name(db),
        "year": year,
        "field": field,
        "shares": shares,
        "parties": farm_with,
        "farm_with_parties": farm_with,
        "farm_with_ids": [p.id for p in farm_with],
        "party_added": party_added,
        "ownership_labels": OWNERSHIP_LABELS,
        "error": error,
    }


@app.get("/fields/new", response_class=HTMLResponse)
def field_new(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    year = get_active_year(db)
    if not year:
        return RedirectResponse("/settings", status_code=303)
    return templates.TemplateResponse(
        "field_form.html",
        _field_form_context(
            request,
            db,
            user=user,
            year=year,
            field=None,
            shares=[
                FieldShare(partner_name="Me", share_pct=100.0, is_me=1, sort_order=0),
            ],
        ),
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
    shares = _seed_shares_from_legacy(field)
    return templates.TemplateResponse(
        "field_form.html",
        _field_form_context(
            request,
            db,
            user=user,
            year=year,
            field=field,
            shares=shares,
        ),
    )


def _parse_float(value: str, default: float | None = 0.0) -> float | None:
    return parse_float(value, default=default)


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
    share_pids = _as_form_list(share_party_id)
    share_me_flags = _as_form_list(share_is_me)

    if ownership_mode == "on_shares":
        has_partner = False
        n = max(len(share_pids), len(share_me_flags), len(share_names), 0)
        for i in range(n):
            pid_raw = str(share_pids[i] if i < len(share_pids) else "").strip()
            is_me_raw = str(share_me_flags[i] if i < len(share_me_flags) else "0").strip().lower()
            is_me = is_me_raw in ("1", "true", "yes", "on", "me") or pid_raw in ("__me__", "me")
            if is_me or pid_raw in ("", "__add_new__"):
                continue
            if pid_raw.isdigit() or (share_names[i] if i < len(share_names) else "").strip():
                has_partner = True
                break
        if not has_partner and party_id.strip().isdigit():
            # Landlord / primary party fills in for missing share rows
            from app import marketing_analytics as mkt

            party = db.get(Party, int(party_id.strip()))
            if party:
                mkt.link_field_to_share_partner(
                    db,
                    field,
                    party,
                    me_pct=float(field.my_share_pct) if field.my_share_pct is not None else 50.0,
                )
                db.commit()
                return RedirectResponse(f"/fields/{field.id}", status_code=303)
        if not has_partner:
            dest = f"/fields/{field_id}/edit" if field_id else "/fields/new"
            return RedirectResponse(f"{dest}?msg=share_partner_required", status_code=303)

    if ownership_mode == "on_shares" or any(str(x).strip() for x in share_names) or any(
        str(x).strip() for x in share_pcts
    ) or any(str(x).strip() not in ("", "__add_new__") for x in share_pids):
        _replace_field_shares(
            db,
            field,
            share_name=share_names,
            share_pct=share_pcts,
            share_party_id=share_pids,
            share_is_me=share_me_flags,
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
            "warn_default_password": admin_still_on_default_password(db),
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


@app.post("/settings/password")
def settings_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    session_user = current_user(request)
    if not session_user:
        return RedirectResponse("/login", status_code=303)

    db_user = db.get(User, session_user.get("id"))
    if not db_user:
        return redirect_flash(request, "/settings", "Could not find your account.", "error")

    if not verify_password(current_password, db_user.password_hash):
        return redirect_flash(request, "/settings", "Current password is incorrect.", "error")

    new_password = (new_password or "").strip()
    confirm_password = (confirm_password or "").strip()
    if len(new_password) < 8:
        return redirect_flash(
            request, "/settings", "New password must be at least 8 characters.", "error"
        )
    if new_password != confirm_password:
        return redirect_flash(request, "/settings", "New passwords do not match.", "error")
    if verify_password(new_password, db_user.password_hash):
        return redirect_flash(
            request, "/settings", "New password must be different from the current one.", "warn"
        )

    db_user.password_hash = hash_password(new_password)
    db.commit()
    return redirect_flash(request, "/settings", "Password updated.", "ok")


@app.post("/backup/save")
def backup_save(
    request: Request,
    next: str = Form("/"),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    next_url = (next or "/").strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    try:
        dest, pruned = local_backup.create_save()
        msg = f"Saved: {dest.name}"
        if pruned:
            msg += f" (removed {pruned} older save{'s' if pruned != 1 else ''})"
        return redirect_flash(request, next_url, msg, "ok")
    except Exception as exc:
        return redirect_flash(request, next_url, f"Save failed: {exc}", "error")


@app.get("/backup/saves", response_class=HTMLResponse)
def backup_saves_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    saves = local_backup.list_saves()
    return templates.TemplateResponse(
        "backup_saves.html",
        {
            "request": request,
            "user": user,
            "active": "settings",
            "farm_name": farm_name(db),
            "saves": saves,
            "backup_dir": str(local_backup.BACKUP_DIR),
        },
    )


@app.post("/backup/restore")
def backup_restore(
    request: Request,
    filename: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        src = local_backup.restore_save(filename)
        return redirect_flash(
            request,
            "/",
            f"Restored from {src.name}. Refresh if anything looks stale.",
            "ok",
        )
    except Exception as exc:
        return redirect_flash(request, "/backup/saves", f"Restore failed: {exc}", "error")


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


def _hub_ops_page(
    request: Request,
    db: Session,
    *,
    module: str,
    active: str,
    ops_title: str,
    ops_lede: str,
    ops_actions: list[dict],
    ops_note: str = "",
    ops_lists_href: str = "",
):
    user = require_module(request, module)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "hub_ops.html",
        {
            "request": request,
            "user": user,
            "active": active,
            "farm_name": farm_name(db),
            "ops_title": ops_title,
            "ops_lede": ops_lede,
            "ops_actions": ops_actions,
            "ops_note": ops_note,
            "ops_lists_href": ops_lists_href,
        },
    )


@app.get("/fields/ops", response_class=HTMLResponse)
def fields_ops(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "fields")
    if isinstance(user, RedirectResponse):
        return user

    from datetime import date as date_cls

    from app.models import AppSettings
    from app.operation_rates import effective_rates

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

    settings = db.scalar(select(AppSettings).limit(1))
    op_rates = [
        {
            "key": r.key,
            "number": r.number,
            "label": r.label,
            "rate_per_ac": r.rate_per_ac,
        }
        for r in effective_rates(settings)
    ]

    return templates.TemplateResponse(
        "fields_ops.html",
        {
            "request": request,
            "user": user,
            "active": "fields_ops",
            "farm_name": farm_name(db),
            "year": year,
            "fields": fields,
            "op_rates": op_rates,
            "today": date_cls.today().isoformat(),
            "saved_rates": request.query_params.get("saved_rates"),
            "assigned": request.query_params.get("assigned"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/fields/ops/rates")
async def fields_ops_rates_save(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "fields")
    if isinstance(user, RedirectResponse):
        return user

    from app.activity import log_activity
    from app.models import AppSettings
    from app.operation_rates import save_rate_overrides

    settings = db.scalar(select(AppSettings).limit(1))
    if not settings:
        settings = AppSettings()
        db.add(settings)
        db.flush()

    form = await request.form()
    keys = [str(x) for x in form.getlist("rate_key")]
    labels = [str(x) for x in form.getlist("rate_label")]
    rates = [str(x) for x in form.getlist("rate_per_ac")]
    updates: dict = {}
    for i, key in enumerate(keys):
        label = labels[i] if i < len(labels) else ""
        raw = rates[i] if i < len(rates) else ""
        try:
            rate = float(str(raw).replace(",", ""))
        except ValueError:
            continue
        updates[key] = {"rate": rate, "label": label}

    settings.operation_rates_json = save_rate_overrides(settings, updates)
    log_activity(db, user.get("username"), "op_rates_save", f"{len(updates)} rates")
    db.commit()
    return RedirectResponse("/fields/ops?saved_rates=1", status_code=303)


@app.post("/fields/ops/assign")
async def fields_ops_assign(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "fields")
    if isinstance(user, RedirectResponse):
        return user

    from datetime import date as date_cls

    from app.activity import log_activity
    from app.models import AppSettings
    from app.op_cost_ensure import add_rate_operation
    from app.operation_rates import rate_by_key

    form = await request.form()
    rate_key = str(form.get("op_rate_key") or "").strip()
    notes = str(form.get("notes") or "").strip()
    op_date_raw = str(form.get("op_date") or "").strip()
    field_ids = [int(x) for x in form.getlist("field_id") if str(x).isdigit()]

    settings = db.scalar(select(AppSettings).limit(1))
    rate = rate_by_key(rate_key, settings)
    if not rate:
        return RedirectResponse("/fields/ops?error=bad_op", status_code=303)
    if not field_ids:
        return RedirectResponse("/fields/ops?error=no_fields", status_code=303)

    when = date_cls.today()
    if op_date_raw:
        try:
            when = date_cls.fromisoformat(op_date_raw)
        except ValueError:
            when = date_cls.today()

    n = 0
    for fid in field_ids:
        field = db.get(Field, fid)
        if not field:
            continue
        row = add_rate_operation(
            db,
            field,
            rate_key,
            op_date=when,
            extra_note=notes,
            billable=0,
            settings=settings,
        )
        if row is not None:
            n += 1

    log_activity(
        db,
        user.get("username"),
        "op_assign",
        f"{rate.label} → {n} fields",
    )
    db.commit()
    return RedirectResponse(f"/fields/ops?assigned={n}", status_code=303)


@app.get("/inputs/ops", response_class=HTMLResponse)
def inputs_ops(request: Request, db: Session = Depends(get_db)):
    return _hub_ops_page(
        request,
        db,
        module="library",
        active="inputs_ops",
        ops_title="Inputs — main operations",
        ops_lede="Catalog seed and chem, edit hybrid costs on the sheet, assign to fields, or bring in a purchase file.",
        ops_actions=[
            {"href": "/inputs", "label": "Catalog", "primary": True},
            {"href": "/inputs/hybrids/sheet", "label": "Hybrid sheet"},
            {"href": "/inputs/assign", "label": "Assign to fields"},
            {"href": "/purchases", "label": "Inventory / purchases"},
            {"href": "/inputs/upload", "label": "Upload inputs"},
        ],
        ops_lists_href="/inputs/lists",
    )


@app.get("/money", response_class=HTMLResponse)
def money_ops(request: Request, db: Session = Depends(get_db)):
    """Legacy Money URL — Budget hub now owns money tabs."""
    return RedirectResponse("/budget/money", status_code=303)


@app.get("/capture", response_class=HTMLResponse)
def capture_ops(request: Request, db: Session = Depends(get_db)):
    return _hub_ops_page(
        request,
        db,
        module="upload",
        active="capture_ops",
        ops_title="Capture — main operations",
        ops_lede="Scan a receipt, import a spreadsheet, or drop photos. Reports and Panorama are in the tabs.",
        ops_actions=[
            {"href": "/scan", "label": "Scan docs", "primary": True},
            {"href": "/upload", "label": "Import files"},
            {"href": "/photos", "label": "Photos"},
            {"href": "/export", "label": "Export / reports"},
        ],
        ops_lists_href="/capture/lists",
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_ops(request: Request, db: Session = Depends(get_db)):
    user = require_module(request, "settings")
    if isinstance(user, RedirectResponse):
        return user
    if user.get("role") != "owner":
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "hub_ops.html",
        {
            "request": request,
            "user": user,
            "active": "admin_ops",
            "farm_name": farm_name(db),
            "ops_title": "Admin — main operations",
            "ops_lede": "Farm settings, team logins, and the activity log.",
            "ops_actions": [
                {"href": "/settings", "label": "Settings", "primary": True},
                {"href": "/lists", "label": "All selection lists"},
                {"href": "/team", "label": "Team"},
                {"href": "/activity", "label": "Activity log"},
                {"href": "/export", "label": "Export"},
            ],
            "ops_note": "",
            "ops_lists_href": "/lists",
        },
    )


# After core routes so /fields/new and /fields/ops are registered before /fields/{field_id}
app.include_router(budget_router)
app.include_router(extra_router)
app.include_router(more_router)
