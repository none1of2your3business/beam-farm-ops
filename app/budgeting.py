"""Field-level budgeting: planned envelope, actual spend, affordable max, P/L tone."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.field_ledger import field_mark_prices, load_year_field_cards
from app.models import (
    AppSettings,
    Field,
    FieldBudget,
    FieldBudgetFertLine,
    FieldBudgetLine,
    FieldBudgetSeedLine,
    FieldBudgetSprayPass,
)

BUDGET_CATEGORIES: list[tuple[str, str]] = [
    ("seed", "Seed"),
    ("fertilizer", "Fertilizer"),
    ("chemical", "Chemical"),
    ("fuel_ops", "Fuel & field ops"),
    ("rent", "Rent / land"),
    ("insurance", "Crop insurance"),
    ("other", "Other"),
]

CATEGORY_KEYS = [k for k, _ in BUDGET_CATEGORIES]
CATEGORY_LABELS = dict(BUDGET_CATEGORIES)


def category_label(key: str) -> str:
    return CATEGORY_LABELS.get(key, key.replace("_", " ").title())


def _empty_categories() -> dict[str, float]:
    return {k: 0.0 for k, _ in BUDGET_CATEGORIES}


def _default_pass_templates(crop: str) -> dict[str, dict[str, Any]]:
    from app.budget_detail import DEFAULT_PASSES

    out: dict[str, dict[str, Any]] = {}
    for key, label, trips, _op in DEFAULT_PASSES:
        enabled = 1
        if key == "corn_plant" and crop not in ("Corn",):
            enabled = 0
        if key == "soy_plant" and crop not in ("Soybeans", "Soy"):
            enabled = 0
        if key == "combine" and crop not in ("Corn", "Soybeans", "Soy"):
            enabled = 0
        if key == "spray":
            enabled = 0
        out[key] = {
            "label": label,
            "enabled": enabled,
            "is_hired": 0,
            "self_rate_ac": 0.0,
            "hired_rate_ac": 0.0,
            "round_trips": float(trips),
        }
    return out


def _normalize_crop_template(crop: str, raw: Any) -> dict[str, Any]:
    """Accept legacy flat category maps or richer template objects."""
    cats = _empty_categories()
    seed = {"population": None, "bag_kernels": 80000.0, "cost_per_bag": None}
    passes = _default_pass_templates(crop)
    fert: list[dict[str, Any]] = []
    margin_goal_ac = 0.0

    if not isinstance(raw, dict):
        return {
            "categories": cats,
            "seed": seed,
            "passes": passes,
            "fert": fert,
            "margin_goal_ac": margin_goal_ac,
        }

    # New shape: {"categories": {...}, "seed": {...}, ...}
    if "categories" in raw or "passes" in raw or "seed" in raw or "fert" in raw:
        src_cats = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
        for k, _ in BUDGET_CATEGORIES:
            if k in src_cats:
                cats[k] = float(src_cats.get(k) or 0)
            elif k in raw and not isinstance(raw.get(k), (dict, list)):
                # tolerate mixed
                cats[k] = float(raw.get(k) or 0)
        seed_raw = raw.get("seed") if isinstance(raw.get("seed"), dict) else {}
        if seed_raw.get("population") not in (None, ""):
            try:
                seed["population"] = float(seed_raw["population"])
            except (TypeError, ValueError):
                pass
        try:
            seed["bag_kernels"] = float(seed_raw.get("bag_kernels") or 80000)
        except (TypeError, ValueError):
            seed["bag_kernels"] = 80000.0
        if seed_raw.get("cost_per_bag") not in (None, ""):
            try:
                seed["cost_per_bag"] = float(seed_raw["cost_per_bag"])
            except (TypeError, ValueError):
                pass
        passes_raw = raw.get("passes") if isinstance(raw.get("passes"), dict) else {}
        for key, spec in passes.items():
            src = passes_raw.get(key) if isinstance(passes_raw.get(key), dict) else {}
            if src:
                spec["enabled"] = 1 if int(src.get("enabled") or 0) else 0
                spec["is_hired"] = 1 if int(src.get("is_hired") or 0) else 0
                spec["self_rate_ac"] = float(src.get("self_rate_ac") or 0)
                spec["hired_rate_ac"] = float(src.get("hired_rate_ac") or 0)
                spec["round_trips"] = float(src.get("round_trips") or spec["round_trips"])
                if src.get("label"):
                    spec["label"] = str(src["label"])
        fert_raw = raw.get("fert") if isinstance(raw.get("fert"), list) else []
        for item in fert_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("product_name") or item.get("name") or "").strip()
            if not name:
                continue
            try:
                rate = float(item.get("rate_per_ac") or 0)
            except (TypeError, ValueError):
                rate = 0.0
            if rate <= 0:
                continue
            unit = str(item.get("apply_unit") or "lb").strip().lower()
            if unit not in ("lb", "gal"):
                unit = "lb"
            row: dict[str, Any] = {
                "product_name": name,
                "rate_per_ac": rate,
                "apply_unit": unit,
            }
            if item.get("price_per_ton") not in (None, ""):
                try:
                    row["price_per_ton"] = float(item["price_per_ton"])
                except (TypeError, ValueError):
                    pass
            if item.get("density_lb_per_gal") not in (None, ""):
                try:
                    row["density_lb_per_gal"] = float(item["density_lb_per_gal"])
                except (TypeError, ValueError):
                    pass
            fert.append(row)
        try:
            margin_goal_ac = float(raw.get("margin_goal_ac") or 0)
        except (TypeError, ValueError):
            margin_goal_ac = 0.0
    else:
        # Legacy flat {seed: 120, fertilizer: ...}
        for k, _ in BUDGET_CATEGORIES:
            if k in raw:
                try:
                    cats[k] = float(raw.get(k) or 0)
                except (TypeError, ValueError):
                    cats[k] = 0.0

    return {
        "categories": cats,
        "seed": seed,
        "passes": passes,
        "fert": fert,
        "margin_goal_ac": margin_goal_ac,
    }


def load_budget_templates(settings: AppSettings | None) -> dict[str, dict[str, Any]]:
    """Crop starter templates used by the Defaults hub."""
    raw = getattr(settings, "budget_defaults_json", None) if settings else None
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError):
            data = {}
    out: dict[str, dict[str, Any]] = {}
    for crop in ("Corn", "Soybeans"):
        out[crop] = _normalize_crop_template(crop, data.get(crop))
    # Keep any other crop keys if present
    for crop, val in data.items():
        if crop in out:
            continue
        out[str(crop)] = _normalize_crop_template(str(crop), val)
    return out


def load_budget_defaults(settings: AppSettings | None) -> dict[str, dict[str, float]]:
    """Category $/ac maps only (backward compatible)."""
    templates = load_budget_templates(settings)
    return {crop: dict(t.get("categories") or {}) for crop, t in templates.items()}


def save_budget_templates(settings: AppSettings, templates: dict[str, dict[str, Any]]) -> None:
    clean: dict[str, Any] = {}
    for crop, tmpl in (templates or {}).items():
        norm = _normalize_crop_template(str(crop), tmpl)
        clean[str(crop)] = {
            "categories": {
                k: round(float(norm["categories"].get(k) or 0), 2) for k, _ in BUDGET_CATEGORIES
            },
            "seed": {
                "population": norm["seed"]["population"],
                "bag_kernels": float(norm["seed"]["bag_kernels"] or 80000),
                "cost_per_bag": norm["seed"]["cost_per_bag"],
            },
            "passes": norm["passes"],
            "fert": [
                {
                    "product_name": f["product_name"],
                    "rate_per_ac": round(float(f["rate_per_ac"]), 4),
                    "apply_unit": f.get("apply_unit") or "lb",
                    **(
                        {"price_per_ton": round(float(f["price_per_ton"]), 4)}
                        if f.get("price_per_ton") is not None
                        else {}
                    ),
                    **(
                        {"density_lb_per_gal": round(float(f["density_lb_per_gal"]), 4)}
                        if f.get("density_lb_per_gal") is not None
                        else {}
                    ),
                }
                for f in norm["fert"]
            ],
            "margin_goal_ac": round(float(norm["margin_goal_ac"] or 0), 2),
        }
    settings.budget_defaults_json = json.dumps(clean)


def save_budget_defaults(settings: AppSettings, defaults: dict[str, dict[str, float]]) -> None:
    """Update category maps inside templates (preserves passes/seed/fert)."""
    templates = load_budget_templates(settings)
    for crop, cats in (defaults or {}).items():
        tmpl = templates.setdefault(str(crop), _normalize_crop_template(str(crop), {}))
        tmpl["categories"] = {
            k: round(float((cats or {}).get(k) or 0), 2) for k, _ in BUDGET_CATEGORIES
        }
    save_budget_templates(settings, templates)


def board_price_for_crop(
    settings: AppSettings | None,
    board: dict[str, Any],
    crop: str,
    contracts: list[Any],
) -> float | None:
    """Board/settings cash mark, then price assumption fallback."""
    marks = field_mark_prices(settings, board or {}, crop, contracts or [])
    price = marks.get("futures")
    if price is not None:
        return float(price)
    if settings:
        if crop == "Corn" and settings.corn_price_assumption is not None:
            return float(settings.corn_price_assumption)
        if crop in ("Soybeans", "Soy") and settings.soy_price_assumption is not None:
            return float(settings.soy_price_assumption)
    return None


def _acres(field: Field) -> float:
    return float(field.acres_mine or field.acres_total or 0)


def _tone_for_profit(profit: float | None, revenue: float | None, acres: float) -> str:
    """Color coordination: green = good profit, red = loss, amber = thin, slate = unknown."""
    if profit is None:
        return "neutral"
    if profit < -0.5:
        return "loss"
    if acres > 0 and profit / acres >= 25:
        return "profit"
    if revenue and revenue > 0 and profit / revenue >= 0.08:
        return "profit"
    if profit >= 0:
        return "tight"
    return "loss"


def _budget_load_opts():
    return (
        joinedload(FieldBudget.lines),
        joinedload(FieldBudget.seed_lines).joinedload(FieldBudgetSeedLine.hybrid),
        joinedload(FieldBudget.fert_lines).joinedload(FieldBudgetFertLine.product),
        joinedload(FieldBudget.passes),
        joinedload(FieldBudget.spray_passes).joinedload(FieldBudgetSprayPass.spray_mix),
        joinedload(FieldBudget.seed_hybrid),
    )


def _get_field_budget(db: Session, field_id: int, year_id: int) -> FieldBudget | None:
    return (
        db.scalars(
            select(FieldBudget)
            .where(FieldBudget.field_id == field_id, FieldBudget.crop_year_id == year_id)
            .options(*_budget_load_opts())
        )
        .unique()
        .first()
    )


def ensure_field_budget(
    db: Session,
    field: Field,
    year_id: int,
    *,
    defaults: dict[str, dict[str, float]] | None = None,
    settings: AppSettings | None = None,
) -> FieldBudget:
    """Get or create a budget row; seed lines + default passes when new."""
    from app.budget_detail import ensure_default_passes, ensure_seed_lines_from_legacy, seed_fertilizer_catalog

    seed_fertilizer_catalog(db)
    budget = _get_field_budget(db, field.id, year_id)
    if budget:
        ensure_seed_lines_from_legacy(db, budget, field)
        ensure_default_passes(db, budget, field, settings)
        db.flush()
        return _get_field_budget(db, field.id, year_id) or budget

    budget = FieldBudget(field_id=field.id, crop_year_id=year_id, margin_goal_ac=0.0)
    db.add(budget)
    db.flush()

    crop_defs = (defaults or {}).get(field.crop or "", {}) if defaults else {}
    for i, (key, _label) in enumerate(BUDGET_CATEGORIES):
        amt = float(crop_defs.get(key) or 0)
        if key == "rent" and amt <= 0:
            amt = float(field.rent_per_acre or 0)
        db.add(
            FieldBudgetLine(
                budget_id=budget.id,
                category=key,
                amount_per_ac=amt,
                sort_order=i,
            )
        )
    db.flush()
    ensure_default_passes(db, budget, field, settings)
    db.flush()
    loaded = _get_field_budget(db, field.id, year_id)
    assert loaded is not None
    return loaded


def set_budget_lines(
    db: Session,
    budget: FieldBudget,
    amounts: dict[str, float],
) -> None:
    by_cat = {ln.category: ln for ln in (budget.lines or [])}
    for i, (key, _label) in enumerate(BUDGET_CATEGORIES):
        amt = round(float(amounts.get(key) or 0), 2)
        line = by_cat.get(key)
        if line:
            line.amount_per_ac = amt
            line.sort_order = i
        else:
            db.add(
                FieldBudgetLine(
                    budget_id=budget.id,
                    category=key,
                    amount_per_ac=amt,
                    sort_order=i,
                )
            )


def build_field_budget_card(
    field: Field,
    budget: FieldBudget | None,
    *,
    actual_total: float,
    price: float | None,
    price_source: str,
) -> dict[str, Any]:
    acres = _acres(field)
    yld = field.expected_yield
    expected_bu = round(acres * float(yld), 1) if yld is not None else None

    lines_out: list[dict[str, Any]] = []
    planned_ac = 0.0
    line_map = {ln.category: ln for ln in (budget.lines or [])} if budget else {}
    for key, label in BUDGET_CATEGORIES:
        ln = line_map.get(key)
        amt = float(ln.amount_per_ac or 0) if ln else 0.0
        planned_ac += amt
        lines_out.append(
            {
                "category": key,
                "label": label,
                "amount_per_ac": amt,
                "amount_total": round(amt * acres, 2),
                "line_id": ln.id if ln else None,
            }
        )

    planned_total = round(planned_ac * acres, 2)
    actual_total = round(float(actual_total or 0), 2)
    remaining = round(planned_total - actual_total, 2)
    used_pct = round(100.0 * actual_total / planned_total, 1) if planned_total > 0.05 else None

    margin_goal_ac = float(budget.margin_goal_ac or 0) if budget else 0.0
    revenue = (
        round(float(expected_bu) * float(price), 2)
        if expected_bu is not None and price is not None
        else None
    )
    affordable_max = (
        round(revenue - margin_goal_ac * acres, 2) if revenue is not None else None
    )
    affordable_left = (
        round(affordable_max - actual_total, 2) if affordable_max is not None else None
    )
    projected_profit = (
        round(revenue - planned_total, 2) if revenue is not None else None
    )
    projected_profit_ac = (
        round(projected_profit / acres, 2)
        if projected_profit is not None and acres > 0
        else None
    )

    tone = _tone_for_profit(projected_profit, revenue, acres)
    if planned_total <= 0.05 and not budget:
        tone = "neutral"

    over_plan = planned_total > 0.05 and actual_total > planned_total + 0.5
    over_affordable = (
        affordable_max is not None and actual_total > affordable_max + 0.5
    )
    if over_plan or over_affordable:
        # Spending already past envelope still shows red urgency
        if tone == "profit":
            tone = "tight"
        if over_affordable:
            tone = "loss"

    return {
        "field": field,
        "field_id": field.id,
        "name": field.name,
        "crop": field.crop or "None",
        "acres": acres,
        "budget": budget,
        "budget_id": budget.id if budget else None,
        "has_budget": budget is not None and planned_total > 0.05,
        "lines": lines_out,
        "planned_ac": round(planned_ac, 2),
        "planned_total": planned_total,
        "actual_total": actual_total,
        "actual_ac": round(actual_total / acres, 2) if acres > 0 else 0.0,
        "remaining": remaining,
        "used_pct": used_pct,
        "price": price,
        "price_source": price_source,
        "price_override": float(budget.price_override) if budget and budget.price_override is not None else None,
        "margin_goal_ac": margin_goal_ac,
        "expected_yield": yld,
        "expected_bu": expected_bu,
        "expected_revenue": revenue,
        "affordable_max": affordable_max,
        "affordable_left": affordable_left,
        "projected_profit": projected_profit,
        "projected_profit_ac": projected_profit_ac,
        "tone": tone,
        "over_plan": over_plan,
        "over_affordable": over_affordable,
        "notes": budget.notes if budget else None,
    }


def load_budget_board(
    db: Session,
    year: Any,
    fields: list[Field],
    settings: AppSettings | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build budget cards for all fields + farm rollup."""
    from app import cme_quotes

    cards_ledger, meta = load_year_field_cards(db, year, fields)
    actual_by_id = {c["field"].id: float(c.get("running_cost") or 0) for c in cards_ledger}

    board = cme_quotes.board_from_settings(settings)
    contracts = meta.get("contracts") or []
    defaults = load_budget_defaults(settings)

    budgets = list(
        db.scalars(
            select(FieldBudget)
            .where(FieldBudget.crop_year_id == year.id)
            .options(joinedload(FieldBudget.lines))
        ).unique()
    )
    budget_by_field = {b.field_id: b for b in budgets}

    out: list[dict[str, Any]] = []
    for field in fields:
        budget = budget_by_field.get(field.id)
        crop = field.crop or "None"
        override = float(budget.price_override) if budget and budget.price_override is not None else None
        if override is not None:
            price, src = override, "field override"
        else:
            price = board_price_for_crop(settings, board, crop, contracts)
            src = "board / settings" if price is not None else "no price"
        out.append(
            build_field_budget_card(
                field,
                budget,
                actual_total=actual_by_id.get(field.id, 0.0),
                price=price,
                price_source=src,
            )
        )

    out.sort(key=lambda c: ((c["crop"] or ""), (c["name"] or "").lower()))

    planned = sum(c["planned_total"] for c in out)
    actual = sum(c["actual_total"] for c in out)
    revenue = sum(c["expected_revenue"] or 0 for c in out if c["expected_revenue"] is not None)
    profit = sum(c["projected_profit"] or 0 for c in out if c["projected_profit"] is not None)
    with_plan = sum(1 for c in out if c["has_budget"])
    loss_n = sum(1 for c in out if c["tone"] == "loss")
    profit_n = sum(1 for c in out if c["tone"] == "profit")
    over_plan_n = sum(1 for c in out if c["over_plan"])
    over_aff_n = sum(1 for c in out if c["over_affordable"])
    farm_tone = _tone_for_profit(
        profit if any(c["projected_profit"] is not None for c in out) else None,
        revenue if revenue else None,
        sum(c["acres"] for c in out) or 1,
    )

    rollup = {
        "planned": round(planned, 2),
        "actual": round(actual, 2),
        "remaining": round(planned - actual, 2),
        "revenue": round(revenue, 2),
        "profit": round(profit, 2),
        "used_pct": round(100 * actual / planned, 1) if planned > 0.05 else None,
        "field_count": len(out),
        "with_plan": with_plan,
        "loss_n": loss_n,
        "profit_n": profit_n,
        "over_plan_n": over_plan_n,
        "over_affordable_n": over_aff_n,
        "tone": farm_tone,
        "defaults": defaults,
        "board": board,
    }
    return out, rollup


def apply_defaults_to_fields(
    db: Session,
    year_id: int,
    fields: list[Field],
    defaults: dict[str, dict[str, float]],
    *,
    only_empty: bool = True,
    settings: AppSettings | None = None,
) -> int:
    """Push crop category defaults onto field budgets. Returns fields updated."""
    templates = load_budget_templates(settings) if settings else {}
    for crop, cats in (defaults or {}).items():
        tmpl = templates.setdefault(str(crop), _normalize_crop_template(str(crop), {}))
        tmpl["categories"] = {
            k: float((cats or {}).get(k) or 0) for k, _ in BUDGET_CATEGORIES
        }
    return apply_budget_templates_to_fields(
        db,
        year_id,
        fields,
        templates,
        settings=settings,
        only_empty=only_empty,
        parts={"categories"},
    )


def _budget_has_plan(budget: FieldBudget | None) -> bool:
    if not budget:
        return False
    if any(float(ln.amount_per_ac or 0) > 0 for ln in (budget.lines or [])):
        return True
    if budget.seed_lines:
        return True
    if budget.fert_lines:
        return True
    if any(int(p.enabled or 0) for p in (budget.passes or [])):
        # default passes are often enabled — treat as plan only if category/seed/fert set
        pass
    return False


def apply_budget_templates_to_fields(
    db: Session,
    year_id: int,
    fields: list[Field],
    templates: dict[str, dict[str, Any]],
    *,
    settings: AppSettings | None = None,
    only_empty: bool = True,
    parts: set[str] | None = None,
) -> int:
    """Apply crop starter templates to selected fields. Returns count updated."""
    from app.budget_detail import default_self_rate
    from app.models import FertilizerProduct, FieldBudgetFertLine, FieldBudgetPass, FieldBudgetSeedLine

    parts = parts or {"categories", "passes", "seed", "fert", "margin"}
    cat_defaults = {
        crop: dict(t.get("categories") or {}) for crop, t in (templates or {}).items()
    }
    n = 0
    for field in fields:
        crop = field.crop or ""
        # Map Soy → Soybeans template
        tmpl_key = crop
        if crop == "Soy":
            tmpl_key = "Soybeans"
        tmpl = (templates or {}).get(tmpl_key) or (templates or {}).get(crop)
        if not tmpl:
            continue

        existing = (
            db.scalars(
                select(FieldBudget)
                .where(FieldBudget.field_id == field.id, FieldBudget.crop_year_id == year_id)
                .options(*_budget_load_opts())
            )
            .unique()
            .first()
        )
        if only_empty and _budget_has_plan(existing):
            continue

        budget = ensure_field_budget(
            db, field, year_id, defaults=cat_defaults, settings=settings
        )

        if "margin" in parts:
            budget.margin_goal_ac = float(tmpl.get("margin_goal_ac") or 0)

        if "categories" in parts:
            amounts = dict(tmpl.get("categories") or {})
            if not amounts.get("rent"):
                amounts["rent"] = float(field.rent_per_acre or 0)
            set_budget_lines(db, budget, amounts)

        if "passes" in parts:
            pass_specs = tmpl.get("passes") or {}
            by_key = {p.pass_key: p for p in (budget.passes or [])}
            # Ensure default pass rows exist
            if not by_key:
                from app.budget_detail import ensure_default_passes

                ensure_default_passes(db, budget, field, settings)
                budget = ensure_field_budget(
                    db, field, year_id, defaults=cat_defaults, settings=settings
                )
                by_key = {p.pass_key: p for p in (budget.passes or [])}
            for key, spec in pass_specs.items():
                p = by_key.get(key)
                if not p:
                    db.add(
                        FieldBudgetPass(
                            budget_id=budget.id,
                            pass_key=key,
                            pass_label=str(spec.get("label") or key),
                            enabled=1 if int(spec.get("enabled") or 0) else 0,
                            is_hired=1 if int(spec.get("is_hired") or 0) else 0,
                            hired_rate_ac=float(spec.get("hired_rate_ac") or 0),
                            self_rate_ac=float(spec.get("self_rate_ac") or 0)
                            or default_self_rate(settings, key, crop),
                            round_trips=float(spec.get("round_trips") or 1),
                            sort_order=len(by_key),
                        )
                    )
                    continue
                p.enabled = 1 if int(spec.get("enabled") or 0) else 0
                p.is_hired = 1 if int(spec.get("is_hired") or 0) else 0
                p.hired_rate_ac = float(spec.get("hired_rate_ac") or 0)
                self_rate = float(spec.get("self_rate_ac") or 0)
                if self_rate > 0:
                    p.self_rate_ac = self_rate
                elif not p.self_rate_ac:
                    p.self_rate_ac = default_self_rate(settings, key, crop)
                p.round_trips = float(spec.get("round_trips") or p.round_trips or 1)
                if spec.get("label"):
                    p.pass_label = str(spec["label"])

        if "seed" in parts:
            seed = tmpl.get("seed") or {}
            pop = seed.get("population")
            bag = float(seed.get("bag_kernels") or 80000)
            cpb = seed.get("cost_per_bag")
            # Replace empty seed plan with one full-field starter row (no hybrid)
            if not budget.seed_lines:
                acres = _acres(field)
                if pop or cpb is not None:
                    db.add(
                        FieldBudgetSeedLine(
                            budget_id=budget.id,
                            hybrid_id=None,
                            acres=acres,
                            population=float(pop) if pop is not None else None,
                            bag_kernels=bag,
                            cost_per_bag=float(cpb) if cpb is not None else None,
                            sort_order=0,
                        )
                    )
            else:
                # Update population/bag defaults on existing lines missing values
                for ln in budget.seed_lines:
                    if ln.population is None and pop is not None:
                        ln.population = float(pop)
                    if not ln.bag_kernels:
                        ln.bag_kernels = bag
                    if ln.cost_per_bag is None and cpb is not None:
                        ln.cost_per_bag = float(cpb)

        if "fert" in parts:
            fert_specs = tmpl.get("fert") or []
            if fert_specs:
                for ln in list(budget.fert_lines or []):
                    db.delete(ln)
                db.flush()
                for i, spec in enumerate(fert_specs):
                    name = str(spec.get("product_name") or "").strip()
                    rate = float(spec.get("rate_per_ac") or 0)
                    if not name or rate <= 0:
                        continue
                    prod = db.scalar(
                        select(FertilizerProduct).where(FertilizerProduct.name == name)
                    )
                    if not prod:
                        continue
                    db.add(
                        FieldBudgetFertLine(
                            budget_id=budget.id,
                            product_id=prod.id,
                            rate_per_ac=rate,
                            sort_order=i,
                        )
                    )
        n += 1
    return n


def what_if_extra_spend(
    *,
    acres: float,
    expected_bu: float | None,
    price: float | None,
    planned_total: float,
    extra_spend: float,
    yield_gain_bu_ac: float = 0.0,
) -> dict[str, Any]:
    """Partial budget: is extra spend worth it?"""
    acres = max(0.0, float(acres or 0))
    extra = round(float(extra_spend or 0), 2)
    gain_ac = float(yield_gain_bu_ac or 0)
    bu0 = float(expected_bu or 0)
    bu1 = bu0 + gain_ac * acres
    rev0 = round(bu0 * float(price), 2) if price is not None else None
    rev1 = round(bu1 * float(price), 2) if price is not None else None
    cost1 = planned_total + extra
    profit0 = round(rev0 - planned_total, 2) if rev0 is not None else None
    profit1 = round(rev1 - cost1, 2) if rev1 is not None else None
    delta = round(profit1 - profit0, 2) if profit0 is not None and profit1 is not None else None
    return {
        "extra_spend": extra,
        "yield_gain_bu_ac": gain_ac,
        "profit_before": profit0,
        "profit_after": profit1,
        "delta": delta,
        "worth_it": delta is not None and delta > 0.5,
        "tone": "profit" if delta is not None and delta > 0.5 else ("loss" if delta is not None and delta < -0.5 else "tight"),
    }
