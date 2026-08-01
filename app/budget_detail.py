"""Detailed field budget math: seed bags, fertilizer density, passes, spray, travel."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Field,
    FieldBudget,
    FieldBudgetFertLine,
    FieldBudgetLine,
    FieldBudgetPass,
    FieldBudgetSeedLine,
    FieldBudgetSprayPass,
    FieldHybrid,
    FieldOperation,
    FieldPlan,
    FieldSprayMix,
    FertilizerProduct,
    FertilizerPurchase,
    Hybrid,
    SprayMix,
)
from app.operation_rates import effective_rates

# Starter pass catalog (Add New adds LookupValue / custom pass_key)
DEFAULT_PASSES: list[tuple[str, str, float, str | None]] = [
    # key, label, default round_trips, op_rate_key (for self $/ac default)
    ("aerway", "Aer-way", 1.0, "spring_till"),
    ("soil_finisher", "Soil finisher", 1.0, "spring_till"),
    ("corn_plant", "Corn planting", 1.0, "corn_plant"),
    ("soy_plant", "Soybean planting", 1.0, "soy_plant_60"),
    ("spray", "Spraying", 1.0, "spray"),
    ("dry_spread", "Dry spread Fertilizer", 1.0, None),
    ("sidedress", "Sidedress", 1.0, "sidedress"),
    ("combine", "Combine", 1.0, None),
]

DEFAULT_FERT_PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "11-52-0",
        "form": "dry",
        "apply_unit": "lb",
        "density_lb_per_gal": None,
        "price_per_ton": 0.0,
        "sort_order": 10,
    },
    {
        "name": "0-0-60",
        "form": "dry",
        "apply_unit": "lb",
        "density_lb_per_gal": None,
        "price_per_ton": 0.0,
        "sort_order": 20,
    },
    {
        "name": "32-0-0",
        "form": "liquid",
        "apply_unit": "gal",
        "density_lb_per_gal": 11.08,
        "price_per_ton": 0.0,
        "sort_order": 30,
    },
    {
        "name": "AMS",
        "form": "dry",
        "apply_unit": "lb",
        "density_lb_per_gal": None,
        "price_per_ton": 0.0,
        "sort_order": 40,
    },
    {
        "name": "ATS",
        "form": "liquid",
        "apply_unit": "gal",
        "density_lb_per_gal": 11.1,
        "price_per_ton": 0.0,
        "sort_order": 50,
    },
]

BUDGET_OP_MARKER = "[budget_pass]"
BUDGET_SPRAY_MARKER = "[budget_spray]"
BUDGET_FERT_MARKER = "[budget_fert]"
BUDGET_SEED_MARKER = "[budget_seed]"


def seed_fertilizer_catalog(db: Session) -> None:
    existing = {p.name for p in db.scalars(select(FertilizerProduct))}
    for spec in DEFAULT_FERT_PRODUCTS:
        if spec["name"] in existing:
            continue
        db.add(FertilizerProduct(**spec, is_active=1))
    db.flush()


def travel_hours(*, miles: float, speed_mph: float, round_trips: float) -> float:
    miles = max(0.0, float(miles or 0))
    speed = max(1.0, float(speed_mph or 30))
    trips = max(0.0, float(round_trips or 0))
    one_way_hr = miles / speed
    return round(2.0 * one_way_hr * trips, 4)


def travel_cost(
    *,
    miles: float,
    speed_mph: float,
    round_trips: float,
    rate_per_hr: float,
) -> float:
    return round(travel_hours(miles=miles, speed_mph=speed_mph, round_trips=round_trips) * float(rate_per_hr or 0), 2)


def seed_cost(
    *,
    acres: float,
    population: float | None,
    bag_kernels: float,
    cost_per_bag: float | None,
) -> dict[str, float | None]:
    acres = float(acres or 0)
    pop = float(population or 0)
    bag = max(1.0, float(bag_kernels or 80000))
    cpb = float(cost_per_bag) if cost_per_bag is not None else None
    if acres <= 0 or pop <= 0 or cpb is None:
        return {"bags": None, "total": None, "per_ac": None}
    bags = (pop * acres) / bag
    total = bags * cpb
    return {
        "bags": round(bags, 3),
        "total": round(total, 2),
        "per_ac": round(total / acres, 2) if acres else None,
    }


def ensure_seed_lines_from_legacy(db: Session, budget: FieldBudget, field: Field) -> None:
    """One-time: move legacy single-hybrid fields into seed_lines."""
    if budget.seed_lines:
        return
    if not budget.seed_hybrid_id and not budget.seed_population and budget.seed_cost_per_bag is None:
        return
    acres = float(field.acres_mine or field.acres_total or 0)
    db.add(
        FieldBudgetSeedLine(
            budget_id=budget.id,
            hybrid_id=budget.seed_hybrid_id,
            acres=acres,
            population=budget.seed_population,
            bag_kernels=float(budget.seed_bag_kernels or 80000),
            cost_per_bag=budget.seed_cost_per_bag,
            sort_order=0,
        )
    )
    db.flush()


def compute_seed_plan(
    field: Field,
    budget: FieldBudget,
) -> dict[str, Any]:
    """Multi-hybrid seed rollup + coverage vs field acres."""
    field_acres = float(field.acres_mine or field.acres_total or 0)
    rows = []
    bags_total = 0.0
    cost_total = 0.0
    allocated = 0.0
    has_any = False

    lines = list(budget.seed_lines or [])
    if not lines and budget.seed_hybrid_id:
        # Fallback if migrate not flushed yet
        lines = [
            FieldBudgetSeedLine(
                hybrid_id=budget.seed_hybrid_id,
                acres=field_acres,
                population=budget.seed_population,
                bag_kernels=float(budget.seed_bag_kernels or 80000),
                cost_per_bag=budget.seed_cost_per_bag,
            )
        ]

    for ln in lines:
        ac = float(ln.acres or 0)
        # Prefer line cost; else catalog hybrid cost_per_unit
        cpb = ln.cost_per_bag
        if cpb is None and ln.hybrid is not None and ln.hybrid.cost_per_unit is not None:
            cpb = float(ln.hybrid.cost_per_unit)
        calc = seed_cost(
            acres=ac,
            population=ln.population,
            bag_kernels=ln.bag_kernels or 80000,
            cost_per_bag=cpb,
        )
        allocated += ac
        if calc["bags"] is not None:
            bags_total += float(calc["bags"])
            has_any = True
        if calc["total"] is not None:
            cost_total += float(calc["total"])
            has_any = True
        rows.append(
            {
                "line": ln,
                "hybrid": ln.hybrid,
                "acres": ac,
                "population": ln.population,
                "bag_kernels": float(ln.bag_kernels or 80000),
                "cost_per_bag": cpb,
                "bags": calc["bags"],
                "total": calc["total"],
                "per_ac": calc["per_ac"],
            }
        )

    remaining = round(field_acres - allocated, 2)
    coverage_pct = round(100.0 * allocated / field_acres, 1) if field_acres > 0.05 else None
    return {
        "rows": rows,
        "bags": round(bags_total, 3) if has_any else None,
        "total": round(cost_total, 2) if has_any else None,
        "per_ac": round(cost_total / field_acres, 2) if has_any and field_acres > 0 else None,
        "field_acres": field_acres,
        "allocated_acres": round(allocated, 2),
        "remaining_acres": remaining,
        "coverage_pct": coverage_pct,
        "is_short": remaining > 0.05,
        "is_over": remaining < -0.05,
    }


def apply_fertilizer_purchase(
    db: Session,
    product: FertilizerProduct,
    *,
    tons: float,
    total_cost: float,
    purchase_date: date,
    vendor: str | None = None,
    notes: str | None = None,
    crop_year_id: int | None = None,
) -> FertilizerPurchase:
    """Log a buy lot and recompute weighted-average $/ton on the product."""
    tons = max(0.0, float(tons or 0))
    total_cost = max(0.0, float(total_cost or 0))
    old_tons = float(product.tons_purchased or 0)
    old_price = float(product.price_per_ton or 0)
    old_value = old_tons * old_price
    new_tons = old_tons + tons
    product.tons_purchased = new_tons
    if new_tons > 0:
        product.price_per_ton = round((old_value + total_cost) / new_tons, 4)
    lot = FertilizerPurchase(
        product_id=product.id,
        crop_year_id=crop_year_id,
        purchase_date=purchase_date,
        vendor=(vendor or None),
        tons=tons,
        total_cost=total_cost,
        notes=notes,
    )
    db.add(lot)
    db.flush()
    return lot


def recompute_fertilizer_avg(db: Session, product: FertilizerProduct) -> None:
    """Rebuild avg $/ton + tons_purchased from all lots (after deletes/edits)."""
    lots = list(
        db.scalars(
            select(FertilizerPurchase).where(FertilizerPurchase.product_id == product.id)
        )
    )
    tons = sum(float(l.tons or 0) for l in lots)
    value = sum(float(l.total_cost or 0) for l in lots)
    product.tons_purchased = tons
    if tons > 0:
        product.price_per_ton = round(value / tons, 4)


def fert_line_cost(
    product: FertilizerProduct,
    *,
    rate_per_ac: float,
    acres: float,
) -> dict[str, float | None]:
    """Convert rate × acres → tons → $ using product form/density."""
    acres = float(acres or 0)
    rate = float(rate_per_ac or 0)
    price = float(product.price_per_ton or 0)
    if acres <= 0 or rate <= 0:
        return {"tons": 0.0, "total": 0.0, "per_ac": 0.0}

    apply_unit = (product.apply_unit or "lb").lower()
    form = (product.form or "dry").lower()
    if form == "liquid" or apply_unit == "gal":
        dens = float(product.density_lb_per_gal or 0)
        if dens <= 0:
            return {"tons": None, "total": None, "per_ac": None}
        total_gal = rate * acres
        total_lb = total_gal * dens
    else:
        # dry lb/ac
        total_lb = rate * acres

    tons = total_lb / 2000.0
    total = tons * price
    return {
        "tons": round(tons, 4),
        "total": round(total, 2),
        "per_ac": round(total / acres, 2),
    }


def default_self_rate(settings: Any, pass_key: str, crop: str) -> float:
    rates = effective_rates(settings)
    by_key = {r.key: r for r in rates}
    for key, _label, _trips, op_key in DEFAULT_PASSES:
        if key != pass_key:
            continue
        if op_key and op_key in by_key:
            return float(by_key[op_key].rate_per_ac)
        if pass_key == "combine":
            if crop == "Corn" and "corn_harvest" in by_key:
                return float(by_key["corn_harvest"].rate_per_ac)
            if crop in ("Soybeans", "Soy") and "soy_harvest" in by_key:
                return float(by_key["soy_harvest"].rate_per_ac)
        if pass_key == "dry_spread" and "anhydrous" in by_key:
            return 0.0
    return 0.0


def compute_detailed_budget(
    field: Field,
    budget: FieldBudget,
    *,
    settings: Any,
    spray_mixes_by_id: dict[int, SprayMix] | None = None,
) -> dict[str, Any]:
    acres = float(field.acres_mine or field.acres_total or 0)
    acres_ops = float(field.acres_total or field.acres_mine or 0) or acres
    miles = float(field.distance_miles or 0)
    speed = float(getattr(settings, "budget_travel_speed_mph", None) or 30)
    rate_hr = float(getattr(settings, "budget_travel_rate_per_hr", None) or 150)

    seed = compute_seed_plan(field, budget)

    fert_rows = []
    fert_total = 0.0
    for ln in budget.fert_lines or []:
        prod = ln.product
        if not prod:
            continue
        calc = fert_line_cost(prod, rate_per_ac=float(ln.rate_per_ac or 0), acres=acres)
        total = float(calc["total"] or 0)
        fert_total += total
        fert_rows.append(
            {
                "line": ln,
                "product": prod,
                "rate_per_ac": float(ln.rate_per_ac or 0),
                "tons": calc["tons"],
                "total": calc["total"],
                "per_ac": calc["per_ac"],
            }
        )

    pass_rows = []
    pass_mach_total = 0.0
    travel_total = 0.0
    for p in budget.passes or []:
        if not int(p.enabled or 0):
            continue
        hired = bool(int(p.is_hired or 0))
        rate_ac = float(p.hired_rate_ac or 0) if hired else float(p.self_rate_ac or 0)
        mach = round(rate_ac * acres_ops, 2)
        trips = float(p.round_trips or 0)
        tcost = travel_cost(miles=miles, speed_mph=speed, round_trips=trips, rate_per_hr=rate_hr)
        pass_mach_total += mach
        travel_total += tcost
        pass_rows.append(
            {
                "pass": p,
                "hired": hired,
                "mach_total": mach,
                "mach_ac": rate_ac,
                "travel_total": tcost,
                "travel_hours": travel_hours(miles=miles, speed_mph=speed, round_trips=trips),
            }
        )

    spray_rows = []
    spray_chem_total = 0.0
    spray_hire_total = 0.0
    mixes = spray_mixes_by_id or {}
    for sp in budget.spray_passes or []:
        mix = sp.spray_mix or mixes.get(sp.spray_mix_id or 0)
        chem_ac = float(getattr(mix, "cost_per_acre", None) or 0) if mix else 0.0
        chem = round(chem_ac * acres, 2)
        hired = bool(int(sp.is_hired or 0))
        hire_ac = float(sp.hired_rate_ac or 0) if hired else 0.0
        # If not hired, spraying machinery may already be a FieldBudgetPass "spray"
        hire = round(hire_ac * acres_ops, 2)
        trips = float(sp.round_trips or 0)
        tcost = travel_cost(miles=miles, speed_mph=speed, round_trips=trips, rate_per_hr=rate_hr)
        spray_chem_total += chem
        spray_hire_total += hire
        travel_total += tcost
        spray_rows.append(
            {
                "pass": sp,
                "mix": mix,
                "chem_total": chem,
                "hire_total": hire,
                "travel_total": tcost,
                "travel_hours": travel_hours(miles=miles, speed_mph=speed, round_trips=trips),
            }
        )

    seed_total = float(seed["total"] or 0)
    planned_total = round(
        seed_total + fert_total + pass_mach_total + spray_chem_total + spray_hire_total + travel_total,
        2,
    )
    # Rollup $/ac for board categories
    rollup = {
        "seed": round(seed_total / acres, 2) if acres else 0.0,
        "fertilizer": round(fert_total / acres, 2) if acres else 0.0,
        "chemical": round(spray_chem_total / acres, 2) if acres else 0.0,
        "fuel_ops": round((pass_mach_total + spray_hire_total + travel_total) / acres, 2) if acres else 0.0,
        "rent": float(field.rent_per_acre or 0),
        "insurance": 0.0,
        "other": 0.0,
    }
    rent_total = round(float(field.rent_per_acre or 0) * acres, 2)
    planned_total = round(planned_total + rent_total, 2)

    return {
        "acres": acres,
        "acres_ops": acres_ops,
        "miles": miles,
        "speed_mph": speed,
        "rate_per_hr": rate_hr,
        "seed": seed,
        "fert_rows": fert_rows,
        "fert_total": round(fert_total, 2),
        "pass_rows": pass_rows,
        "pass_mach_total": round(pass_mach_total, 2),
        "spray_rows": spray_rows,
        "spray_chem_total": round(spray_chem_total, 2),
        "spray_hire_total": round(spray_hire_total, 2),
        "travel_total": round(travel_total, 2),
        "rent_total": rent_total,
        "planned_total": planned_total,
        "planned_ac": round(planned_total / acres, 2) if acres else 0.0,
        "rollup": rollup,
    }


def sync_rollup_lines(db: Session, budget: FieldBudget, rollup: dict[str, float]) -> None:
    from app.budgeting import BUDGET_CATEGORIES, set_budget_lines

    existing = {ln.category: float(ln.amount_per_ac or 0) for ln in (budget.lines or [])}
    amounts = {k: float(rollup.get(k) or 0) for k, _ in BUDGET_CATEGORIES}
    # Keep insurance/other as manual overrides (detail engine doesn't own them)
    for keep in ("insurance", "other"):
        amounts[keep] = existing.get(keep, amounts.get(keep, 0.0))
    set_budget_lines(db, budget, amounts)


def ensure_default_passes(db: Session, budget: FieldBudget, field: Field, settings: Any) -> None:
    if budget.passes:
        return
    crop = field.crop or ""
    for i, (key, label, trips, _op) in enumerate(DEFAULT_PASSES):
        # Skip crop-mismatched plant/combine defaults being enabled
        enabled = 1
        if key == "corn_plant" and crop not in ("Corn",):
            enabled = 0
        if key == "soy_plant" and crop not in ("Soybeans", "Soy"):
            enabled = 0
        if key == "combine" and crop not in ("Corn", "Soybeans", "Soy"):
            enabled = 0
        if key == "spray":
            enabled = 0  # spray passes section owns spray apps
        db.add(
            FieldBudgetPass(
                budget_id=budget.id,
                pass_key=key,
                pass_label=label,
                enabled=enabled,
                is_hired=0,
                hired_rate_ac=0.0,
                self_rate_ac=default_self_rate(settings, key, crop),
                round_trips=trips,
                sort_order=i,
            )
        )
    db.flush()


def sync_budget_to_operations(db: Session, field: Field, budget: FieldBudget, detail: dict[str, Any]) -> None:
    """Push plan into FieldHybrid / FieldOperation / FieldSprayMix / FieldPlan (actual-side records)."""
    year_id = budget.crop_year_id
    today = date.today()

    # Seed → FieldHybrid (one link per budget seed line, tagged)
    old_hybrids = list(db.scalars(select(FieldHybrid).where(FieldHybrid.field_id == field.id)))
    for link in old_hybrids:
        if link.notes and BUDGET_SEED_MARKER in (link.notes or ""):
            db.delete(link)
    db.flush()

    for row in detail["seed"].get("rows") or []:
        ln = row["line"]
        hid = getattr(ln, "hybrid_id", None) or (row["hybrid"].id if row.get("hybrid") else None)
        if not hid:
            continue
        bags = row.get("bags")
        link = FieldHybrid(
            field_id=field.id,
            hybrid_id=hid,
            population=row.get("population"),
            acres=float(row.get("acres") or 0) or None,
            units=float(bags) if bags is not None else None,
            notes=BUDGET_SEED_MARKER,
        )
        db.add(link)
        cpb = row.get("cost_per_bag")
        hyb = db.get(Hybrid, hid)
        if hyb and cpb is not None:
            hyb.cost_per_unit = float(cpb)
            hyb.unit_label = hyb.unit_label or "bag"

    # Mirror first line onto legacy columns for older readers
    seed_rows = detail["seed"].get("rows") or []
    if seed_rows:
        first = seed_rows[0]
        fl = first["line"]
        budget.seed_hybrid_id = getattr(fl, "hybrid_id", None)
        budget.seed_population = first.get("population")
        budget.seed_bag_kernels = float(first.get("bag_kernels") or 80000)
        budget.seed_cost_per_bag = first.get("cost_per_bag")
    else:
        budget.seed_hybrid_id = None

    # Clear prior budget-tagged ops for this field
    old_ops = list(db.scalars(select(FieldOperation).where(FieldOperation.field_id == field.id)))
    for op in old_ops:
        desc = op.description or ""
        if BUDGET_OP_MARKER in desc or BUDGET_SPRAY_MARKER in desc:
            db.delete(op)

    for row in detail["pass_rows"]:
        p = row["pass"]
        cost = float(row["mach_total"] or 0) + float(row["travel_total"] or 0)
        db.add(
            FieldOperation(
                field_id=field.id,
                op_date=today,
                op_type=p.pass_label,
                description=f"{BUDGET_OP_MARKER} {'hired' if row['hired'] else 'self'} · travel ${row['travel_total']:.0f}",
                cost=cost,
                billable=0,
            )
        )

    # Spray links
    old_sprays = list(db.scalars(select(FieldSprayMix).where(FieldSprayMix.field_id == field.id)))
    for link in old_sprays:
        if link.notes and BUDGET_SPRAY_MARKER in (link.notes or ""):
            db.delete(link)

    for row in detail["spray_rows"]:
        sp = row["pass"]
        if not sp.spray_mix_id:
            continue
        db.add(
            FieldSprayMix(
                field_id=field.id,
                spray_mix_id=sp.spray_mix_id,
                timing_label=sp.label,
                notes=f"{BUDGET_SPRAY_MARKER} chem ${row['chem_total']:.0f} hire ${row['hire_total']:.0f} travel ${row['travel_total']:.0f}",
            )
        )
        # Hire + travel as ops
        extra = float(row["hire_total"] or 0) + float(row["travel_total"] or 0)
        if extra > 0:
            db.add(
                FieldOperation(
                    field_id=field.id,
                    op_date=today,
                    op_type=f"Spray · {sp.label}",
                    description=f"{BUDGET_SPRAY_MARKER} application/travel",
                    cost=extra,
                    billable=0,
                )
            )

    # Fertilizer plans (estimates)
    old_plans = list(
        db.scalars(
            select(FieldPlan).where(
                FieldPlan.field_id == field.id,
                FieldPlan.crop_year_id == year_id,
                FieldPlan.plan_type == "fertilizer",
            )
        )
    )
    for pl in old_plans:
        if pl.details and BUDGET_FERT_MARKER in (pl.details or ""):
            db.delete(pl)

    for row in detail["fert_rows"]:
        prod = row["product"]
        db.add(
            FieldPlan(
                field_id=field.id,
                crop_year_id=year_id,
                plan_type="fertilizer",
                title=f"{prod.name} @ {row['rate_per_ac']:g} {prod.apply_unit}/ac",
                status="planned",
                details=f"{BUDGET_FERT_MARKER} tons={row['tons']} total=${row['total']}",
                estimated_cost_per_acre=row["per_ac"],
            )
        )
