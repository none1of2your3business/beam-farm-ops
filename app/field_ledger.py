"""Field-year operations ledger: events, running cost, dual break-even."""

from __future__ import annotations

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
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "op",
                "type_label": "Op",
                "title": getattr(o, "op_type", None) or "Operation",
                "detail": getattr(o, "description", None) or "",
                "amount": _money(amt),
                "est_amount": None,
                "counts": True,
                "sort_key": (1 if d else 0, d or date.min, getattr(o, "id", 0) or 0),
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
            }
        )

    for link, mix in spray_links:
        cpa = getattr(mix, "cost_per_acre", None) if mix else None
        amt = float(cpa) * acres if cpa is not None and acres else None
        d = getattr(link, "applied_date", None)
        timing = getattr(link, "timing_label", None) or (getattr(mix, "timing", None) if mix else None)
        title = getattr(mix, "name", None) if mix else f"Spray #{getattr(link, 'spray_mix_id', '?')}"
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "spray",
                "type_label": "Spray",
                "title": title,
                "detail": (timing or "")
                + (f" · ${cpa:.2f}/ac × {acres:g} ac" if cpa is not None else ""),
                "amount": _money(amt) if amt is not None else None,
                "est_amount": None,
                "counts": amt is not None,
                "sort_key": (1 if d else 0, d or date.min, getattr(link, "id", 0) or 0),
            }
        )

    for link, hybrid in hybrid_links:
        cpa = getattr(hybrid, "cost_per_acre", None) if hybrid else None
        amt = float(cpa) * acres if cpa is not None and acres else None
        d = getattr(link, "applied_date", None)
        rate = getattr(link, "rate", None)
        title = getattr(hybrid, "name", None) if hybrid else f"Hybrid #{getattr(link, 'hybrid_id', '?')}"
        detail_bits = []
        if rate:
            detail_bits.append(str(rate))
        if cpa is not None:
            detail_bits.append(f"${cpa:.2f}/ac × {acres:g} ac")
        events.append(
            {
                "date": d,
                "undated": d is None,
                "type": "hybrid",
                "type_label": "Hybrid",
                "title": title,
                "detail": " · ".join(detail_bits),
                "amount": _money(amt) if amt is not None else None,
                "est_amount": None,
                "counts": amt is not None,
                "sort_key": (1 if d else 0, d or date.min, getattr(link, "id", 0) or 0),
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
        "vs_be_contracted": yld_vs(be_c),
        "vs_be_futures": yld_vs(be_f),
        "crop": crop,
    }
