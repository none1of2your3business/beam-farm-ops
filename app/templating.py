"""Shared Jinja templates + request context (active crop year on every page)."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.requests import Request

from app.database import SessionLocal
from app.flash import pop_flash
from app.models import AppSettings, CropYear

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _year_context(request: Request) -> dict:
    """Inject active crop year + one-shot flash toast into every template."""
    empty = {"active_year": None, "crop_years": [], "flash": None}
    try:
        user = request.session.get("user")
    except AssertionError:
        return empty
    flash = pop_flash(request) if user else None
    if not user:
        return empty
    db = SessionLocal()
    try:
        years = list(db.scalars(select(CropYear).order_by(CropYear.year.desc())))
        settings = db.scalar(select(AppSettings).limit(1))
        active = None
        if settings and settings.active_crop_year_id:
            active = db.get(CropYear, settings.active_crop_year_id)
        if active is None:
            active = next((y for y in years if y.is_active), years[0] if years else None)
        return {"active_year": active, "crop_years": years, "flash": flash}
    finally:
        db.close()


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_year_context],
)
