"""Field-year operations ledger: events, running cost, dual break-even."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from app.marketing_analytics import equiv_price, market_cash_equiv


def weighted_contract_price(contracts: list[Any], crop: str) -> Optional[float]:
    """Bushel-weighted average cash-equiv price for open contracts of a crop."""
    num = 0.0
    den = 0.0
    crop_l = (crop or "").strip().lower()
    for c in contracts:
        if (getattr(c, "crop", "") or "").strip().lower() != crop_l:
            continue
        if (getattr(c, "status", "open") or "open") != "open":
            continue
        price = equiv_price(c)
        bu = float(getattr(c, "bushels", 0) or 0)
        if price is None or bu <= 0:
            continue
        num += price * bu
        den += bu
    if den <= 0:
        return None
    return round(num / den, 4)


def board_futures_price(board: dict[str, Any], crop: str) -> Optional[float]:
    row = (board or {}).get(crop) if board else None
    if not row:
        return None
    price = row.get("price") if isinstance(row, dict) else None
    return float(price) if price is not None else None


def field_mark_prices(
    settings: Any,
    board: dict[str, Any],
    crop: str,
    contracts: list[Any],
) -> dict[str, Any]:
    """Contracted average + futures mark (respect carry mark mode for basis)."""
    contracted = weighted_contract_price(contracts, crop)
    futures = board_futures_price(board, crop)
    if futures is None and settings:
        if crop == "Corn":
            futures = getattr(settings, "corn_futures", None)
        elif crop in ("Soybeans", "Soy"):
            futures = getattr(settings, "soy_futures", None)
    basis = None
    if settings:
        if crop == "Corn":
            basis = getattr(settings, "corn_local_basis", None)
        elif crop in ("Soybeans", "Soy"):
            basis = getattr(settings, "soy_local_basis", None)
    mode = (getattr(settings, "carry_mark_mode", None) if settings else "cash") or "cash"
    if mode.lower() == "futures":
        futures_mark = float(futures) if futures is not None else None
        futures_label = "Futures only"
    else:
        futures_mark = market_cash_equiv(futures, basis) if futures is not None else None
        futures_label = "Futures + basis"
    return {
        "contracted": contracted,
        "contracted_label": "Avg contracted",
        "futures": futures_mark,
        "futures_raw": float(futures) if futures is not None else None,
        "basis": float(basis) if basis is not None else None,
        "futures_label": futures_label,
    }


def _acres(field: Any) -> float:
    return float(getattr(field, "acres_mine", 0) or getattr(field, "acres_total", 0) or 0)


def _money(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(float(v), 2)


# Seed unit sizes used to derive planted acres from units ÷ population.
# acres = units × seeds_per_unit / population (seeds/ac)
CORN_SEEDS_PER_UNIT = 80_000.0
SOY_SEEDS_PER_UNIT = 140_000.0
BECKS_SOY_SEEDS_PER_UNIT = 130_000.0


def _norm_brand(brand: Any) -> str:
    s = str(brand or "").strip().lower()
    return s.replace("'", "").replace("’", "").replace(".", "")


def seeds_per_unit(crop: Any = None, brand: Any = None) -> float:
    """Seeds in one bag/unit for acre math."""
    c = str(crop or "").strip().lower()
    b = _norm_brand(brand)
    if c.startswith("soy"):
        if b.startswith("beck"):
            return BECKS_SOY_SEEDS_PER_UNIT
        return SOY_SEEDS_PER_UNIT
    # Corn and anything else treated as corn unit size unless soy
    return CORN_SEEDS_PER_UNIT


def _fnum(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def population_from_link(link: Any) -> Optional[float]:
    """Seeding rate (seeds/ac) from population column or rate text."""
    pop = _fnum(getattr(link, "population", None))
    if pop is not None and pop > 0:
        return pop
    rate = str(getattr(link, "rate", None) or "").strip().lower()
    if not rate:
        return None
    # e.g. "140,000 seeds/ac" or "140k"
    m = re.search(r"([\d,.]+)\s*k\b", rate)
    if m:
        try:
            return float(m.group(1).replace(",", "")) * 1000.0
        except ValueError:
            pass
    m = re.search(r"([\d,.]+)", rate)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            return v if v > 0 else None
        except ValueError:
            return None
    return None


def planted_acres_from_units(
    units: Any,
    population: Any,
    *,
    crop: Any = None,
    brand: Any = None,
) -> Optional[float]:
    """acres = units × seeds_per_unit / population. Never uses imported acres."""
    u = _fnum(units)
    pop = _fnum(population)
    if u is None or u <= 0 or pop is None or pop <= 0:
        return None
    spu = seeds_per_unit(crop, brand)
    return round((u * spu) / pop, 4)


def link_planted_acres(link: Any, hybrid: Any = None) -> Optional[float]:
    """Planted acres for a FieldHybrid from units + seeding rate."""
    crop = getattr(hybrid, "crop", None) if hybrid else None
    brand = getattr(hybrid, "brand", None) if hybrid else None
    pop = population_from_link(link)
    return planted_acres_from_units(
        getattr(link, "units", None),
        pop,
        crop=crop,
        brand=brand,
    )


def planting_coverage(
    field: Any,
    hybrid_links: list[tuple[Any, Any]],
    *,
    complete_pct: float = 0.98,
    min_gap_acres: float = 0.5,
) -> dict[str, Any]:
    """Compare as-planted acres (from units × rate) to field acres_total.

    status: none | incomplete | complete
    Uses acres_total (physical coverage), not acres_mine (ownership share).
    Planted acres are derived from units and population — imported acres ignored.
    """
    field_acres = float(getattr(field, "acres_total", 0) or 0)
    rows: list[dict[str, Any]] = []
    planted = 0.0
    for link, hybrid in hybrid_links or []:
        pop_f = population_from_link(link)
        units_f = _fnum(getattr(link, "units", None))
        crop = getattr(hybrid, "crop", None) if hybrid else None
        brand = getattr(hybrid, "brand", None) if hybrid else None
        ac_f = planted_acres_from_units(units_f, pop_f, crop=crop, brand=brand) or 0.0
        if ac_f < 0:
            ac_f = 0.0
        planted += ac_f
        name = getattr(hybrid, "name", None) if hybrid else None
        spu = seeds_per_unit(crop, brand)
        rows.append(
            {
                "link_id": getattr(link, "id", None),
                "hybrid_id": getattr(link, "hybrid_id", None),
                "hybrid_name": name or "Hybrid",
                "brand": brand,
                "crop": crop,
                "acres": round(ac_f, 4),
                "units": units_f,
                "population": pop_f,
                "seeds_per_unit": spu,
                "rate": getattr(link, "rate", None),
            }
        )
    planted = round(planted, 4)
    remaining = round(max(0.0, field_acres - planted), 4) if field_acres > 0 else 0.0

    if planted <= 0 and not rows:
        status = "none"
    elif planted <= 0 and rows:
        # Have hybrid rows but can't compute acres (missing units/rate)
        status = "incomplete" if field_acres > 0 else "none"
    elif field_acres <= 0:
        status = "incomplete" if rows else "none"
        remaining = 0.0
    elif remaining <= min_gap_acres or (field_acres > 0 and planted / field_acres >= complete_pct):
        status = "complete"
        remaining = 0.0 if planted >= field_acres else remaining
    else:
        status = "incomplete"

    primary = None
    if rows:
        primary = max(rows, key=lambda r: float(r.get("acres") or 0))

    return {
        "status": status,
        "planted_acres": planted,
        "field_acres": round(field_acres, 4),
        "remaining_acres": remaining,
        "rows": rows,
        "primary": primary,
        "acres_basis": "units_x_rate",
        "label": {
            "none": "Planting = not started",
            "incomplete": "Planting = incomplete",
            "complete": "Planting = complete",
        }.get(status, "Planting"),
    }


def estimate_units_for_acres(
    *,
    acres: float,
    source_acres: Optional[float],
    source_units: Optional[float],
    population: Optional[float],
    seeds_per_unit: float = SOY_SEEDS_PER_UNIT,
) -> Optional[float]:
    """Estimate seed units for additional acres from population or prior units/ac."""
    if acres <= 0:
        return None
    if population and population > 0 and seeds_per_unit > 0:
        return round((population * acres) / seeds_per_unit, 4)
    if source_acres and source_acres > 0 and source_units and source_units > 0:
        return round(source_units * (acres / source_acres), 4)
    return None


def apply_planting_fill(
    db: Any,
    field: Any,
    *,
    mode: str,
    acres: float,
    units: Optional[float] = None,
    source_link_id: Optional[int] = None,
    hybrid_id: Optional[int] = None,
    rate: Optional[str] = None,
    population: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Fill remaining planting acres by extending a link or adding another hybrid."""
    from sqlalchemy import select

    from app.models import FieldHybrid, Hybrid

    acres = float(acres or 0)
    if acres <= 0:
        return {"ok": False, "error": "Acres to add must be greater than 0."}

    mode = (mode or "").strip().lower()
    note_bit = (notes or "").strip() or None
    units_override = _fnum(units)

    def _sync_acres(link: Any, hybrid: Any) -> None:
        calc = link_planted_acres(link, hybrid)
        if calc is not None:
            link.acres = calc

    if mode == "extend":
        if not source_link_id:
            return {"ok": False, "error": "Pick which planted hybrid to extend."}
        link = db.get(FieldHybrid, int(source_link_id))
        if not link or link.field_id != field.id:
            return {"ok": False, "error": "Planted hybrid row not found on this field."}
        hybrid = db.get(Hybrid, link.hybrid_id)
        pop = population_from_link(link)
        if population is not None:
            pop = population
            link.population = population
        spu = seeds_per_unit(
            getattr(hybrid, "crop", None) if hybrid else None,
            getattr(hybrid, "brand", None) if hybrid else None,
        )
        old_units = _fnum(getattr(link, "units", None)) or 0.0
        add_units = units_override
        if add_units is None:
            add_units = estimate_units_for_acres(
                acres=acres,
                source_acres=None,
                source_units=None,
                population=pop,
                seeds_per_unit=spu,
            )
        if add_units is None:
            return {
                "ok": False,
                "error": "Need a seeding rate (population) on that hybrid to extend by acres.",
            }
        link.units = round(old_units + float(add_units), 4)
        if rate and not link.rate:
            link.rate = rate.strip()
        if note_bit:
            prev = (link.notes or "").strip()
            fill_note = f"Filled +{acres:g} ac / +{float(add_units):g} units"
            link.notes = f"{prev} · {fill_note}".strip(" ·") if prev else fill_note
        _sync_acres(link, hybrid)
        return {
            "ok": True,
            "mode": "extend",
            "hybrid_name": hybrid.name if hybrid else "Hybrid",
            "acres_added": acres,
            "units_added": float(add_units),
            "link_id": link.id,
        }

    if mode == "new":
        if not hybrid_id:
            return {"ok": False, "error": "Select a hybrid / variety."}
        hybrid = db.get(Hybrid, int(hybrid_id))
        if not hybrid:
            return {"ok": False, "error": "Hybrid not found."}
        pop = population
        rate_s = (rate or "").strip() or None
        if pop is not None and not rate_s:
            rate_s = f"{pop:g} seeds/ac"
        spu = seeds_per_unit(hybrid.crop, hybrid.brand)
        add_units = units_override
        if add_units is None:
            add_units = estimate_units_for_acres(
                acres=acres,
                source_acres=None,
                source_units=None,
                population=pop,
                seeds_per_unit=spu,
            )
        if add_units is None:
            return {"ok": False, "error": "Enter population (seeds/ac) to size the fill."}

        existing = db.scalar(
            select(FieldHybrid).where(
                FieldHybrid.field_id == field.id,
                FieldHybrid.hybrid_id == hybrid.id,
            )
        )
        if existing:
            old_units = _fnum(getattr(existing, "units", None)) or 0.0
            existing.units = round(old_units + float(add_units), 4)
            if pop is not None:
                existing.population = pop
            if rate_s:
                existing.rate = rate_s
            if note_bit:
                prev = (existing.notes or "").strip()
                fill_note = f"Filled +{acres:g} ac / +{float(add_units):g} units"
                existing.notes = f"{prev} · {fill_note}".strip(" ·") if prev else fill_note
            link = existing
        else:
            link = FieldHybrid(
                field_id=field.id,
                hybrid_id=hybrid.id,
                units=float(add_units),
                population=pop,
                rate=rate_s,
                notes=note_bit or f"Filled {acres:g} ac ({float(add_units):g} units) to complete planting",
            )
            db.add(link)
        _sync_acres(link, hybrid)
        db.flush()
        return {
            "ok": True,
            "mode": "new",
            "hybrid_name": hybrid.name,
            "acres_added": acres,
            "units_added": float(add_units),
            "link_id": link.id,
        }

    return {"ok": False, "error": "Choose extend current hybrid or add another."}


def hybrid_link_cost(
    link: Any,
    hybrid: Any,
    field_acres: float,
) -> tuple[Optional[float], str]:
    """
    Cost for a planted hybrid on a field.

    Prefer as-planted units × catalog cost_per_unit (accurate P&L).
    Else catalog cost_per_acre × planted acres from units×rate (not imported acres).
    """
    units = _fnum(getattr(link, "units", None))
    plant_acres = link_planted_acres(link, hybrid)
    if plant_acres is None or plant_acres <= 0:
        plant_acres = float(field_acres or 0) or None

    cpu = getattr(hybrid, "cost_per_unit", None) if hybrid else None
    cpa = getattr(hybrid, "cost_per_acre", None) if hybrid else None
    try:
        cpu = float(cpu) if cpu is not None else None
    except (TypeError, ValueError):
        cpu = None
    try:
        cpa = float(cpa) if cpa is not None else None
    except (TypeError, ValueError):
        cpa = None

    rate = getattr(link, "rate", None)
    bits: list[str] = []
    if rate:
        bits.append(str(rate))
    calc = link_planted_acres(link, hybrid)
    if calc is not None:
        bits.append(f"{calc:g} ac from units")

    if units is not None and units > 0 and cpu is not None:
        amt = units * cpu
        bits.append(f"{units:g} units × ${cpu:.2f}")
        return _money(amt), " · ".join(bits)

    if cpa is not None and plant_acres:
        amt = cpa * plant_acres
        bits.append(f"${cpa:.2f}/ac × {plant_acres:g} ac")
        return _money(amt), " · ".join(bits)

    if units is not None and units > 0:
        bits.append(f"{units:g} units · set cost/unit on hybrid for $")
        return None, " · ".join(bits)

    if cpu is not None:
        bits.append(f"${cpu:.2f}/{getattr(hybrid, 'unit_label', None) or 'unit'} · need units on field")
    return None, " · ".join(bits)


def build_field_ledger(
    field: Any,
    *,
    ops: list[Any],
    assigns: list[Any],
    products_by_id: dict[int, Any],
    spray_links: list[tuple[Any, Any]],
    hybrid_links: list[tuple[Any, Any]],
    plans: list[Any],
    soils: list[Any],
    settings: Any,
    board: dict[str, Any],
    contracts: list[Any],
) -> dict[str, Any]:
    """
    Chronological ledger + economics strip for one field season.

    Plans appear as estimate activity (est_amount) and do NOT count in running cost.
    """
    acres = _acres(field)
    rent = float(getattr(field, "rent_my_share", 0) or 0)
    crop = getattr(field, "crop", "") or ""
    events: list[dict[str, Any]] = []

    if rent:
        events.append(
            {
                "date": None,
                "undated": True,
                "type": "rent",
                "type_label": "Rent",
                "title": "Cash rent (my share)",
                "detail": f"{acres:g} ac × ${float(getattr(field, 'rent_per_acre', 0) or 0):.2f}/ac",
                "amount": _money(rent),
                "est_amount": None,
                "counts": True,
                "sort_key": (0, date.min, 0),
            }
        )

    for o in ops:
        amt = float(getattr(o, "cost", 0) or 0)
        d = getattr(o, "op_date", None)
        detail = getattr(o, "description", None) or ""
        from_budget = "[budget_pass]" in detail or "[budget_spray]" in detail
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "op",
                "type_label": "Op",
                "title": getattr(o, "op_type", None) or "Operation",
                "detail": detail,
                "amount": _money(amt) if not from_budget else None,
                "est_amount": _money(amt) if from_budget else None,
                "counts": not from_budget,
                "sort_key": (1 if d else 0, d or date.min, getattr(o, "id", 0) or 0),
                "source_kind": "op",
                "source_id": getattr(o, "id", None),
                "invoice_id": getattr(o, "invoice_id", None),
            }
        )

    for a in assigns:
        qty = float(getattr(a, "quantity", 0) or 0)
        unit = float(getattr(a, "unit_cost", 0) or 0)
        amt = qty * unit
        prod = products_by_id.get(getattr(a, "product_id", 0))
        pname = getattr(prod, "name", None) if prod else f"Product #{getattr(a, 'product_id', '?')}"
        unit_label = getattr(prod, "unit", None) if prod else ""
        d = getattr(a, "assign_date", None)
        detail = f"{qty:g} {unit_label or 'units'} @ ${unit:.4g}"
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "input",
                "type_label": "Input",
                "title": pname,
                "detail": detail,
                "amount": _money(amt),
                "est_amount": None,
                "counts": True,
                "sort_key": (1 if d else 0, d or date.min, getattr(a, "id", 0) or 0),
                "source_kind": "input",
                "source_id": getattr(a, "id", None),
            }
        )

    for link, mix in spray_links:
        cpa = getattr(mix, "cost_per_acre", None) if mix else None
        amt = float(cpa) * acres if cpa is not None and acres else None
        d = getattr(link, "applied_date", None)
        timing = getattr(link, "timing_label", None) or (getattr(mix, "timing", None) if mix else None)
        title = getattr(mix, "name", None) if mix else f"Spray #{getattr(link, 'spray_mix_id', '?')}"
        notes = getattr(link, "notes", None) or ""
        from_budget = "[budget_spray]" in notes
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "spray",
                "type_label": "Spray",
                "title": title,
                "detail": (timing or "")
                + (f" · ${cpa:.2f}/ac × {acres:g} ac" if cpa is not None else "")
                + ((" · " + notes) if notes and not from_budget else ""),
                "amount": _money(amt) if amt is not None and not from_budget else None,
                "est_amount": _money(amt) if amt is not None and from_budget else None,
                "counts": amt is not None and not from_budget,
                "sort_key": (1 if d else 0, d or date.min, getattr(link, "id", 0) or 0),
                "source_kind": "spray",
                "source_id": getattr(link, "id", None),
            }
        )

    for link, hybrid in hybrid_links:
        amt, detail = hybrid_link_cost(link, hybrid, acres)
        d = getattr(link, "applied_date", None)
        title = getattr(hybrid, "name", None) if hybrid else f"Hybrid #{getattr(link, 'hybrid_id', '?')}"
        brand = getattr(hybrid, "brand", None) if hybrid else None
        if brand:
            title = f"{title} ({brand})"
        notes = getattr(link, "notes", None) or ""
        from_budget = "[budget_seed]" in notes
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "hybrid",
                "type_label": "Hybrid",
                "title": title,
                "detail": detail,
                "amount": amt if not from_budget else None,
                "est_amount": amt if from_budget else None,
                "counts": amt is not None and not from_budget,
                "sort_key": (1 if d else 0, d or date.min, getattr(link, "id", 0) or 0),
                "source_kind": "hybrid",
                "source_id": getattr(link, "id", None),
            }
        )

    for p in plans:
        d = getattr(p, "completed_date", None) or getattr(p, "target_date", None)
        cpa = getattr(p, "estimated_cost_per_acre", None)
        est = float(cpa) * acres if cpa is not None and acres else None
        status = getattr(p, "status", "") or ""
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "plan",
                "type_label": "Plan",
                "title": getattr(p, "title", None) or "Plan",
                "detail": f"{getattr(p, 'plan_type', '')} · {status}"
                + (f" · est ${cpa:.2f}/ac" if cpa is not None else ""),
                "amount": None,
                "est_amount": _money(est) if est is not None else None,
                "counts": False,
                "sort_key": (1 if d else 0, d or date.min, getattr(p, "id", 0) or 0),
            }
        )

    for s in soils:
        d = getattr(s, "test_date", None)
        bits = []
        if getattr(s, "ph", None) is not None:
            bits.append(f"pH {s.ph}")
        if getattr(s, "p", None) is not None:
            bits.append(f"P {s.p}")
        if getattr(s, "k", None) is not None:
            bits.append(f"K {s.k}")
        lab = getattr(s, "lab", None)
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "soil",
                "type_label": "Soil",
                "title": "Soil test" + (f" · {lab}" if lab else ""),
                "detail": " · ".join(bits),
                "amount": 0.0,
                "est_amount": None,
                "counts": False,
                "sort_key": (1 if d else 0, d or date.min, getattr(s, "id", 0) or 0),
            }
        )

    # Undated first (season group), then chronological
    events.sort(key=lambda e: e["sort_key"])

    running = 0.0
    for e in events:
        if e.get("counts") and e.get("amount") is not None:
            running += float(e["amount"])
        e["running"] = round(running, 2) if e.get("counts") and e.get("amount") is not None else None
        # Always show running after cost events; for non-cost keep last running
    last_run = 0.0
    for e in events:
        if e.get("running") is not None:
            last_run = e["running"]
        else:
            e["running_display"] = last_run
    for e in events:
        if e.get("running") is None:
            e["running"] = e.get("running_display", last_run)
        e.pop("running_display", None)
        e.pop("sort_key", None)

    running_cost = round(running, 2)
    cost_ac = round(running_cost / acres, 2) if acres else None
    expected_bu = getattr(field, "expected_bushels", None)
    expected_yld = getattr(field, "expected_yield", None)

    marks = field_mark_prices(settings, board, crop, contracts)
    contracted = marks["contracted"]
    futures = marks["futures"]

    def projected(mark: Optional[float]) -> Optional[float]:
        if mark is None or expected_bu is None:
            return None
        return round(float(expected_bu) * float(mark), 2)

    def be_yield(mark: Optional[float]) -> Optional[float]:
        if mark is None or not acres or mark <= 0:
            return None
        return round(running_cost / (acres * float(mark)), 1)

    be_c = be_yield(contracted)
    be_f = be_yield(futures)
    proj_c = projected(contracted)
    proj_f = projected(futures)

    # $/bu needed to cover running cost at expected (average) yield
    be_price = None
    if expected_bu is not None and float(expected_bu) > 0:
        be_price = round(running_cost / float(expected_bu), 2)
    elif expected_yld is not None and acres and float(expected_yld) > 0:
        be_price = round(running_cost / (acres * float(expected_yld)), 2)

    def gap(proj: Optional[float]) -> Optional[float]:
        if proj is None:
            return None
        return round(proj - running_cost, 2)

    def yld_vs(be: Optional[float]) -> Optional[float]:
        if be is None or expected_yld is None:
            return None
        return round(float(expected_yld) - float(be), 1)

    return {
        "events": events,
        "running_cost": running_cost,
        "cost_ac": cost_ac,
        "acres": acres,
        "rent": rent,
        "expected_bu": expected_bu,
        "expected_yield": expected_yld,
        "marks": marks,
        "projected_contracted": proj_c,
        "projected_futures": proj_f,
        "gap_contracted": gap(proj_c),
        "gap_futures": gap(proj_f),
        "be_yield_contracted": be_c,
        "be_yield_futures": be_f,
        "be_price_at_yield": be_price,
        "vs_be_contracted": yld_vs(be_c),
        "vs_be_futures": yld_vs(be_f),
        "crop": crop,
    }


def build_year_field_cards(
    fields: list[Any],
    *,
    ops_by_field: dict[int, list[Any]],
    assigns_by_field: dict[int, list[Any]],
    products_by_id: dict[int, Any],
    sprays_by_field: dict[int, list[tuple[Any, Any]]],
    hybrids_by_field: dict[int, list[tuple[Any, Any]]],
    plans_by_field: dict[int, list[Any]],
    soils_by_field: dict[int, list[Any]],
    settings: Any,
    board: dict[str, Any],
    contracts: list[Any],
) -> list[dict[str, Any]]:
    """One ledger card per field for Fields main ops."""
    cards: list[dict[str, Any]] = []
    farm_total = 0.0
    farm_acres = 0.0
    for field in fields:
        fid = getattr(field, "id", None)
        ledger = build_field_ledger(
            field,
            ops=ops_by_field.get(fid, []),
            assigns=assigns_by_field.get(fid, []),
            products_by_id=products_by_id,
            spray_links=sprays_by_field.get(fid, []),
            hybrid_links=hybrids_by_field.get(fid, []),
            plans=plans_by_field.get(fid, []),
            soils=soils_by_field.get(fid, []),
            settings=settings,
            board=board,
            contracts=contracts,
        )
        # Ops list for the hub: cost-bearing + hybrid/spray activity (skip soil noise unless alone)
        op_events = [
            e
            for e in ledger["events"]
            if e.get("type") in ("rent", "op", "input", "spray", "hybrid", "plan")
        ]
        # Compact “done” labels for overview cards (skip rent — always known)
        completed = [
            e
            for e in op_events
            if e.get("type") in ("op", "input", "spray", "hybrid")
        ]
        planting = planting_coverage(field, hybrids_by_field.get(fid, []))
        farm_total += float(ledger["running_cost"] or 0)
        farm_acres += float(ledger["acres"] or 0)
        cards.append(
            {
                "field": field,
                "ledger": ledger,
                "events": op_events,
                "completed": completed,
                "event_count": len(op_events),
                "completed_count": len(completed),
                "running_cost": ledger["running_cost"],
                "cost_ac": ledger["cost_ac"],
                "acres": ledger["acres"],
                "be_yield_contracted": ledger["be_yield_contracted"],
                "be_yield_futures": ledger["be_yield_futures"],
                "be_price_at_yield": ledger["be_price_at_yield"],
                "marks": ledger["marks"],
                "expected_yield": ledger["expected_yield"],
                "planting": planting,
            }
        )
    return cards


def load_year_field_cards(db: Any, year: Any, fields: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load ledger inputs from DB and build per-field cards + year rollup.

    Returns (cards, meta) where meta has year_cost, year_acres, year_cost_ac,
    unmapped_plantings, board, contracts.
    """
    from collections import defaultdict

    from sqlalchemy import func, select

    from app import cme_quotes
    from app.models import (
        AppSettings,
        FieldAssignment,
        FieldHybrid,
        FieldOperation,
        FieldPlan,
        FieldSprayMix,
        GrainContract,
        Hybrid,
        InputProduct,
        PlantingRecord,
        SoilTest,
        SprayMix,
    )

    meta: dict[str, Any] = {
        "year_cost": 0.0,
        "year_acres": 0.0,
        "year_cost_ac": None,
        "unmapped_plantings": 0,
        "board": {},
        "contracts": [],
        "settings": None,
    }
    if not year or not fields:
        return [], meta

    fids = [f.id for f in fields]
    settings = db.scalar(select(AppSettings).limit(1))
    board = cme_quotes.board_from_settings(settings)
    contracts = list(
        db.scalars(
            select(GrainContract).where(
                GrainContract.crop_year_id == year.id,
                GrainContract.status == "open",
            )
        )
    )
    meta["settings"] = settings
    meta["board"] = board
    meta["contracts"] = contracts

    ops_by: dict[int, list] = defaultdict(list)
    for o in db.scalars(select(FieldOperation).where(FieldOperation.field_id.in_(fids))):
        ops_by[o.field_id].append(o)

    assigns_by: dict[int, list] = defaultdict(list)
    for a in db.scalars(select(FieldAssignment).where(FieldAssignment.field_id.in_(fids))):
        assigns_by[a.field_id].append(a)

    products = {p.id: p for p in db.scalars(select(InputProduct))}

    hybrids_map = {h.id: h for h in db.scalars(select(Hybrid).where(Hybrid.crop_year_id == year.id))}
    hybrids_by: dict[int, list] = defaultdict(list)
    for link in db.scalars(select(FieldHybrid).where(FieldHybrid.field_id.in_(fids))):
        hybrids_by[link.field_id].append((link, hybrids_map.get(link.hybrid_id)))

    sprays_map = {s.id: s for s in db.scalars(select(SprayMix).where(SprayMix.crop_year_id == year.id))}
    sprays_by: dict[int, list] = defaultdict(list)
    for link in db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id.in_(fids))):
        sprays_by[link.field_id].append((link, sprays_map.get(link.spray_mix_id)))

    plans_by: dict[int, list] = defaultdict(list)
    for p in db.scalars(select(FieldPlan).where(FieldPlan.field_id.in_(fids))):
        plans_by[p.field_id].append(p)

    soils_by: dict[int, list] = defaultdict(list)
    for s in db.scalars(select(SoilTest).where(SoilTest.field_id.in_(fids))):
        soils_by[s.field_id].append(s)

    meta["unmapped_plantings"] = (
        db.scalar(
            select(func.count())
            .select_from(PlantingRecord)
            .where(
                PlantingRecord.crop_year_id == year.id,
                PlantingRecord.field_id.is_(None),
            )
        )
        or 0
    )

    cards = build_year_field_cards(
        fields,
        ops_by_field=ops_by,
        assigns_by_field=assigns_by,
        products_by_id=products,
        sprays_by_field=sprays_by,
        hybrids_by_field=hybrids_by,
        plans_by_field=plans_by,
        soils_by_field=soils_by,
        settings=settings,
        board=board,
        contracts=contracts,
    )
    year_cost = round(sum(float(c["running_cost"] or 0) for c in cards), 2)
    year_acres = round(sum(float(c["acres"] or 0) for c in cards), 2)
    meta["year_cost"] = year_cost
    meta["year_acres"] = year_acres
    meta["year_cost_ac"] = round(year_cost / year_acres, 2) if year_acres else None
    return cards, meta
