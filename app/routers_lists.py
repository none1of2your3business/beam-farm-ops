"""Per-hub Lists & settings pages for editable dropdown masters."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import lookups as lu
from app import permissions as perms
from app.activity import log_activity
from app.database import get_db
from app.models import AppSettings, LookupValue
from app.templating import templates

router = APIRouter()


def _user(request: Request):
    return request.session.get("user")


def _farm(db: Session) -> str:
    s = db.scalar(select(AppSettings).limit(1))
    return s.farm_name if s else "Beam Farm"


def _need(request: Request, module: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not perms.can_access(user.get("role"), module):
        return perms.deny()
    return user


def _lists_page(request: Request, db: Session, hub: str):
    cfg = lu.HUB_LISTS.get(hub)
    if not cfg:
        return RedirectResponse("/", status_code=303)
    user = _need(request, cfg["module"])
    if isinstance(user, RedirectResponse):
        return user
    if hub == "admin" and user.get("role") != "owner":
        return RedirectResponse("/", status_code=303)
    grouped = lu.grouped_for_hub(db, hub)
    sections = []
    for cat in cfg["categories"]:
        sections.append(
            {
                "category": cat,
                "title": lu.CATEGORY_LABELS.get(cat, cat),
                "rows": grouped.get(cat, []),
                "is_freight": cat == lu.FREIGHT_RATE,
                "has_label": cat
                in (
                    lu.OWNERSHIP_MODE,
                    lu.LEASE_TYPE,
                    lu.PARTY_TYPE,
                    lu.CONTRACT_TYPE,
                    lu.FINANCE_TYPE,
                    lu.EQUIP_STATUS,
                    lu.DOC_KIND,
                    lu.BALANCE_SIDE,
                    lu.BALANCE_CATEGORY,
                ),
            }
        )
    return templates.TemplateResponse(
        "hub_lists.html",
        {
            "request": request,
            "user": user,
            "active": cfg["active"],
            "farm_name": _farm(db),
            "hub": hub,
            "page_title": cfg["title"],
            "page_lede": cfg["lede"],
            "back_href": cfg["back_href"],
            "sections": sections,
            "related_links": cfg.get("links") or [],
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/fields/lists", response_class=HTMLResponse)
def fields_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "fields")


@router.get("/inputs/lists", response_class=HTMLResponse)
def inputs_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "inputs")


@router.get("/bins/lists", response_class=HTMLResponse)
def bins_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "bins")


@router.get("/marketing/lists", response_class=HTMLResponse)
@router.get("/risk/lists", response_class=HTMLResponse)
def marketing_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "marketing")


@router.get("/money/lists", response_class=HTMLResponse)
def money_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "money")


@router.get("/capture/lists", response_class=HTMLResponse)
def capture_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "capture")


@router.get("/admin/lists", response_class=HTMLResponse)
@router.get("/lists", response_class=HTMLResponse)
def admin_lists(request: Request, db: Session = Depends(get_db)):
    return _lists_page(request, db, "admin")


def _lists_redirect(hub: str, **params) -> str:
    """Admin / all-lists live at /lists and /admin/lists — posts must not go to /admin/lists only."""
    q = "&".join(f"{k}={v}" for k, v in params.items() if v is not None and str(v) != "")
    if hub == "admin":
        base = "/lists"
    else:
        base = f"/{hub}/lists"
    return f"{base}?{q}" if q else base


@router.post("/lists/{hub}/add")
def lists_add(
    request: Request,
    hub: str,
    category: str = Form(...),
    name: str = Form(...),
    label: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    cfg = lu.HUB_LISTS.get(hub)
    if not cfg or category not in cfg["categories"]:
        return RedirectResponse("/", status_code=303)
    user = _need(request, cfg["module"])
    if isinstance(user, RedirectResponse):
        return user
    if hub == "admin" and user.get("role") != "owner":
        return RedirectResponse("/", status_code=303)
    raw = (name or "").strip()
    if not raw:
        return RedirectResponse(_lists_redirect(hub, error="empty"), status_code=303)
    if category == lu.FREIGHT_RATE:
        try:
            rate = float(raw.replace("$", "").replace("/bu", "").strip())
        except ValueError:
            return RedirectResponse(_lists_redirect(hub, error="freight"), status_code=303)
        lu.ensure_freight(db, rate, commit=True)
    else:
        lu.ensure(
            db,
            category,
            raw,
            label=(label.strip() or None),
            notes=(notes.strip() or None),
            commit=True,
        )
    log_activity(db, user.get("username"), "lookup_add", f"{category}: {raw}")
    return RedirectResponse(_lists_redirect(hub, saved="1") + f"#{category}", status_code=303)


@router.post("/lists/{hub}/{item_id}/rename")
def lists_rename(
    request: Request,
    hub: str,
    item_id: int,
    name: str = Form(...),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    cfg = lu.HUB_LISTS.get(hub)
    if not cfg:
        return RedirectResponse("/", status_code=303)
    user = _need(request, cfg["module"])
    if isinstance(user, RedirectResponse):
        return user
    if hub == "admin" and user.get("role") != "owner":
        return RedirectResponse("/", status_code=303)
    row = lu.rename(db, item_id, name, label if label.strip() else None)
    if row is None:
        return RedirectResponse(_lists_redirect(hub, error="rename"), status_code=303)
    log_activity(db, user.get("username"), "lookup_rename", f"{row.category}: {row.name}")
    return RedirectResponse(_lists_redirect(hub, saved="1") + f"#{row.category}", status_code=303)


@router.post("/lists/{hub}/{item_id}/toggle")
def lists_toggle(
    request: Request,
    hub: str,
    item_id: int,
    db: Session = Depends(get_db),
):
    cfg = lu.HUB_LISTS.get(hub)
    if not cfg:
        return RedirectResponse("/", status_code=303)
    user = _need(request, cfg["module"])
    if isinstance(user, RedirectResponse):
        return user
    if hub == "admin" and user.get("role") != "owner":
        return RedirectResponse("/", status_code=303)
    row = db.get(LookupValue, item_id)
    if not row or row.category not in cfg["categories"]:
        return RedirectResponse(_lists_redirect(hub), status_code=303)
    row.is_active = 0 if row.is_active else 1
    db.commit()
    log_activity(
        db,
        user.get("username"),
        "lookup_toggle",
        f"{row.category}: {row.name} → {'on' if row.is_active else 'off'}",
    )
    return RedirectResponse(_lists_redirect(hub, saved="1") + f"#{row.category}", status_code=303)
